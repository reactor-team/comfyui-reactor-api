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

// A clip model's clips as the Realtime window sees them: the clip playing, the model's queue of clips built and still
// to play (`playout`) and still to build (`generation`), and the prompts applied here that the model hasn't queued yet.
export class ClipQueue {
    last = null;             // the clip that plays last so far, which a continuation carries on
    playing = null;          // the clip playing now
    queued = { generation: [], playout: [] };
    unsent = [];             // prompts applied here and not yet queued by the model, in order
    #built = new Map();      // a clip the model has finished with to whether it built
    #waiters = new Map();    // a clip to the continuation waiting for it to build; prompts go out one at a time

    // Takes in a message from the model.
    seen(message) {
        const type = message?.type;
        const clip = message?.data?.clip?.clip_id;
        if (clip) {
            // The run queues the opening clip itself, so the window learns its id from the clips it sees.
            if (!this.last && (type === "clip_generated" || type === "clip_started")) this.last = clip;
            if (type === "clip_generated" || type === "clip_failed") {
                this.#built.set(clip, type === "clip_generated");
                this.#waiters.get(clip)?.(type === "clip_generated");
                this.#waiters.delete(clip);
            }
            if (type === "clip_started") this.playing = message.data.clip;
            else if ((type === "clip_finished" || type === "clip_stopped") && this.playing?.clip_id === clip) this.playing = null;
        }
        if (type === "queue_update") {
            this.queued = { generation: message.data?.generation ?? [], playout: message.data?.playout ?? [] };
        }
    }

    // Queues a prompt applied here, to be sent in turn.
    apply(prompt, continuing) {
        const entry = { prompt, continuing, waiting: false };
        this.unsent.push(entry);
        return entry;
    }

    // The clip `entry` continues, once it has built, or null when it starts fresh. `failed` is true when the clip
    // it would continue failed to build.
    async anchor(entry) {
        if (!entry.continuing || !this.last) return { anchor: null, failed: false };
        const anchor = this.last;
        let ok = this.#built.get(anchor);
        if (ok === undefined) {
            entry.waiting = true;
            ok = await new Promise((resolve) => this.#waiters.set(anchor, resolve));
        }
        return ok ? { anchor, failed: false } : { anchor: null, failed: true };
    }

    // `entry`'s prompt has gone out. A clip the model queues from now on with the same prompt is the one it made for
    // it, so the entry stops being listed, even before the model answers.
    dispatch(entry) {
        entry.known = new Set([...this.queued.generation, ...this.queued.playout].map((c) => c.clip_id));
    }

    // The model answered `entry`'s prompt, with the clip it queued for it when it took it.
    sent(entry, clip) {
        this.unsent.splice(this.unsent.indexOf(entry), 1);
        if (!clip) return;
        this.last = clip.clip_id;
        if (![...this.queued.generation, ...this.queued.playout].some((c) => c.clip_id === clip.clip_id)) this.queued.generation.push(clip);
    }

    #queuedFor(entry) {
        return entry.known && [...this.queued.generation, ...this.queued.playout]
            .some((c) => c.prompt === entry.prompt && !entry.known.has(c.clip_id));
    }

    // Nothing is playing next: no clip built, building, or applied here.
    get empty() {
        return !this.queued.generation.length && !this.queued.playout.length && !this.unsent.length;
    }

    // Every clip in play order, with its stage: Playing, Ready, Building, then Waiting (for the clip it continues to
    // build) or Sending.
    rows() {
        const { playing, queued } = this;
        return [
            ...(playing ? [{ stage: "Playing", continues: !!playing.continue_from_clip_id, prompt: playing.prompt }] : []),
            ...queued.playout.filter((c) => c.clip_id !== playing?.clip_id)
                .map((c) => ({ stage: "Ready", continues: !!c.continue_from_clip_id, prompt: c.prompt })),
            ...queued.generation.map((c) => ({ stage: "Building", continues: !!c.continue_from_clip_id, prompt: c.prompt })),
            ...this.unsent.filter((e) => !this.#queuedFor(e))
                .map((e) => ({ stage: e.waiting ? "Waiting" : "Sending", continues: e.continuing, prompt: e.prompt })),
        ];
    }
}
