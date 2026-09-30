import asyncio
import io
import json
from types import SimpleNamespace

import av
import numpy as np
import pytest
from PIL import Image

from reactor_render import live, timeline
from reactor_render.timeline import MODELS, POSES

X2_INPUT = MODELS["X2"].frame_size(1920, 1080)

WORLD_2 = MODELS["LingBot World 2"]
LINGBOT = MODELS["LingBot"]


def test_model_tracks_list_the_source_track_only_for_models_that_take_one():
    assert live.model_tracks(MODELS["Sana Streaming"]) == [
        {"name": "camera", "kind": "video", "direction": "sendonly"},
        {"name": "main_video", "kind": "video", "direction": "recvonly"}]
    assert live.model_tracks(WORLD_2) == [{"name": "main_video", "kind": "video", "direction": "recvonly"}]
    assert live.model_tracks(MODELS["Vidu S2-Avatar"]) == [
        {"name": "mic", "kind": "audio", "direction": "sendonly"},
        {"name": "webcam", "kind": "video", "direction": "sendonly"},
        {"name": "main_video", "kind": "video", "direction": "recvonly"},
        {"name": "main_audio", "kind": "audio", "direction": "recvonly"}]


def lane(spec, field):
    return next(lane for lane in live.drive_lanes(spec) if lane["field"] == field)


def test_each_world_2_key_pair_drives_its_own_lane():
    assert [lane["field"] for lane in live.drive_lanes(WORLD_2)] == [
        "move_longitudinal", "move_lateral", "camera_pose", "look_horizontal", "look_vertical"]
    assert lane(WORLD_2, "move_longitudinal") == {"field": "move_longitudinal", "command": "set_move_longitudinal",
                                                 "idle": "idle", "axes": [["KeyS", "KeyW", "back", "forward"]]}
    assert lane(WORLD_2, "look_vertical")["axes"] == [["ArrowUp", "ArrowDown", "up", "down"]]


def test_q_and_e_orbit_on_the_camera_pose_lane():
    assert lane(WORLD_2, "camera_pose") == {"field": "camera_pose", "command": "set_camera_pose", "idle": [],
                                           "axes": [["KeyQ", "KeyE", POSES["orbit_left"], POSES["orbit_right"]]]}


def test_lingbot_v1_has_one_movement_lane_where_w_and_s_come_first():
    assert [lane["field"] for lane in live.drive_lanes(LINGBOT)] == ["movement", "look_horizontal", "look_vertical"]
    assert lane(LINGBOT, "movement")["axes"] == [["KeyS", "KeyW", "back", "forward"],
                                                 ["KeyA", "KeyD", "strafe_left", "strafe_right"]]


def test_only_models_with_camera_lanes_get_drive_lanes():
    assert live.drive_lanes(MODELS["LongLive-2.0"]) == live.drive_lanes(MODELS["Helios"]) == []


class RecvTrack:
    kind, direction = "video", "recvonly"

    def on_frame(self, fn):
        self.push = fn


class SendTrack:
    """A track the server publishes, recording the frames pushed to it."""

    def __init__(self, name):
        self.name = name
        self.pushed = []

    def push_frame(self, frame):
        self.pushed.append(frame)


class FakeLiveReactor:
    """Stands in for reactor_sdk.Reactor on a live take.

    The stream opens with a black placeholder frame, then white frames at `interval` until
    disconnect — a live session has no planned end. Commands land in `sent`, uploads in
    `uploads`, and frames the server publishes in `published.pushed`.
    """

    def __init__(self, interval=0.005):
        self.interval = interval
        self.session_id = "session-1"
        self.sent = []
        self.uploads = []
        self.published = None
        self.disconnected = False
        self.track = RecvTrack()
        self.events = {}

    def __call__(self, slug, **connect):
        self.slug, self.options = slug, connect
        return self

    def on_track(self, func):
        self.track_handler = func
        return func

    def on_message(self, func):
        self.message_handler = func
        return func

    def on(self, event, func):
        self.events[event] = func

    def on_error(self, func):
        self.events["error"] = func
        return func

    def on_status(self, status):
        def register(func):
            self.dropped = func
            return func
        return register

    async def publish_track(self, name):
        self.published = SendTrack(name)
        return self.published

    async def disconnect(self):
        self.disconnected = True
        self.streamer.cancel()
        self.dropped("disconnected")

    def close(self):
        pass

    async def connect(self):
        self.track_handler(self.track)
        self.track.push(np.full((16, 16, 3), 0, dtype=np.uint8))
        self.streamer = asyncio.get_running_loop().create_task(self._emit())

    async def upload_file(self, data, name, mime_type):
        self.uploads.append((data, name, mime_type))
        return f"ref:{data.decode()}"

    async def send_command(self, command, data):
        self.sent.append((command, data))

    async def get_stats(self):
        video = SimpleNamespace(kind="video", frames_per_second=24.0)
        return SimpleNamespace(rtt_ms=40.0, packet_loss_ratio=0.0, incoming_bitrate_bps=3e6,
                               outgoing_bitrate_bps=None, inbound=[video])

    async def _emit(self):
        while True:
            self.track.push(np.full((16, 16, 3), 255, dtype=np.uint8))
            await asyncio.sleep(self.interval)


class AudioRecvTrack:
    kind, direction = "audio", "recvonly"

    def on_frame(self, fn):
        self.push = fn


class FakeLiveCall(FakeLiveReactor):
    """A live Vidu S2-Avatar call: `create_avatar` and `start_call` reach their phases, and once
    the call is live the character streams at a size of its own, with sound."""

    live = False

    async def connect(self):
        self.audio = AudioRecvTrack()
        self.track_handler(self.audio)
        await super().connect()

    async def send_command(self, command, data):
        await super().send_command(command, data)
        phase = {"create_avatar": "avatar_ready", "start_call": "live"}.get(command)
        if phase:
            self.live = phase == "live"
            self.message_handler({"type": "session_state", "data": {"phase": phase}})

    async def _emit(self):
        # As on cloud sessions, the placeholder streams on for a little while after the call is live.
        after_live = 0
        while True:
            after_live += self.live
            shown = after_live > 10
            self.track.push(np.full((24, 16, 3) if shown else (16, 16, 3), 255 if shown else 0, dtype=np.uint8))
            if self.live:
                self.audio.push(np.full((240, 1), 8000, dtype=np.int16), 48000)
            await asyncio.sleep(self.interval)


def frames_in(path):
    with av.open(str(path)) as container:
        return [f.to_ndarray(format="rgb24") for f in container.decode(video=0)]


async def until(predicate, seconds=5.0):
    for _ in range(int(seconds * 50)):
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def texts(sent):
    return [json.loads(m) for m in sent if isinstance(m, str)]


def make_run(fake, monkeypatch, mode, model, path, prompt="a", seed=42, image=None, size=None, connect=None):
    monkeypatch.setattr(live, "Reactor", fake)
    monkeypatch.setattr(live, "CONNECT_SETTLE_SECONDS", 0)
    fake.tokens = []
    monkeypatch.setattr(live, "session_token", lambda *args: fake.tokens.append(args) or "jwt-test")
    setup = (live.style_setup(MODELS[model], prompt, seed, image, {}) if mode == "style"
             else live.drive_setup(model, timeline.Beat(prompt, 1, image=image), seed, {}))
    return live.LiveRun(mode, MODELS[model], str(path), prompt, setup, size, connect or {"api_key": "rk_test"})


def join(sent):
    return next((m for m in texts(sent) if m["type"] == "join"), None)


async def test_a_camera_run_has_the_browser_publish_and_records_the_take(tmp_path, monkeypatch):
    fake = FakeLiveReactor()
    path = tmp_path / "take.mp4"
    run = make_run(fake, monkeypatch, "style", "X2", path, prompt="make it noir", size=X2_INPUT)
    task = asyncio.create_task(run.run(lambda: None))
    sent = []
    assert run.connected(sent.append)
    assert not run.connected(lambda m: None)
    assert await until(lambda: join(sent) is not None)
    assert join(sent) == {"type": "join", "model": "xmax/x2", "session_id": "session-1", "jwt": "jwt-test",
                          "local": False, "tracks": live.model_tracks(MODELS["X2"]), "publish": "source"}
    assert fake.tokens == [("rk_test", "xmax/x2", "session-1")]
    # Setup waits for the browser's camera, and the server never publishes one of its own.
    await asyncio.sleep(0.1)
    assert fake.sent == []
    run.receive('{"type":"published"}')
    assert await until(lambda: run._writer.received > 0)
    run.receive('{"type":"done"}')
    assert await asyncio.wait_for(task, 10) == str(path)
    assert fake.published is None
    assert (fake.slug, fake.options) == ("xmax/x2", {"api_key": "rk_test", "max_session_duration_seconds": 1800})
    assert fake.sent == [("set_keep_backlog", {"keep_backlog": False}), ("set_prompt", {"prompt": "make it noir"})]
    messages = texts(sent)
    assert messages[0] == {"type": "config", "mode": "style", "prompt": "make it noir", "lanes": [],
                           "prompt_command": "set_prompt", "prompt_field": "prompt", "switch": None, "preview": {"width": 1480, "height": 832},
                           "input": {"width": 1480, "height": 832, "fps": 24}}
    assert [m["text"] for m in messages if m["type"] == "status"] == ["Connecting to Reactor…", ""]
    assert [m for m in messages if m["type"] == "ended"] == [
        {"type": "ended", "error": None}]
    assert frames_in(path) and all(f.mean() > 200 for f in frames_in(path))
    assert fake.disconnected


async def test_a_browser_that_never_publishes_its_camera_fails_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "PUBLISH_TIMEOUT_SECONDS", 0.3)
    fake = FakeLiveReactor()
    run = make_run(fake, monkeypatch, "style", "X2", tmp_path / "take.mp4", size=X2_INPUT)
    task = asyncio.create_task(run.run(lambda: None))
    sent = []
    run.connected(sent.append)
    with pytest.raises(RuntimeError, match="did not start its camera"):
        await asyncio.wait_for(task, 10)
    assert fake.disconnected


async def test_a_cancel_while_waiting_for_the_camera_ends_the_run_at_once(tmp_path, monkeypatch):
    fake = FakeLiveReactor()
    run = make_run(fake, monkeypatch, "style", "X2", tmp_path / "take.mp4", size=X2_INPUT)
    task = asyncio.create_task(run.run(lambda: None))
    sent = []
    run.connected(sent.append)
    assert await until(lambda: join(sent) is not None)
    run.receive('{"type":"cancel","error":"This browser lost its connection to the Reactor session."}')
    with pytest.raises(RuntimeError, match="browser lost its connection"):
        await asyncio.wait_for(task, 2)
    assert [m for m in texts(sent) if m["type"] == "ended"] == [
        {"type": "ended", "error": "This browser lost its connection to the Reactor session."}]


async def test_a_dropped_session_ends_the_run_and_tells_the_browser(tmp_path, monkeypatch):
    fake = FakeLiveReactor()
    run = make_run(fake, monkeypatch, "drive", "LongLive-2.0", tmp_path / "take.mp4")
    task = asyncio.create_task(run.run(lambda: None))
    sent = []
    run.connected(sent.append)
    assert await until(lambda: run._writer is not None and run._writer.received > 0)
    fake.dropped("disconnected")
    with pytest.raises(RuntimeError, match="ended unexpectedly"):
        await asyncio.wait_for(task, 10)
    assert [m for m in texts(sent) if m["type"] == "ended"] == [
        {"type": "ended", "error": "The Reactor session ended unexpectedly."}]


async def test_reactor_ending_the_session_ends_the_run_with_its_reason(tmp_path, monkeypatch):
    reason = "Session ended: your credits ran out. Add more to continue."
    fake = FakeLiveReactor()
    run = make_run(fake, monkeypatch, "drive", "LongLive-2.0", tmp_path / "take.mp4")
    task = asyncio.create_task(run.run(lambda: None))
    sent = []
    run.connected(sent.append)
    assert await until(lambda: run._writer is not None and run._writer.received > 0)
    # Reactor sends its reason just before the transport closes.
    fake.events["runtime_message"]({"type": "sessionEnded", "data": {"reason": reason}})
    fake.dropped("disconnected")
    with pytest.raises(RuntimeError, match=reason):
        await asyncio.wait_for(task, 10)
    assert [m for m in texts(sent) if m["type"] == "ended"] == [{"type": "ended", "error": reason}]


async def test_a_local_runtime_is_joined_without_a_token(tmp_path, monkeypatch):
    fake = FakeLiveReactor()
    run = make_run(fake, monkeypatch, "drive", "LongLive-2.0", tmp_path / "take.mp4", connect={"local": True})
    task = asyncio.create_task(run.run(lambda: None))
    sent = []
    run.connected(sent.append)
    assert await until(lambda: join(sent) is not None)
    assert join(sent)["jwt"] is None and join(sent)["local"] is True
    assert fake.tokens == []
    run.receive('{"type":"cancel"}')
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(task, 10)


async def test_a_cancel_raises_and_reports_the_error(tmp_path, monkeypatch):
    fake = FakeLiveReactor()
    run = make_run(fake, monkeypatch, "style", "X2", tmp_path / "take.mp4", size=X2_INPUT)
    task = asyncio.create_task(run.run(lambda: None))
    sent = []
    run.connected(sent.append)
    run.receive('{"type":"published"}')
    assert await until(lambda: any(m.get("text") == "" for m in texts(sent)))
    run.receive('{"type":"cancel"}')
    with pytest.raises(RuntimeError, match="Cancelled."):
        await asyncio.wait_for(task, 10)
    assert [m for m in texts(sent) if m["type"] == "ended"] == [{"type": "ended", "error": "Cancelled."}]
    assert fake.disconnected


async def test_a_cancel_while_waiting_for_a_server_ends_the_run_at_once(tmp_path, monkeypatch):
    fake = FakeLiveReactor()
    fake.connect = lambda: asyncio.Event().wait()
    run = make_run(fake, monkeypatch, "style", "X2", tmp_path / "take.mp4", size=X2_INPUT)
    task = asyncio.create_task(run.run(lambda: None))
    run.connected(lambda m: None)
    await asyncio.sleep(0.3)
    run.receive('{"type":"cancel"}')
    with pytest.raises(RuntimeError, match="Cancelled."):
        await asyncio.wait_for(task, 2)


async def test_a_socket_closing_without_done_cancels_the_run(tmp_path, monkeypatch):
    fake = FakeLiveReactor()
    run = make_run(fake, monkeypatch, "style", "X2", tmp_path / "take.mp4", size=X2_INPUT)
    task = asyncio.create_task(run.run(lambda: None))
    run.connected(lambda m: None)
    run.receive('{"type":"published"}')
    assert await until(lambda: ("set_prompt", {"prompt": "a"}) in fake.sent)
    run.disconnected()
    with pytest.raises(RuntimeError, match="Cancelled."):
        await asyncio.wait_for(task, 10)


async def test_a_socket_closing_after_done_still_saves_the_take(tmp_path, monkeypatch):
    fake = FakeLiveReactor()
    run = make_run(fake, monkeypatch, "style", "X2", tmp_path / "take.mp4", size=X2_INPUT)
    task = asyncio.create_task(run.run(lambda: None))
    run.connected(lambda m: None)
    run.receive('{"type":"published"}')
    assert await until(lambda: run._writer is not None and run._writer.received > 0)
    run.receive('{"type":"done"}')
    run.disconnected()
    assert await asyncio.wait_for(task, 10) == str(tmp_path / "take.mp4")
    assert frames_in(tmp_path / "take.mp4")


async def test_a_run_without_a_browser_raises_the_open_a_tab_error(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "BROWSER_TIMEOUT_SECONDS", 0.3)
    run = live.LiveRun("drive", MODELS["LingBot"], str(tmp_path / "take.mp4"), "a", [], None, {"api_key": "k"})
    with pytest.raises(RuntimeError, match="browser tab"):
        await run.run(lambda: None)


async def test_a_drive_run_uploads_the_image_and_sends_lane_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "STATS_INTERVAL_SECONDS", 0.01)
    fake = FakeLiveReactor()
    run = make_run(fake, monkeypatch, "drive", "LingBot World 2", tmp_path / "take.mp4",
                   prompt="explore", seed=7, image=b"pngdata")
    task = asyncio.create_task(run.run(lambda: None))
    sent = []
    run.connected(sent.append)
    assert await until(lambda: ("start", {}) in fake.sent)
    assert fake.slug == "reactor/lingbot-world-2"
    assert fake.sent == [("set_seed", {"seed": 7}), ("set_image", {"image": "ref:pngdata"}),
                         ("set_prompt", {"prompt": "explore"}), ("start", {})]
    assert fake.uploads == [(b"pngdata", "image.png", "image/png")]
    assert await until(lambda: {"type": "started"} in texts(sent))
    assert await until(lambda: {"type": "stats", "rtt_ms": 40.0, "loss": 0.0, "in_bps": 3e6, "out_bps": None,
                                "fps": 24.0} in texts(sent))
    assert await until(lambda: run._writer.received > 0)
    run.receive('{"type":"done"}')
    assert await asyncio.wait_for(task, 10) == str(tmp_path / "take.mp4")
    messages = texts(sent)
    assert messages[0] == {"type": "config", "mode": "drive", "prompt": "explore", "lanes": live.drive_lanes(WORLD_2),
                           "prompt_command": "set_prompt", "prompt_field": "prompt", "switch": None, "preview": {"width": 1664, "height": 960}, "input": None}
    assert join(sent)["publish"] is None
    assert frames_in(tmp_path / "take.mp4")


async def test_a_promptable_model_leaves_mid_run_prompts_to_the_browser(tmp_path, monkeypatch):
    fake = FakeLiveReactor()
    run = make_run(fake, monkeypatch, "drive", "LongLive-2.0", tmp_path / "take.mp4", prompt="a harbour", seed=3)
    task = asyncio.create_task(run.run(lambda: None))
    sent = []
    run.connected(sent.append)
    assert await until(lambda: {"type": "started"} in texts(sent))
    run.receive('{"type":"prompt","prompt":"a storm rolls in"}')
    run.receive('{"type":"done"}')
    await asyncio.wait_for(task, 10)
    assert fake.sent == [("set_seed", {"seed": 3}), ("set_shot", {"prompt": "a harbour"}), ("start", {})]
    assert texts(sent)[0]["lanes"] == [] and texts(sent)[0]["prompt_command"] == "set_shot"


def test_an_edit_starts_from_its_image_and_offers_its_switch_controls():
    spec = MODELS["Vidu S2-Editing"]
    assert live.style_setup(spec, "", 0, b"clay", {"editing_type": "virtual_tryon"}) == [
        ("start_edit", {"reference_image": b"clay", "editing_type": "virtual_tryon"})]
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "orange").save(buf, format="PNG")
    setup = live.style_setup(spec, "", 0, buf.getvalue(), {"editing_type": "virtual_tryon"})
    assert live.switch_controls(spec, {"editing_type": "virtual_tryon"}, setup) == {
        "image": "reference_image", "reference": timeline.data_url(buf.getvalue()),
        "settings": [{"field": "editing_type", "options": list(timeline.EDIT_TYPES), "value": "virtual_tryon"}]}
    assert live.switch_controls(MODELS["X2"], {}, []) is None


async def test_a_source_clip_loops_in_place_of_the_camera(tmp_path, monkeypatch):
    fake = FakeLiveReactor()
    clip = [np.full((64, 96, 3), level, dtype=np.uint8) for level in (10, 20, 30)]
    monkeypatch.setattr(live, "Reactor", fake)
    monkeypatch.setattr(live, "CONNECT_SETTLE_SECONDS", 0)
    monkeypatch.setattr(live, "session_token", lambda *args: "jwt-test")
    run = live.LiveRun("style", MODELS["X2"], str(tmp_path / "take.mp4"), "a",
                       live.style_setup(MODELS["X2"], "a", 42, None, {}), None, {"api_key": "rk_test"}, clip)
    task = asyncio.create_task(run.run(lambda: None))
    sent = []
    run.connected(sent.append)
    assert await until(lambda: fake.published is not None and len(fake.published.pushed) > len(clip))
    run.receive('{"type":"done"}')
    await asyncio.wait_for(task, 10)
    levels = [int(f[0, 0, 0]) for f in fake.published.pushed]
    assert levels[:4] == [10, 20, 30, 10]
    assert texts(sent)[0]["input"] is None and texts(sent)[0]["preview"] == {"width": 96, "height": 64}
    assert join(sent)["publish"] is None




async def test_a_call_waits_for_the_browser_mic_and_records_the_character_once_it_appears(tmp_path, monkeypatch):
    fake = FakeLiveCall()
    monkeypatch.setattr(live, "Reactor", fake)
    monkeypatch.setattr(live, "CONNECT_SETTLE_SECONDS", 0)
    monkeypatch.setattr(live, "session_token", lambda *args: "jwt-test")
    setup = timeline.call_setup("Vidu S2-Avatar", b"face", {"persona": "A fisherman.", "voice": "Jennifer", "greeting": ""})
    run = live.LiveRun("call", MODELS["Vidu S2-Avatar"], str(tmp_path / "take.mp4"), "", setup, None, {"api_key": "rk_test"})
    task = asyncio.create_task(run.run(lambda: None))
    sent = []
    run.connected(sent.append)
    assert await until(lambda: join(sent) is not None)
    assert join(sent)["publish"] == "mic"
    await asyncio.sleep(0.1)
    assert fake.sent == []
    run.receive('{"type":"published"}')
    assert await until(lambda: {"type": "started"} in texts(sent))
    assert fake.sent == [("create_avatar", {"image": "ref:face"}),
                         ("start_call", {"call_mode": "audio", "transcripts": False, "persona": "A fisherman.", "voice": "Jennifer"})]
    assert texts(sent)[0]["prompt_command"] == "say" and texts(sent)[0]["prompt_field"] == "text"
    assert await until(lambda: run._writer.received > 5)
    run.receive('{"type":"done"}')
    assert await asyncio.wait_for(task, 10) == str(tmp_path / "take.mp4")
    # The connect placeholder is left out, and the character's sound is kept.
    assert frames_in(tmp_path / "take.mp4") and all(f.shape[0] == 24 for f in frames_in(tmp_path / "take.mp4"))
    with av.open(str(tmp_path / "take.mp4")) as container:
        assert container.streams.audio


async def test_a_call_the_model_ends_saves_the_take(tmp_path, monkeypatch):
    fake = FakeLiveCall()
    monkeypatch.setattr(live, "Reactor", fake)
    monkeypatch.setattr(live, "CONNECT_SETTLE_SECONDS", 0)
    monkeypatch.setattr(live, "session_token", lambda *args: "jwt-test")
    setup = timeline.call_setup("Vidu S2-Avatar", b"face", {"persona": "A fisherman."})
    run = live.LiveRun("call", MODELS["Vidu S2-Avatar"], str(tmp_path / "take.mp4"), "", setup, None, {"api_key": "rk_test"})
    task = asyncio.create_task(run.run(lambda: None))
    sent = []
    run.connected(sent.append)
    run.receive('{"type":"published"}')
    assert await until(lambda: {"type": "started"} in texts(sent))
    fake.message_handler({"type": "session_state", "data": {"phase": "ended", "end_reason": "idle_timeout"}})
    assert await asyncio.wait_for(task, 10) == str(tmp_path / "take.mp4")
    assert {"type": "ended", "error": None} in texts(sent)


async def test_a_call_that_fails_on_the_server_fails_the_run(tmp_path, monkeypatch):
    fake = FakeLiveCall()
    monkeypatch.setattr(live, "Reactor", fake)
    monkeypatch.setattr(live, "CONNECT_SETTLE_SECONDS", 0)
    monkeypatch.setattr(live, "session_token", lambda *args: "jwt-test")
    setup = timeline.call_setup("Vidu S2-Avatar", b"face", {"persona": "A fisherman."})
    run = live.LiveRun("call", MODELS["Vidu S2-Avatar"], str(tmp_path / "take.mp4"), "", setup, None, {"api_key": "rk_test"})
    task = asyncio.create_task(run.run(lambda: None))
    sent = []
    run.connected(sent.append)
    run.receive('{"type":"published"}')
    assert await until(lambda: {"type": "started"} in texts(sent))
    fake.message_handler({"type": "session_state", "data": {"phase": "failed", "last_error": {"reason": "upstream error"}}})
    with pytest.raises(RuntimeError, match="upstream error"):
        await asyncio.wait_for(task, 10)
    assert {"type": "ended", "error": "The session failed: upstream error"} in texts(sent)
