import threading

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


def tone(seconds, rate=48000):
    t = np.arange(round(seconds * rate)) / rate
    return (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16).reshape(-1, 1)


@pytest.mark.parametrize("late_limit", [None, 18])
def test_audio_is_muxed_in_and_cut_to_the_videos_length(tmp_path, late_limit):
    out = tmp_path / "out.mp4"
    writer = FrameWriter(str(out), 18.0)
    for _ in range(36):
        writer.push(np.full((32, 32, 3), 200, dtype=np.uint8))
    for block in np.split(tone(3.0), 300):
        writer.push_audio(block, 48000)
    writer.limit = late_limit
    writer.close()
    with av.open(str(out)) as c:
        (audio,) = c.streams.audio
        samples = sum(f.samples for f in c.decode(audio=0))
    assert audio.sample_rate == 48000
    expected_frames = late_limit or 36
    assert samples == pytest.approx(expected_frames / 18 * 48000, abs=2048)
    assert len(read(out)) == expected_frames


def test_audio_before_the_first_frame_is_left_out(tmp_path):
    out = tmp_path / "out.mp4"
    writer = FrameWriter(str(out), 18.0)
    writer.push_audio(np.zeros((96000, 1), dtype=np.int16), 48000)
    for _ in range(18):
        writer.push(np.full((32, 32, 3), 200, dtype=np.uint8))
    writer.push_audio(tone(1.0), 48000)
    writer.close()
    with av.open(str(out)) as c:
        samples = sum(f.samples for f in c.decode(audio=0))
    assert samples == pytest.approx(48000, abs=2048)


def test_silent_audio_is_left_out(tmp_path):
    out = tmp_path / "out.mp4"
    writer = FrameWriter(str(out), 18.0)
    writer.push(np.full((32, 32, 3), 200, dtype=np.uint8))
    writer.push_audio(np.zeros((4800, 1), dtype=np.int16), 48000)
    writer.close()
    with av.open(str(out)) as c:
        assert not c.streams.audio


def test_fill_gaps_puts_sound_the_stream_never_sent_back_as_silence(tmp_path):
    out = tmp_path / "out.mp4"
    writer = FrameWriter(str(out), 10.0)
    writer.fill_gaps = True
    # Two seconds of frames, with sound for only the first and last half second.
    for i in range(20):
        writer.push(np.full((32, 32, 3), 200, dtype=np.uint8))
        if i < 5 or i >= 15:
            writer.push_audio(tone(0.1), 48000)
    writer.close()
    with av.open(str(out)) as c:
        pcm = np.concatenate([f.to_ndarray().reshape(-1) for f in c.decode(audio=0)])
    assert len(pcm) == pytest.approx(2 * 48000, abs=2048)
    assert np.abs(pcm[round(1.0 * 48000):round(1.3 * 48000)]).max() < 0.01
    assert np.abs(pcm[round(1.7 * 48000):round(1.9 * 48000)]).max() > 0.05


def test_a_limit_set_after_frames_arrive_trims_the_saved_video(tmp_path):
    out = tmp_path / "out.mp4"
    writer = FrameWriter(str(out), 24.0)
    for level in range(10):
        writer.push(np.full((32, 32, 3), level * 20, dtype=np.uint8))
    writer.limit = 4
    writer.close()
    frames = read(out)
    assert len(frames) == 4
    assert [round(float(frame.mean()) / 20) for frame in frames] == list(range(4))


def test_container_finalization_failure_does_not_hang_close(tmp_path, monkeypatch):
    class BrokenContainer:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            raise OSError("finalization failed")

    monkeypatch.setattr(av, "open", lambda *args: BrokenContainer())
    writer = FrameWriter(str(tmp_path / "out.mp4"), 24.0)
    errors = []

    def close():
        try:
            writer.close()
        except OSError as e:
            errors.append(str(e))

    thread = threading.Thread(target=close, daemon=True)
    thread.start()
    thread.join(timeout=2)
    try:
        assert not thread.is_alive()
        assert errors == ["finalization failed"]
    finally:
        if thread.is_alive():
            writer.frames.put(None)
            thread.join(timeout=2)
