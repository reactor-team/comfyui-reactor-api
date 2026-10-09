import asyncio
import contextlib
import json
import logging
import math
import time
from collections.abc import Callable

import numpy as np

from reactor_sdk import Reactor
from reactor_sdk.errors import RateLimitedError, ReactorError, UnauthorizedError

from .clip import ClipStream
from .encode import FrameWriter
from .recording import Recording
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

# The phase each setup command that reports one through `session_state` ends in, and how long it may
# take. Creating an avatar took 33 s on a cloud session.
SETUP_PHASES = {"create_avatar": "avatar_ready", "start_call": "live", "start_edit": "live"}
# The snapshot flag that turns true once the model's output has started, for a setup whose phase comes first:
# a live edit streams a dark placeholder until its first edited frame (docs: vidu-s2-editing/schema).
SETUP_READY = {"start_edit": "video_receiving"}
SETUP_SECONDS = 180.0
# How long a chunked render tolerates the model going silent — no message and no new frame — before
# it calls the render dead; without a bound a stalled model would hang the node forever.
CHUNK_STALL_SECONDS = 120.0
# A reply is over once the character's sound has been quiet this long; replies measured on cloud
# sessions paused at most 0.5 s, and began about 3 s after the line was said.
REPLY_QUIET_SECONDS = 1.0
REPLY_TIMEOUT_SECONDS = 30.0
REPLY_ESTIMATE_SECONDS = 9.0
# Sound above this RMS, in int16 steps, is the character speaking.
SPEECH_LEVEL = 330.0

Progress = Callable[[int, int, np.ndarray | None], None]


class Session:
    """A connected Reactor session as a runner drives it: commands out, messages in, video captured."""

    def __init__(self, reactor: Reactor, writer: FrameWriter, planned: int, check_interrupt: Callable[[], None],
                 on_progress: Progress):
        # `planned` estimates the frames to capture, for progress until the model reports the real count.
        self.reactor, self.writer, self.planned = reactor, writer, planned
        self.uploads: dict[bytes, object] = {}
        self.check_interrupt, self.on_progress = check_interrupt, on_progress
        self.messages: asyncio.Queue[dict] = asyncio.Queue()
        # Observed on cloud sessions: the stream opens with a placeholder frame at connect, before the model produces anything.
        self._capturing = False
        # The session's own recording, which keeps the spans captured here; None saves only the frames received.
        self.recording: Recording | None = None
        # Set to start capturing at the character's next speech rather than at once.
        self.capture_on_speech = False
        # Set to start capturing at the next frame rather than at `start`, so sound sent before it stays out.
        self.capture_on_frame = False
        # A frame count at which capturing stops by itself, so frames past a take's end stay out.
        self.capture_until: int | None = None
        self.frame_shape: tuple[int, ...] | None = None
        self.reported = -1
        self.reported_at = -PROGRESS_INTERVAL_SECONDS
        # When the model's sound was last loud enough to be speech, on the SDK's media thread.
        self.sounded_at = 0.0
        # Why Reactor ended the session or the SDK gave up on it, raised at the next message wait.
        self.failure: str | None = None

    @property
    def capturing(self) -> bool:
        return self._capturing

    @capturing.setter
    def capturing(self, on: bool) -> None:
        if on != self._capturing and self.recording is not None:
            self.recording.mark()
        self._capturing = on

    def push_video(self, frame: np.ndarray) -> None:
        self.frame_shape = frame.shape
        if self.capture_on_frame:
            self.capture_on_frame, self.capturing = False, True
        if self.capturing:
            self.writer.push(frame)
            if self.capture_until is not None and self.writer.received >= self.capture_until:
                self.capturing = False

    def push_audio(self, pcm: np.ndarray, sample_rate: int) -> None:
        if np.sqrt(np.mean(np.square(pcm, dtype=np.float32))) > SPEECH_LEVEL:
            self.sounded_at = time.monotonic()
            if self.capture_on_speech:
                self.capture_on_speech, self.capturing = False, True
        if self.capturing:
            self.writer.push_audio(pcm, sample_rate)

    async def send(self, command: str, data: dict) -> dict | None:
        async def upload(key: str, value):
            if isinstance(value, list) and value and all(isinstance(v, bytes) for v in value):
                return [await upload(key, v) for v in value]
            if not isinstance(value, bytes):
                return value
            # A file is uploaded once a session, however many clips reuse it.
            if value not in self.uploads:
                # An upload under `video` is the source clip; every other file is a PNG image.
                name, mime_type = ("video.mp4", "video/mp4") if key == "video" else (f"{key}.png", "image/png")
                self.uploads[value] = await self.reactor.upload_file(value, name=name, mime_type=mime_type)
            return self.uploads[value]

        data = {k: await upload(k, v) for k, v in data.items()}
        if command == "start" and not self.capture_on_frame:
            self.capturing = True
        reply = await self.reactor.send_command(command, data)
        if reply is not None and reply.get("type") == "command_error":
            raise RuntimeError(f"Reactor rejected {command}: {reply.get('data', {}).get('reason')}")
        return reply

    async def next_message(self, timeout: float = PROGRESS_INTERVAL_SECONDS) -> dict | None:
        """The next model message, or None after `timeout` without one. Raises on a rejected command."""
        self.check_interrupt()
        if self.failure is not None:
            raise RuntimeError(self.failure)
        if self.writer.received != self.reported and time.monotonic() - self.reported_at >= PROGRESS_INTERVAL_SECONDS:
            self.reported, self.reported_at = self.writer.received, time.monotonic()
            self.on_progress(self.writer.received, self.writer.limit or self.planned, self.writer.previous)
        try:
            # A queued message comes first: wait_for with no time left cancels its get() before it runs, so a
            # loop running behind its frame clock would otherwise never read one.
            msg = self.messages.get_nowait()
        except asyncio.QueueEmpty:
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
    last_sign_at = time.monotonic()
    received = session.writer.received
    while not session.capture_ended(model_done_at):
        due = []
        while timed and chunks >= timed[0][0]:
            due.append(timed.pop(0))
        # Sent together: each send waits for its reply, which a model may give only at its next chunk,
        # so commands sent in turn would land on successive chunks.
        await asyncio.gather(*(session.send(command, payload) for _, command, payload in due))
        msg = await session.next_message()
        if session.writer.received != received:
            received = session.writer.received
            last_sign_at = time.monotonic()
        if msg is None:
            if time.monotonic() - last_sign_at > CHUNK_STALL_SECONDS:
                raise RuntimeError(f"The model went silent for {CHUNK_STALL_SECONDS:.0f}s mid-render; the session is dead.")
            continue
        last_sign_at = time.monotonic()
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
    frames = await ClipStream.open(spec, plan.source)
    if frames is None:
        raise ValueError("The source video has no frames.")
    try:
        await push_source(session, spec, plan, frames)
    finally:
        await frames.close()


async def push_source(session: Session, spec: ModelSpec, plan: Plan, frames: ClipStream) -> None:
    # The source track is up before setup, so a model's `start` finds it.
    track = await session.reactor.publish_track(spec.source_track)
    for command, data in plan.setup:
        await session.send(command, data)
        if command in SETUP_PHASES:
            await hold_until(session, spec, track, frames.first, SETUP_PHASES[command], SETUP_READY.get(command))
    timed = sorted(plan.timed, key=lambda t: t[0])
    session.capturing = True
    pushed = 0
    model_done_at = None
    # The clip's length is the plan's until the clip runs out first; only frames up to it are ever decoded.
    clip = plan.chunks
    frame = frames.first
    # A model can hold back the clip's tail (it generates whole chunks, and frames sit in flight), so the last
    # frame repeats until the clip's length is back; the writer's limit drops what the repeats produce.
    # TODO: Stop on the output frame tagged with the clip's last index once models echo input frame tags.
    session.writer.limit = clip
    pad_until = None
    next_frame_at = time.monotonic() + 1 / spec.fps
    while not session.writer.full and not session.capture_ended(model_done_at):
        if pushed >= clip and pad_until is None:
            pad_until = time.monotonic() + TAIL_PAD_SECONDS
        if pad_until is not None and time.monotonic() > pad_until:
            break
        while timed and timed[0][0] <= pushed:
            _, command, data = timed.pop(0)
            await session.send(command, data)
        # Messages may wake the wait early; only the frame deadline advances the source.
        msg = await session.next_message(max(next_frame_at - time.monotonic(), 0.0))
        try:
            session_phase(msg)
        except SessionEnded as e:
            if e.phase != "ended":
                raise
            # An edit has a time limit, and the video keeps what came before it.
            logging.warning("Reactor: %s; the video ends there.", e)
            break
        if msg is not None and msg.get("type") in ("generation_complete", "generation_stopped"):
            model_done_at = model_done_at or time.monotonic()
        if time.monotonic() < next_frame_at:
            continue
        if pushed < clip:
            if (decoded := await frames.next()) is None:
                clip = session.writer.limit = pushed
            else:
                frame = decoded
        track.push_frame(frame)
        pushed += 1
        next_frame_at += 1 / spec.fps
    model_done_at = model_done_at or time.monotonic()
    # The capture ends as `capture_ended` always does: its frames in, or a grace beat without one.
    while not session.capture_ended(model_done_at):
        await session.next_message()


class SessionEnded(RuntimeError):
    """The model ended its call or edit on its own, or it failed; `phase` says which."""

    def __init__(self, phase: str, reason: object):
        # `last_error` is an object with a `reason` on some models, and a string on others.
        reason = reason.get("reason") if isinstance(reason, dict) else reason
        super().__init__(f"The session {phase}: {reason}")
        self.phase = phase


async def hold_until(session: Session, spec: ModelSpec, track, frame: np.ndarray, phase: str, ready: str | None = None) -> None:
    """Push `frame` at the model's frame rate until the session reaches `phase`, with its `ready` flag set if one is
    given, so a model that needs its source flowing to get there has it, and the clip itself starts once the model takes it."""
    give_up = time.monotonic() + SETUP_SECONDS
    next_frame_at = time.monotonic() + 1 / spec.fps
    while True:
        if time.monotonic() > give_up:
            raise RuntimeError(f"The session never reached {phase}{f' with {ready}' if ready else ''}.")
        msg = await session.next_message(max(next_frame_at - time.monotonic(), 0.0))
        if session_phase(msg) == phase and (ready is None or msg["data"].get(ready)):
            return
        if time.monotonic() < next_frame_at:
            continue
        track.push_frame(frame)
        next_frame_at += 1 / spec.fps


def session_phase(msg: dict | None, in_call: bool = True) -> str | None:
    """The phase a `session_state` reports, if `msg` is one. Raises SessionEnded once the session has failed or ended,
    unless not `in_call`: observed on cloud sessions, a new session can first report the end of the call before it.
    A `failed` phase is always this session's own, though: nothing stale reports one."""
    if msg is None or msg.get("type") != "session_state":
        return None
    data = msg.get("data") or {}
    phase = data.get("phase")
    if phase == "failed" or (in_call and phase == "ended"):
        raise SessionEnded(phase, data.get("last_error") or data.get("end_reason"))
    return phase


async def run_call(session: Session, spec: ModelSpec, plan: Plan) -> None:
    """Drive a call model; its clock is the character's replies, each over once its sound goes quiet and its hold has played."""
    for command, data in plan.setup:
        await session.send(command, data)
        give_up = time.monotonic() + SETUP_SECONDS
        while session_phase(await session.next_message(), command != "create_avatar") != SETUP_PHASES[command]:
            if time.monotonic() > give_up:
                raise RuntimeError(f"The session never reached {SETUP_PHASES[command]}.")
    # Observed on cloud sessions: a live call keeps streaming the connect placeholder for a few
    # seconds, until the character appears at a size of its own.
    placeholder = session.frame_shape
    give_up = time.monotonic() + REPLY_TIMEOUT_SECONDS
    while session.frame_shape == placeholder:
        if time.monotonic() > give_up:
            raise RuntimeError("The avatar never appeared.")
        session_phase(await session.next_message(timeout=0.05))
    # With no greeting the character waits for the first line, so the take starts at its answer.
    greeting = any(data.get("greeting") for command, data in plan.setup if command == "start_call")
    session.capturing = greeting
    session.writer.fill_gaps = True
    session.planned = sum(max(hold, round(REPLY_ESTIMATE_SECONDS * spec.fps)) for hold in plan.holds)
    timed = sorted(plan.timed, key=lambda t: t[0])
    for replies in range(plan.chunks):
        # Sound from before this turn's commands is the last reply's.
        since, held = time.monotonic(), session.writer.received + plan.holds[replies]
        while timed and timed[0][0] <= replies:
            _, command, data = timed.pop(0)
            await session.send(command, data)
        if replies == 0 and not greeting:
            session.capture_on_speech = True
        give_up = time.monotonic() + REPLY_TIMEOUT_SECONDS
        finish_by = None
        while (session.sounded_at <= since or time.monotonic() - session.sounded_at < REPLY_QUIET_SECONDS
               or session.writer.received < held):
            if session.sounded_at <= since and time.monotonic() > give_up:
                raise RuntimeError("The avatar never answered.")
            if session.sounded_at > since:
                # The reply itself is bounded too, by its timeout plus the frames it must still
                # play: a reply whose sound never goes quiet, or whose frames never arrive, hangs here.
                finish_by = finish_by or time.monotonic() + REPLY_TIMEOUT_SECONDS + plan.holds[replies] / spec.fps
                if time.monotonic() > finish_by:
                    raise RuntimeError("The avatar's reply started but never finished.")
            session_phase(await session.next_message())


async def run_clips(session: Session, spec: ModelSpec, plan: Plan) -> None:
    """Drive a model that builds queued clips and plays them; its clock is the clips played.

    A clip that continues another is queued once that one is built, since a continuation of an
    unbuilt clip opens fresh. Playing starts once every clip is built, or the playout queue is full,
    so autoplay does not run dry between clips. The take is the frames the model reports for its
    clips, which it snaps to lengths it can make.
    """
    for command, data in plan.setup:
        await session.send(command, data)
    pending = sorted(plan.timed, key=lambda t: t[0])
    ids: list[str] = []
    frames = 0
    building: set[str] = set()
    full = playing = False
    model_done_at = None
    last_sign_at = time.monotonic()
    while not session.capture_ended(model_done_at):
        while pending and not (isinstance(anchor := pending[0][2].get("continue_from_clip_id"), int) and ids[anchor] in building):
            i, command, data = pending.pop(0)
            if isinstance(anchor, int):
                data = {**data, "continue_from_clip_id": ids[anchor]}
            reply = await session.send(command, data)
            clip = ((reply or {}).get("data") or {}).get("clip")
            if not clip:
                raise RuntimeError(f"The model did not queue segment {i + 1}.")
            ids.append(clip["clip_id"])
            building.add(clip["clip_id"])
            frames += int(clip["frames"])
            if not pending:
                session.planned = session.writer.limit = frames
        if not playing and ((not pending and not building) or full):
            await session.send("set_autoplay", {"enabled": True})
            playing = True
        msg = await session.next_message()
        if msg is None:
            if model_done_at is None and time.monotonic() - last_sign_at > CHUNK_STALL_SECONDS:
                raise RuntimeError(f"The model went silent for {CHUNK_STALL_SECONDS:.0f}s mid-render; the session is dead.")
            continue
        last_sign_at = time.monotonic()
        kind, data = msg.get("type"), msg.get("data") or {}
        clip_id = (data.get("clip") or {}).get("clip_id")
        if kind == "state_update" and "playout_queued" in data:
            full = data["playout_queued"] >= data.get("playout_capacity", math.inf)
        elif kind == "clip_generated":
            building.discard(clip_id)
        elif kind == "clip_failed" and clip_id in ids:
            raise RuntimeError(f"Segment {ids.index(clip_id) + 1} failed to build: {data.get('reason') or data.get('error') or 'no reason given'}")
        elif kind == "clip_started" and ids and clip_id == ids[0]:
            session.capturing = True
        elif kind in ("clip_finished", "clip_stopped") and not pending and clip_id == ids[-1]:
            model_done_at = model_done_at or time.monotonic()


async def run_takes(session: Session, spec: ModelSpec, plan: Plan) -> None:
    """Drive a model that generates one take at a time; each take is recorded from its start to its end.

    A take's commands go out once the take before has ended, as a model refuses `start` mid-take. Each take
    records its planned frames and no more, or with none planned every frame until it ends, so the idle frames
    between takes stay out of the video.
    """
    for command, data in plan.setup:
        await session.send(command, data)
    for take, frames in enumerate(plan.holds):
        session.capture_until = session.writer.received + frames if frames else None
        # Measured on LTX: sound keeps streaming while a take is generated, before its first frame arrives.
        session.capture_on_frame = True
        for _, command, data in (t for t in plan.timed if t[0] == take):
            await session.send(command, data)
        done = False
        last_sign_at = time.monotonic()
        # The take ends at `generation_complete`; its frames may trail the message, so they are waited for.
        while not done or (session.capturing and time.monotonic() - max(last_sign_at, session.writer.last_frame_at or 0) <= FRAME_GRACE_SECONDS):
            msg = await session.next_message()
            if msg is None:
                if not done and time.monotonic() - last_sign_at > CHUNK_STALL_SECONDS:
                    raise RuntimeError(f"The model went silent for {CHUNK_STALL_SECONDS:.0f}s mid-render; the session is dead.")
                continue
            kind, data = msg.get("type"), msg.get("data") or {}
            if kind in ("generation_started", "window_progress"):
                last_sign_at = time.monotonic()
            elif kind == "generation_complete":
                done, last_sign_at = True, time.monotonic()
            elif kind in ("generation_failed", "generation_stopped", "generation_reset"):
                raise RuntimeError(f"Segment {take + 1} did not finish: {data.get('reason') or kind}")
        session.capturing = session.capture_on_frame = False
    session.planned = session.writer.received


RUNNERS = {"chunked": run_chunked, "source": run_source, "clips": run_clips, "takes": run_takes, "call": run_call}


def api_message(text: str) -> str:
    """The sentence in a Reactor API error reply's JSON body, with its link if it has one; `text` itself when it holds none."""
    try:
        body, _ = json.JSONDecoder().raw_decode(text, text.index("{"))
    except ValueError:
        return text
    if not isinstance(body, dict) or not body.get("message"):
        return text
    return " ".join(filter(None, (body["message"], body.get("url"))))


def reactor_error(e: ReactorError) -> str:
    """What to show for a Reactor SDK error: the API's own sentence where its reply carries one."""
    text = api_message(str(e))
    if isinstance(e, UnauthorizedError):
        return f"Reactor rejected the API key; check REACTOR_API_KEY. ({text})"
    # A 402 is about credits or billing: point at the dashboard unless the reply links its own page.
    if e.status == 402 and "https://" not in text:
        return f"{text} https://reactor.inc/dashboard"
    return text


def watch_for_failure(reactor: Reactor, fail: Callable[[str], None]) -> None:
    """Call `fail` with the reason when Reactor ends the session, as when the credits run out, or the SDK hits an error it can't recover from."""
    def ended(msg: dict) -> None:
        if msg.get("type") == "sessionEnded":
            fail((msg.get("data") or {}).get("reason") or "Reactor ended the session.")

    @reactor.on_error
    def errored(e: ReactorError) -> None:
        if e.recoverable:
            logging.warning("Reactor: %s", e)
        else:
            fail(reactor_error(e))

    reactor.on("runtime_message", ended)


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
        except ReactorError as e:
            raise RuntimeError(reactor_error(e)) from e


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
    """Run one Reactor session through `plan` and encode its streamed video, and any sound, to `out_path`.

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

    watch_for_failure(reactor, lambda reason: setattr(session, "failure", session.failure or reason))

    @reactor.on_track
    def capture(track) -> None:
        if track.kind == "video" and track.direction == "recvonly":
            track.on_frame(session.push_video)
        if track.kind == "audio" and track.direction == "recvonly":
            track.on_frame(session.push_audio)

    try:
        async with closing_session(reactor):
            on_status("Connecting to Reactor…")
            await connect_with_retry(reactor, check_interrupt, on_status)
            await asyncio.sleep(CONNECT_SETTLE_SECONDS)
            session.recording = Recording(reactor)
            on_status("Rendering")
            await RUNNERS[spec.pattern](session, spec, plan)
            clip, jwt = await session.recording.finish(), None
            if clip is not None:
                try:
                    jwt = await recording_token(reactor, spec, connect)
                except Exception as e:
                    logging.warning("Reactor: no token to download the session's recording (%s); keeping the frames this client received.", e)
                    clip = None
    except BaseException:
        # The session's error is the one worth reporting, not the encoder's.
        with contextlib.suppress(Exception):
            writer.close()
        raise
    writer.close()
    if clip is not None:
        on_status("Saving the session's recording…")
        await session.recording.save(clip, jwt, out_path, spec.fps, writer.limit)
    on_progress(writer.received, writer.received, writer.previous)


async def recording_token(reactor: Reactor, spec: ModelSpec, connect: dict) -> str | None:
    """A token bound to this session that can download its recording; a local runtime needs none."""
    if "api_key" not in connect:
        return None
    # Imported here: live imports this module.
    from .live import session_token
    return await asyncio.to_thread(session_token, connect["api_key"], spec.slug, reactor.session_id)
