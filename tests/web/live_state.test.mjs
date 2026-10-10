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
