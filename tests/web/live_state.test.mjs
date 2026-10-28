import { test } from "node:test";
import assert from "node:assert/strict";
import { PHASES, canMove, statusLine, CLIP_ENDED, THEN_WAITING } from "../../web/reactor_live_state.mjs";

test("every phase moves only to phases that exist", () => {
    for (const [from, tos] of Object.entries(PHASES)) {
        for (const to of tos) assert.ok(to in PHASES, `${from} -> ${to}`);
    }
});

test("a take can be saved, held or closed from any phase before it is saved", () => {
    for (const from of ["connecting", "firstFrame", "playing", "stalled", "awaitingClip", "clipEnded"]) {
        for (const to of ["saving", "held", "closed"]) assert.ok(canMove(from, to), `${from} -> ${to}`);
    }
});

test("a saving take never plays again", () => {
    for (const to of ["connecting", "firstFrame", "playing", "stalled", "awaitingClip", "clipEnded"]) {
        assert.ok(!canMove("saving", to), to);
    }
});

test("a held window can only close, and a closed one goes nowhere", () => {
    assert.deepEqual(PHASES.held, ["closed"]);
    assert.deepEqual(PHASES.closed, []);
});

test("the first frame is the only way into playing from connecting", () => {
    assert.ok(!canMove("connecting", "playing"));
    assert.ok(canMove("connecting", "firstFrame"));
    assert.ok(canMove("firstFrame", "playing"));
});

test("a clip model waiting for its next clip plays it only after frames resume", () => {
    assert.ok(!canMove("awaitingClip", "playing"));
    assert.ok(canMove("awaitingClip", "stalled"));
    assert.ok(canMove("stalled", "playing"));
});

test("staying in a phase is not a move", () => {
    for (const phase of Object.keys(PHASES)) assert.ok(!canMove(phase, phase), phase);
});

test("the status line asks what happens next once a clip model's clips have ended", () => {
    assert.equal(statusLine({ phase: "clipEnded", note: "", thenWaiting: false }), CLIP_ENDED);
});

test("the status line drops the question once the take moves on", () => {
    for (const phase of ["playing", "awaitingClip", "saving"]) {
        assert.equal(statusLine({ phase, note: "", thenWaiting: false }), "", phase);
    }
});

test("a note covers the question, and clearing it brings the question back", () => {
    assert.equal(statusLine({ phase: "clipEnded", note: "Reactor rejected prompt: busy", thenWaiting: false }),
        "Reactor rejected prompt: busy");
    assert.equal(statusLine({ phase: "clipEnded", note: "", thenWaiting: false }), CLIP_ENDED);
});

test("a note left from the ended clips stays after the take plays again", () => {
    assert.equal(statusLine({ phase: "playing", note: "Switched", thenWaiting: false }), "Switched");
});

test("a take waiting to start says so until it starts", () => {
    assert.equal(statusLine({ phase: "playing", note: "", thenWaiting: true }), THEN_WAITING);
    assert.equal(statusLine({ phase: "playing", note: "", thenWaiting: false }), "");
});

test("a held point goes out as a window of its last four frames, and letting go sends an empty one once", async () => {
    const { DragTracker } = await import("../../web/reactor_live_state.mjs");
    const drag = new DragTracker(2);
    assert.equal(drag.tick(), null);
    assert.ok(drag.down(1, 0.1, 0.5));
    assert.deepEqual(drag.tick(), Array(4).fill([{ x: 0.1, y: 0.5 }]));
    drag.move(1, 0.2, 0.5);
    assert.deepEqual(drag.tick().at(-1), [{ x: 0.2, y: 0.5 }]);
    drag.tick();
    drag.up(1);
    drag.tick();
    assert.deepEqual(drag.tick(), [[], [], [], []]);
    assert.equal(drag.tick(), null);
    assert.ok(!drag.active);
});

test("a drag keeps each finger as a point, up to what the model takes, and clamps to the frame", async () => {
    const { DragTracker } = await import("../../web/reactor_live_state.mjs");
    const drag = new DragTracker(2);
    assert.ok(drag.down(1, 0.1, 0.1) && drag.down(2, 1.5, -0.2));
    assert.ok(!drag.down(3, 0.5, 0.5));
    assert.deepEqual(drag.tick().at(-1), [{ x: 0.1, y: 0.1 }, { x: 1, y: 0 }]);
});

test("a drag point is measured across the video itself, not the bars around it", async () => {
    const { videoPoint } = await import("../../web/reactor_live_state.mjs");
    const box = { left: 100, top: 50, width: 800, height: 400 };
    // A square video in a wide box sits in the middle 400 pixels.
    assert.deepEqual(videoPoint(box, { width: 100, height: 100 }, 500, 250), [0.5, 0.5]);
    assert.deepEqual(videoPoint(box, { width: 100, height: 100 }, 300, 50), [0, 0]);
    assert.deepEqual(videoPoint(box, { width: 0, height: 0 }, 500, 250), [0.5, 0.5]);
});

test("a point let go between two ticks is still sent once, where it was let go", async () => {
    const { DragTracker } = await import("../../web/reactor_live_state.mjs");
    const drag = new DragTracker(2);
    drag.down(1, 0.1, 0.5);
    drag.move(1, 0.9, 0.5);
    drag.up(1);
    assert.ok(drag.active);
    assert.deepEqual(drag.tick().at(-1), [{ x: 0.9, y: 0.5 }]);
    assert.deepEqual(drag.tick(), [[], [], [], []]);
    assert.equal(drag.tick(), null);
});
