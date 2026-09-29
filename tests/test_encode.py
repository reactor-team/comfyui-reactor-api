import av
import numpy as np
import pytest

from reactor_render.encode import FrameWriter


def read(path):
    with av.open(str(path)) as c:
        return [f.to_ndarray(format="rgb24") for f in c.decode(video=0)]


def test_frames_past_the_limit_are_dropped_and_sizes_follow_the_first_frame(tmp_path):
    out = tmp_path / "out.mp4"
    writer = FrameWriter(str(out), 24.0)
    writer.limit = 4
    for size in [(64, 96), (64, 96), (32, 48), (64, 96), (64, 96), (64, 96)]:
        writer.push(np.full((*size, 3), 200, dtype=np.uint8))
    writer.close()
    frames = read(out)
    assert len(frames) == 4
    assert {f.shape for f in frames} == {(64, 96, 3)}


def test_a_non_contiguous_view_encodes(tmp_path):
    # The SDK hands over a BGRA buffer sliced to RGB, which is not contiguous.
    bgra = np.zeros((32, 32, 4), dtype=np.uint8)
    bgra[..., 2] = 255
    out = tmp_path / "out.mp4"
    writer = FrameWriter(str(out), 24.0)
    writer.push(bgra[..., 2::-1])
    writer.close()
    (frame,) = read(out)
    assert frame[16, 16, 0] > 200 and frame[16, 16, 2] < 50


def test_no_frames_is_an_error(tmp_path):
    writer = FrameWriter(str(tmp_path / "out.mp4"), 24.0)
    with pytest.raises(RuntimeError, match="no video frames"):
        writer.close()
