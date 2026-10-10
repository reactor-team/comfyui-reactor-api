// The Realtime window's state, kept apart from the page so it can be tested without a browser. The `.mjs` name keeps
// ComfyUI from loading this file as an extension of its own; reactor_live.js imports it.

// The phases of a take in the Realtime window, each with the phases it can move to. A take connects, waits for its
// first frame and plays; frames can stall, and a clip model can end its clips and wait for the next one. Save, an
// error or a close can end it from anywhere: "held" keeps the window open on a message, and "closed" removes it.
export const PHASES = {
    connecting: ["firstFrame", "saving", "held", "closed"],
    firstFrame: ["playing", "saving", "held", "closed"],
    playing: ["stalled", "awaitingClip", "clipEnded", "saving", "held", "closed"],
    stalled: ["playing", "awaitingClip", "clipEnded", "saving", "held", "closed"],
    awaitingClip: ["stalled", "saving", "held", "closed"],
    clipEnded: ["playing", "awaitingClip", "saving", "held", "closed"],
    saving: ["held", "closed"],
    held: ["closed"],
    closed: [],
};

export function canMove(from, to) {
    return from !== to && PHASES[from].includes(to);
}

export const CLIP_ENDED = "What happens next?";
export const THEN_WAITING = "Starts when the current take ends.";

// The line under the video: the latest note, or else what the take is waiting on. Entering a wait clears the note,
// so the wait shows; a note that comes later covers it until the note is cleared.
export function statusLine({ phase, note, thenWaiting }) {
    if (note) return note;
    if (thenWaiting) return THEN_WAITING;
    return phase === "clipEnded" ? CLIP_ENDED : "";
}
