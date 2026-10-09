import asyncio
import logging
import os
import threading
import time
import urllib.error

import av
import numpy as np

from reactor_sdk import Reactor
from reactor_sdk._recording import download_clip
from reactor_sdk.errors import ReactorError

from .encode import FrameWriter, add_audio

# How far back a mark's clip reaches; only the clip's `now_marker` is read.
MARK_CLIP_SECONDS = 0.1
# How long a recording may take to be ready once the session has ended; measured on cloud sessions, about a second
# and at most 3.5 s.
READY_SECONDS = 15.0
# A ready playlist can still list a segment that isn't served yet (measured: one 404); retry this many times, waiting longer each time.
DOWNLOAD_ATTEMPTS = 4
RETRY_SECONDS = 1.0


class Recording:
    """The session's own recording of a take: the spans of it the take keeps, then the file cut from them.

    The runtime records the model's output with its sound already in step and with no frame lost on
    the way to this client, so a take is saved from it rather than from the frames received here. A
    take keeps the same spans the client captures: `mark` notes the recording's media time each time
    capturing starts or stops, and a span runs from one mark to the next (docs: deploy/development/recording).
    """

    def __init__(self, reactor: Reactor):
        self.reactor = reactor
        self.loop = asyncio.get_running_loop()
        self.marks: list[float | None] = []
        self.pending: list = []
        self.failure: str | None = None
        self._lock = threading.Lock()

    def mark(self) -> None:
        """Note the recording's media time now. Safe from any thread."""
        with self._lock:
            index = len(self.marks)
            self.marks.append(None)
        self.pending.append(asyncio.run_coroutine_threadsafe(self._mark(index), self.loop))

    async def _mark(self, index: int) -> None:
        sent = time.monotonic()
        try:
            clip = await self.reactor.request_clip(MARK_CLIP_SECONDS)
        except ReactorError as e:
            if "no media generated yet" in str(e):
                self.marks[index] = 0.0
            else:
                self.failure = str(e)
            return
        except Exception as e:
            self.failure = f"{type(e).__name__}: {e}"
            return
        # Measured on cloud sessions: the recorder's clock runs ahead of the frames seen here by about a round trip.
        self.marks[index] = max(0.0, clip.now_marker - (time.monotonic() - sent))

    async def finish(self):
        """The recording's clip, asked for while the session is still up, or None when there's none to save."""
        await asyncio.gather(*(asyncio.wrap_future(f) for f in self.pending))
        try:
            if self.failure is not None:
                raise RuntimeError(self.failure)
            clip = await self.reactor.request_recording()
        except Exception as e:
            logging.warning("Reactor: the session's recording isn't available (%s); keeping the frames this client received.", e)
            return None
        if len(self.marks) % 2:
            self.marks.append(clip.end_marker)
        return clip

    async def save(self, clip, jwt: str | None, path: str, fps: float, limit: int | None = None) -> bool:
        """Write the take's spans of `clip` to `path` at `fps`. False, leaving `path` as it was, when that fails.

        Call it once the session has ended: the recording's last chunk is written only then.
        """
        source = path + ".recording.mp4"
        try:
            for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
                try:
                    await download_clip(clip, source, jwt=jwt, ready_timeout=READY_SECONDS)
                    break
                except urllib.error.HTTPError as e:
                    if e.code != 404 or attempt == DOWNLOAD_ATTEMPTS:
                        raise
                    await asyncio.sleep(RETRY_SECONDS * attempt)
            spans = list(zip(self.marks[::2], self.marks[1::2]))
            await asyncio.to_thread(cut, source, spans, fps, path, limit)
            return True
        except Exception:
            logging.warning("Reactor: saving the session's recording failed; keeping the frames this client received.", exc_info=True)
            return False
        finally:
            if os.path.exists(source):
                os.remove(source)


def cut(source: str, spans: list[tuple[float, float]], fps: float, path: str, limit: int | None = None) -> None:
    """Write `spans` of the recording at `source`, in media seconds, to `path` at `fps`, keeping at most `limit` frames.

    Each output frame is the recorded frame nearest its time, so a recording made at a higher rate than
    the model's loses its repeated frames. Each span's sound is cut to its frames' length.
    """
    slots = [(start + k / fps, i) for i, (start, end) in enumerate(spans) for k in range(round((end - start) * fps))]
    slots = slots[:limit] if limit is not None else slots
    counts = [0] * len(spans)
    tmp = path + ".cut.mp4"
    writer = FrameWriter(tmp, fps)
    try:
        with av.open(source) as container:
            nearest = iter(slots)
            slot = next(nearest, None)
            previous = None
            for frame in container.decode(video=0):
                while slot is not None and previous is not None and slot[0] < (previous[0] + frame.time) / 2:
                    writer.push(previous[1])
                    counts[slot[1]] += 1
                    slot = next(nearest, None)
                if slot is None:
                    break
                previous = (frame.time, frame.to_ndarray(format="rgb24"))
            while slot is not None and previous is not None:
                writer.push(previous[1])
                counts[slot[1]] += 1
                slot = next(nearest, None)
    finally:
        writer.close()
    pcm, rate = read_audio(source)
    if pcm is not None:
        pcm = np.concatenate([pcm[round(start * rate):round(start * rate) + round(n / fps * rate)]
                              for (start, _), n in zip(spans, counts)])
        if pcm.any():
            add_audio(tmp, pcm, rate)
    os.replace(tmp, path)


def read_audio(source: str) -> tuple[np.ndarray | None, int]:
    """The file's sound as `(samples, channels)` int16 PCM from its media time 0, and its rate; None without sound."""
    with av.open(source) as container:
        if not container.streams.audio:
            return None, 0
        stream = container.streams.audio[0]
        rate, channels = stream.rate, stream.channels
        resampler = av.AudioResampler(format="s16", layout="mono" if channels == 1 else "stereo", rate=rate)
        blocks = []
        start = None
        for frame in container.decode(audio=0):
            if start is None:
                start = frame.time or 0.0
            for out in resampler.resample(frame):
                blocks.append(out.to_ndarray().reshape(-1, channels))
        for out in resampler.resample(None):
            blocks.append(out.to_ndarray().reshape(-1, channels))
    if not blocks:
        return None, rate
    lead = np.zeros((round((start or 0.0) * rate), channels), np.int16)
    return np.concatenate([lead, *blocks]), rate
