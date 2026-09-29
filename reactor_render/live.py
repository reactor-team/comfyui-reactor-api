import asyncio
import contextlib
import itertools
import json
import logging
import queue
import threading
import time
import uuid
from fractions import Fraction

import av
import numpy as np

from reactor_sdk import Reactor

from .encode import FrameWriter
from .session import CONNECT_SETTLE_SECONDS, closing_session, connect_with_retry
from .timeline import MODELS, Beat, ModelSpec, compile_timeline

# A live take is capped at half an hour; the SDK ends the session when the token hits this.
LIVE_SESSION_LIMIT_SECONDS = 1800
# How long a run waits for the browser modal to open its socket before giving up.
BROWSER_TIMEOUT_SECONDS = 60.0
INPUT_FPS = 24
# The preview the browser shows is capped at this width and, by default, this bitrate; the take is
# unaffected. The browser's Reactor setting overrides the bitrate per run.
PREVIEW_MAX_WIDTH = 832
PREVIEW_KBPS = 3000
# How often the browser gets the model connection's stats.
STATS_INTERVAL_SECONDS = 1.0

OPEN_TAB_ERROR = "Open this workflow in a ComfyUI browser tab to record a live take."

# The live takes in progress, by run id: the websocket handler finds its run here.
RUNS: dict[str, "LiveRun"] = {}

def pack_frame(keyframe: bool, timestamp_us: int, access_unit: bytes) -> bytes:
    """The websocket's binary framing: a keyframe byte, a big-endian microsecond timestamp, the AU."""
    return bytes([keyframe]) + timestamp_us.to_bytes(8, "big") + access_unit



class PreviewSender:
    """Sends preview frames to one socket, newest first, without breaking the H.264 chain.

    A frame waits while the one before it is sent. When a newer delta frame arrives behind a
    waiting one, the socket has fallen behind: both are dropped, a keyframe is requested, and
    deltas are dropped until it arrives, since a delta decoded after a gap shows corruption.
    """

    def __init__(self, send, request_keyframe):
        self._send, self._request_keyframe = send, request_keyframe
        self._waiting: bytes | None = None
        self._sending = False
        self._resync = False

    async def push(self, frame: bytes) -> None:
        keyframe = frame[0] == 1
        if not keyframe and (self._resync or self._waiting is not None):
            if not self._resync:
                self._resync = True
                self._request_keyframe()
            self._waiting = None
            return
        self._resync = self._resync and not keyframe
        self._waiting = frame
        if self._sending:
            return
        self._sending = True
        try:
            while self._waiting is not None:
                frame, self._waiting = self._waiting, None
                await self._send(frame)
        finally:
            self._sending = False


def unpack_frame(message: bytes) -> tuple[bool, int, bytes]:
    return bool(message[0]), int.from_bytes(message[1:9], "big"), message[9:]


def preview_size(width: int, height: int) -> tuple[int, int]:
    """The size the preview encoder targets: at most PREVIEW_MAX_WIDTH wide, even dimensions."""
    if width <= PREVIEW_MAX_WIDTH:
        return width, height
    return PREVIEW_MAX_WIDTH, max(2, round(height * PREVIEW_MAX_WIDTH / width) & ~1)


def _axis(held: set, negative: str, positive: str) -> int:
    """-1, 0, or 1 for a pair of opposing key codes; holding both cancels out."""
    return (positive in held) - (negative in held)


# Each two-way camera lane with its keys and options: (field, negative key, positive key, negative option, positive option).
AXES = [("move_longitudinal", "KeyS", "KeyW", "back", "forward"),
        ("move_lateral", "KeyA", "KeyD", "strafe_left", "strafe_right"),
        ("camera_pose", "KeyQ", "KeyE", "orbit_left", "orbit_right"),
        ("look_horizontal", "ArrowLeft", "ArrowRight", "left", "right"),
        ("look_vertical", "ArrowUp", "ArrowDown", "up", "down")]


def drive_keys(spec: ModelSpec) -> list[str]:
    """The key codes a model's camera lanes answer to; none for a model without camera control."""
    keys = ["KeyW", "KeyA", "KeyS", "KeyD"] if "movement" in spec.camera else []
    return keys + [key for field, *pair, _, _ in AXES if field in spec.camera for key in pair]


def drive_commands(spec: ModelSpec, held: set[str], state: dict[str, object]) -> list[tuple[str, dict]]:
    """The lane commands for the keys held now, minus the lanes already holding those values.

    Each lane in AXES follows its two keys. LingBot's single `movement` lane takes W/S/A/D,
    where W/S beats A/D. `state` maps each lane's command field to the value last sent (its idle
    value when absent) and is updated in place.
    """
    camera = spec.camera

    def pick(lane, axis, negative, positive):
        return lane.options[positive if axis > 0 else negative] if axis else lane.idle

    desired = {field: pick(camera[field], _axis(held, low, high), negative, positive)
               for field, low, high, negative, positive in AXES if field in camera}
    if "movement" in camera:
        lane = camera["movement"]
        longitudinal, lateral = _axis(held, "KeyS", "KeyW"), _axis(held, "KeyA", "KeyD")
        desired["movement"] = (pick(lane, longitudinal, "back", "forward") if longitudinal
                               else pick(lane, lateral, "strafe_left", "strafe_right"))
    commands = []
    for field, value in desired.items():
        lane = camera[field]
        if state.get(field, lane.idle) != value:
            state[field] = value
            commands.append((lane.command, {field: value}))
    return commands


class H264Decoder:
    """Annex-B H.264 access units decoded to rgb24 arrays of one fixed size."""

    def __init__(self, width: int, height: int):
        self.width, self.height = width, height
        self.context = av.CodecContext.create("h264", "r")

    def decode(self, access_unit: bytes) -> np.ndarray | None:
        """The newest frame the unit yields, or None while the decoder waits for more input."""
        frames = self.context.decode(av.Packet(access_unit))
        if not frames:
            return None
        return frames[-1].reformat(self.width, self.height, format="rgb24").to_ndarray()


class H264Encoder:
    """rgb24 arrays encoded to Annex-B H.264 access units the browser decodes cold per keyframe:
    baseline, no B-frames, a keyframe at least every two seconds, SPS/PPS inline before each one."""

    def __init__(self, width: int, height: int, fps: int, max_kbps: int | None = None):
        self.context = av.CodecContext.create("libx264", "w")
        self.context.width, self.context.height, self.context.pix_fmt = width, height, "yuv420p"
        self.context.framerate = Fraction(fps)
        self.context.time_base = Fraction(1, fps)
        self.context.gop_size = fps * 2
        self.context.max_b_frames = 0
        # A max_kbps ceiling holds over half a second, so a keyframe cannot burst past it.
        vbv = f":vbv-maxrate={max_kbps}:vbv-bufsize={max_kbps // 2}" if max_kbps else ""
        self.context.options = {"preset": "ultrafast", "tune": "zerolatency", "profile": "baseline",
                                "x264-params": "repeat-headers=1" + vbv}
        self.context.open()
        self.index = 0

    def encode(self, frame: np.ndarray, force_keyframe: bool = False) -> tuple[bytes, bool]:
        """The access unit for `frame`, and whether it starts with a keyframe."""
        out = av.VideoFrame.from_ndarray(np.ascontiguousarray(frame), format="rgb24").reformat(
            self.context.width, self.context.height, format="yuv420p")
        out.pts = self.index
        self.index += 1
        if force_keyframe:
            out.pict_type = av.video.frame.PictureType.I
        packets = self.context.encode(out)
        return b"".join(bytes(p) for p in packets), any(p.is_keyframe for p in packets)


def drive_setup(model: str, prompt: str, seed: int, image: bytes | None, settings: dict[str, str]) -> list[tuple[str, dict]]:
    """The commands that open a live drive run: a one-chunk timeline's setup."""
    return compile_timeline(model, [Beat(prompt, frames=MODELS[model].frames_in(1), image=image)], seed, settings).setup


def style_setup(spec: ModelSpec, prompt: str, seed: int, image: bytes | None) -> list[tuple[str, dict]]:
    """The commands that open a live style run."""
    if not spec.starts:
        # X2 generates once a prompt is set and source frames arrive.
        commands = [("set_keep_backlog", {"keep_backlog": False})]
        if image is not None:
            commands.append(("set_reference_image", {"reference_image": image}))
        return commands + [("set_prompt", {"prompt": prompt})]
    return [("set_seed", {"seed": seed}), ("set_prompt", {"prompt": prompt}), ("start", {})]


class LiveRun:
    """One live take in progress: a Reactor session bridged to the browser modal's websocket.

    The node awaits `run` on its own event loop, which `__init__` captures; the websocket
    handler calls `connected`/`receive`/`disconnected` from the server thread. The SDK's media
    thread delivers model frames. The webcam decode and the preview encode would block the
    loop, so each runs on a thread of its own.
    """

    def __init__(self, mode: str, spec: ModelSpec, path: str, prompt: str, setup: list[tuple[str, dict]],
                 input_size: tuple[int, int] | None, connect: dict, clip: list[np.ndarray] | None = None):
        self.run_id = uuid.uuid4().hex
        self.mode, self.spec, self.path = mode, spec, path
        self.prompt, self.setup = prompt, setup
        self.input_size, self.clip = input_size, clip
        self.connect = connect
        # The config message's preview hint; the encoder retargets on the first real frame.
        size = (clip[0].shape[1], clip[0].shape[0]) if clip else input_size if mode == "style" else spec.size
        self.preview_hint = preview_size(*size)
        self.preview_fps = int(spec.fps)
        # Set just before "ended" goes out; the websocket handler closes the socket on it.
        self.ended = False
        # Gates recording and preview, as Session.capturing does: the stream opens with a
        # placeholder frame before the model produces anything.
        self.capturing = False
        self._loop = asyncio.get_running_loop()
        self._send = None
        self._send_lock = threading.Lock()
        self._connected = asyncio.Event()
        self._finished = asyncio.Event()
        self._done = False
        self._error: str | None = None
        self._reactor = None
        self._writer: FrameWriter | None = None
        self._messages: asyncio.Queue[dict] = asyncio.Queue()
        # Drive state: the held key codes, and the value each lane was last sent.
        self.held: set[str] = set()
        self.lanes: dict[str, object] = {}
        # Webcam path (style): every access unit is decoded on this thread to keep the stream
        # valid; only the newest decoded frame is kept, for the paced push to take.
        self._decoder = H264Decoder(*input_size) if input_size else None
        self._webcam_in: queue.Queue[bytes | None] = queue.Queue()
        self._latest: tuple[np.ndarray, int] | None = None
        self._latest_lock = threading.Lock()
        self._decoded = 0
        # Preview path: the media thread keeps only the newest frame pending for the encoder
        # thread, which never re-queues.
        self._pending: np.ndarray | None = None
        self._pending_cond = threading.Condition()
        self._force_keyframe = False
        self.preview_kbps = PREVIEW_KBPS
        self._closed = False
        self._threads = [threading.Thread(target=self._decode_webcam, daemon=True),
                         threading.Thread(target=self._encode_preview, daemon=True)]
        for thread in self._threads:
            thread.start()

    def connected(self, send, preview_kbps: int = PREVIEW_KBPS) -> bool:
        """Attach the browser's sender and its preview bitrate. False when the run already has (or had) a socket."""
        with self._send_lock:
            if self._send is not None or self.ended:
                return False
            self._send = send
            self.preview_kbps = preview_kbps
        self._loop.call_soon_threadsafe(self._connected.set)
        return True

    def request_keyframe(self) -> None:
        """Make the next preview frame a keyframe. Thread-safe."""
        with self._pending_cond:
            self._force_keyframe = True

    def receive(self, message) -> None:
        """A websocket message from the browser, handed to the run's loop."""
        self._loop.call_soon_threadsafe(self._on_message, message)

    def disconnected(self) -> None:
        """The socket went away: a cancel unless the run already ended."""
        self._loop.call_soon_threadsafe(self._on_disconnected)

    def close(self) -> None:
        """Stop the codec threads. Idempotent."""
        with self._pending_cond:
            self._closed = True
            self._pending_cond.notify_all()
        self._webcam_in.put(None)
        for thread in self._threads:
            thread.join(timeout=2)

    async def run(self, check_interrupt) -> str:
        """Stream until the browser says done; return the saved take's path.

        Raises on cancel (the browser's cancel, its socket closing, or an interrupt) and on any
        session error. Either way the session is disconnected and the browser gets "ended".
        """
        try:
            await self._poll(self._connected, BROWSER_TIMEOUT_SECONDS, OPEN_TAB_ERROR, check_interrupt)
            self._send_json({"type": "config", "mode": self.mode, "prompt": self.prompt,
                             "keys": drive_keys(self.spec),
                             "preview": {"width": self.preview_hint[0], "height": self.preview_hint[1],
                                         "fps": self.preview_fps},
                             "input": None if self.input_size is None else
                                      {"width": self.input_size[0], "height": self.input_size[1], "fps": INPUT_FPS}})
            await self._session(check_interrupt)
            self._finish(error=None)
            return self.path
        except BaseException as e:
            self._finish(error=self._error or str(e) or "Cancelled.")
            raise
        finally:
            self.close()

    async def _poll(self, event: asyncio.Event, timeout: float, error: str, check_interrupt) -> None:
        deadline = time.monotonic() + timeout
        while not event.is_set():
            check_interrupt()
            if time.monotonic() > deadline:
                raise RuntimeError(error)
            await asyncio.sleep(0.05)

    async def _session(self, check_interrupt) -> None:
        self._status("Connecting to Reactor…")
        reactor = Reactor(self.spec.slug, max_session_duration_seconds=LIVE_SESSION_LIMIT_SECONDS, **self.connect)
        self._reactor = reactor
        writer = self._writer = FrameWriter(self.path, self.spec.fps)

        @reactor.on_message
        def queue_message(msg: dict) -> None:
            self._messages.put_nowait(msg)

        @reactor.on_track
        def capture_video(track) -> None:
            if track.kind == "video" and track.direction == "recvonly":
                track.on_frame(lambda frame: self.capturing and self._model_frame(frame))

        push = stats = None
        try:
            async with closing_session(reactor):
                await self._connect(reactor, check_interrupt)
                await asyncio.sleep(CONNECT_SETTLE_SECONDS)
                source = None
                if self.mode == "style":
                    source = await reactor.publish_track(self.spec.source_track)
                for command, data in self.setup:
                    await self._send_command(command, data)
                self.capturing = True
                if self.mode == "drive":
                    self._apply_drive()
                if source is not None:
                    push = asyncio.create_task(self._push_clip(source) if self.clip else self._push_webcam(source))
                    push.add_done_callback(self._push_failed)
                self._status("")
                stats = asyncio.create_task(self._report_stats(reactor))
                await self._pump(check_interrupt)
        except BaseException:
            # The session's error is the one worth reporting, not the encoder's.
            with contextlib.suppress(Exception):
                writer.close()
            raise
        finally:
            for task in (push, stats):
                if task is not None:
                    task.cancel()
        try:
            writer.close()
        except Exception:
            # A cancelled run keeps its error, even one that left the take empty.
            if self._error is None:
                raise
        if self._error is not None:
            raise RuntimeError(self._error)

    async def _connect(self, reactor, check_interrupt) -> None:
        task = asyncio.ensure_future(connect_with_retry(reactor, check_interrupt))
        try:
            waited = 0.0
            while True:
                try:
                    return await asyncio.wait_for(asyncio.shield(task), timeout=0.25)
                except asyncio.TimeoutError:
                    check_interrupt()
                    waited += 0.25
                    if waited >= 2.0:
                        self._status("Waiting for a free server…")
        finally:
            # Let a cancelled connect unwind before the session is ended.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _pump(self, check_interrupt) -> None:
        """Wait out the run: nothing but an interrupt wakes it, so poll between model messages."""
        while not self._finished.is_set():
            check_interrupt()
            try:
                msg = await asyncio.wait_for(self._messages.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue
            if msg.get("type") == "command_error":
                data = msg.get("data") or {}
                logging.warning("Reactor live: %s rejected mid-run: %s", data.get("command"), data.get("reason"))

    async def _push_webcam(self, track) -> None:
        """Push the newest decoded webcam frame at the input rate, skipping none twice."""
        pushed = -1
        while True:
            with self._latest_lock:
                latest = self._latest
            if latest is not None and latest[1] != pushed:
                frame, pushed = latest
                track.push_frame(frame)
            await asyncio.sleep(1 / INPUT_FPS)

    async def _push_clip(self, track) -> None:
        """Push the source clip at the model's frame rate, looping until the run ends."""
        next_at = time.monotonic()
        for frame in itertools.cycle(self.clip):
            track.push_frame(frame)
            next_at += 1 / self.spec.fps
            await asyncio.sleep(max(next_at - time.monotonic(), 0))

    async def _report_stats(self, reactor) -> None:
        while True:
            await asyncio.sleep(STATS_INTERVAL_SECONDS)
            stats = await reactor.get_stats()
            video = next((s for s in stats.inbound if s.kind == "video"), None)
            self._send_json({"type": "stats", "rtt_ms": stats.rtt_ms, "loss": stats.packet_loss_ratio,
                             "in_bps": stats.incoming_bitrate_bps, "out_bps": stats.outgoing_bitrate_bps,
                             "fps": video and video.frames_per_second})

    @staticmethod
    def _push_failed(task) -> None:
        """A dead push task stops the model's whole input; never let it die quietly."""
        if not task.cancelled() and task.exception() is not None:
            logging.error("Reactor live: source push task died.", exc_info=task.exception())

    async def _send_command(self, command: str, data: dict) -> None:
        data = {key: await self._reactor.upload_file(value, name=f"{key}.png", mime_type="image/png")
                if isinstance(value, bytes) else value for key, value in data.items()}
        reply = await self._reactor.send_command(command, data)
        if reply is not None and reply.get("type") == "command_error":
            raise RuntimeError(f"Reactor rejected {command}: {reply.get('data', {}).get('reason')}")

    async def _send_live(self, command: str, data: dict) -> None:
        """A mid-run command: a rejection costs the change, not the take."""
        try:
            await self._send_command(command, data)
        except Exception:
            logging.warning("Reactor live: %s failed mid-run.", command, exc_info=True)

    def _apply_drive(self) -> None:
        if self._reactor is None or not self.capturing:
            return
        for command, data in drive_commands(self.spec, self.held, self.lanes):
            self._loop.create_task(self._send_live(command, data))
        # The browser lights the keys it sees here: the held set the model is being driven by.
        self._send_json({"type": "keys", "held": sorted(self.held)})

    def _model_frame(self, frame) -> None:
        # On the SDK's media thread; both sinks are thread-safe queues.
        self._writer.push(frame)
        self._offer_preview(frame)

    def _decode_webcam(self) -> None:
        failed = False
        while (access_unit := self._webcam_in.get()) is not None:
            try:
                frame = self._decoder.decode(access_unit)
            except Exception:
                # The decoder lost sync; start fresh and ask the browser to send a keyframe.
                if not failed:
                    logging.warning("Reactor live: webcam decode failed; requesting a keyframe.", exc_info=True)
                failed = True
                self._decoder = H264Decoder(*self.input_size)
                self._send_json({"type": "keyframe"})
                continue
            failed = False
            if frame is not None:
                with self._latest_lock:
                    self._decoded += 1
                    self._latest = (frame, self._decoded)

    def _offer_preview(self, frame) -> None:
        with self._pending_cond:
            self._pending = frame
            self._pending_cond.notify()

    def _encode_preview(self) -> None:
        encoder = None
        while True:
            with self._pending_cond:
                while self._pending is None and not self._closed:
                    self._pending_cond.wait()
                if self._pending is None:
                    return
                frame, self._pending = self._pending, None
                force, self._force_keyframe = self._force_keyframe, False
            if encoder is None:
                height, width = frame.shape[:2]
                encoder = H264Encoder(*preview_size(width, height), self.preview_fps, self.preview_kbps)
            access_unit, keyframe = encoder.encode(frame, force_keyframe=force)
            if access_unit:
                self._send_bytes(pack_frame(keyframe, time.monotonic_ns() // 1000, access_unit))

    def _on_message(self, message) -> None:
        if isinstance(message, (bytes, bytearray)):
            if self._decoder is not None:
                _, _, access_unit = unpack_frame(bytes(message))
                self._webcam_in.put(access_unit)
            return
        try:
            msg = json.loads(message)
        except ValueError:
            return
        kind = msg.get("type")
        if kind == "prompt":
            self.prompt = str(msg.get("prompt", ""))
            if self._reactor is not None and self.capturing:
                self._loop.create_task(self._send_live(self.spec.prompt_command, {"prompt": self.prompt}))
        elif kind == "keys" and self.mode == "drive":
            self.held = set(msg.get("held") or [])
            self._apply_drive()
        elif kind == "keyframe":
            self.request_keyframe()
        elif kind == "done":
            self._done = True
            self._finished.set()
        elif kind == "cancel":
            self._cancel()

    def _on_disconnected(self) -> None:
        # A close after "done" is benign: the take finishes and its "ended" just goes nowhere.
        if not self.ended and not self._done:
            self._cancel()

    def _cancel(self) -> None:
        if self._error is None:
            self._error = "Cancelled."
        self._finished.set()

    def _finish(self, error: str | None) -> None:
        if not self.ended:
            self.ended = True
            self._send_json({"type": "ended", "error": error})
        self._finished.set()

    def _status(self, text: str) -> None:
        self._send_json({"type": "status", "text": text})

    def _send_json(self, message: dict) -> None:
        self._send_raw(json.dumps(message))

    def _send_bytes(self, message: bytes) -> None:
        self._send_raw(message)

    def _send_raw(self, message) -> None:
        with self._send_lock:
            send = self._send
        if send is not None:
            send(message)
