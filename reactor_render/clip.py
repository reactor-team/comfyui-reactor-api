import asyncio
import io
from collections.abc import Iterator

import av
import numpy as np

from .timeline import ModelSpec


def fit_frame(spec: ModelSpec, frame: av.VideoFrame) -> np.ndarray:
    """A decoded source frame as an upright RGB uint8 array, scaled to cover the model's frame size and
    center-cropped to it, as `ModelSpec.fit` does for an image. Not `fit` itself: a PIL resize of the full
    frame costs several times the decode, where swscale converts and scales in one pass."""
    # The display rotation a phone records, read as ComfyUI's own video loader reads it.
    turns = int(round(frame.rotation // 90)) % 4 if frame.rotation else 0
    width, height = (frame.height, frame.width) if turns % 2 else (frame.width, frame.height)
    w, h = spec.frame_size(width, height)
    scale = max(w / width, h / height)
    cover_w, cover_h = max(w, round(width * scale)), max(h, round(height * scale))
    size = (cover_h, cover_w) if turns % 2 else (cover_w, cover_h)
    rgb = frame.reformat(*size, format="rgb24", interpolation="AREA").to_ndarray()
    if turns:
        rgb = np.rot90(rgb, k=turns, axes=(0, 1))
    x, y = (cover_w - w) // 2, (cover_h - h) // 2
    return np.ascontiguousarray(rgb[y:y + h, x:x + w])


def fitted_frames(spec: ModelSpec, source: bytes, loop: bool) -> Iterator[np.ndarray]:
    """The clip resampled to the model's frame rate: each tick shows the source frame on screen at that time, so a
    clip plays at its own speed whatever its rate, and a frame no tick shows is never fitted."""
    step = 1 / spec.fps
    while True:
        decoded = False
        with av.open(io.BytesIO(source)) as container:
            stream = container.streams.video[0]
            stream.thread_type = "AUTO"
            interval = 1 / (stream.average_rate or stream.guessed_rate or spec.fps)
            tick, start, held, fitted, at = 0, None, None, None, 0.0
            for index, frame in enumerate(container.decode(stream)):
                t = frame.time if frame.time is not None else index * interval
                start = t if start is None else start
                # Ticks before this frame's time show the frame before it; the epsilon keeps equal rates one to one.
                while held is not None and tick * step < t - start - 1e-6:
                    fitted = fit_frame(spec, held) if fitted is None else fitted
                    decoded = True
                    yield fitted
                    tick += 1
                held, fitted, at = frame, None, t - start
            # The last frame holds for one source frame.
            while held is not None and (tick * step < at + interval - 1e-6 or not decoded):
                fitted = fit_frame(spec, held) if fitted is None else fitted
                decoded = True
                yield fitted
                tick += 1
        # A clip with no frames would otherwise loop forever.
        if not loop or not decoded:
            return


class ClipStream:
    """A source clip read frame by frame for streaming to a model, decoded on a thread one frame ahead of its
    reader so only a couple of fitted frames are ever held, however long the clip. With `loop`, it starts over
    at its end, forever."""

    def __init__(self, frames: Iterator[np.ndarray], first: np.ndarray):
        self.first = first
        self._frames = frames
        self._head: np.ndarray | None = first
        self._ahead = asyncio.ensure_future(asyncio.to_thread(next, frames, None))

    @classmethod
    async def open(cls, spec: ModelSpec, source: bytes, loop: bool = False) -> "ClipStream | None":
        """The clip ready to read, or None if it has no frames."""
        frames = fitted_frames(spec, source, loop)
        first = await asyncio.to_thread(next, frames, None)
        return None if first is None else cls(frames, first)

    async def next(self) -> np.ndarray | None:
        """The clip's next frame, or None once it has ended."""
        if self._head is not None:
            frame, self._head = self._head, None
            return frame
        frame = await self._ahead
        if frame is not None:
            self._ahead = asyncio.ensure_future(asyncio.to_thread(next, self._frames, None))
        return frame

    async def close(self) -> None:
        # The decode in flight runs on until it returns; the generator is only closed once nothing is using it.
        await asyncio.gather(self._ahead, return_exceptions=True)
        self._frames.close()
