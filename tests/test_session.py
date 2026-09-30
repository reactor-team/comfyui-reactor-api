import asyncio

import dataclasses

import av
import io
import numpy as np
import pytest

from reactor_sdk.errors import BadRequestError, RateLimitedError

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
    `connect_error` is raised by every connect; with `ended`, Reactor ends the session with that reason after the first chunk.
    """

    def __init__(self, chunks, frames=2, first=None, per_chunk=False, replies=None, complete_after=None, stall=False,
                 refusals=0, retry_after_ms=10, refusal="too many requests", reply_seconds=0, whole_chunks=None,
                 connect_error=None, ended=None):
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
        self.connect_error, self.ended, self.events = connect_error, ended, {}

    def __call__(self, slug, **connect):
        self.slug, self.options = slug, connect
        return self

    def on_message(self, func):
        self.handler = func
        return func

    def on_track(self, func):
        self.track_handler = func
        return func

    def on(self, event, func):
        self.events[event] = func

    def on_error(self, func):
        self.events["error"] = func
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
        if self.connect_error is not None:
            raise self.connect_error
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
            if self.ended is not None:
                self.events["runtime_message"]({"type": "sessionEnded", "data": {"reason": self.ended}})
                return
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


# A 402 as the Python SDK raises it, from a session refused for the account's credits.
DEPLETED = ('connect: [BAD_REQUEST] unexpected HTTP status 402 from create session: '
            '{"error": "credits_depleted", "message": "Your credits have been depleted. Please add credits to continue."}')


async def test_a_connect_refused_for_credits_raises_the_api_sentence_and_where_to_add_them(tmp_path, monkeypatch):
    fake = FakeReactor(chunks=1, connect_error=BadRequestError(DEPLETED, status=402))
    _, coro = run(fake, Plan(setup=[START], chunks=1), tmp_path, monkeypatch)
    with pytest.raises(RuntimeError) as raised:
        await coro
    assert str(raised.value) == "Your credits have been depleted. Please add credits to continue. https://reactor.inc/dashboard"
    assert fake.connects == 1 and fake.disconnected


async def test_reactor_ending_the_session_mid_render_raises_its_reason(tmp_path, monkeypatch):
    reason = "Session ended: your credits ran out. Add more to continue."
    fake = FakeReactor(chunks=5, ended=reason)
    _, coro = run(fake, Plan(setup=[START], chunks=5), tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match=reason):
        await asyncio.wait_for(coro, timeout=10)
    assert fake.disconnected


@pytest.mark.parametrize("text, shown", [
    ('create session: {"error": "billing_not_setup", "message": "Please complete your billing setup to start sessions.", "url": "https://reactor.inc/billing"}',
     "Please complete your billing setup to start sessions. https://reactor.inc/billing"),
    ("connect: [NETWORK_ERROR] connection refused", "connect: [NETWORK_ERROR] connection refused"),
    ('404 from create session: {"error": "model not found"}', '404 from create session: {"error": "model not found"}'),
])
def test_an_api_error_shows_its_own_sentence_when_its_body_has_one(text, shown):
    assert session.api_message(text) == shown


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


async def test_an_edit_holds_the_first_frame_until_edited_video_arrives_then_plays_the_clip(tmp_path, monkeypatch, fast_grace):
    fake = FakeReactor(chunks=0, whole_chunks=1)
    sent = fake.send_command

    async def send_command(command, data):
        reply = await sent(command, data)
        if command == "start_edit":
            # The model warms up on a few source frames, goes live, and edits a few more before its output starts.
            async def go_live():
                for pushed, receiving in ((3, False), (6, True)):
                    while len(fake.published.pushed) < pushed:
                        await asyncio.sleep(0.001)
                    fake.handler({"type": "session_state", "data": {"phase": "live", "video_receiving": receiving}})
            asyncio.get_running_loop().create_task(go_live())
        return reply

    fake.send_command = send_command
    plan = Plan(setup=[("start_edit", {"reference_image": b"clay"})], chunks=3, source=source_clip([40, 80, 120]))
    _, coro = run(fake, plan, tmp_path, monkeypatch, model="Vidu S2-Editing", fps=1000)
    await asyncio.wait_for(coro, timeout=10)
    levels = [round(int(f.mean()) / 40) for f in fake.published.pushed]
    held = levels.index(2)
    assert held >= 6 and set(levels[:held]) == {1} and levels[held:held + 2] == [2, 3]
    assert len(frames_in(tmp_path / "out.mp4")) == 3


async def test_an_edit_the_model_ends_keeps_the_video_so_far(tmp_path, monkeypatch, fast_grace):
    fake = FakeReactor(chunks=0, whole_chunks=1)
    sent = fake.send_command

    async def send_command(command, data):
        reply = await sent(command, data)
        if command == "start_edit":
            fake.handler({"type": "session_state", "data": {"phase": "live", "video_receiving": True}})
        return reply

    def on_push(pushed):
        fake.track.push(np.full((16, 16, 3), 255, dtype=np.uint8))
        if pushed == 3:
            fake.handler({"type": "session_state", "data": {"phase": "ended", "end_reason": "max_duration"}})

    fake.send_command = send_command
    fake._return_whole_chunks = on_push
    plan = Plan(setup=[("start_edit", {"reference_image": b"clay"})], chunks=10, source=source_clip([40] * 10))
    _, coro = run(fake, plan, tmp_path, monkeypatch, model="Vidu S2-Editing", fps=1000)
    await asyncio.wait_for(coro, timeout=10)
    assert 0 < len(frames_in(tmp_path / "out.mp4")) < 10


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


async def test_progress_is_reported_at_most_once_per_interval(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(session.time, "monotonic", lambda: clock[0])
    writer = type("Writer", (), {"received": 0, "limit": None, "previous": None})()
    reports = []
    s = session.Session(None, writer, 24, lambda: None, lambda done, total, frame: reports.append(done))
    for frame in range(1, 25):
        writer.received = frame
        clock[0] = frame * 0.05
        await s.next_message(timeout=0)
    assert len(reports) == 8


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


class AudioTrack:
    kind, direction = "audio", "recvonly"

    def on_frame(self, fn):
        self.push = fn


PLACEHOLDER = np.zeros((9, 16, 3), dtype=np.uint8)


class FakeCall:
    """Stands in for reactor_sdk.Reactor on a Vidu S2-Avatar call.

    `create_avatar` and `start_call` walk `session_state` through their phases. A placeholder frame
    streams from connect until a few frames into the call; once the call is live, video and 10 ms
    audio blocks stream every 10 ms; the audio is loud for `talk` seconds after
    the character appears and after each `say`, unless `silent`. `fail` names a phase after which the call fails.
    """

    # A call has no chunks.
    frames = None

    def __init__(self, talk=0.1, silent=False, fail=None):
        self.talk, self.silent, self.fail = talk, silent, fail
        self.sent, self.uploads = [], []
        # For each `say`, whether the character was still talking when it went out.
        self.interrupted = []
        # For each `say`, how many frames had streamed when it went out.
        self.said_at = []
        self.streamed = 0
        self.loud_until = 0.0
        self.disconnected = False
        self.video, self.audio = FakeTrack(), AudioTrack()

    def __call__(self, slug, **connect):
        self.slug = slug
        return self

    def on_message(self, func):
        self.handler = func
        return func

    def on_track(self, func):
        self.track_handler = func
        return func

    def on(self, event, func):
        pass

    def on_error(self, func):
        return func

    async def connect(self):
        self.track_handler(self.video)
        self.track_handler(self.audio)
        self.video.push(PLACEHOLDER)
        self.state("idle")

    def state(self, phase):
        self.handler({"type": "session_state", "data": {"phase": phase, "last_error": "boom" if phase == "failed" else None}})

    async def walk(self, *phases):
        for phase in phases:
            await asyncio.sleep(0.01)
            self.state(phase)
            if phase == self.fail:
                self.state("failed")
                return
        if phases[-1] == "live":
            while not self.disconnected:
                self.streamed += 1
                if self.streamed == 6:
                    self.speak()
                loud = asyncio.get_running_loop().time() < self.loud_until
                self.video.push(PLACEHOLDER if self.streamed <= 5 else np.full((16, 16, 3), WHITE, dtype=np.uint8))
                self.audio.push(np.full((480, 1), 8000 if loud else 0, dtype=np.int16), 48000)
                await asyncio.sleep(0.01)

    def speak(self):
        if not self.silent:
            self.loud_until = asyncio.get_running_loop().time() + self.talk

    async def upload_file(self, data, name, mime_type):
        self.uploads.append((data, name, mime_type))
        return f"ref:{data.decode()}"

    async def send_command(self, command, data):
        self.sent.append((command, data))
        loop = asyncio.get_running_loop()
        if command == "create_avatar":
            loop.create_task(self.walk("preparing_avatar", "avatar_ready"))
        if command == "start_call":
            loop.create_task(self.walk("starting", "warming_up", "live"))
        if command == "say":
            self.interrupted.append(loop.time() < self.loud_until)
            self.said_at.append(self.streamed)
            self.speak()

    async def disconnect(self):
        self.disconnected = True

    def close(self):
        pass


@pytest.fixture
def quick_replies(monkeypatch):
    monkeypatch.setattr(session, "REPLY_QUIET_SECONDS", 0.05)
    monkeypatch.setattr(session, "REPLY_TIMEOUT_SECONDS", 0.5)


CALL = Plan(setup=[("create_avatar", {"image": b"photo"}), ("start_call", {"persona": "p"})], chunks=3,
            timed=[(1, "say", {"text": "one"}), (2, "say", {"text": "two"})], holds=[0, 0, 0])


async def test_a_call_says_each_line_once_the_last_reply_has_gone_quiet(tmp_path, monkeypatch, quick_replies):
    fake = FakeCall()
    _, coro = run(fake, CALL, tmp_path, monkeypatch, model="Vidu S2-Avatar")
    await asyncio.wait_for(coro, timeout=10)
    assert [c for c, _ in fake.sent] == ["create_avatar", "start_call", "say", "say"]
    assert fake.uploads == [(b"photo", "image.png", "image/png")]
    assert fake.interrupted == [False, False]
    assert fake.disconnected
    with av.open(str(tmp_path / "out.mp4")) as c:
        assert c.streams.audio
        # Capture starts at the character's first frame, not the placeholder before it.
        assert (c.streams.video[0].height, c.streams.video[0].width) == (16, 16)


async def test_a_call_holds_the_next_line_until_the_beats_frames_have_played(tmp_path, monkeypatch, quick_replies):
    fake = FakeCall()
    # A reply here lasts about 15 frames, so a 40-frame hold is what keeps the second line back.
    _, coro = run(fake, dataclasses.replace(CALL, holds=[0, 40, 0]), tmp_path, monkeypatch, model="Vidu S2-Avatar")
    await asyncio.wait_for(coro, timeout=10)
    assert fake.said_at[1] - fake.said_at[0] >= 40
    assert fake.interrupted == [False, False]


async def test_a_call_whose_character_never_answers_fails(tmp_path, monkeypatch, quick_replies):
    _, coro = run(FakeCall(silent=True), CALL, tmp_path, monkeypatch, model="Vidu S2-Avatar")
    with pytest.raises(RuntimeError, match="never answered"):
        await asyncio.wait_for(coro, timeout=10)


async def test_a_call_that_fails_while_starting_raises_its_error(tmp_path, monkeypatch, quick_replies):
    _, coro = run(FakeCall(fail="warming_up"), CALL, tmp_path, monkeypatch, model="Vidu S2-Avatar")
    with pytest.raises(RuntimeError, match="failed: boom"):
        await asyncio.wait_for(coro, timeout=10)
