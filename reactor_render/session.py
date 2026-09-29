import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Callable

import numpy as np

from reactor_sdk import Reactor
from reactor_sdk.errors import RateLimitedError

from .encode import FrameWriter
from .timeline import ModelSpec, Plan

# Once the model is done, how long to wait without a frame before closing a short capture.
FRAME_GRACE_SECONDS = 3.0
# TODO: Drop this once a command sent at once after `connect()` is reliably answered. Observed on
# cloud sessions: such a command can go unanswered, timing out the render.
CONNECT_SETTLE_SECONDS = 1.0
# A 429 at connect is retried this many times. Without a Retry-After, wait one refill of the
# session bucket (docs: resources/rate-limits).
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_WAIT_SECONDS = 6.0
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
        data = {k: await self.reactor.upload_file(v, name=f"{k}.png", mime_type="image/png") if isinstance(v, bytes) else v
                for k, v in data.items()}
        if command == "start":
            self.capturing = True
        reply = await self.reactor.send_command(command, data)
        if reply is not None and reply.get("type") == "command_error":
            raise RuntimeError(f"Reactor rejected {command}: {reply.get('data', {}).get('reason')}")

    async def next_message(self) -> dict | None:
        """The next model message, or None after a progress interval without one. Raises on a rejected command."""
        self.check_interrupt()
        if self.writer.received != self.reported:
            self.reported = self.writer.received
            self.on_progress(self.writer.received, self.writer.limit or self.planned, self.writer.previous)
        try:
            msg = await asyncio.wait_for(self.messages.get(), timeout=PROGRESS_INTERVAL_SECONDS)
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


RUNNERS = {"chunked": run_chunked}


async def connect_with_retry(reactor: Reactor, check_interrupt: Callable[[], None]) -> None:
    """`reactor.connect()`, retried while Reactor refuses the new session with a 429."""
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            return await reactor.connect()
        except RateLimitedError as e:
            wait = e.retry_after_ms / 1000 if e.retry_after_ms is not None else RATE_LIMIT_WAIT_SECONDS
            if attempt == RATE_LIMIT_RETRIES:
                raise RuntimeError(f"Reactor is rate-limiting new sessions; try again in {math.ceil(wait)}s.") from e
            logging.warning("Reactor: rate-limited starting a session; retrying in %.0fs.", wait)
            # A fresh native handle for the next attempt; handlers stay registered on `reactor`.
            reactor.close()
            deadline = time.monotonic() + wait
            while (left := deadline - time.monotonic()) > 0:
                check_interrupt()
                await asyncio.sleep(min(left, 1.0))


async def render(spec: ModelSpec, plan: Plan, out_path: str, on_progress: Progress,
                 check_interrupt: Callable[[], None], **connect) -> None:
    """Run one Reactor session through `plan` and encode its streamed video to `out_path`.

    `connect` is passed to `Reactor` as-is: `api_key` for the cloud, `local`/`api_url` for
    `reactor run`. Leaving the `async with` block disconnects, which ends the session
    server-side, so an interrupt or an error never leaves a session running.
    """
    reactor = Reactor(spec.slug, **connect)
    writer = FrameWriter(out_path, spec.fps)
    session = Session(reactor, writer, plan.chunks * spec.frames_per_chunk, check_interrupt, on_progress)

    @reactor.on_message
    def queue_message(msg: dict) -> None:
        session.messages.put_nowait(msg)

    @reactor.on_track
    def capture_video(track) -> None:
        if track.kind == "video" and track.direction == "recvonly":
            track.on_frame(lambda frame: session.capturing and writer.push(frame))

    try:
        async with reactor:
            await connect_with_retry(reactor, check_interrupt)
            await asyncio.sleep(CONNECT_SETTLE_SECONDS)
            await RUNNERS[spec.pattern](session, spec, plan)
    except BaseException:
        # The session's error is the one worth reporting, not the encoder's.
        with contextlib.suppress(Exception):
            writer.close()
        raise
    writer.close()
    on_progress(writer.received, writer.received, writer.previous)
