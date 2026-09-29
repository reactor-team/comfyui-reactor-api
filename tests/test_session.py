import asyncio

import dataclasses

import av
import numpy as np
import pytest

from reactor_sdk.errors import RateLimitedError

from reactor_render import session
from reactor_render.timeline import MODELS, Plan

BLACK, WHITE = 0, 255


class FakeTrack:
    kind, direction = "video", "recvonly"

    def on_frame(self, fn):
        self.push = fn


class FakeReactor:
    """Stands in for reactor_sdk.Reactor.

    The stream opens with a black placeholder frame; `start` then emits `chunks` chunk_complete
    messages, each followed shortly by its white frames, as media trails the messages. A chunk is
    `frames` frames, or `first` for the first one. `frames_emitted` counts the session's frames so
    far, as LongLive reports it, or with `per_chunk` only the chunk's, as Helios does. The first `refusals` connects raise a 429 carrying `retry_after_ms`.
    Each command's reply takes `reply_seconds`.
    """

    def __init__(self, chunks, frames=2, first=None, per_chunk=False, replies=None, complete_after=None, stall=False,
                 refusals=0, retry_after_ms=10, reply_seconds=0):
        self.chunks, self.frames, self.first, self.per_chunk = chunks, frames, frames if first is None else first, per_chunk
        self.replies = replies or {}
        self.complete_after = complete_after
        self.stall = stall
        self.sent = []
        self.disconnected = False
        self.track = FakeTrack()
        self.refusals, self.retry_after_ms = refusals, retry_after_ms
        self.connects = self.closes = 0
        self.reply_seconds, self.in_flight, self.most_in_flight = reply_seconds, 0, 0

    def __call__(self, slug, **connect):
        self.slug, self.options = slug, connect
        return self

    def on_message(self, func):
        self.handler = func
        return func

    def on_track(self, func):
        self.track_handler = func
        return func

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.disconnected = True

    async def connect(self):
        self.connects += 1
        if self.connects <= self.refusals:
            raise RateLimitedError("too many requests", retry_after_ms=self.retry_after_ms)
        self.track_handler(self.track)
        self.track.push(np.full((16, 16, 3), BLACK, dtype=np.uint8))

    def close(self):
        self.closes += 1

    async def upload_file(self, data, name, mime_type):
        return f"ref:{data.decode()}"

    async def send_command(self, command, data):
        self.sent.append((command, data))
        if command == "start":
            asyncio.get_running_loop().create_task(self._emit())
        self.in_flight += 1
        self.most_in_flight = max(self.most_in_flight, self.in_flight)
        await asyncio.sleep(self.reply_seconds)
        self.in_flight -= 1
        return self.replies.get(command)

    async def _emit(self):
        emitted = 0
        for i in range(self.chunks):
            size = self.first if i == 0 else self.frames
            emitted += size
            self.handler({"type": "chunk_complete", "data": {"chunk_index": float(i), "frames_emitted": float(size if self.per_chunk else emitted)}})
            await asyncio.sleep(0.01)
            if self.stall:
                continue
            for _ in range(size):
                self.track.push(np.full((16, 16, 3), WHITE, dtype=np.uint8))
            if self.complete_after == i + 1:
                self.handler({"type": "generation_complete", "data": {}})
                return


def frames_in(path):
    with av.open(str(path)) as c:
        return [f.to_ndarray(format="rgb24") for f in c.decode(video=0)]


START = ("start", {})


def run(fake, plan, tmp_path, monkeypatch, check_interrupt=lambda: None, model="LongLive-2.0", **connect):
    monkeypatch.setattr(session, "Reactor", fake)
    monkeypatch.setattr(session, "CONNECT_SETTLE_SECONDS", 0)
    spec = dataclasses.replace(MODELS[model], frames_per_chunk=fake.frames)
    progress = []
    coro = session.render(spec, plan, str(tmp_path / "out.mp4"), lambda done, total, frame: progress.append((done, total)), check_interrupt,
                          **(connect or {"api_key": "rk_test"}))
    return progress, coro


async def test_render_sends_setup_starts_and_encodes_the_planned_frames(tmp_path, monkeypatch):
    fake = FakeReactor(chunks=10)
    plan = Plan(setup=[("set_shot", {"prompt": "a"}), START], chunks=3)
    progress, coro = run(fake, plan, tmp_path, monkeypatch)
    await coro
    assert (fake.slug, fake.options) == ("reactor/longlive-v2", {"api_key": "rk_test"})
    assert [c for c, _ in fake.sent] == ["set_shot", "start"]
    assert progress[-1] == (6, 6)
    frames = frames_in(tmp_path / "out.mp4")
    assert len(frames) == 6
    assert fake.disconnected


async def test_the_placeholder_frame_before_the_first_chunk_is_skipped(tmp_path, monkeypatch):
    fake = FakeReactor(chunks=1, frames=1)
    _, coro = run(fake, Plan(setup=[START], chunks=1), tmp_path, monkeypatch)
    await coro
    (frame,) = frames_in(tmp_path / "out.mp4")
    assert frame.mean() > 200


async def test_timed_commands_upload_images_and_go_out_after_their_chunk(tmp_path, monkeypatch):
    fake = FakeReactor(chunks=10)
    plan = Plan(setup=[START], timed=[(2, "set_conditioning", {"prompt": "b", "image": b"png"})],
                chunks=3)
    _, coro = run(fake, plan, tmp_path, monkeypatch)
    await coro
    assert fake.sent == [("start", {}), ("set_conditioning", {"prompt": "b", "image": "ref:png"})]


async def test_generation_complete_keeps_what_streamed(tmp_path, monkeypatch, fast_grace):
    fake = FakeReactor(chunks=10, complete_after=2)
    _, coro = run(fake, Plan(setup=[START], chunks=5), tmp_path, monkeypatch)
    await coro
    assert len(frames_in(tmp_path / "out.mp4")) == 4


async def test_the_capture_ends_on_the_frames_the_model_emitted_by_the_last_chunk(tmp_path, monkeypatch):
    fake = FakeReactor(chunks=10, frames=3, first=2)
    progress, coro = run(fake, Plan(setup=[START], chunks=3), tmp_path, monkeypatch)
    await coro
    assert progress[-1] == (8, 8)
    assert len(frames_in(tmp_path / "out.mp4")) == 8


async def test_per_chunk_frame_counts_add_up_to_the_capture_length(tmp_path, monkeypatch):
    fake = FakeReactor(chunks=10, frames=3, per_chunk=True)
    _, coro = run(fake, Plan(setup=[START], chunks=3), tmp_path, monkeypatch, model="Helios")
    await coro
    assert len(frames_in(tmp_path / "out.mp4")) == 9


async def test_no_frames_after_the_last_chunk_is_an_error(tmp_path, monkeypatch, fast_grace):
    fake = FakeReactor(chunks=1, stall=True)
    _, coro = run(fake, Plan(setup=[START], chunks=1), tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="no video frames"):
        await asyncio.wait_for(coro, timeout=10)
    assert fake.disconnected


async def test_rejected_command_raises_and_still_disconnects(tmp_path, monkeypatch):
    fake = FakeReactor(chunks=1, replies={"set_shot": {"type": "command_error", "data": {"reason": "bad prompt"}}})
    _, coro = run(fake, Plan(setup=[("set_shot", {"prompt": ""}), START], chunks=1), tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="bad prompt"):
        await coro
    assert fake.disconnected


class Interrupted(BaseException):
    """Mirrors ComfyUI's InterruptProcessingException, which is a BaseException."""


def interrupt_after(n):
    calls = 0

    def check_interrupt():
        nonlocal calls
        calls += 1
        if calls > n:
            raise Interrupted
    return check_interrupt


@pytest.fixture
def fast_grace(monkeypatch):
    monkeypatch.setattr(session, "FRAME_GRACE_SECONDS", 0.1)


async def test_interrupt_stops_the_session(tmp_path, monkeypatch):
    fake = FakeReactor(chunks=10, stall=True)
    _, coro = run(fake, Plan(setup=[START], chunks=5), tmp_path, monkeypatch, check_interrupt=interrupt_after(2))
    with pytest.raises(Interrupted):
        await asyncio.wait_for(coro, timeout=10)
    assert fake.disconnected


async def test_a_chunk_that_emits_no_frames_is_not_counted(tmp_path, monkeypatch):
    fake = FakeReactor(chunks=5, frames=3, first=0, per_chunk=True)
    _, coro = run(fake, Plan(setup=[START], chunks=2), tmp_path, monkeypatch, model="Visko Orbis Dynamic")
    await coro
    assert len(frames_in(tmp_path / "out.mp4")) == 6


async def test_a_timed_command_for_chunk_zero_goes_out_before_any_chunk(tmp_path, monkeypatch):
    fake = FakeReactor(chunks=0)
    plan = Plan(setup=[START], timed=[(0, "set_prompt", {"prompt": "b"})], chunks=1)
    _, coro = run(fake, plan, tmp_path, monkeypatch, check_interrupt=interrupt_after(1))
    with pytest.raises(Interrupted):
        await asyncio.wait_for(coro, timeout=5)
    assert fake.sent == [START, ("set_prompt", {"prompt": "b"})]


async def test_connect_options_reach_the_client_unchanged(tmp_path, monkeypatch):
    fake = FakeReactor(chunks=1)
    _, coro = run(fake, Plan(setup=[START], chunks=1), tmp_path, monkeypatch, local=True, api_url="http://localhost:8090")
    await coro
    assert fake.options == {"local": True, "api_url": "http://localhost:8090"}


async def test_timed_commands_fire_once_in_order_on_the_clock(tmp_path, monkeypatch):
    fake = FakeReactor(chunks=10)
    timed = [(3, "set_prompt", {"prompt": "c"}), (1, "set_prompt", {"prompt": "a"}), (1, "set_prompt", {"prompt": "b"})]
    _, coro = run(fake, Plan(setup=[START], timed=timed, chunks=5), tmp_path, monkeypatch)
    await coro
    assert [d["prompt"] for c, d in fake.sent if c == "set_prompt"] == ["a", "b", "c"]


async def test_commands_due_on_one_chunk_are_sent_together(tmp_path, monkeypatch):
    fake = FakeReactor(chunks=10, reply_seconds=0.05)
    timed = [(1, "set_movement", {"movement": "forward"}), (1, "set_look_horizontal", {"look_horizontal": "left"}),
             (1, "set_prompt", {"prompt": "b"})]
    _, coro = run(fake, Plan(setup=[START], timed=timed, chunks=3), tmp_path, monkeypatch)
    await coro
    assert fake.most_in_flight == 3


async def test_a_rate_limited_connect_waits_retry_after_and_retries(tmp_path, monkeypatch):
    fake = FakeReactor(chunks=1, refusals=2)
    _, coro = run(fake, Plan(setup=[START], chunks=1), tmp_path, monkeypatch)
    await coro
    assert (fake.connects, fake.closes) == (3, 2)
    assert START in fake.sent


async def test_a_connect_still_rate_limited_after_the_retries_raises_with_the_wait(tmp_path, monkeypatch):
    fake = FakeReactor(chunks=1, refusals=10, retry_after_ms=1500)
    monkeypatch.setattr(session, "RATE_LIMIT_RETRIES", 1)
    _, coro = run(fake, Plan(setup=[START], chunks=1), tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="try again in 2s"):
        await coro
    assert fake.connects == 2 and fake.sent == [] and fake.disconnected


async def test_an_interrupt_during_the_rate_limit_wait_stops_the_render(tmp_path, monkeypatch):
    fake = FakeReactor(chunks=1, refusals=1, retry_after_ms=None)
    _, coro = run(fake, Plan(setup=[START], chunks=1), tmp_path, monkeypatch, check_interrupt=interrupt_after(0))
    with pytest.raises(Interrupted):
        await asyncio.wait_for(coro, timeout=5)
    assert fake.connects == 1
