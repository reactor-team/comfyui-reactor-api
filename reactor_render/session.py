import asyncio
import contextlib
import io
import logging
import math
import time
from collections.abc import Callable

import av
import numpy as np

from reactor_sdk import Reactor
from reactor_sdk.errors import RateLimitedError

from .encode import FrameWriter
from .timeline import ModelSpec, Plan

# Once the model is done, how long to wait without a frame before closing a short capture.
FRAME_GRACE_SECONDS = 3.0
# How long a source clip's last frame repeats while the model's output catches up to the clip's length.
TAIL_PAD_SECONDS = 10.0
# TODO: Drop this once a command sent at once after `connect()` is reliably answered. Observed on
# cloud sessions: such a command can go unanswered, timing out the render.
CONNECT_SETTLE_SECONDS = 1.0
# A 429 at connect is retried for this long. Without a Retry-After, wait one refill of the
# session bucket (docs: resources/rate-limits).
CONNECT_RETRY_SECONDS = 300.0
RATE_LIMIT_WAIT_SECONDS = 6.0
# How long ending a session may take before its handle is destroyed anyway.
DISCONNECT_TIMEOUT_SECONDS = 10.0
# How often a render reports progress, with its newest frame as a preview.
PROGRESS_INTERVAL_SECONDS = 0.125

Progress = Callable[[int, int, np.ndarray | None], None]


class Session:
    """A connected Reactor session as a runner drives it: commands out, messages in, video captured."""

    def __init__(self, reactor: Reactor, writer: FrameWriter, planned: int, check_interrupt: Callable[[], None],
                 on_progress: Progress):
        # `planned` estimates the frames to capture, for progress until the model reports the real count.
        self.reactor, self.writer, self.planned = reactor, writer, planned
        self.check_interrupt, self.on_progress = check_interrupt, on_progress
        self.messages: asyncio.Queue[dict] = asyncio.Queue()
        # Observed on cloud sessions: the stream opens with a placeholder frame at connect, before the model produces anything.
        self.capturing = False
        self.reported = -1

    async def send(self, command: str, data: dict) -> None:
        async def upload(key: str, value: bytes):
            # An upload under `video` is the source clip; every other file is a PNG image.
            name, mime_type = ("video.mp4", "video/mp4") if key == "video" else (f"{key}.png", "image/png")
            return await self.reactor.upload_file(value, name=name, mime_type=mime_type)

        data = {k: await upload(k, v) if isinstance(v, bytes) else v for k, v in data.items()}
        if command == "start":
            self.capturing = True
        reply = await self.reactor.send_command(command, data)
        if reply is not None and reply.get("type") == "command_error":
            raise RuntimeError(f"Reactor rejected {command}: {reply.get('data', {}).get('reason')}")

    async def next_message(self, timeout: float = PROGRESS_INTERVAL_SECONDS) -> dict | None:
        """The next model message, or None after `timeout` without one. Raises on a rejected command."""
        self.check_interrupt()
        if self.writer.received != self.reported:
            self.reported = self.writer.received
            self.on_progress(self.writer.received, self.writer.limit or self.planned, self.writer.previous)
        try:
            msg = await asyncio.wait_for(self.messages.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        if msg.get("type") == "command_error":
            data = msg.get("data") or {}
            raise RuntimeError(f"Reactor rejected {data.get('command')}: {data.get('reason')}")
        return msg

    def capture_ended(self, model_done_at: float | None) -> bool:
        if model_done_at is None:
            return False
        if self.writer.full:
            return True
        return time.monotonic() - max(model_done_at, self.writer.last_frame_at or 0) > FRAME_GRACE_SECONDS


async def run_chunked(session: Session, spec: ModelSpec, plan: Plan) -> None:
    """Drive a model that reports each `chunk_complete`; its clock is the chunks completed."""
    for command, data in plan.setup:
        await session.send(command, data)
    timed = sorted(plan.timed, key=lambda t: t[0])
    chunks = frames = 0
    model_done_at = None
    while not session.capture_ended(model_done_at):
        due = []
        while timed and chunks >= timed[0][0]:
            due.append(timed.pop(0))
        # Sent together: each send waits for its reply, which a model may give only at its next chunk,
        # so commands sent in turn would land on successive chunks.
        await asyncio.gather(*(session.send(command, payload) for _, command, payload in due))
        msg = await session.next_message()
        if msg is None:
            continue
        kind, data = msg.get("type"), msg.get("data") or {}
        if kind == "generation_complete":
            model_done_at = model_done_at or time.monotonic()
        if kind == "chunk_complete" and model_done_at is None:
            emitted = int(data["frames_emitted"])
            # A warm-up chunk emits no video, so it does not move the clock.
            if emitted == 0:
                continue
            chunks += 1
            frames = emitted if spec.session_frames else frames + emitted
            if chunks >= plan.chunks:
                # Video trails the messages, so the capture ends once the frames emitted so far
                # arrive, dropping any from later chunks.
                session.writer.limit = frames
                model_done_at = time.monotonic()


async def run_source(session: Session, spec: ModelSpec, plan: Plan) -> None:
    """Drive a model that transforms a live source track; its clock is the source frames pushed."""
    # The source track is up before setup, so a model's `start` finds it.
    track = await session.reactor.publish_track(spec.source_track)
    for command, data in plan.setup:
        await session.send(command, data)
    timed = sorted(plan.timed, key=lambda t: t[0])
    session.capturing = True
    pushed = 0
    model_done_at = None
    # The clip decodes on a thread: a blocking PyAV decode between pushes would starve the message loop.
    frames = await asyncio.to_thread(
        lambda: [np.asarray(spec.fit(f.to_image())) for f in av.open(io.BytesIO(plan.source)).decode(video=0)])
    clip = min(len(frames), plan.chunks)
    # A model can hold back the clip's tail (it generates whole chunks, and frames sit in flight), so the last
    # frame repeats until the clip's length is back; the writer's limit drops what the repeats produce.
    # TODO: Stop on the output frame tagged with the clip's last index once models echo input frame tags.
    session.writer.limit = clip
    pad_until = None
    next_frame_at = time.monotonic()
    while not session.writer.full and not session.capture_ended(model_done_at):
        if pushed == clip:
            pad_until = time.monotonic() + TAIL_PAD_SECONDS
        if pad_until is not None and time.monotonic() > pad_until:
            break
        while timed and timed[0][0] <= pushed:
            _, command, data = timed.pop(0)
            await session.send(command, data)
        next_frame_at += 1 / spec.fps
        # The wait between frames is the message wait, so a polled message never stalls the pacing.
        msg = await session.next_message(max(next_frame_at - time.monotonic(), 0.0))
        if msg is not None and msg.get("type") in ("generation_complete", "generation_stopped"):
            model_done_at = model_done_at or time.monotonic()
        track.push_frame(frames[min(pushed, clip - 1)])
        pushed += 1
    model_done_at = model_done_at or time.monotonic()
    # The capture ends as `capture_ended` always does: its frames in, or a grace beat without one.
    while not session.capture_ended(model_done_at):
        await session.next_message()


RUNNERS = {"chunked": run_chunked, "source": run_source}


async def connect_with_retry(reactor: Reactor, check_interrupt: Callable[[], None], on_status: Callable[[str], None]) -> None:
    """`reactor.connect()`, retried while Reactor refuses the new session with a 429."""
    give_up = time.monotonic() + CONNECT_RETRY_SECONDS
    while True:
        try:
            return await reactor.connect()
        except RateLimitedError as e:
            wait = e.retry_after_ms / 1000 if e.retry_after_ms is not None else RATE_LIMIT_WAIT_SECONDS
            # Reactor also answers 429 when the model has no free servers, with no Retry-After.
            busy = "no available capacity" in str(e)
            if time.monotonic() + wait > give_up:
                if busy:
                    raise RuntimeError("Reactor has no free servers for this model right now; try again shortly.") from e
                raise RuntimeError(f"Reactor is rate-limiting new sessions; try again in {math.ceil(wait)}s.") from e
            logging.warning("Reactor: %s starting a session; retrying in %.0fs.", "no free servers" if busy else "rate-limited", wait)
            on_status("Waiting for a free server…" if busy else "Rate-limited; retrying…")
            # A fresh native handle for the next attempt; handlers stay registered on `reactor`.
            reactor.close()
            deadline = time.monotonic() + wait
            while (left := deadline - time.monotonic()) > 0:
                check_interrupt()
                await asyncio.sleep(min(left, 1.0))


@contextlib.asynccontextmanager
async def closing_session(reactor: Reactor):
    """Hold `reactor` for the block, then end its session with `disconnect()` and destroy its handle.

    The SDK's own `async with` skips `disconnect()` once the client reports "disconnected", as after
    a dropped connection, which can leave the session running server-side; this always sends it.
    """
    try:
        yield reactor
    finally:
        try:
            await asyncio.wait_for(reactor.disconnect(), DISCONNECT_TIMEOUT_SECONDS)
        except Exception:
            logging.warning("Reactor: ending the session failed.", exc_info=True)
        finally:
            reactor.close()


async def render(spec: ModelSpec, plan: Plan, out_path: str, on_progress: Progress, on_status: Callable[[str], None],
                 check_interrupt: Callable[[], None], **connect) -> None:
    """Run one Reactor session through `plan` and encode its streamed video to `out_path`.

    `connect` is passed to `Reactor` as-is: `api_key` for the cloud, `local`/`api_url` for
    `reactor run`. The session is ended on the way out, so an interrupt or an error never
    leaves one running.
    """
    reactor = Reactor(spec.slug, **connect)
    writer = FrameWriter(out_path, spec.fps)
    # A "source" plan's chunks count its source frames, so the estimate is the count itself.
    planned = plan.chunks * (spec.frames_per_chunk or 1)
    session = Session(reactor, writer, planned, check_interrupt, on_progress)

    @reactor.on_message
    def queue_message(msg: dict) -> None:
        session.messages.put_nowait(msg)

    @reactor.on_track
    def capture_video(track) -> None:
        if track.kind == "video" and track.direction == "recvonly":
            track.on_frame(lambda frame: session.capturing and writer.push(frame))

    try:
        async with closing_session(reactor):
            on_status("Connecting to Reactor…")
            await connect_with_retry(reactor, check_interrupt, on_status)
            await asyncio.sleep(CONNECT_SETTLE_SECONDS)
            on_status("Rendering")
            await RUNNERS[spec.pattern](session, spec, plan)
    except BaseException:
        # The session's error is the one worth reporting, not the encoder's.
        with contextlib.suppress(Exception):
            writer.close()
        raise
    writer.close()
    on_progress(writer.received, writer.received, writer.previous)
