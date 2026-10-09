import av
import numpy as np

from reactor_render.encode import FrameWriter, add_audio
from reactor_render.recording import cut, read_audio

RATE = 48000


def recording_file(path, seconds, fps=30, level=lambda i: min(2 * i, 255), loudness=lambda second: 1000 * (second + 1)):
    """A recording like the runtime's: frames at `fps` whose gray is `level(index)`, over a tone `loudness(second)` loud."""
    writer = FrameWriter(str(path), fps)
    for i in range(round(seconds * fps)):
        writer.push(np.full((16, 16, 3), level(i), dtype=np.uint8))
    writer.close()
    t = np.arange(round(seconds * RATE)) / RATE
    pcm = (np.sin(2 * np.pi * 440 * t) * [loudness(int(s)) for s in t]).astype(np.int16).reshape(-1, 1)
    add_audio(str(path), pcm, RATE)


def frames_in(path):
    with av.open(str(path)) as c:
        return [f.to_ndarray(format="rgb24") for f in c.decode(video=0)]


def rms(pcm):
    return float(np.sqrt(np.mean(np.square(pcm, dtype=np.float64))))


def test_a_cut_keeps_its_spans_at_the_model_rate_with_their_sound(tmp_path):
    source, out = tmp_path / "recording.mp4", tmp_path / "take.mp4"
    recording_file(source, 4)
    cut(str(source), [(1.0, 2.0), (3.0, 3.5)], 24.0, str(out))
    frames = frames_in(out)
    assert len(frames) == 36
    # Each span opens on the recorded frame at its start: frame 30 (gray 60), then frame 90 (gray 180).
    assert abs(frames[0].mean() - 60) < 4 and abs(frames[24].mean() - 180) < 4
    pcm, rate = read_audio(str(out))
    assert rate == RATE and abs(len(pcm) - 1.5 * RATE) < 2048
    # The first span's sound is the recording's second second (2000 loud), the next its fourth (4000 loud).
    assert abs(rms(pcm[2400:21600]) - 2000 / 2 ** 0.5) < 150
    assert abs(rms(pcm[48000 + 2400:48000 + 21600]) - 4000 / 2 ** 0.5) < 300


def test_a_cut_keeps_at_most_its_limit_of_frames(tmp_path):
    source, out = tmp_path / "recording.mp4", tmp_path / "take.mp4"
    recording_file(source, 2)
    cut(str(source), [(0.0, 2.0)], 24.0, str(out), limit=10)
    assert len(frames_in(out)) == 10
