"""Debug logging of a Reactor session, on in development mode: the REACTOR_DEV_MODE environment variable set to anything but 0.

It goes to ComfyUI's log, one line per event, each timed from the session's start, so the log alone shows whether a
session worked: what it connected to and how long that took, every command with its reply and how long the reply
took, every upload, every message from the model, the tracks, when the video started, every pause in it and every
change of its size, how long each clip played against its length, the sound and source frames that flowed, when the
take was recording, why the session ended, and the saved file as it is on disk. The Realtime window sends its own
events to the same log. Secrets never appear: an API key or token is only ever said to be present."""

import functools
import importlib.metadata
import logging
import os
import re
import time

log = logging.getLogger("reactor")


def development_mode(environ=os.environ) -> bool:
    """Whether REACTOR_DEV_MODE turns development mode on: 1, true, yes or on. Unset, 0, false or anything else is off."""
    return environ.get("REACTOR_DEV_MODE", "").strip().lower() in ("1", "true", "yes", "on")


# Logged at INFO, since ComfyUI's console shows nothing below it; this switch alone turns it on.
DEV_MODE = development_mode()

# A pause in the frames this long or longer is logged. Observed on cloud sessions: frames arrive in bursts a few
# frame times apart, so shorter pauses are jitter, not a stop.
PAUSE_SECONDS = 0.5


def version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def plugin_version() -> str:
    """This plugin's version. ComfyUI loads it as a folder, not an installed package, so it is read from pyproject.toml."""
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pyproject.toml")) as f:
            found = re.search(r'^version\s*=\s*"([^"]+)"', f.read(), re.M)
        return found.group(1) if found else "unknown"
    except OSError:
        return version("comfyui-reactor-api")


def rounded(value, scale: float = 1, digits: int = 0) -> str:
    """A measured number as a log line shows it, or "none" where the SDK had no reading."""
    return "none" if value is None else f"{value / scale:.{digits}f}"


def brief(value, depth: int = 0):
    """A value cut down to what a log line needs: short text, file sizes, list lengths, and no image data."""
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, str):
        return value if len(value) <= 60 and not value.startswith("data:") else value[:57] + "..."
    if isinstance(value, dict):
        return {k: brief(v, depth + 1) for k, v in value.items()} if depth < 2 else f"<{len(value)} fields>"
    if isinstance(value, (list, tuple)):
        return [brief(v, depth + 1) for v in value[:4]] + ([f"<{len(value) - 4} more>"] if len(value) > 4 else [])
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return f"<{type(value).__name__}>"


def on_disk(path: str) -> str:
    """What a saved video holds, read back from the file."""
    if not os.path.exists(path):
        return "missing"
    import av  # Imported here: only a debug run reads its file back.
    try:
        with av.open(path) as container:
            video = container.streams.video[0] if container.streams.video else None
            frames = sum(1 for _ in container.demux(video)) - 1 if video else 0  # the demuxer ends on an empty packet
            seconds = float(container.duration or 0) / av.time_base
            sound = "with sound" if container.streams.audio else "without sound"
            size = f"{video.codec_context.width}x{video.codec_context.height}" if video else "no video"
        return f"{os.path.getsize(path)} bytes, {size}, {frames} frames, {seconds:.2f} s, {sound}"
    except Exception as e:
        return f"{os.path.getsize(path)} bytes, unreadable: {e}"


def quiet(method):
    """A Trace method: it does nothing outside development mode, and a fault in it is logged, never raised into the session."""
    @functools.wraps(method)
    def guarded(self, *args):
        if not DEV_MODE:
            return
        try:
            method(self, *args)
        except Exception as e:
            log.warning("Reactor %s: the debug log failed in %s: %r", self.name, method.__name__, e)
    return guarded


class Trace:
    """One session's debug log. Each method does nothing outside development mode, and never raises."""

    def __init__(self, name: str, clock=time.monotonic):
        self.name, self.clock, self.start = name, clock, clock()
        self.frames = 0
        self.first = self.last = None
        self.shape = None
        self.pauses = 0
        self.longest = 0.0
        self.playing: dict | None = None
        self.clips = 0
        self.sound = 0.0
        self.pushed = 0
        self.asked: dict[str, float] = {}
        # What was playing when the video last stopped, once the stop is logged; None while it flows.
        self.stopped: str | None = None

    def __call__(self, text: str, *args) -> None:
        if DEV_MODE:
            log.info(f"Reactor {self.name} +%.2fs " + text, self.clock() - self.start, *args)

    @quiet
    def begin(self, slug: str, connect: dict, what: str) -> None:
        where = "cloud" if "api_key" in connect else f"local at {connect.get('api_url', 'the default address')}"
        self("%s on %s (%s), API key %s; plugin %s, reactor-sdk %s", what, slug, where,
             "present" if connect.get("api_key") else "absent", plugin_version(), version("reactor-sdk"))

    @quiet
    def status(self, status) -> None:
        self("status %s", getattr(status, "value", status))

    @quiet
    def sent(self, command: str, data: dict) -> None:
        self.asked[command] = self.clock()
        self("-> %s %s", command, brief(data))

    @quiet
    def replied(self, command: str, reply: dict | None) -> None:
        took = (self.clock() - self.asked.pop(command, self.clock())) * 1000
        self("<- reply to %s in %.0f ms: %s", command, took, brief(reply))

    @quiet
    def uploaded(self, name: str, data: bytes, began: float) -> None:
        self("uploaded %s, %d bytes, in %.0f ms", name, len(data), (self.clock() - began) * 1000)

    @quiet
    def message(self, msg: dict) -> None:
        kind = msg.get("type")
        data = msg.get("data") or {}
        clip = data.get("clip") or {}
        # A clip's own fields are many; its id and length say which it is.
        self("<- %s %s", kind, {"clip": str(clip.get("clip_id"))[:8], "frames": clip.get("frames")} if clip else brief(data))
        playing = self.playing
        if kind == "clip_started":
            self.playing = {"id": clip.get("clip_id"), "at": self.clock(), "frames": self.frames, "holds": clip.get("frames")}
        elif kind in ("clip_finished", "clip_stopped") and playing and playing["id"] == clip.get("clip_id"):
            self.playing = None
            self.clips += kind == "clip_finished"
            # A count can be off by a frame or two, since a model's messages and its frames arrive apart.
            holds = f" of the {playing['holds']:g} it holds" if playing["holds"] else ""
            self("clip %s %s after %.2f s, with %d frames%s", str(playing["id"])[:8], kind.removeprefix("clip_"),
                 self.clock() - playing["at"], self.frames - playing["frames"], holds)

    @quiet
    def track(self, track) -> None:
        self("track %s: %s, %s", getattr(track, "name", "?"), track.kind, track.direction)

    @quiet
    def frame(self, shape) -> None:
        now = self.clock()
        if self.first is None:
            self("first video frame")
        elif now - self.last >= PAUSE_SECONDS:
            self.pauses += 1
            self.longest = max(self.longest, now - self.last)
            self("video resumed after %.2f s with none, which stopped %s", now - self.last, self.stopped or self.during())
            self.stopped = None
        if shape != self.shape:
            self("video is %dx%d", shape[1], shape[0])
            self.shape = shape
        self.first = now if self.first is None else self.first
        self.last = now
        self.frames += 1

    def during(self) -> str:
        """What was playing: a stop mid-clip is a fault, while a clip model sends nothing between clips."""
        # Read once: the model's messages, which set it, and the frames, which read it, come on different threads.
        playing = self.playing
        return f"during clip {str(playing['id'])[:8]}" if playing else "with no clip playing" if self.clips else "mid-stream"

    @quiet
    def check(self) -> None:
        """Logs the video stopping as soon as it has, rather than only once it comes back. Call it every second or so."""
        if self.last is not None and self.stopped is None and self.clock() - self.last >= PAUSE_SECONDS:
            self.stopped = self.during()
            self("video stopped %s; the last frame came %.2f s ago", self.stopped, self.clock() - self.last)

    @quiet
    def audio(self, samples: int, rate: int) -> None:
        if not self.sound:
            self("first sound, at %d Hz", rate)
        self.sound += samples / rate

    @quiet
    def push(self) -> None:
        if not self.pushed:
            self("first source frame pushed")
        self.pushed += 1

    @quiet
    def summary(self) -> None:
        if self.first is None:
            self("summary: no video arrived")
        else:
            span = self.last - self.first
            self("summary: %d frames over %.1f s (%.1f fps), %d clips played to the end, %d pauses of %g s or more, the longest %.2f s",
                 self.frames, span, (self.frames - 1) / span if span > 0 else 0.0, self.clips, self.pauses,
                 PAUSE_SECONDS, self.longest)
            self("summary: the last frame came %.1f s before the end", self.clock() - self.last)
        self("summary: %.1f s of sound received, %d source frames pushed", self.sound, self.pushed)

    @quiet
    def recording(self, clip, marks: list) -> None:
        """Which session's recording a take is cut from, and the spans of it the take keeps."""
        if clip is not None:
            spans = ", ".join(f"{start:.2f}-{end:.2f} s" for start, end in zip(marks[::2], marks[1::2]))
            self("the session's recording (session %s) runs to %.2f s; the take keeps %s",
                 str(clip.session_id)[:8], clip.end_marker, spans or "nothing")

    @quiet
    def saved(self, path: str) -> None:
        self("saved %s: %s", path, on_disk(path))
