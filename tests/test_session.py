import asyncio

import dataclasses

import av
import io
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


class SendOnlyTrack:
    """A published source track, recording the frames pushed to it."""

    def __init__(self, name, on_push=None):
        self.name = name
        self.pushed = []
        self.on_push = on_push

    def push_frame(self, frame):
        self.pushed.append(frame)
        if self.on_push:
            self.on_push(len(self.pushed))


class FakeReactor:
    """Stands in for reactor_sdk.Reactor.

    The stream opens with a black placeholder frame; `start` then emits `chunks` chunk_complete
    messages, each followed shortly by its white frames, as media trails the messages. A chunk is
    `frames` frames, or `first` for the first one. `frames_emitted` counts the session's frames so
    far, as LongLive reports it, or with `per_chunk` only the chunk's, as Helios does. The first `refusals` connects raise a 429 carrying `retry_after_ms` and the message `refusal`.
    Each command's reply takes `reply_seconds`. With `whole_chunks`, the model instead returns a
    chunk of white frames only once that many more source frames are pushed, holding back a partial one.
    """

    def __init__(self, chunks, frames=2, first=None, per_chunk=False, replies=None, complete_after=None, stall=False,
                 refusals=0, retry_after_ms=10, refusal="too many requests", reply_seconds=0, whole_chunks=None):
        self.chunks, self.frames, self.first, self.per_chunk = chunks, frames, frames if first is None else first, per_chunk
        self.replies = replies or {}
        self.complete_after = complete_after
        self.stall = stall
        self.sent = []
        # `sent_at[i]` is how many source frames were pushed when `sent[i]` went out.
        self.sent_at = []
        self.uploads = []
        self.disconnected = False
        self.track = FakeTrack()
        self.published = None
        self.refusals, self.retry_after_ms, self.refusal = refusals, retry_after_ms, refusal
        self.connects = self.closes = 0
        self.reply_seconds, self.in_flight, self.most_in_flight = reply_seconds, 0, 0
        self.whole_chunks = whole_chunks

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
        # A published source is what the model transforms, so the render streams from here on.
        if self.whole_chunks:
            self.published = SendOnlyTrack(name, self._return_whole_chunks)
        else:
            self.published = SendOnlyTrack(name)
            asyncio.get_running_loop().create_task(self._emit())
        return self.published

    def _return_whole_chunks(self, pushed):
        if pushed % self.whole_chunks == 0:
            for _ in range(self.whole_chunks):
                self.track.push(np.full((16, 16, 3), WHITE, dtype=np.uint8))

    async def disconnect(self):
        self.disconnected = True

    async def connect(self):
        self.connects += 1
        if self.connects <= self.refusals:
            raise RateLimitedError(self.refusal, retry_after_ms=self.retry_after_ms)
        self.track_handler(self.track)
        self.track.push(np.full((16, 16, 3), BLACK, dtype=np.uint8))

    def close(self):
        self.closes += 1

    async def upload_file(self, data, name, mime_type):
        self.uploads.append((data, name, mime_type))
        return f"ref:{data.decode()}"

    async def send_command(self, command, data):
        self.sent.append((command, data))
        self.sent_at.append(None if self.published is None else len(self.published.pushed))
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


def source_clip(levels):
    """An H.264 MP4 whose frames hold these gray levels, in order."""
    buf = io.BytesIO()
    container = av.open(buf, "w", format="mp4")
    stream = container.add_stream("libx264", rate=24)
    stream.width, stream.height, stream.pix_fmt = 16, 16, "yuv420p"
    for level in levels:
        frame = av.VideoFrame.from_ndarray(np.full((16, 16, 3), level, dtype=np.uint8), format="rgb24")
        container.mux(stream.encode(frame.reformat(16, 16, "yuv420p")))
    container.mux(stream.encode())
    container.close()
    return buf.getvalue()


START = ("start", {})


def run(fake, plan, tmp_path, monkeypatch, check_interrupt=lambda: None, model="LongLive-2.0", fps=None, on_status=lambda text: None, **connect):
    monkeypatch.setattr(session, "Reactor", fake)
    monkeypatch.setattr(session, "CONNECT_SETTLE_SECONDS", 0)
    spec = dataclasses.replace(MODELS[model], frames_per_chunk=fake.frames, fps=fps or MODELS[model].fps)
    progress = []
    coro = session.render(spec, plan, str(tmp_path / "out.mp4"), lambda done, total, frame: progress.append((done, total)), on_status, check_interrupt,
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
    monkeypatch.setattr(session, "TAIL_PAD_SECONDS", 0.2)


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
    statuses = []
    _, coro = run(fake, Plan(setup=[START], chunks=1), tmp_path, monkeypatch, on_status=statuses.append)
    await coro
    assert (fake.connects, fake.closes) == (3, 3)
    assert START in fake.sent
    assert statuses == ["Connecting to Reactor…", "Rate-limited; retrying…", "Rate-limited; retrying…", "Rendering"]


async def test_a_connect_still_rate_limited_when_retrying_ends_raises_with_the_wait(tmp_path, monkeypatch):
    fake = FakeReactor(chunks=1, refusals=10, retry_after_ms=1500)
    monkeypatch.setattr(session, "CONNECT_RETRY_SECONDS", 2)
    _, coro = run(fake, Plan(setup=[START], chunks=1), tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="try again in 2s"):
        await coro
    assert fake.connects == 2 and fake.sent == [] and fake.disconnected


async def test_a_connect_refused_for_capacity_keeps_retrying_then_says_so(tmp_path, monkeypatch):
    fake = FakeReactor(chunks=1, refusals=10, retry_after_ms=None,
                       refusal='429 from create session: {"error":"no available capacity: no available servers to handle the request"}')
    monkeypatch.setattr(session, "CONNECT_RETRY_SECONDS", 0.2)
    monkeypatch.setattr(session, "RATE_LIMIT_WAIT_SECONDS", 0.05)
    statuses = []
    _, coro = run(fake, Plan(setup=[START], chunks=1), tmp_path, monkeypatch, on_status=statuses.append)
    with pytest.raises(RuntimeError, match="no free servers"):
        await coro
    assert fake.connects > 2
    assert statuses[:2] == ["Connecting to Reactor…", "Waiting for a free server…"] and "Rendering" not in statuses


async def test_an_interrupt_during_the_rate_limit_wait_stops_the_render(tmp_path, monkeypatch):
    fake = FakeReactor(chunks=1, refusals=1, retry_after_ms=None)
    _, coro = run(fake, Plan(setup=[START], chunks=1), tmp_path, monkeypatch, check_interrupt=interrupt_after(0))
    with pytest.raises(Interrupted):
        await asyncio.wait_for(coro, timeout=5)
    assert fake.connects == 1


# Source tests run at a pacing of 1000 fps so the pushes finish at once; the grace waits stay real.


async def test_send_uploads_a_video_as_mp4_and_images_as_png(tmp_path, monkeypatch):
    fake = FakeReactor(chunks=1)
    plan = Plan(setup=[("set_video", {"video": b"clip"}), ("set_conditioning", {"image": b"png"}), START], chunks=1)
    _, coro = run(fake, plan, tmp_path, monkeypatch)
    await coro
    assert fake.uploads == [(b"clip", "video.mp4", "video/mp4"), (b"png", "image.png", "image/png")]


async def test_source_frames_are_pushed_in_order(tmp_path, monkeypatch, fast_grace):
    fake = FakeReactor(chunks=2, frames=1)
    # Source frames carry levels 0, 40, 80, 120, so H.264's quantization cannot reorder them.
    plan = Plan(setup=[], chunks=4, source=source_clip([i * 40 for i in range(4)]))
    _, coro = run(fake, plan, tmp_path, monkeypatch, model="X2", fps=1000)
    await asyncio.wait_for(coro, timeout=10)
    pushed = fake.published.pushed
    levels = [round(int(f.mean()) / 40) for f in pushed]
    # Past the clip, its last frame repeats until the output catches up.
    assert levels[:4] == [0, 1, 2, 3] and set(levels[4:]) <= {3}
    # X2 keeps the square clip's aspect and scales it to its native short side.
    assert all(f.shape == (832, 832, 3) and f.dtype == np.uint8 for f in pushed)


async def test_source_frames_past_the_planned_count_are_never_pushed(tmp_path, monkeypatch, fast_grace):
    fake = FakeReactor(chunks=2, frames=1)
    plan = Plan(setup=[], chunks=2, source=source_clip([i * 40 for i in range(6)]))
    _, coro = run(fake, plan, tmp_path, monkeypatch, model="X2", fps=1000)
    await asyncio.wait_for(coro, timeout=10)
    assert {round(int(f.mean()) / 40) for f in fake.published.pushed} == {0, 1}


async def test_a_source_tail_the_model_holds_back_is_padded_until_it_returns(tmp_path, monkeypatch, fast_grace):
    fake = FakeReactor(chunks=0, whole_chunks=4)
    plan = Plan(setup=[], chunks=5, source=source_clip([i * 40 for i in range(5)]))
    _, coro = run(fake, plan, tmp_path, monkeypatch, model="X2", fps=1000)
    await asyncio.wait_for(coro, timeout=10)
    assert len(fake.published.pushed) == 8
    assert len(frames_in(tmp_path / "out.mp4")) == 5


async def test_source_padding_gives_up_on_a_tail_that_never_returns(tmp_path, monkeypatch, fast_grace):
    fake = FakeReactor(chunks=2, frames=1)
    plan = Plan(setup=[], chunks=4, source=source_clip([i * 40 for i in range(4)]))
    _, coro = run(fake, plan, tmp_path, monkeypatch, model="X2", fps=1000)
    await asyncio.wait_for(coro, timeout=10)
    assert len(frames_in(tmp_path / "out.mp4")) == 2


async def test_a_timed_command_goes_out_when_the_source_reaches_its_frame(tmp_path, monkeypatch, fast_grace):
    fake = FakeReactor(chunks=2, frames=1)
    plan = Plan(setup=[("set_keep_backlog", {"keep_backlog": True})], timed=[(2, "set_prompt", {"prompt": "b"})],
                chunks=4, source=source_clip([i * 40 for i in range(4)]))
    _, coro = run(fake, plan, tmp_path, monkeypatch, model="X2", fps=1000)
    await asyncio.wait_for(coro, timeout=10)
    assert [c for c, _ in fake.sent] == ["set_keep_backlog", "set_prompt"]
    assert fake.sent_at == [0, 2]


async def test_source_capture_records_the_stream_from_the_first_push(tmp_path, monkeypatch, fast_grace):
    fake = FakeReactor(chunks=3, frames=1)
    plan = Plan(setup=[], chunks=2, source=source_clip([0, 40]))
    _, coro = run(fake, plan, tmp_path, monkeypatch, model="X2", fps=1000)
    await asyncio.wait_for(coro, timeout=10)
    frames = frames_in(tmp_path / "out.mp4")
    # The output is the clip's length: frames past it answer the padding.
    assert len(frames) == 2
    assert all(f.mean() > 200 for f in frames)


async def test_a_failed_disconnect_still_destroys_the_handle(caplog):
    class Failing:
        closes = 0

        async def disconnect(self):
            raise RuntimeError("gone")

        def close(self):
            self.closes += 1

    reactor = Failing()
    async with session.closing_session(reactor):
        pass
    assert reactor.closes == 1 and "ending the session failed" in caplog.text
