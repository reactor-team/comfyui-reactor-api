import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// Grid facts per model, from Reactor Model's node definition (timeline.model_facts).
let MODEL_FACTS = {};

const STYLE = `
.reactor-tl { display: flex; flex-direction: column; gap: 6px; font: 12px sans-serif; color: var(--input-text); }
.reactor-tl-ruler { position: relative; flex: none; height: 16px; border-bottom: 1px solid var(--border-color); }
.reactor-tl-tick { position: absolute; top: 0; height: 100%; border-left: 1px solid var(--border-color); padding-left: 2px; font-size: 10px; opacity: 0.8; }
.reactor-tl-playhead { position: absolute; top: 0; bottom: 0; width: 2px; margin-left: -1px; background: var(--p-primary-color, #4a9eff); pointer-events: none; z-index: 1; }
.reactor-tl-grid { position: absolute; top: 0; bottom: 0; border-left: 1px dotted var(--border-color); opacity: 0.5; pointer-events: none; }
.reactor-tl-track { position: relative; height: 34px; background: var(--comfy-input-bg); border-radius: 4px; overflow: hidden; }
.reactor-tl-beat { position: absolute; top: 3px; bottom: 3px; box-sizing: border-box; padding: 2px 6px; border-radius: 3px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; cursor: pointer; user-select: none; background: var(--comfy-menu-bg); border: 1px solid var(--border-color); }
.reactor-tl-length { opacity: 0.6; }
.reactor-tl-resize { position: absolute; top: 0; right: 0; bottom: 0; width: 6px; cursor: ew-resize; }
.reactor-tl-boundary { position: absolute; top: 0; bottom: 0; width: 12px; margin-left: -6px; cursor: ew-resize; z-index: 1; }
.reactor-tl-boundary.shot { background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--input-text) 45%, transparent), transparent); }
.reactor-tl-boundary.cut::after { content: ""; position: absolute; top: 0; bottom: 0; left: 4px; width: 4px; background: var(--input-text); }
.reactor-tl-boundary.locked { cursor: default; }
.reactor-tl-beat.selected { outline: 2px solid var(--p-primary-color, #4a9eff); }
.reactor-tl-beat.invalid { border-color: #e05252; background: color-mix(in srgb, #e05252 25%, var(--comfy-menu-bg)); }
.reactor-tl-beat.locked { cursor: default; opacity: 0.85; }
.reactor-tl-panel { display: grid; grid-template-columns: auto 1fr auto 1fr; gap: 4px 6px; align-items: center; }
.reactor-tl-panel textarea { grid-column: 1 / -1; min-height: 44px; resize: vertical; }
.reactor-tl-panel textarea, .reactor-tl-panel input, .reactor-tl-panel select, .reactor-tl button { background: var(--comfy-input-bg); color: var(--input-text); border: 1px solid var(--border-color); border-radius: 3px; font: inherit; }
.reactor-tl-row { display: flex; gap: 6px; align-items: center; }
.reactor-tl-note { opacity: 0.75; }
.reactor-tl-problem { color: #e05252; grid-column: 1 / -1; }
.reactor-tl-lane { position: relative; height: 22px; background: var(--comfy-input-bg); border-radius: 4px; overflow: hidden; cursor: copy; }
.reactor-tl-lane-name { position: absolute; left: 4px; top: 4px; font-size: 10px; opacity: 0.55; pointer-events: none; }
.reactor-tl-move { position: absolute; top: 2px; bottom: 2px; box-sizing: border-box; padding: 1px 6px; border-radius: 3px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; cursor: grab; user-select: none; font-size: 11px; background: color-mix(in srgb, var(--p-primary-color, #4a9eff) 30%, var(--comfy-menu-bg)); border: 1px solid var(--border-color); }
.reactor-tl-move.selected { outline: 2px solid var(--p-primary-color, #4a9eff); }
.reactor-tl-lane.locked, .reactor-tl-move.locked { cursor: default; }
.reactor-tl.readonly .reactor-tl-track, .reactor-tl.readonly .reactor-tl-lane { background: transparent; outline: 1px dashed var(--border-color); outline-offset: -1px; }
.reactor-tl.readonly .reactor-tl-beat:not(.invalid), .reactor-tl.readonly .reactor-tl-move:not(.invalid) { filter: grayscale(1); opacity: 0.6; }
.reactor-tl-badge { padding: 1px 6px; border: 1px solid var(--border-color); border-radius: 8px; font-size: 10px; opacity: 0.8; }
.reactor-tl-move.invalid { border-color: #e05252; background: color-mix(in srgb, #e05252 25%, var(--comfy-menu-bg)); }
`;

function el(tag, attrs = {}, ...children) {
    const node = Object.assign(document.createElement(tag), attrs);
    node.append(...children);
    return node;
}

function upstream(node, inputName) {
    const slot = node.inputs?.findIndex((i) => i.name === inputName) ?? -1;
    return slot >= 0 && node.inputs[slot].link != null ? node.getInputNode(slot) : null;
}

function widgetValue(node, name) {
    return node.widgets?.find((w) => w.name === name)?.value;
}

// An input's value: its widget's, or a linked primitive's; undefined when another node computes it.
function inputValue(node, name) {
    const source = upstream(node, name);
    return source ? widgetValue(source, "value") : widgetValue(node, name);
}

// The model's grid facts, or null when the model link does not come straight from a Reactor Model.
function modelFacts(node) {
    const source = upstream(node, "model");
    return source?.comfyClass === "ReactorModel" ? MODEL_FACTS[widgetValue(source, "model")] ?? null : null;
}

// The beats of a Reactor Beat chain ending at `last`, first to last, or "unknown" when the chain can't be read.
function beatsUpTo(last) {
    const beats = [];
    for (let beat = last; beat; beat = upstream(beat, "chain")) {
        if (beat.comfyClass !== "ReactorBeat") return "unknown";
        const prompt = inputValue(beat, "prompt") ?? "(linked prompt)", frames = inputValue(beat, "frames"), kind = widgetValue(beat, "kind");
        if (typeof prompt !== "string" || typeof frames !== "number") return "unknown";
        beats.push({ prompt, frames, cut: kind === "cut", image: upstream(beat, "image") ? "image" : null, moves: widgetValue(beat, "moves")?.moves ?? [] });
    }
    return beats.reverse();
}

// Beats from a connected Reactor Beat chain, first to last: null with no chain, "unknown" when the chain can't be read.
function chainBeats(node) {
    const slot = node.inputs?.findIndex((i) => i.name === "chain") ?? -1;
    return slot < 0 || node.inputs[slot].link == null ? null : beatsUpTo(node.getInputNode(slot));
}

function connectedImageSlots(node) {
    // Autogrow names its sockets "images.image_N"; the backend receives them keyed "image_N".
    return (node.inputs ?? []).filter((i) => /^images\.image_\d+$/.test(i.name) && i.link != null).map((i) => i.name.slice("images.".length));
}

// A scene's chunks nearest `frames` of video, and the frames in its first `n` chunks; mirrors ModelSpec.
const chunksFor = (frames, facts) => frames < facts.first_chunk_frames / 2 ? 0 : 1 + Math.round((frames - facts.first_chunk_frames) / facts.frames_per_chunk);
const framesIn = (n, facts) => n === 0 ? 0 : facts.first_chunk_frames + (n - 1) * facts.frames_per_chunk;

// Each beat's [start, end] frame, placed as timeline.scheduled_chunks places them.
function layout(beats, facts) {
    let total = 0;
    if (!facts) return beats.map((b) => [total, (total += b.frames)]);
    let sceneChunk = 0, sceneFrames = 0, chunk = 0;
    const at = () => sceneFrames + framesIn(chunk - sceneChunk, facts);
    return beats.map((b, i) => {
        if (i && b.cut) [sceneFrames, sceneChunk] = [at(), chunk];
        const start = at();
        total += b.frames;
        chunk = sceneChunk + chunksFor(total - sceneFrames, facts);
        return [start, at()];
    });
}

// A frame moved to the nearest chunk edge, as ModelSpec.chunks_for places a move's edges.
function snapFrame(frame, facts) {
    frame = Math.max(0, Math.round(frame));
    return facts ? framesIn(chunksFor(frame, facts), facts) : frame;
}

// The chunk edge after `frame`, so a move is never shorter than one chunk.
function nextEdge(frame, facts) {
    return facts ? framesIn(chunksFor(frame, facts) + 1, facts) : frame + 1;
}

// A length rounded to whole chunks (at least one).
function snapLength(frames, facts) {
    if (!facts) return Math.max(1, Math.round(frames));
    return Math.max(1, Math.round(frames / facts.frames_per_chunk)) * facts.frames_per_chunk;
}

// A ruler step from the 1-2-5 series that puts at most `ticks` ticks across `length` frames.
function tickStep(length, ticks) {
    const step = 10 ** Math.max(0, Math.floor(Math.log10(length / ticks)) || 0);
    return [1, 2, 5, 10].map((k) => k * step).find((t) => length / t <= ticks) ?? 10 * step;
}

// The facts of the model a Reactor Beat's chain feeds, or null when it doesn't reach a Reactor Timeline.
function downstreamFacts(node) {
    for (let next = node.getOutputNodes?.(0)?.[0]; next; next = next.getOutputNodes?.(0)?.[0]) {
        if (next.comfyClass === "ReactorTimeline") return modelFacts(next);
        if (next.comfyClass !== "ReactorBeat") return null;
    }
    return null;
}

// Fits a Reactor Beat's inputs to the model its chain feeds: whole-chunk lengths, no cuts where the
// model has none, and an image socket labelled when the model won't read it.
function adaptBeat(node) {
    const facts = downstreamFacts(node);
    const opens = upstream(node, "chain") === null;
    const frames = node.widgets?.find((w) => w.name === "frames");
    const kind = node.widgets?.find((w) => w.name === "kind");
    const key = JSON.stringify([facts, opens, kind?.value]);
    if (!frames || !kind || key === node.reactorFacts) return;
    node.reactorFacts = key;
    frames.options.step2 = facts ? facts.frames_per_chunk : 1;
    kind.disabled = !!facts && !facts.supports_cuts && kind.value !== "cut";
    const image = node.inputs?.find((i) => i.name === "image");
    const unread = facts && (facts.images === "none" || (facts.images === "first" && !opens));
    if (image) image.label = unread ? "image (unused)" : undefined;
    node.setDirtyCanvas?.(true, true);
}

// Why the model would reject each beat, keyed by index; mirrors compile_timeline.
function problems(beats, spans, facts, fromChain, slots) {
    const found = new Map();
    const add = (i, why) => found.set(i, [...(found.get(i) ?? []), why]);
    beats.forEach((b, i) => {
        if (!fromChain && b.image && !slots.includes(b.image)) add(i, `${b.image} has no image connected`);
        if (!facts) return;
        if (spans[i][1] <= spans[i][0]) add(i, `shorter than one ${facts.frames_per_chunk}-frame chunk`);
        if (b.cut && i > 0 && !facts.supports_cuts) add(i, "this model has no hard cuts");
        if (b.image && facts.images === "none") add(i, "this model takes no reference images");
        if (b.image && facts.images === "first" && i > 0) add(i, "this model reads an image only on the first beat");
        if (!b.image && facts.image_required && i === 0) add(i, "this model needs an image on the first beat");
    });
    if (facts?.max_scene_chunks) {
        let first = 0;
        for (let i = 1; i <= beats.length; i++) {
            if (i < beats.length && !beats[i].cut) continue;
            const limit = framesIn(facts.max_scene_chunks, facts);
            if (spans[i - 1][1] - spans[first][0] > limit)
                for (let j = first; j < i; j++) add(j, facts.supports_cuts ? `scene is longer than ${limit} frames; add a cut` : `a render on this model lasts at most ${limit} frames`);
            first = i;
        }
    }
    return found;
}

// Why the model would refuse each camera move, keyed by index; mirrors timeline.camera_commands.
function moveProblems(moves, facts) {
    const found = new Map();
    const add = (i, why) => found.set(i, [...(found.get(i) ?? []), why]);
    if (!facts) return found;
    const camera = facts.camera ?? {};
    const chunk = (f) => chunksFor(f, facts);
    moves.forEach((m, i) => {
        if (!Object.keys(camera).length) return add(i, "this model has no camera controls");
        if (!camera[m.lane]) return add(i, `this model has no ${m.lane} camera control`);
        const [first, end] = [chunk(m.start_frame), chunk(m.start_frame + m.frames)];
        if (end <= first) add(i, `shorter than one ${facts.frames_per_chunk}-frame chunk`);
        const overlapping = moves.filter((o, j) => j !== i && chunk(o.start_frame) < end && first < chunk(o.start_frame + o.frames));
        if (overlapping.some((o) => o.lane === m.lane)) add(i, `overlaps another ${m.lane} move`);
        const speed = (o) => o.speed ?? camera[o.lane]?.speed?.idle;
        if (camera[m.lane].speed && overlapping.some((o) => camera[o.lane]?.speed && speed(o) !== speed(m))) add(i, "overlaps a move that turns at a different speed");
    });
    return found;
}

// A chain's beat moves on the timeline's frames: placed from each beat's start, cut at its end, and
// joined where the next beat continues the same move, as compile_timeline sends them.
function chainMoves(beats, spans) {
    const moves = [];
    beats.forEach((b, i) => {
        const [start, end] = spans[i];
        for (const m of b.moves) {
            const from = start + m.start_frame, to = Math.min(from + m.frames, end);
            if (to <= from) continue;
            const before = moves.findLast((o) => o.lane === m.lane);
            if (before && before.value === m.value && before.speed === m.speed && before.start_frame + before.frames === from) before.frames += to - from;
            else moves.push({ ...m, start_frame: from, frames: to - from });
        }
    });
    return moves;
}

const moveLabel = (m) => (typeof m.value === "number" ? `${m.value}°` : String(m.value).replace("_", " ")) + (m.speed != null ? ` ${m.speed}°` : "");

const HINT = "Click a beat to edit it. Drag the line after a beat to set how long it plays; click a line between two beats to switch shot and cut.";

// Drags listen on the window, since each redraw replaces the element the drag started on.
const dragger = (changed) => (onMove, onUp) => {
    const move = (m) => { onMove(m); changed(); };
    const up = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        onUp?.();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
};

// The editor clicked last. The canvas takes keyboard focus, so Delete or Backspace is caught on the
// window before the canvas deletes the node, and goes to that editor while it has a selection.
let deleteTarget = null;
window.addEventListener("pointerdown", (e) => { if (!deleteTarget?.root.contains(e.target)) deleteTarget = null; }, true);
window.addEventListener("keydown", (e) => {
    if ((e.key !== "Delete" && e.key !== "Backspace") || !deleteTarget?.root.isConnected || e.target.closest?.("input, textarea, select")) return;
    if (deleteTarget.remove() === false) return;
    e.stopImmediatePropagation();
    e.preventDefault();
}, true);

// `remove` deletes the editor's selection, returning false when nothing is selected.
function deleteKey(root, remove) {
    root.addEventListener("pointerdown", () => { deleteTarget = { root, remove }; }, true);
}

function drawRuler(ruler, length, pct) {
    ruler.replaceChildren();
    const step = tickStep(length, Math.min(10, Math.floor(ruler.clientWidth / 50) || 10));
    for (let f = 0; f < length; f += step) ruler.append(el("div", { className: "reactor-tl-tick", textContent: f ? `${f}` : "0 frames", style: `left:${pct(f)}` }));
}

// One row per camera lane, plus any lane a move names that this model lacks, so it can be deleted.
// With `edit` null the lanes are read-only; otherwise it adds, selects and drags moves, and a move
// added or resized stays within `edit.limit`.
function drawLanes(lanes, moves, { camera, facts, length, pct, at, found, selectedMove, pastEnd, edit }) {
    lanes.replaceChildren();
    for (const name of [...new Set([...Object.keys(camera), ...moves.map((m) => m.lane)])]) {
        const lane = el("div", { className: `reactor-tl-lane${edit ? "" : " locked"}`, title: edit && camera[name] ? `Click to add a ${name} move` : "" },
                        el("span", { className: "reactor-tl-lane-name", textContent: name }));
        if (edit && camera[name])
            lane.addEventListener("pointerdown", (e) => {
                const start = snapFrame(at(e.clientX), facts);
                if (start >= edit.limit) return;
                const options = camera[name].options;
                const end = Math.max(Math.min(nextEdge(nextEdge(start, facts), facts), edit.limit), nextEdge(start, facts));
                edit.add({ lane: name, value: options.length ? options[0] : 10, start_frame: start, frames: end - start });
            });
        moves.forEach((m, i) => {
            if (m.lane !== name) return;
            const end = m.start_frame + m.frames;
            const block = el("div", {
                className: ["reactor-tl-move", i === selectedMove && "selected", found.has(i) && "invalid", !edit && "locked"].filter(Boolean).join(" "),
                title: [`${m.lane} ${moveLabel(m)}, frames ${m.start_frame} to ${end}`,
                        ...(end > length ? [pastEnd] : []), ...(found.get(i) ?? [])].join("\n"),
                style: `left:${pct(m.start_frame)};width:${pct(m.frames)}`,
            }, moveLabel(m));
            if (edit) {
                block.addEventListener("pointerdown", (e) => {
                    e.stopPropagation();
                    edit.select(i);
                    const from = at(e.clientX), start = m.start_frame;
                    edit.drag((ev) => { m.start_frame = snapFrame(start + at(ev.clientX) - from, facts); });
                });
                const handle = el("div", { className: "reactor-tl-resize", title: "Drag to set how long this move lasts" });
                handle.addEventListener("pointerdown", (e) => {
                    e.stopPropagation();
                    edit.drag((ev) => {
                        m.frames = Math.max(snapFrame(Math.min(at(ev.clientX), edit.limit), facts), nextEdge(m.start_frame, facts)) - m.start_frame;
                    });
                });
                block.append(handle);
            }
            lane.append(block);
        });
        lanes.append(lane);
    }
}

// The selected move's value, placement and delete button.
function drawMovePanel(panel, move, { camera, facts, problems, remove, changed }) {
    const lane = camera[move.lane];
    let pick;
    if (lane?.options.length) {
        pick = el("select", {}, ...lane.options.map((o) => el("option", { value: o, textContent: o.replace("_", " ") })));
        pick.value = move.value;
        pick.addEventListener("change", () => { move.value = pick.value; changed(); });
    } else {
        pick = el("input", { type: "number", min: 0, max: lane?.maximum ?? undefined, step: 1, value: move.value, disabled: !lane });
        pick.addEventListener("change", () => { move.value = Number(pick.value) || 0; changed(); });
    }
    const step = facts?.frames_per_chunk ?? 1;
    const start = el("input", { type: "number", min: 0, step, value: move.start_frame });
    start.addEventListener("change", () => { move.start_frame = snapFrame(Number(start.value) || 0, facts); changed(); });
    const frames = el("input", { type: "number", min: 1, step, value: move.frames });
    frames.addEventListener("change", () => {
        const end = Math.max(snapFrame(move.start_frame + (Number(frames.value) || 0), facts), nextEdge(move.start_frame, facts));
        move.frames = end - move.start_frame;
        changed();
    });
    const button = el("button", { textContent: "Delete move" });
    button.addEventListener("click", remove);
    panel.className = "reactor-tl-panel";
    panel.append(el("span", { textContent: move.lane }), pick, el("span", { textContent: "start frame" }), start,
                 el("span", { textContent: "frames" }), frames);
    if (lane?.speed) {
        const speed = el("input", { type: "number", min: 0, max: lane.speed.maximum ?? undefined, step: 1, value: move.speed ?? "", placeholder: `${lane.speed.idle}` });
        speed.addEventListener("change", () => {
            if (speed.value === "") delete move.speed;
            else move.speed = Number(speed.value);
            changed();
        });
        panel.append(el("span", { textContent: "speed (°)" }), speed);
    }
    panel.append(el("span"), button);
    if (problems) panel.append(el("div", { className: "reactor-tl-problem", textContent: problems.join("; ") }));
}

function createEditor(node, inputName, inputData) {
    let value = structuredClone(inputData?.[1]?.default ?? { beats: [] });
    value.moves ??= [];
    let selected = null;
    let selectedMove = null;
    let signature = "";

    const root = el("div", { className: "reactor-tl" });
    const ruler = el("div", { className: "reactor-tl-ruler" });
    const track = el("div", { className: "reactor-tl-track" });
    const lanes = el("div", { className: "reactor-tl" });
    const playhead = el("div", { className: "reactor-tl-playhead", hidden: true });
    const panel = el("div");
    const footer = el("div", { className: "reactor-tl-row" });
    root.append(ruler, track, lanes, panel, footer);
    deleteKey(root, () => {
        if (selectedMove !== null) value.moves.splice(selectedMove, 1);
        else if (selected !== null && chainBeats(node) === null) value.beats.splice(selected, 1);
        else return false;
        selected = selectedMove = null;
        changed();
    });

    const changed = () => {
        signature = "";
        render();
        node.graph?.setDirtyCanvas(true, true);
    };

    // What the editor draws from besides its own value; a change here, including upstream edits, triggers a redraw.
    const state = () => ({ chain: chainBeats(node), facts: modelFacts(node), slots: connectedImageSlots(node), width: track.clientWidth });

    const drag = dragger(changed);

    function render() {
        const s = state();
        const fromChain = s.chain !== null;
        root.classList.toggle("readonly", fromChain);
        const beats = fromChain ? (s.chain === "unknown" ? [] : s.chain) : value.beats;
        const spans = layout(beats, s.facts);
        const length = spans.length ? Math.max(spans.at(-1)[1], s.facts?.frames_per_chunk ?? 1) : 240;
        const found = problems(beats, spans, s.facts, fromChain, s.slots);
        const pct = (frame) => `${(100 * frame) / length}%`;
        const at = (clientX) => {
            const rect = track.getBoundingClientRect();
            return ((clientX - rect.left) / rect.width) * length;
        };
        const cuts = !s.facts || s.facts.supports_cuts;
        if (selected !== null && selected >= beats.length) selected = null;
        if (fromChain || (selectedMove !== null && selectedMove >= value.moves.length)) selectedMove = null;
        const camera = s.facts?.camera ?? {};
        // A chain's moves live on its beats, so they are drawn here read-only.
        const moves = fromChain ? chainMoves(beats, spans) : value.moves;
        const moveFound = moveProblems(moves, s.facts);

        drawRuler(ruler, length, pct);

        track.replaceChildren(playhead);
        if (s.facts) {
            // Chunk lines thin to every `every`th chunk so they stay at least 6px apart at any length.
            const every = Math.max(1, Math.ceil((6 * length) / ((s.width || 400) * s.facts.frames_per_chunk)));
            // A cut opens a scene whose chunks count afresh, as in layout.
            const scenes = [0, ...spans.filter((_, i) => i && beats[i].cut).map(([start]) => start), length];
            for (let k = 0; k < scenes.length - 1; k++)
                for (let n = every, f; (f = scenes[k] + framesIn(n, s.facts)) < scenes[k + 1]; n += every)
                    track.append(el("div", { className: "reactor-tl-grid", style: `left:${pct(f)}` }));
        }
        beats.forEach((b, i) => {
            const [start, end] = spans[i];
            const block = el("div", {
                className: ["reactor-tl-beat", i === selected && "selected", found.has(i) && "invalid", fromChain && "locked"].filter(Boolean).join(" "),
                title: [`frames ${start} to ${end}`, ...(found.get(i) ?? [])].join("\n"),
                style: `left:${pct(start)};width:${pct(Math.max(0, end - start))}`,
            }, el("span", { textContent: b.prompt || "(empty prompt)" }), el("span", { className: "reactor-tl-length", textContent: ` ${end - start}f` }));
            block.addEventListener("pointerdown", (e) => {
                e.stopPropagation();
                selected = i;
                selectedMove = null;
                changed();
            });
            // The last beat's end has no boundary to drag, so it gets a handle of its own.
            if (!fromChain && i === beats.length - 1) {
                const handle = el("div", { className: "reactor-tl-resize", title: "Drag to set how long this beat plays" });
                handle.addEventListener("pointerdown", (e) => {
                    e.stopPropagation();
                    drag((m) => { value.beats[i].frames = snapLength(at(m.clientX) - start, s.facts); });
                });
                block.append(handle);
            }
            track.append(block);
            if (i === 0) return;
            // Shot or cut is how this beat enters from the one before, so it is drawn on their boundary.
            // A cut stays drawn on a model without cuts, so the flagged beat can be clicked back to a shot.
            const kind = b.cut ? "cut" : "shot";
            const boundary = el("div", {
                className: `reactor-tl-boundary ${kind}${fromChain ? " locked" : ""}`,
                title: (kind === "cut" ? "Cut: starts a fresh scene." + (fromChain ? "" : " Click for a shot.")
                    : "Shot: blends from the beat before." + (fromChain ? "" : cuts ? " Click for a cut." : " This model has no hard cuts."))
                    + (fromChain ? "" : "\nDrag to change how long the beat before plays."),
                style: `left:${pct(start)}`,
            });
            // Dragging the boundary resizes the beat before it; a click without a drag switches shot and cut.
            if (!fromChain)
                boundary.addEventListener("pointerdown", (e) => {
                    e.stopPropagation();
                    let moved = false;
                    drag((m) => {
                        moved ||= Math.abs(m.clientX - e.clientX) > 3;
                        if (moved) value.beats[i - 1].frames = snapLength(at(m.clientX) - spans[i - 1][0], s.facts);
                    }, () => {
                        if (moved || (!cuts && !value.beats[i].cut)) return;
                        value.beats[i].cut = !value.beats[i].cut;
                        changed();
                    });
                });
            track.append(boundary);
        });

        drawLanes(lanes, moves, {
            camera, facts: s.facts, length, pct, at, found: moveFound, selectedMove, pastEnd: "runs past the end; cut at the last chunk",
            edit: fromChain ? null : {
                limit: Infinity,
                drag,
                select: (i) => { selectedMove = i; selected = null; changed(); },
                add: (move) => { value.moves.push(move); selectedMove = value.moves.length - 1; selected = null; changed(); },
            },
        });

        panel.replaceChildren();
        panel.className = "reactor-tl-panel";
        if (selectedMove !== null) {
            drawMovePanel(panel, value.moves[selectedMove], {
                camera, facts: s.facts, problems: moveFound.get(selectedMove), changed,
                remove: () => { value.moves.splice(selectedMove, 1); selectedMove = null; changed(); },
            });
        } else if (fromChain) {
            panel.className = "reactor-tl-note";
            panel.textContent = s.chain === "unknown"
                ? "Beats come from the chain input, which this editor can't read. Edit them upstream."
                : selected !== null ? beats[selected].prompt || "(empty prompt)" : "Beats come from the chain input, in chain order. Edit them and their camera moves on their Reactor Beat nodes.";
        } else if (selected !== null) {
            const beat = value.beats[selected];
            const prompt = el("textarea", { value: beat.prompt, placeholder: "Prompt" });
            prompt.addEventListener("input", () => { beat.prompt = prompt.value; node.graph?.setDirtyCanvas(true, false); });
            prompt.addEventListener("change", changed);
            const frames = el("input", { type: "number", min: 1, step: s.facts?.frames_per_chunk ?? 1, value: beat.frames });
            frames.addEventListener("change", () => { beat.frames = snapLength(Number(frames.value) || 0, s.facts); changed(); });
            const kind = el("select", { disabled: selected === 0 || (!cuts && !beat.cut) },
                el("option", { value: "shot", textContent: "shot (blend in)" }), el("option", { value: "cut", textContent: "cut (new scene)" }));
            kind.value = beat.cut && selected > 0 ? "cut" : "shot";
            kind.addEventListener("change", () => { beat.cut = kind.value === "cut"; changed(); });
            const slots = [...new Set([...s.slots, ...(beat.image ? [beat.image] : [])])];
            const image = el("select", {}, el("option", { value: "", textContent: "no image" }), ...slots.map((n) => el("option", { value: n, textContent: n })));
            image.value = beat.image ?? "";
            image.addEventListener("change", () => { beat.image = image.value || null; changed(); });
            const remove = el("button", { textContent: "Delete beat" });
            remove.addEventListener("click", () => { value.beats.splice(selected, 1); selected = null; changed(); });
            panel.append(prompt, el("span", { textContent: "frames" }), frames, el("span", { textContent: "enters as" }), kind,
                         el("span", { textContent: "image" }), image, el("span"), remove);
        } else {
            panel.className = "reactor-tl-note";
            panel.textContent = (beats.length ? HINT : "No beats yet. Add one to start the timeline.")
                + (Object.keys(camera).length ? " Click a camera lane to add a move; moves on different lanes can overlap." : "");
        }
        if (selectedMove === null && found.has(selected)) panel.append(el("div", { className: "reactor-tl-problem", textContent: found.get(selected).join("; ") }));

        footer.replaceChildren();
        if (!fromChain) {
            const add = el("button", { textContent: "+ Beat" });
            add.addEventListener("click", () => {
                value.beats.push({ prompt: "", frames: snapLength(120, s.facts), cut: false, image: null });
                selected = value.beats.length - 1;
                changed();
            });
            footer.append(add);
        } else {
            footer.append(el("span", { className: "reactor-tl-badge", textContent: "Read-only", title: "Beats come from the chain input; edit them on their Reactor Beat nodes." }));
        }
        if (s.chain !== "unknown") footer.append(el("span", { className: "reactor-tl-note", textContent: s.facts ? `${length} frames, ${(length / s.facts.fps).toFixed(1)} s at ${s.facts.fps} fps` : `${length} frames` }));
        if (!s.facts) footer.append(el("span", { className: "reactor-tl-note", textContent: "Connect a Reactor Model to snap to its chunks." }));
    }

    const widget = node.addDOMWidget(inputName, "REACTOR_BEAT_EDITOR", root, {
        getValue: () => value,
        setValue: (v) => {
            value = v && Array.isArray(v.beats) ? structuredClone(v) : { beats: [] };
            value.moves = Array.isArray(value.moves) ? value.moves : [];
            selected = null;
            selectedMove = null;
            changed();
        },
        getMinHeight: () => 190 + 26 * new Set([...Object.keys(modelFacts(node)?.camera ?? {}), ...value.moves.map((m) => m.lane)]).size,
        onDraw: () => {
            const s = state();
            const next = JSON.stringify([s.chain, s.facts, s.slots, s.width]);
            if (next !== signature) {
                signature = next;
                render();
            }
        },
    });
    // A Reactor Render fed by this timeline reports frames rendered over frames planned, and the
    // planned length is this ruler's length, so the ratio places the playhead.
    const rendersThis = (id) => (node.outputs?.[0]?.links ?? []).some((l) => String(node.graph?.links[l]?.target_id) === String(id));
    const onProgress = ({ detail }) => {
        if (!rendersThis(detail.node)) return;
        playhead.hidden = false;
        playhead.style.left = `${(100 * detail.value) / detail.max}%`;
    };
    const onStop = ({ detail }) => {
        if (!rendersThis(detail?.node ?? detail)) playhead.hidden = true;
    };
    api.addEventListener("progress", onProgress);
    api.addEventListener("executing", onStop);
    api.addEventListener("execution_interrupted", onStop);
    const onRemove = widget.onRemove;
    widget.onRemove = function () {
        api.removeEventListener("progress", onProgress);
        api.removeEventListener("executing", onStop);
        api.removeEventListener("execution_interrupted", onStop);
        onRemove?.call(this);
    };
    render();
    return { widget };
}

// A Reactor Beat's camera lanes: moves counted from the beat's own start, on the lanes of the model
// its chain feeds, and ending with the beat.
function createMoveEditor(node, inputName) {
    let value = { moves: [] };
    let selectedMove = null;
    let signature = "";
    const root = el("div", { className: "reactor-tl" });
    const ruler = el("div", { className: "reactor-tl-ruler" });
    const lanes = el("div", { className: "reactor-tl" });
    const panel = el("div");
    root.append(ruler, lanes, panel);
    deleteKey(root, () => {
        if (selectedMove === null) return false;
        value.moves.splice(selectedMove, 1);
        selectedMove = null;
        changed();
    });

    const changed = () => {
        signature = "";
        render();
        node.graph?.setDirtyCanvas(true, true);
    };
    const drag = dragger(changed);
    const state = () => ({ facts: downstreamFacts(node), beats: beatsUpTo(node), width: lanes.clientWidth });

    function render() {
        const s = state();
        const camera = s.facts?.camera ?? {};
        const shown = Object.keys(camera).length > 0 || value.moves.length > 0;
        ruler.hidden = !shown;
        if (selectedMove !== null && selectedMove >= value.moves.length) selectedMove = null;
        // Past a scene's first chunk every chunk is full length, so a beat that doesn't open its scene
        // snaps its moves to a grid of whole chunks from its start.
        const opens = s.beats === "unknown" || s.beats.length === 1 || s.beats.at(-1).cut;
        const facts = s.facts && (opens ? s.facts : { ...s.facts, first_chunk_frames: s.facts.frames_per_chunk });
        const span = s.facts && s.beats !== "unknown" ? layout(s.beats, s.facts).at(-1) : null;
        const length = span && span[1] > span[0] ? span[1] - span[0] : Math.max(1, widgetValue(node, "frames") ?? 1);
        const pct = (frame) => `${(100 * frame) / length}%`;
        const at = (clientX) => {
            const rect = lanes.getBoundingClientRect();
            return ((clientX - rect.left) / rect.width) * length;
        };
        const found = moveProblems(value.moves, facts);

        drawRuler(ruler, length, pct);
        drawLanes(lanes, value.moves, {
            camera, facts, length, pct, at, found, selectedMove, pastEnd: "runs past the beat's end; cut there",
            edit: {
                limit: length,
                drag,
                select: (i) => { selectedMove = i; changed(); },
                add: (move) => { value.moves.push(move); selectedMove = value.moves.length - 1; changed(); },
            },
        });

        panel.replaceChildren();
        if (selectedMove !== null) {
            drawMovePanel(panel, value.moves[selectedMove], {
                camera, facts, problems: found.get(selectedMove), changed,
                remove: () => { value.moves.splice(selectedMove, 1); selectedMove = null; changed(); },
            });
        } else {
            panel.className = "reactor-tl-note";
            panel.textContent = !s.facts ? "Feed this chain to a Reactor Timeline to add camera moves."
                : !shown ? "This model has no camera controls."
                : "Click a lane to add a camera move. A move ends with this beat; add the same move at the start of the next beat to keep it going.";
        }
    }

    const widget = node.addDOMWidget(inputName, "REACTOR_MOVE_EDITOR", root, {
        getValue: () => value,
        setValue: (v) => {
            value = { moves: Array.isArray(v?.moves) ? structuredClone(v.moves) : [] };
            selectedMove = null;
            changed();
        },
        getMinHeight: () => 60 + 28 * new Set([...Object.keys(downstreamFacts(node)?.camera ?? {}), ...value.moves.map((m) => m.lane)]).size,
        onDraw: () => {
            const s = state();
            const next = JSON.stringify([s.facts, s.beats, s.width]);
            if (next !== signature) {
                signature = next;
                render();
            }
        },
    });
    render();
    return widget;
}

app.registerExtension({
    name: "Reactor.TimelineEditor",
    setup() {
        document.head.append(el("style", { textContent: STYLE }));
    },
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name === "ReactorModel") MODEL_FACTS = nodeData.input.required.model[1].model_facts ?? {};
        if (nodeData.name === "ReactorBeat") {
            const onNodeCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function (...args) {
                const result = onNodeCreated?.apply(this, args);
                const frames = this.widgets?.find((w) => w.name === "frames");
                const callback = frames?.callback;
                if (frames)
                    frames.callback = (value, ...rest) => {
                        const facts = downstreamFacts(this);
                        if (facts) frames.value = snapLength(value, facts);
                        return callback?.call(frames, frames.value, ...rest);
                    };
                return result;
            };
            const onDrawForeground = nodeType.prototype.onDrawForeground;
            nodeType.prototype.onDrawForeground = function (...args) {
                adaptBeat(this);
                return onDrawForeground?.apply(this, args);
            };
        }
    },
    getCustomWidgets() {
        return {
            REACTOR_BEAT_EDITOR: (node, inputName, inputData) => createEditor(node, inputName, inputData),
            REACTOR_MOVE_EDITOR: (node, inputName) => ({ widget: createMoveEditor(node, inputName) }),
        };
    },
});
