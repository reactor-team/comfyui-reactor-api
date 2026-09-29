import queue
import threading
import time
from fractions import Fraction

import av
import numpy as np


class FrameWriter:
    """Encodes a session's streamed video to an H.264 MP4, keeping its first `limit` frames once one is set.

    `push` is called on the SDK's media thread, so it only queues; encoding runs on a thread of
    its own. Every frame is scaled to the first one's size, since WebRTC may change resolution.
    Frames are kept in arrival order: cloud frames carry no frame id or timestamp to place them by.
    """

    def __init__(self, path: str, fps: float):
        self.path, self.fps = path, fps
        self.limit: int | None = None
        self.received = 0
        self.previous: np.ndarray | None = None
        self.last_frame_at: float | None = None
        self.error: BaseException | None = None
        self.frames: queue.Queue[np.ndarray | None] = queue.Queue()
        self.thread = threading.Thread(target=self._encode, daemon=True)
        self.thread.start()

    @property
    def full(self) -> bool:
        return self.limit is not None and self.received >= self.limit

    def push(self, frame: np.ndarray) -> None:
        self.last_frame_at = time.monotonic()
        if not self.full:
            self.received += 1
            self.frames.put(frame)
        self.previous = frame

    def close(self) -> None:
        """Finish the file. Raises what the encoder raised, or if no frame ever arrived."""
        self.frames.put(None)
        self.thread.join()
        if self.error is not None:
            raise self.error
        if self.received == 0:
            raise RuntimeError("The session streamed no video frames.")

    def _encode(self) -> None:
        try:
            with av.open(self.path, "w") as container:
                stream = None
                index = 0
                while (frame := self.frames.get()) is not None:
                    if stream is None:
                        stream = container.add_stream("libx264", rate=Fraction(self.fps).limit_denominator(1001))
                        stream.height, stream.width = frame.shape[:2]
                        stream.pix_fmt = "yuv420p"
                    out = av.VideoFrame.from_ndarray(np.ascontiguousarray(frame), format="rgb24")
                    out = out.reformat(stream.width, stream.height, "yuv420p")
                    # Known gap: timing is the frame slot at the model's nominal fps, since frames
                    # arrive unstamped, so a model that adapts its rate plays at the wrong speed.
                    # Model-attached chunk/frame `user_data` (docs: concepts/frame-metadata)
                    # would make this exact.
                    out.pts = index
                    index += 1
                    container.mux(stream.encode(out))
                if stream is not None:
                    container.mux(stream.encode())
        except BaseException as e:
            self.error = e
            # Keep draining so `push` never backs up behind a dead encoder.
            while self.frames.get() is not None:
                pass
