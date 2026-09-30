import os
import queue
import threading
import time
from fractions import Fraction

import av
import numpy as np

# How far sound may trail the frames before `fill_gaps` takes it as missing rather than late.
AUDIO_SLACK_SECONDS = 0.2


class FrameWriter:
    """Encodes a session's streamed video to an H.264 MP4, keeping its first `limit` frames once one is set.

    `push` is called on the SDK's media thread, so it only queues; encoding runs on a thread of
    its own. Every frame is scaled to the first one's size, since WebRTC may change resolution.
    Frames are kept in arrival order: cloud frames carry no frame id or timestamp to place them by.
    Audio pushed with `push_audio` is muxed in on `close`, from the first video frame to the video's end.
    With `fill_gaps`, sound the stream never sent is filled with silence, keeping the audio in step with the frames.
    """

    def __init__(self, path: str, fps: float):
        self.path, self.fps = path, fps
        self.limit: int | None = None
        self.received = 0
        self.previous: np.ndarray | None = None
        self.last_frame_at: float | None = None
        self.error: BaseException | None = None
        self.frames: queue.Queue[np.ndarray | None] = queue.Queue()
        self.audio: list[np.ndarray] = []
        self.audio_samples = 0
        self.sample_rate = 0
        self.fill_gaps = False
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

    def push_audio(self, pcm: np.ndarray, sample_rate: int) -> None:
        """Keep a block of `(samples, channels)` int16 PCM, as the SDK hands it over."""
        # The stream carries silence until the first chunk, whose sound arrives with its first frame.
        if self.received == 0:
            return
        # Observed on avatar calls: blocks go missing around pauses, while frames keep a steady rate.
        behind = round(self.received / self.fps * sample_rate) - self.audio_samples - len(pcm)
        if self.fill_gaps and behind > AUDIO_SLACK_SECONDS * sample_rate:
            self.audio.append(np.zeros((behind, pcm.shape[1]), np.int16))
            self.audio_samples += behind
        self.audio.append(pcm.copy())
        self.audio_samples += len(pcm)
        self.sample_rate = sample_rate

    def close(self) -> None:
        """Finish the file. Raises what the encoder raised, or if no frame ever arrived."""
        self.frames.put(None)
        self.thread.join()
        if self.error is not None:
            raise self.error
        if self.received == 0:
            raise RuntimeError("The session streamed no video frames.")
        pcm = np.concatenate(self.audio)[:round(min(self.received, self.limit or self.received) / self.fps * self.sample_rate)] if self.audio else None
        # A model with its sound turned off still streams silence, which is left out.
        if pcm is not None and pcm.any():
            add_audio(self.path, pcm, self.sample_rate)

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


def add_audio(path: str, pcm: np.ndarray, sample_rate: int) -> None:
    """Rewrite the MP4 at `path` with `(samples, channels)` int16 PCM as an AAC track beside its video."""
    tmp = path + ".audio.mp4"
    with av.open(path) as src, av.open(tmp, "w") as dst:
        video = dst.add_stream_from_template(src.streams.video[0])
        audio = dst.add_stream("aac", rate=sample_rate, layout="mono" if pcm.shape[1] == 1 else "stereo")
        for packet in src.demux(src.streams.video[0]):
            if packet.dts is not None:
                packet.stream = video
                dst.mux(packet)
        frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(pcm).reshape(1, -1), format="s16", layout=audio.layout.name)
        frame.sample_rate = sample_rate
        dst.mux(audio.encode(frame))
        dst.mux(audio.encode())
    os.replace(tmp, path)
