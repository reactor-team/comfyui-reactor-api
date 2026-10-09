import io
import time

import av
import numpy as np
import pytest

from reactor_render import clip
from reactor_render.clip import ClipStream
from reactor_render.timeline import MODELS


def encode(frames, rate=24):
    """An H.264 MP4 of these RGB frames."""
    buf = io.BytesIO()
    container = av.open(buf, "w", format="mp4")
    stream = container.add_stream("libx264", rate=rate)
    stream.height, stream.width = frames[0].shape[:2]
    stream.pix_fmt = "yuv420p"
    for rgb in frames:
        container.mux(stream.encode(av.VideoFrame.from_ndarray(rgb, format="rgb24")))
    container.mux(stream.encode())
    container.close()
    return buf.getvalue()


def gray(levels, size=(16, 16)):
    return [np.full((*size, 3), level, dtype=np.uint8) for level in levels]


async def read(stream, n):
    return [await stream.next() for _ in range(n)]


async def test_a_clip_streams_its_frames_in_order_as_uint8_at_the_model_size():
    stream = await ClipStream.open(MODELS["X2"], encode(gray([0, 40, 80, 120])))
    frames = await read(stream, 5)
    await stream.close()
    assert [round(int(f.mean()) / 40) for f in frames[:4]] == [0, 1, 2, 3] and frames[4] is None
    assert all(f.shape == (832, 832, 3) and f.dtype == np.uint8 for f in frames[:4])


async def test_a_wide_clip_covers_the_frame_and_is_center_cropped():
    # Sana Streaming takes a fixed 1280x704, so a wider clip loses its sides: the red bars on its edges are cropped away.
    rgb = np.full((90, 320, 3), 128, dtype=np.uint8)
    rgb[:, :40] = rgb[:, -40:] = (255, 0, 0)
    stream = await ClipStream.open(MODELS["Sana Streaming"], encode([rgb]))
    frame = await stream.next()
    await stream.close()
    assert frame.shape == (704, 1280, 3)
    assert np.abs(frame.astype(int) - 128).max() < 16


async def test_a_looping_clip_starts_over_at_its_end():
    stream = await ClipStream.open(MODELS["X2"], encode(gray([0, 80, 160])), loop=True)
    frames = await read(stream, 7)
    await stream.close()
    assert [round(int(f.mean()) / 80) for f in frames] == [0, 1, 2, 0, 1, 2, 0]


async def test_a_clip_is_decoded_no_further_than_one_frame_past_what_was_read(monkeypatch):
    decoded = []
    fit = clip.fit_frame
    monkeypatch.setattr(clip, "fit_frame", lambda spec, frame: decoded.append(1) or fit(spec, frame))
    stream = await ClipStream.open(MODELS["X2"], encode(gray([40] * 40)), loop=True)
    await read(stream, 10)
    await stream.close()
    assert len(decoded) <= 11


class Turned:
    """A decoded frame carrying a display rotation, as a phone's portrait clip does; PyAV's own is read-only."""

    def __init__(self, frame, rotation):
        self.frame, self.rotation = frame, rotation

    def __getattr__(self, name):
        return getattr(self.frame, name)


def test_a_rotated_frame_is_turned_upright_as_comfyuis_video_loader_turns_it():
    # Stored landscape with its left half white; a quarter turn as np.rot90 makes it puts that half at the bottom.
    rgb = np.zeros((90, 160, 3), dtype=np.uint8)
    rgb[:, :80] = 255
    frame = clip.fit_frame(MODELS["Vidu S2-Editing"], Turned(av.VideoFrame.from_ndarray(rgb, format="rgb24"), 90))
    assert frame.shape == (968, 544, 3)
    assert frame[-100:].mean() > 240 and frame[:100].mean() < 15


@pytest.mark.perf
def test_fitting_a_frame_costs_no_more_than_decoding_it_again():
    """Guards the decode path's speed as a ratio to the bare decode, which holds on any machine: a full-size
    resize in PIL costs several times the decode, swscale's scale-and-convert about the same."""
    x = np.arange(1920, dtype=np.uint16)[None, :]
    y = np.arange(1080, dtype=np.uint16)[:, None]
    rng = np.random.default_rng(0)
    frames = []
    for i in range(24):
        rgb = np.stack(np.broadcast_arrays((x + i * 7) % 256, (y + i * 5) % 256, (x + y) // 2 % 256), -1).astype(np.uint8)
        rgb[::8, ::8] = rng.integers(0, 255, rgb[::8, ::8].shape, dtype=np.uint8)
        frames.append(rgb)
    source = encode(frames, rate=30)
    spec = MODELS["Vidu S2-Editing"]

    def timed(fn):
        best = float("inf")
        for _ in range(3):
            with av.open(io.BytesIO(source)) as c:
                t = time.perf_counter()
                for f in c.decode(video=0):
                    fn(f)
                best = min(best, time.perf_counter() - t)
        return best

    decode = timed(lambda f: f.to_ndarray(format="rgb24"))
    fit = timed(lambda f: clip.fit_frame(spec, f))
    assert fit < 2 * decode, f"fit {fit * 1000 / 24:.1f} ms/frame vs decode {decode * 1000 / 24:.1f} ms/frame"


async def test_a_faster_clip_drops_frames_to_play_at_its_own_speed():
    # One second at 48 fps reaches a 24 fps model as one second: every other frame.
    stream = await ClipStream.open(MODELS["X2"], encode(gray(range(0, 240, 5)), rate=48))
    frames = await read(stream, 25)
    await stream.close()
    assert frames[24] is None
    assert [round(int(f.mean()) / 10) for f in frames[:24]] == list(range(24))


async def test_a_slower_clip_repeats_frames_to_play_at_its_own_speed():
    stream = await ClipStream.open(MODELS["X2"], encode(gray([0, 80, 160]), rate=12))
    frames = await read(stream, 7)
    await stream.close()
    assert frames[6] is None
    assert [round(int(f.mean()) / 80) for f in frames[:6]] == [0, 0, 1, 1, 2, 2]


async def test_a_looping_clip_keeps_its_speed_on_every_pass():
    stream = await ClipStream.open(MODELS["X2"], encode(gray([0, 80]), rate=12), loop=True)
    frames = await read(stream, 8)
    await stream.close()
    assert [round(int(f.mean()) / 80) for f in frames] == [0, 0, 1, 1, 0, 0, 1, 1]


async def test_a_dropped_frame_is_never_fitted(monkeypatch):
    fitted = []
    fit = clip.fit_frame
    monkeypatch.setattr(clip, "fit_frame", lambda spec, frame: fitted.append(1) or fit(spec, frame))
    stream = await ClipStream.open(MODELS["X2"], encode(gray([40] * 48), rate=48))
    while await stream.next() is not None:
        pass
    await stream.close()
    assert len(fitted) == 24
