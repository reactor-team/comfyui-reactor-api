import { test } from "node:test";
import assert from "node:assert/strict";
import { ClipQueue } from "../../web/reactor_live_state.mjs";

const clip = (id, prompt, from) => ({ clip_id: id, prompt, ...(from ? { continue_from_clip_id: from } : {}) });
const msg = (type, data) => ({ type, data });
const stages = (q) => q.rows().map((r) => `${r.stage}:${r.prompt}${r.continues ? "+" : ""}`);

test("the opening clip the run queued becomes the clip to continue once the window sees it", () => {
    const q = new ClipQueue();
    q.seen(msg("queue_update", { generation: [clip("a", "open")], playout: [] }));
    assert.equal(q.last, null);
    q.seen(msg("clip_generated", { clip: clip("a", "open") }));
    assert.equal(q.last, "a");
    q.seen(msg("clip_generated", { clip: clip("b", "later") }));
    assert.equal(q.last, "a");
});

test("clips list in play order: playing, ready, building, then prompts not yet queued", () => {
    const q = new ClipQueue();
    q.seen(msg("queue_update", { generation: [clip("c", "third", "b")], playout: [clip("a", "first"), clip("b", "second")] }));
    q.seen(msg("clip_started", { clip: clip("a", "first") }));
    q.apply("fourth", false);
    q.apply("fifth", true);
    assert.deepEqual(stages(q), ["Playing:first", "Ready:second", "Building:third+", "Sending:fourth", "Sending:fifth+"]);
});

test("the playing clip shows once even while the model still lists it as ready", () => {
    const q = new ClipQueue();
    q.seen(msg("queue_update", { generation: [], playout: [clip("a", "first")] }));
    q.seen(msg("clip_started", { clip: clip("a", "first") }));
    assert.deepEqual(stages(q), ["Playing:first"]);
});

test("a finished clip stops playing, and another clip's end leaves the playing one alone", () => {
    const q = new ClipQueue();
    q.seen(msg("clip_started", { clip: clip("b", "second") }));
    q.seen(msg("clip_finished", { clip: clip("a", "first") }));
    assert.equal(q.playing?.clip_id, "b");
    q.seen(msg("clip_stopped", { clip: clip("b", "second") }));
    assert.equal(q.playing, null);
});

test("an answered prompt leaves the unsent list and becomes the clip to continue", () => {
    const q = new ClipQueue();
    const entry = q.apply("next", false);
    q.sent(entry, clip("n", "next"));
    assert.equal(q.last, "n");
    assert.deepEqual(stages(q), ["Building:next"]);
});

test("an answered prompt the model already queued is not listed twice", () => {
    const q = new ClipQueue();
    const entry = q.apply("next", false);
    q.seen(msg("queue_update", { generation: [], playout: [clip("n", "next")] }));
    q.sent(entry, clip("n", "next"));
    assert.deepEqual(stages(q), ["Ready:next"]);
});

test("a rejected prompt leaves the unsent list and keeps the clip to continue", () => {
    const q = new ClipQueue();
    q.seen(msg("clip_started", { clip: clip("a", "first") }));
    const entry = q.apply("nope", false);
    q.sent(entry, undefined);
    assert.equal(q.last, "a");
    assert.deepEqual(stages(q), ["Playing:first"]);
});

test("a new shot carries nothing on", async () => {
    const q = new ClipQueue();
    q.seen(msg("clip_generated", { clip: clip("a", "first") }));
    assert.deepEqual(await q.anchor(q.apply("fresh", false)), { anchor: null, failed: false });
});

test("a continuation before any clip is known starts fresh", async () => {
    const q = new ClipQueue();
    assert.deepEqual(await q.anchor(q.apply("more", true)), { anchor: null, failed: false });
});

test("a continuation of a built clip carries it on at once", async () => {
    const q = new ClipQueue();
    q.seen(msg("clip_generated", { clip: clip("a", "first") }));
    const entry = q.apply("more", true);
    assert.deepEqual(await q.anchor(entry), { anchor: "a", failed: false });
    assert.equal(entry.waiting, false);
});

test("a continuation waits, listed as Waiting, until the clip it continues builds", async () => {
    const q = new ClipQueue();
    const first = q.apply("first", false);
    q.sent(first, clip("a", "first"));
    const entry = q.apply("more", true);
    const pending = q.anchor(entry);
    assert.deepEqual(stages(q), ["Building:first", "Waiting:more+"]);
    q.seen(msg("clip_generated", { clip: clip("a", "first") }));
    assert.deepEqual(await pending, { anchor: "a", failed: false });
});

test("a continuation of a clip that failed to build starts fresh and says why", async () => {
    const q = new ClipQueue();
    const first = q.apply("first", false);
    q.sent(first, clip("a", "first"));
    const pending = q.anchor(q.apply("more", true));
    q.seen(msg("clip_failed", { clip: clip("a", "first") }));
    assert.deepEqual(await pending, { anchor: null, failed: true });
});

test("the queue is empty only with nothing building, ready, or applied here", () => {
    const q = new ClipQueue();
    assert.ok(q.empty);
    const entry = q.apply("x", false);
    assert.ok(!q.empty);
    q.sent(entry, undefined);
    assert.ok(q.empty);
    q.seen(msg("queue_update", { generation: [], playout: [clip("a", "first")] }));
    assert.ok(!q.empty);
    q.seen(msg("queue_update", { generation: [], playout: [] }));
    q.seen(msg("clip_started", { clip: clip("a", "first") }));
    assert.ok(q.empty);
});

test("a prompt the model queues before its answer arrives is listed once, as the model's clip", () => {
    const q = new ClipQueue();
    const entry = q.apply("next", false);
    q.dispatch(entry);
    q.seen(msg("queue_update", { generation: [clip("n", "next")], playout: [] }));
    assert.deepEqual(stages(q), ["Building:next"]);
    q.sent(entry, clip("n", "next"));
    assert.deepEqual(stages(q), ["Building:next"]);
});

test("a prompt still waiting its turn is not mistaken for an earlier clip with the same words", () => {
    const q = new ClipQueue();
    q.seen(msg("queue_update", { generation: [clip("a", "again")], playout: [] }));
    q.apply("again", false);
    q.seen(msg("queue_update", { generation: [clip("a", "again")], playout: [] }));
    assert.deepEqual(stages(q), ["Building:again", "Sending:again"]);
});
