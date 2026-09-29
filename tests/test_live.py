import asyncio
import json
from types import SimpleNamespace

import av
import numpy as np
import pytest

from reactor_render import live
from reactor_render.timeline import MODELS, POSES

X2_INPUT = MODELS["X2"].frame_size(1920, 1080)

WORLD_2 = MODELS["LingBot World 2"]
LINGBOT = MODELS["LingBot"]


def test_each_key_drives_its_own_lane():
    state = {}
    assert live.drive_commands(WORLD_2, {"KeyW"}, state) == [("set_move_longitudinal", {"move_longitudinal": "forward"})]
    assert live.drive_commands(WORLD_2, {"KeyW"}, state) == []
    assert live.drive_commands(WORLD_2, {"KeyS"}, state) == [("set_move_longitudinal", {"move_longitudinal": "back"})]
    assert live.drive_commands(WORLD_2, {"ArrowLeft"}, state) == [
        ("set_move_longitudinal", {"move_longitudinal": "idle"}), ("set_look_horizontal", {"look_horizontal": "left"})]
    assert live.drive_commands(WORLD_2, {"ArrowUp"}, state) == [
        ("set_look_horizontal", {"look_horizontal": "idle"}), ("set_look_vertical", {"look_vertical": "up"})]


def test_a_diagonal_holds_two_movement_lanes():
    state = {}
    assert live.drive_commands(WORLD_2, {"KeyW", "KeyD"}, state) == [
        ("set_move_longitudinal", {"move_longitudinal": "forward"}), ("set_move_lateral", {"move_lateral": "strafe_right"})]


def test_opposite_keys_held_together_idle_the_lane():
    state = {}
    assert live.drive_commands(WORLD_2, {"KeyW", "KeyS"}, state) == []
    live.drive_commands(WORLD_2, {"KeyW"}, state)
    assert live.drive_commands(WORLD_2, {"KeyW", "KeyS"}, state) == [("set_move_longitudinal", {"move_longitudinal": "idle"})]


def test_release_idles_the_lane():
    state = {}
    live.drive_commands(WORLD_2, {"KeyA", "ArrowDown"}, state)
    assert live.drive_commands(WORLD_2, set(), state) == [
        ("set_move_lateral", {"move_lateral": "idle"}), ("set_look_vertical", {"look_vertical": "idle"})]


def test_q_and_e_orbit_on_the_camera_pose_lane():
    state = {}
    assert live.drive_commands(WORLD_2, {"KeyQ"}, state) == [("set_camera_pose", {"camera_pose": POSES["orbit_left"]})]
    assert live.drive_commands(WORLD_2, {"KeyE"}, state) == [("set_camera_pose", {"camera_pose": POSES["orbit_right"]})]
    assert live.drive_commands(WORLD_2, set(), state) == [("set_camera_pose", {"camera_pose": []})]


def test_only_models_with_camera_lanes_get_drive_keys():
    assert live.drive_keys(MODELS["LongLive-2.0"]) == live.drive_keys(MODELS["Helios"]) == []
    assert set(live.drive_keys(LINGBOT)) == {"KeyW", "KeyA", "KeyS", "KeyD", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"}
    assert set(live.drive_keys(WORLD_2)) == set(live.drive_keys(LINGBOT)) | {"KeyQ", "KeyE"}


def test_lingbot_v1_has_no_orbit():
    assert live.drive_commands(LINGBOT, {"KeyQ"}, {}) == []


def test_lingbot_v1_has_one_movement_lane_and_longitudinal_wins():
    state = {}
    assert live.drive_commands(LINGBOT, {"KeyA"}, state) == [("set_movement", {"movement": "strafe_left"})]
    assert live.drive_commands(LINGBOT, {"KeyW", "KeyA"}, state) == [("set_movement", {"movement": "forward"})]
    assert live.drive_commands(LINGBOT, {"KeyS", "KeyD"}, state) == [("set_movement", {"movement": "back"})]
    assert live.drive_commands(LINGBOT, {"KeyA", "KeyD"}, state) == [("set_movement", {"movement": "idle"})]
    assert live.drive_commands(LINGBOT, {"ArrowUp"}, state) == [("set_look_vertical", {"look_vertical": "up"})]


def test_the_binary_frame_header_round_trips():
    assert live.unpack_frame(live.pack_frame(True, 123456789, b"payload")) == (True, 123456789, b"payload")
    assert live.unpack_frame(live.pack_frame(False, 2**64 - 1, b"")) == (False, 2**64 - 1, b"")


def test_h264_round_trip_between_the_preview_encoder_and_webcam_decoder():
    encoder = live.H264Encoder(160, 96, 24)
    decoder = live.H264Decoder(160, 96)
    decoded = []
    for i, level in enumerate([0, 40, 80, 120, 160, 200]):
        access_unit, keyframe = encoder.encode(np.full((96, 160, 3), level, dtype=np.uint8), force_keyframe=i == 3)
        assert keyframe == (i in (0, 3))
        if keyframe:
            # The browser decodes a keyframe cold, so SPS/PPS are inline before it.
            assert b"\x00\x00\x01\x67" in access_unit or b"\x00\x00\x00\x01\x67" in access_unit
        frame = decoder.decode(access_unit)
        if frame is not None:
            decoded.append(frame)
    assert len(decoded) == 6
    assert {f.shape for f in decoded} == {(96, 160, 3)}
    assert [round(f.mean() / 40) for f in decoded] == [0, 1, 2, 3, 4, 5]


class RecvTrack:
    kind, direction = "video", "recvonly"

    def on_frame(self, fn):
        self.push = fn


class SendTrack:
    """A published webcam track, recording the frames pushed to it."""

    def __init__(self, name):
        self.name = name
        self.pushed = []

    def push_frame(self, frame):
        self.pushed.append(frame)


class FakeLiveReactor:
    """Stands in for reactor_sdk.Reactor on a live take.

    The stream opens with a black placeholder frame, then white frames at `interval` until
    disconnect — a live session has no planned end. Commands land in `sent`, uploads in
    `uploads`, and published webcam frames in `published.pushed`.
    """

    def __init__(self, interval=0.005):
        self.interval = interval
        self.sent = []
        self.uploads = []
        self.published = None
        self.disconnected = False
        self.track = RecvTrack()

    def __call__(self, slug, **connect):
        self.slug, self.options = slug, connect
        return self

    def on_message(self, func):
        self.handler = func
        return func

    def on_track(self, func):
        self.track_handler = func
        return func

    async def publish_track(self, name):
        self.published = SendTrack(name)
        return self.published

    async def disconnect(self):
        self.disconnected = True
        self.streamer.cancel()

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


def frames_in(path):
    with av.open(str(path)) as container:
        return [f.to_ndarray(format="rgb24") for f in container.decode(video=0)]


def webcam_message(size, level):
    """An H.264 webcam AU of one gray frame at the browser's encoding size, protocol-framed."""
    width, height = size
    access_unit, _ = live.H264Encoder(width, height, 24).encode(np.full((height, width, 3), level, dtype=np.uint8))
    return live.pack_frame(True, 0, access_unit)


async def until(predicate, seconds=5.0):
    for _ in range(int(seconds * 50)):
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def texts(sent):
    return [json.loads(m) for m in sent if isinstance(m, str)]


def make_run(fake, monkeypatch, mode, model, path, prompt="a", seed=42, image=None, size=None):
    monkeypatch.setattr(live, "Reactor", fake)
    monkeypatch.setattr(live, "CONNECT_SETTLE_SECONDS", 0)
    setup = (live.style_setup(MODELS[model], prompt, seed, image) if mode == "style"
             else live.drive_setup(model, prompt, seed, image, {}))
    return live.LiveRun(mode, MODELS[model], str(path), prompt, setup, size, {"api_key": "rk_test"})


async def test_a_style_run_streams_pushes_and_records_the_take(tmp_path, monkeypatch):
    fake = FakeLiveReactor()
    path = tmp_path / "take.mp4"
    run = make_run(fake, monkeypatch, "style", "X2", path, prompt="make it noir", size=X2_INPUT)
    task = asyncio.create_task(run.run(lambda: None))
    sent = []
    assert run.connected(sent.append)
    assert not run.connected(lambda m: None)
    run.receive(webcam_message(X2_INPUT, 40))
    assert await until(lambda: fake.published is not None and len(fake.published.pushed) > 0)
    assert await until(lambda: run._writer.received > 0)
    run.receive('{"type":"done"}')
    assert await asyncio.wait_for(task, 10) == str(path)
    assert (fake.slug, fake.options) == ("xmax/x2", {"api_key": "rk_test", "max_session_duration_seconds": 1800})
    assert fake.sent == [("set_keep_backlog", {"keep_backlog": False}), ("set_prompt", {"prompt": "make it noir"})]
    pushed = fake.published.pushed
    assert pushed and {f.shape for f in pushed} == {(X2_INPUT[1], X2_INPUT[0], 3)}
    messages = texts(sent)
    assert messages[0] == {"type": "config", "mode": "style", "prompt": "make it noir", "keys": [],
                           "preview": {"width": 832, "height": 468, "fps": 24},
                           "input": {"width": 1480, "height": 832, "fps": 24}}
    assert [m["text"] for m in messages if m["type"] == "status"] == ["Connecting to Reactor…", ""]
    assert [m for m in messages if m["type"] == "ended"] == [
        {"type": "ended", "error": None}]
    assert frames_in(path) and all(f.mean() > 200 for f in frames_in(path))
    assert fake.disconnected


async def test_a_cancel_raises_and_reports_the_error(tmp_path, monkeypatch):
    fake = FakeLiveReactor()
    run = make_run(fake, monkeypatch, "style", "X2", tmp_path / "take.mp4", size=X2_INPUT)
    task = asyncio.create_task(run.run(lambda: None))
    sent = []
    run.connected(sent.append)
    assert await until(lambda: any(m.get("text") == "" for m in texts(sent)))
    run.receive('{"type":"cancel"}')
    with pytest.raises(RuntimeError, match="Cancelled."):
        await asyncio.wait_for(task, 10)
    assert [m for m in texts(sent) if m["type"] == "ended"] == [{"type": "ended", "error": "Cancelled."}]
    assert fake.disconnected


async def test_a_socket_closing_without_done_cancels_the_run(tmp_path, monkeypatch):
    fake = FakeLiveReactor()
    run = make_run(fake, monkeypatch, "style", "X2", tmp_path / "take.mp4", size=X2_INPUT)
    task = asyncio.create_task(run.run(lambda: None))
    run.connected(lambda m: None)
    assert await until(lambda: ("set_prompt", {"prompt": "a"}) in fake.sent)
    run.disconnected()
    with pytest.raises(RuntimeError, match="Cancelled."):
        await asyncio.wait_for(task, 10)


async def test_a_socket_closing_after_done_still_saves_the_take(tmp_path, monkeypatch):
    fake = FakeLiveReactor()
    run = make_run(fake, monkeypatch, "style", "X2", tmp_path / "take.mp4", size=X2_INPUT)
    task = asyncio.create_task(run.run(lambda: None))
    run.connected(lambda m: None)
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
    run.receive('{"type":"keys","held":["KeyW"]}')
    assert await until(lambda: ("set_move_longitudinal", {"move_longitudinal": "forward"}) in fake.sent)
    assert await until(lambda: {"type": "keys", "held": ["KeyW"]} in texts(sent))
    assert await until(lambda: {"type": "stats", "rtt_ms": 40.0, "loss": 0.0, "in_bps": 3e6, "out_bps": None,
                                "fps": 24.0} in texts(sent))
    assert await until(lambda: run._writer.received > 0)
    run.receive('{"type":"done"}')
    assert await asyncio.wait_for(task, 10) == str(tmp_path / "take.mp4")
    messages = texts(sent)
    assert messages[0] == {"type": "config", "mode": "drive", "prompt": "explore", "keys": live.drive_keys(WORLD_2),
                           "preview": {"width": 832, "height": 480, "fps": 48}, "input": None}
    assert frames_in(tmp_path / "take.mp4")


async def test_a_promptable_model_drives_by_prompt_alone(tmp_path, monkeypatch):
    fake = FakeLiveReactor()
    run = make_run(fake, monkeypatch, "drive", "LongLive-2.0", tmp_path / "take.mp4", prompt="a harbour", seed=3)
    task = asyncio.create_task(run.run(lambda: None))
    sent = []
    run.connected(sent.append)
    assert await until(lambda: ("start", {}) in fake.sent)
    assert fake.sent == [("set_seed", {"seed": 3}), ("set_shot", {"prompt": "a harbour"}), ("start", {})]
    run.receive('{"type":"prompt","prompt":"a storm rolls in"}')
    assert await until(lambda: ("set_shot", {"prompt": "a storm rolls in"}) in fake.sent)
    run.receive('{"type":"keys","held":["KeyW"]}')
    run.receive('{"type":"done"}')
    await asyncio.wait_for(task, 10)
    assert not [c for c, _ in fake.sent if c.startswith("set_move")]
    assert texts(sent)[0]["keys"] == []


async def test_a_source_clip_loops_in_place_of_the_camera(tmp_path, monkeypatch):
    fake = FakeLiveReactor()
    clip = [np.full((64, 96, 3), level, dtype=np.uint8) for level in (10, 20, 30)]
    monkeypatch.setattr(live, "Reactor", fake)
    monkeypatch.setattr(live, "CONNECT_SETTLE_SECONDS", 0)
    run = live.LiveRun("style", MODELS["X2"], str(tmp_path / "take.mp4"), "a",
                       live.style_setup(MODELS["X2"], "a", 42, None), None, {"api_key": "rk_test"}, clip)
    task = asyncio.create_task(run.run(lambda: None))
    sent = []
    run.connected(sent.append)
    assert await until(lambda: fake.published is not None and len(fake.published.pushed) > len(clip))
    run.receive('{"type":"done"}')
    await asyncio.wait_for(task, 10)
    levels = [int(f[0, 0, 0]) for f in fake.published.pushed]
    assert levels[:4] == [10, 20, 30, 10]
    assert texts(sent)[0]["input"] is None and texts(sent)[0]["preview"]["width"] == 96



async def test_every_model_frame_in_a_burst_is_previewed(tmp_path, monkeypatch):
    encoded = []

    class FakeEncoder:
        def __init__(self, width, height, fps, max_kbps=None):
            pass

        def encode(self, frame, force_keyframe=False):
            encoded.append(int(frame[0, 0, 0]))
            return b"", False

    monkeypatch.setattr(live, "H264Encoder", FakeEncoder)
    run = live.LiveRun("drive", WORLD_2, str(tmp_path / "take.mp4"), "a", [], None, {"api_key": "rk_test"})
    try:
        for level in range(6):
            run._offer_preview(np.full((8, 8, 3), level, dtype=np.uint8))
            assert await until(lambda: run._pending is None)
        assert await until(lambda: len(encoded) == 6)
        assert encoded == list(range(6))
    finally:
        run.close()


async def test_a_backed_up_socket_drops_to_the_next_keyframe():
    sent, requests, gate = [], [], asyncio.Event()

    async def send(frame):
        sent.append(frame[9:])
        await gate.wait()

    sender = live.PreviewSender(send, lambda: requests.append(1))
    first = asyncio.create_task(sender.push(live.pack_frame(True, 0, b"K1")))
    await asyncio.sleep(0)
    for name in (b"D1", b"D2", b"D3"):
        await sender.push(live.pack_frame(False, 0, name))
    gate.set()
    await first
    assert sent == [b"K1"] and requests == [1]
    for frame in (live.pack_frame(False, 0, b"D4"), live.pack_frame(True, 0, b"K2"), live.pack_frame(False, 0, b"D5")):
        await sender.push(frame)
    assert sent == [b"K1", b"K2", b"D5"] and requests == [1]


def test_the_preview_bitrate_ceiling_holds_on_noisy_frames():
    rng = np.random.default_rng(0)
    frames = [rng.integers(0, 255, (240, 320, 3), dtype=np.uint8) for _ in range(48)]

    def mbps(max_kbps):
        encoder = live.H264Encoder(320, 240, 24, max_kbps)
        return sum(len(encoder.encode(f)[0]) for f in frames) * 8 / 2 / 1e6

    assert mbps(None) > 3
    assert mbps(1000) < 1.2
