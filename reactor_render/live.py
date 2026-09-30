import asyncio
import contextlib
import itertools
import json
import logging
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import replace
import numpy as np

from reactor_sdk import DEFAULT_API_URL, Reactor, ReactorStatus

from .encode import FrameWriter
from .session import (SETUP_PHASES, SETUP_READY, SETUP_SECONDS, CONNECT_SETTLE_SECONDS, REPLY_TIMEOUT_SECONDS, SessionEnded, session_phase,
                      api_message, closing_session, connect_with_retry, watch_for_failure)
from .timeline import MODELS, Beat, ModelSpec, compile_timeline, data_url, edit_setup

# A live take is capped at half an hour; the SDK ends the session when the token hits this.
LIVE_SESSION_LIMIT_SECONDS = 1800
# How long a run waits for the browser modal to open its socket before giving up.
BROWSER_TIMEOUT_SECONDS = 60.0
INPUT_FPS = 24
# How often the browser gets the model connection's stats.
STATS_INTERVAL_SECONDS = 1.0
# How long the browser gets to join the session and publish its camera, retries included.
PUBLISH_TIMEOUT_SECONDS = 60.0

OPEN_TAB_ERROR = "Open this workflow in a ComfyUI browser tab to record a live take."

# The live takes in progress, by run id: the websocket handler finds its run here.
RUNS: dict[str, "LiveRun"] = {}


def model_tracks(spec: ModelSpec) -> list[dict]:
    """The session's media tracks for a model, as the JS SDK's `modelTracks` must list them: its
    source track, if it takes one, and the video it plays. A call lists `webcam` though only a video
    call reads it; this run publishes only `mic`."""
    if spec.pattern == "call":
        return [{"name": "mic", "kind": "audio", "direction": "sendonly"},
                {"name": "webcam", "kind": "video", "direction": "sendonly"},
                {"name": "main_video", "kind": "video", "direction": "recvonly"},
                {"name": "main_audio", "kind": "audio", "direction": "recvonly"}]
    source = [{"name": spec.source_track, "kind": "video", "direction": "sendonly"}] if spec.source_track else []
    return source + [{"name": "main_video", "kind": "video", "direction": "recvonly"}]


def session_token(api_key: str, model: str, session_id: str, api_url: str = DEFAULT_API_URL) -> str:
    """A token that can join only this session, for the browser to watch it and publish its camera."""
    body = {"expires_after": LIVE_SESSION_LIMIT_SECONDS + 60,
            "authorization_details": [{"type": "session", "resources": {"models": {"match": [model]}, "sessions": {"bind": [session_id]}}}]}
    request = urllib.request.Request(f"{api_url.rstrip('/')}/tokens", data=json.dumps(body).encode(), method="POST",
                                     headers={"Reactor-API-Key": api_key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())["jwt"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Reactor refused a token for the browser ({e.code}): {api_message(e.read().decode(errors='replace'))}") from e


# Each two-way camera lane with its keys and options: (field, negative key, positive key, negative option, positive option).
AXES = [("move_longitudinal", "KeyS", "KeyW", "back", "forward"),
        ("move_lateral", "KeyA", "KeyD", "strafe_left", "strafe_right"),
        ("camera_pose", "KeyQ", "KeyE", "orbit_left", "orbit_right"),
        ("look_horizontal", "ArrowLeft", "ArrowRight", "left", "right"),
        ("look_vertical", "ArrowUp", "ArrowDown", "up", "down")]


def drive_lanes(spec: ModelSpec) -> list[dict]:
    """The camera lanes the modal drives from the keyboard, for a model with camera control.

    Each lane is set with `command` to `{field: value}`. Its `axes` are key pairs as (negative key,
    positive key, negative value, positive value); the first pair with exactly one key held sets
    the lane, and with none the lane rests at `idle`. LingBot's single `movement` lane takes W/S
    ahead of A/D.
    """
    camera = spec.camera

    def lane(field, *axes):
        options = camera[field].options
        return {"field": field, "command": camera[field].command, "idle": camera[field].idle,
                "axes": [[low, high, options[negative], options[positive]] for low, high, negative, positive in axes]}

    lanes = [lane("movement", ("KeyS", "KeyW", "back", "forward"), ("KeyA", "KeyD", "strafe_left", "strafe_right"))
             ] if "movement" in camera else []
    return lanes + [lane(field, tuple(axis)) for field, *axis in AXES if field in camera]


def drive_setup(model: str, beat: Beat, seed: int, settings: dict[str, object]) -> list[tuple[str, dict]]:
    """The commands that open a live drive run from `beat`: its setup as a one-chunk timeline, without its camera moves."""
    return compile_timeline(model, [replace(beat, frames=MODELS[model].frames_in(1), moves=())], seed, settings).setup


def style_setup(spec: ModelSpec, prompt: str, seed: int, image: bytes | None, settings: dict[str, object]) -> list[tuple[str, dict]]:
    """The commands that open a live style run."""
    if not spec.prompted:
        # A model without a prompt is steered by its image and settings alone, as an edit is.
        return edit_setup(image, settings.get("editing_type"))
    if not spec.starts:
        # X2 generates once a prompt is set and source frames arrive.
        commands = [("set_keep_backlog", {"keep_backlog": False})]
        if image is not None:
            commands.append(("set_reference_image", {"reference_image": image}))
        return commands + [("set_prompt", {"prompt": prompt})]
    return [("set_seed", {"seed": seed}), ("set_prompt", {"prompt": prompt}), ("start", {})]


def switch_controls(spec: ModelSpec, settings: dict[str, object], setup: list[tuple[str, dict]]) -> dict | None:
    """What the modal's switch controls send mid-take, for a model steered by an image and settings instead of a prompt.

    `image` is the field of `prompt_command` a new image goes in, `reference` the image the take starts from, and
    each of `settings` is a setting of that command with its options and the value the take starts at.
    """
    if spec.prompted:
        return None
    reference = next((data[spec.switch_image] for _, data in setup if spec.switch_image in data), None)
    return {"image": spec.switch_image, "reference": reference and data_url(reference),
            "settings": [{"field": setting.field or name, "options": list(setting.options), "value": settings.get(name, setting.default)}
                         for name, setting in spec.beat_settings.items() if setting.command == spec.prompt_command and setting.options]}


class LiveRun:
    """One live take in progress: a Reactor session this server records and the browser modal joins.

    The server opens the session, sends its commands, and records the model's output. The browser
    joins the same session with a token bound to it, plays the output as the preview, and
    publishes its camera when that is the source, or its microphone on a call. The websocket
    carries only control messages.

    The node awaits `run` on its own event loop, which `__init__` captures; the websocket
    handler calls `connected`/`receive`/`disconnected` from the server thread. The SDK's media
    thread delivers model frames.
    """

    def __init__(self, mode: str, spec: ModelSpec, path: str, prompt: str, setup: list[tuple[str, dict]],
                 input_size: tuple[int, int] | None, connect: dict, clip: list[np.ndarray] | None = None,
                 settings: dict[str, object] | None = None):
        self.run_id = uuid.uuid4().hex
        self.mode, self.spec, self.path = mode, spec, path
        self.prompt, self.setup = prompt, setup
        # The beat settings the take starts with, which the modal's switch controls open at.
        self.settings = settings or {}
        self.input_size, self.clip = input_size, clip
        self.connect = connect
        # The output's expected size, which the modal lays the preview out at until video arrives.
        self.source_size = (clip[0].shape[1], clip[0].shape[0]) if clip else input_size if mode == "style" else spec.size
        # Set just before "ended" goes out; the websocket handler closes the socket on it.
        self.ended = False
        # Gates recording, as Session.capturing does: the stream opens with a
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
        self._published = asyncio.Event()
        # The model's messages, which a call's setup waits on for its phases.
        self._messages: asyncio.Queue[dict] = asyncio.Queue()
        self._frame_shape: tuple[int, ...] | None = None
        # True from connect until the run starts closing: a disconnect then is a drop, not ours.
        self._watching = False

    def connected(self, send) -> bool:
        """Attach the browser's sender. False when the run already has (or had) a socket."""
        with self._send_lock:
            if self._send is not None or self.ended:
                return False
            self._send = send
        self._loop.call_soon_threadsafe(self._connected.set)
        return True

    def receive(self, message) -> None:
        """A websocket message from the browser, handed to the run's loop."""
        self._loop.call_soon_threadsafe(self._on_message, message)

    def disconnected(self) -> None:
        """The socket went away: a cancel unless the run already ended."""
        self._loop.call_soon_threadsafe(self._on_disconnected)

    async def run(self, check_interrupt) -> str:
        """Stream until the browser says done; return the saved take's path.

        Raises on cancel (the browser's cancel, its socket closing, or an interrupt) and on any
        session error. Either way the session is disconnected and the browser gets "ended".
        """
        try:
            await self._poll(self._connected, BROWSER_TIMEOUT_SECONDS, OPEN_TAB_ERROR, check_interrupt)
            # On a call, the text box sends the character a message, as if spoken, instead of a new prompt.
            command, field = ("say", "text") if self.mode == "call" else (self.spec.prompt_command, "prompt" if self.spec.prompted else None)
            self._send_json({"type": "config", "mode": self.mode, "prompt": self.prompt,
                             "lanes": drive_lanes(self.spec), "prompt_command": command, "prompt_field": field,
                             "switch": switch_controls(self.spec, self.settings, self.setup),
                             "preview": dict(zip(("width", "height"), self.source_size)),
                             "input": None if self.input_size is None else
                                      {"width": self.input_size[0], "height": self.input_size[1], "fps": INPUT_FPS}})
            await self._session(check_interrupt)
            self._finish(error=None)
            return self.path
        except BaseException as e:
            self._finish(error=self._error or str(e) or "Cancelled.")
            raise

    async def _poll(self, event: asyncio.Event, timeout: float, error: str, check_interrupt) -> None:
        deadline = time.monotonic() + timeout
        while not event.is_set():
            check_interrupt()
            if self._finished.is_set():
                raise RuntimeError(self._error or "Cancelled.")
            if time.monotonic() > deadline:
                raise RuntimeError(error)
            await asyncio.sleep(0.05)

    async def _session(self, check_interrupt) -> None:
        self._status("Connecting to Reactor…")
        reactor = Reactor(self.spec.slug, max_session_duration_seconds=LIVE_SESSION_LIMIT_SECONDS, **self.connect)
        self._reactor = reactor
        writer = self._writer = FrameWriter(self.path, self.spec.fps)

        @reactor.on_track
        def capture(track) -> None:
            if track.kind == "video" and track.direction == "recvonly":
                track.on_frame(self._push_video)
            if track.kind == "audio" and track.direction == "recvonly":
                track.on_frame(lambda pcm, sample_rate: self.capturing and writer.push_audio(pcm, sample_rate))

        @reactor.on_message
        def queue_message(msg: dict) -> None:
            self._loop.call_soon_threadsafe(self._messages.put_nowait, msg)

        watch_for_failure(reactor, lambda reason: self._loop.call_soon_threadsafe(self._dropped, reason))

        @reactor.on_status(ReactorStatus.DISCONNECTED)
        def dropped(_) -> None:
            self._loop.call_soon_threadsafe(self._dropped)

        push = stats = None
        try:
            async with closing_session(reactor):
                await self._connect(reactor, check_interrupt)
                self._watching = True
                try:
                    await asyncio.sleep(CONNECT_SETTLE_SECONDS)
                    camera = self.mode == "style" and self.clip is None
                    publish = self.spec.source_track if camera else "mic" if self.mode == "call" else None
                    # A local runtime takes no token.
                    jwt = None if "api_key" not in self.connect else await asyncio.to_thread(
                        session_token, self.connect["api_key"], self.spec.slug, reactor.session_id)
                    self._send_json({"type": "join", "model": self.spec.slug, "session_id": reactor.session_id, "jwt": jwt,
                                     "local": "api_key" not in self.connect, "tracks": model_tracks(self.spec),
                                     "publish": publish})
                    if publish:
                        await self._poll(self._published, PUBLISH_TIMEOUT_SECONDS,
                                         f"The browser did not start its {'microphone' if self.mode == 'call' else 'camera'} in time.",
                                         check_interrupt)
                    elif self.mode == "style":
                        # The clip flows from before setup, as a camera does, for a model that warms up on its source.
                        push = asyncio.create_task(self._push_clip(await reactor.publish_track(self.spec.source_track)))
                        push.add_done_callback(self._push_failed)
                    for command, data in self.setup:
                        await self._send_command(command, data)
                        if command in SETUP_PHASES:
                            self._status("Starting the call…" if self.mode == "call" else "Starting…")
                            await self._wait_for_phase(SETUP_PHASES[command], SETUP_READY.get(command), command != "create_avatar", check_interrupt)
                    if self.mode == "call":
                        await self._wait_for_character(check_interrupt)
                        writer.fill_gaps = True
                    self.capturing = True
                    self._status("")
                    # From here the browser sends the model its prompt and drive commands itself.
                    self._send_json({"type": "started"})
                    stats = asyncio.create_task(self._report_stats(reactor))
                    await self._pump(check_interrupt)
                finally:
                    self._watching = False
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
        task = asyncio.ensure_future(connect_with_retry(reactor, check_interrupt, self._status))
        try:
            waited = 0.0
            while True:
                try:
                    return await asyncio.wait_for(asyncio.shield(task), timeout=0.25)
                except asyncio.TimeoutError:
                    check_interrupt()
                    if self._finished.is_set():
                        raise RuntimeError(self._error or "Cancelled.")
                    waited += 0.25
                    if waited >= 2.0:
                        self._status("Waiting for a free server…")
        finally:
            # Let a cancelled connect unwind before the session is ended.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _pump(self, check_interrupt) -> None:
        """Wait out the run: nothing but an interrupt, or a call ending, wakes it, so poll for those."""
        while not self._finished.is_set():
            check_interrupt()
            while not self._messages.empty():
                try:
                    session_phase(self._messages.get_nowait())
                except SessionEnded as e:
                    if e.phase != "ended":
                        raise
                    # The model ended the take itself, as an edit does at its time limit; the take keeps what it has.
                    logging.warning("Reactor live: %s; saving the take.", e)
                    self._done = True
                    return
            await asyncio.sleep(0.1)

    async def _wait_for_phase(self, phase: str, ready: str | None, in_call: bool, check_interrupt) -> None:
        give_up = time.monotonic() + SETUP_SECONDS
        while True:
            check_interrupt()
            if self._finished.is_set():
                raise RuntimeError(self._error or "Cancelled.")
            if time.monotonic() > give_up:
                raise RuntimeError(f"The session never reached {phase}{f' with {ready}' if ready else ''}.")
            try:
                msg = await asyncio.wait_for(self._messages.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            if session_phase(msg, in_call) == phase and (ready is None or msg["data"].get(ready)):
                return

    async def _wait_for_character(self, check_interrupt) -> None:
        """Wait out the connect placeholder, which a live call keeps streaming until the character appears."""
        placeholder = self._frame_shape
        give_up = time.monotonic() + REPLY_TIMEOUT_SECONDS
        while self._frame_shape == placeholder:
            check_interrupt()
            if self._finished.is_set():
                raise RuntimeError(self._error or "Cancelled.")
            if time.monotonic() > give_up:
                raise RuntimeError("The avatar never appeared.")
            await asyncio.sleep(0.05)

    def _push_video(self, frame: np.ndarray) -> None:
        self._frame_shape = frame.shape
        if self.capturing:
            self._writer.push(frame)

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

    def _on_message(self, message) -> None:
        try:
            msg = json.loads(message)
        except ValueError:
            return
        kind = msg.get("type")
        if kind == "published":
            self._published.set()
        elif kind == "done":
            self._done = True
            self._finished.set()
        elif kind == "cancel":
            self._cancel(msg.get("error"))

    def _on_disconnected(self) -> None:
        # A close after "done" is benign: the take finishes and its "ended" just goes nowhere.
        if not self.ended and not self._done:
            self._cancel()

    def _cancel(self, error: str | None = None) -> None:
        if self._error is None:
            self._error = error or "Cancelled."
        self._finished.set()

    def _dropped(self, reason: str = "The Reactor session ended unexpectedly.") -> None:
        """The server's side of the session went away mid-run; the take ends with what it has."""
        if self._watching:
            self._cancel(reason)

    def _finish(self, error: str | None) -> None:
        if not self.ended:
            self.ended = True
            self._send_json({"type": "ended", "error": error})
        self._finished.set()

    def _status(self, text: str) -> None:
        self._send_json({"type": "status", "text": text})

    def _send_json(self, message: dict) -> None:
        with self._send_lock:
            send = self._send
        if send is not None:
            send(json.dumps(message))
