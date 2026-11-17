import logging
import re
import types

import numpy as np
import pytest

from reactor_render import diagnostics
from reactor_render.diagnostics import Trace, brief, rounded
from reactor_render.encode import FrameWriter


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def lines(caplog, monkeypatch):
    monkeypatch.setattr(diagnostics, "DEV_MODE", True)
    caplog.set_level(logging.INFO, logger="reactor")
    return lambda: [r.getMessage() for r in caplog.records]


def clip(kind, clip_id="abcdef1234", frames=124.0):
    return {"type": kind, "data": {"clip": {"clip_id": clip_id, "frames": frames, "prompt": "x" * 500}}}


def test_a_clip_that_plays_to_its_end_shows_its_frames_against_its_length(lines):
    clock = Clock()
    trace = Trace("live", clock)
    # A model streams before its first clip, as Pika does with black frames; those aren't the clip's.
    for _ in range(10):
        trace.frame((480, 832, 3))
    clock.now = 1.0
    trace.message(clip("clip_started"))
    for _ in range(124):
        clock.now += 1 / 24
        trace.frame((480, 832, 3))
    trace.message(clip("clip_finished"))
    trace.summary()
    assert "Reactor live +6.17s clip abcdef12 finished after 5.17 s, with 124 frames of the 124 it holds" in lines()
    assert any("summary: 134 frames" in line and "1 clips played to the end" in line for line in lines())


def test_a_pause_in_the_video_is_logged_with_its_length(lines):
    clock = Clock()
    trace = Trace("render", clock)
    trace.frame((704, 1280, 3))
    clock.now = 2.0
    trace.frame((704, 1280, 3))
    assert lines()[:2] == ["Reactor render +0.00s first video frame", "Reactor render +0.00s video is 1280x704"]
    assert "Reactor render +2.00s video resumed after 2.00 s with none, which stopped mid-stream" in lines()


def test_video_that_stops_is_logged_at_once_with_what_was_playing(lines):
    clock = Clock()
    trace = Trace("live", clock)
    trace.message(clip("clip_started"))
    trace.frame((480, 832, 3))
    clock.now = 0.2
    trace.check()
    clock.now = 1.0
    trace.check()
    trace.check()
    clock.now = 1.5
    trace.frame((480, 832, 3))
    stops = [line for line in lines() if "stopped" in line]
    # Logged once while it lasts, then again as it resumes, both naming the clip it stopped in.
    assert stops == ["Reactor live +1.00s video stopped during clip abcdef12; the last frame came 1.00 s ago",
                     "Reactor live +1.50s video resumed after 1.50 s with none, which stopped during clip abcdef12"]


def test_video_that_stops_between_clips_says_no_clip_was_playing(lines):
    clock = Clock()
    trace = Trace("live", clock)
    trace.message(clip("clip_started"))
    trace.frame((480, 832, 3))
    trace.message(clip("clip_finished"))
    clock.now = 3.0
    trace.check()
    assert lines()[-1] == "Reactor live +3.00s video stopped with no clip playing; the last frame came 3.00 s ago"


def test_a_message_logs_a_clip_by_its_id_and_length_not_its_fields(lines):
    Trace("live").message(clip("clip_queued"))
    assert lines()[0].endswith("<- clip_queued {'clip': 'abcdef12', 'frames': 124.0}")


def test_a_command_logs_its_data_cut_short_and_its_reply_time(lines):
    clock = Clock()
    trace = Trace("render", clock)
    trace.sent("set_image", {"image": b"png" * 1000, "prompt": "p" * 100, "refs": list(range(6))})
    clock.now = 0.25
    trace.replied("set_image", {"type": "image_accepted"})
    sent, replied = lines()
    assert "<3000 bytes>" in sent and "p" * 57 + "..." in sent and "'<2 more>'" in sent
    assert replied.endswith("<- reply to set_image in 250 ms: {'type': 'image_accepted'}")


def test_the_api_key_is_never_logged(lines):
    Trace("render").begin("reactor/x", {"api_key": "rk_secret"}, "render")
    assert "API key present" in lines()[0] and "rk_secret" not in lines()[0]


def test_the_saved_file_is_read_back_from_disk(lines, tmp_path):
    writer = FrameWriter(str(tmp_path / "take.mp4"), 24.0)
    for _ in range(12):
        writer.push(np.zeros((16, 16, 3), np.uint8))
    writer.close()
    trace = Trace("render")
    trace.saved(str(tmp_path / "take.mp4"))
    trace.saved(str(tmp_path / "gone.mp4"))
    assert "16x16, 12 frames, 0.50 s, without sound" in lines()[0]
    assert lines()[1].endswith("gone.mp4: missing")


def test_nothing_is_logged_or_counted_outside_development_mode(caplog, monkeypatch):
    monkeypatch.setattr(diagnostics, "DEV_MODE", False)
    caplog.set_level(logging.INFO, logger="reactor")
    trace = Trace("live")
    trace.frame((1, 1, 3))
    trace.message(clip("clip_started"))
    trace.summary()
    assert caplog.records == [] and trace.frames == 0


def test_brief_keeps_small_values_as_they_are():
    assert brief({"seed": 3, "on": True, "name": "a"}) == {"seed": 3, "on": True, "name": "a"}
    assert brief("data:image/png;base64,AAAA") == "data:image/png;base64,AAAA"[:57] + "..."


def test_a_measured_number_is_rounded_and_a_missing_one_says_none():
    assert rounded(9746967.9, 1000) == "9747" and rounded(0.000245, 0.01, 2) == "0.02" and rounded(None) == "none"


def test_the_plugin_version_is_read_from_its_own_pyproject():
    # ComfyUI loads the plugin as a folder, where no installed package records its version.
    assert diagnostics.plugin_version() == re.search(r'^version = "(.+)"', open("pyproject.toml").read(), re.M).group(1)


def test_the_recording_a_take_is_cut_from_is_named_with_its_spans(lines):
    clip = types.SimpleNamespace(session_id="5e55104d-aaaa", end_marker=30.4)
    Trace("live").recording(clip, [0.0, 30.17])
    Trace("live").recording(None, [])
    assert lines() == ["Reactor live +0.00s the session's recording (session 5e55104d) runs to 30.40 s; the take keeps 0.00-30.17 s"]
