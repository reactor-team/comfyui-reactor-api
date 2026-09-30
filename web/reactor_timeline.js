import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

// Grid facts per model, from the chain nodes' definitions (timeline.model_facts).
let MODEL_FACTS = {};

const STYLE = `
.reactor-tl { display: flex; flex-direction: column; gap: 6px; font: 12px sans-serif; color: var(--input-text); }
.reactor-tl[hidden] { display: none; }
.reactor-tl > * { flex-shrink: 0; }
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
.reactor-tl-beat.selected { outline: 2px solid var(--p-primary-color, #4a9eff); }
.reactor-tl-beat.invalid { border-color: #e05252; background: color-mix(in srgb, #e05252 25%, var(--comfy-menu-bg)); }
.reactor-tl-panel { display: grid; grid-template-columns: auto 1fr auto 1fr; gap: 4px 6px; align-items: center; }
.reactor-tl-panel input, .reactor-tl-panel select, .reactor-tl button { background: var(--comfy-input-bg); color: var(--input-text); border: 1px solid var(--border-color); border-radius: 3px; font: inherit; }
.reactor-tl-row { display: flex; gap: 6px; align-items: center; }
.reactor-tl-note { opacity: 0.75; }
.reactor-tl-problem { color: #e05252; grid-column: 1 / -1; }
.reactor-tl-lane { position: relative; height: 22px; background: var(--comfy-input-bg); border-radius: 4px; overflow: hidden; cursor: copy; }
.reactor-tl-lane-name { position: absolute; left: 4px; top: 4px; font-size: 10px; opacity: 0.55; pointer-events: none; }
.reactor-tl-move { position: absolute; top: 2px; bottom: 2px; box-sizing: border-box; padding: 1px 6px; border-radius: 3px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; cursor: grab; user-select: none; font-size: 11px; background: color-mix(in srgb, var(--p-primary-color, #4a9eff) 30%, var(--comfy-menu-bg)); border: 1px solid var(--border-color); }
.reactor-tl-move.selected { outline: 2px solid var(--p-primary-color, #4a9eff); }
.reactor-tl-lane.locked, .reactor-tl-move.locked { cursor: default; }
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

// The chains a Reactor Chain Join plays, in order.
function joinedChains(join) {
    return (join.inputs ?? []).map((input, slot) => [input, slot])
        .filter(([input]) => input.name.startsWith("chains.") && input.link != null).map(([, slot]) => join.getInputNode(slot));
}

// Whether a node type is a model's chain beat, such as ReactorHeliosChain.
const isBeat = (name) => /^Reactor\w+Chain$/.test(name);

// The model a node is for: the one its chain's first link picks, or null when the chain can't be
// followed there. Past a Reactor Chain Join it is the first chain's, since the join requires every chain's to match.
function chainModel(node) {
    for (let n = node; n; n = n.comfyClass === "ReactorChainJoin" ? joinedChains(n)[0] : upstream(n, "chain"))
        if (isBeat(n.comfyClass) && !upstream(n, "chain")) return widgetValue(n, "model") ?? n.reactorModels?.[0] ?? null;
    return null;
}

// The grid facts of the model a node is for, or null when they can't be read.
function modelFacts(node) {
    return MODEL_FACTS[chainModel(node)] ?? null;
}

// Shows an optional socket only while the node reads it; a linked socket stays so its link isn't dropped.
function showInput(node, name, type, shown) {
    const slot = node.inputs?.findIndex((i) => i.name === name) ?? -1;
    if (!shown && slot >= 0 && node.inputs[slot].link == null) node.removeInput(slot);
    else if (shown && slot < 0) node.addInput(name, type);
    else return false;
    return true;
}

// Fits a node's optional sockets to what it reads, as [name, type, shown]; a linked socket it won't read is labelled.
function fitInputs(node, sockets) {
    let changed = false;
    for (const [name, type, shown] of sockets) {
        changed = showInput(node, name, type, shown) || changed;
        const input = node.inputs?.find((i) => i.name === name);
        if (input) input.label = shown ? undefined : `${name} (unused)`;
    }
    return changed;
}

// The beats of a chain ending at `last`, first to last, or "unknown" when the chain can't be read.
function beatsUpTo(last) {
    const beats = [];
    for (let beat = last; beat; beat = upstream(beat, "chain")) {
        if (beat.comfyClass === "ReactorChainJoin") {
            const parts = joinedChains(beat).map(beatsUpTo);
            return parts.includes("unknown") ? "unknown" : [...parts.flat(), ...beats.reverse()];
        }
        if (!isBeat(beat.comfyClass)) return "unknown";
        // A model without a prompt has no prompt input, and its beats are drawn as "(no prompt)".
        const prompted = beat.inputs?.some((i) => i.name === "prompt") || beat.widgets?.some((w) => w.name === "prompt");
        const prompt = prompted ? inputValue(beat, "prompt") ?? "(linked prompt)" : "(no prompt)";
        const frames = inputValue(beat, "frames"), kind = widgetValue(beat, "kind");
        if (typeof prompt !== "string" || typeof frames !== "number") return "unknown";
        beats.push({ prompt, frames, cut: kind === "cut", image: upstream(beat, "image") ? "image" : null, moves: widgetValue(beat, "moves")?.moves ?? [],
                     references: connectedSlots(beat, "references") });
    }
    return beats.reverse();
}

// Beats from a connected chain, first to last: null with no chain, "unknown" when the chain can't be read.
function chainBeats(node) {
    const slot = node.inputs?.findIndex((i) => i.name === "chain") ?? -1;
    return slot < 0 || node.inputs[slot].link == null ? null : beatsUpTo(node.getInputNode(slot));
}

// Shows an Autogrow group's sockets, such as images.image_0, only while the node reads them; linked ones stay.
function showGrown(node, group, first, type, shown) {
    const sockets = (node.inputs ?? []).map((input, slot) => [input, slot]).filter(([input]) => input.name.startsWith(`${group}.`));
    const count = node.inputs?.length;
    if (!shown) for (const [input, slot] of sockets.reverse()) { if (input.link == null) node.removeInput(slot); }
    else if (!sockets.length) node.addInput(`${group}.${first}`, type);
    return node.inputs?.length !== count;
}

// An editor's natural height: its visible rows and the gaps between them. Its rows neither stretch nor shrink.
// Null until the editor is laid out, since an element that isn't rendered measures 0.
function contentHeight(root) {
    if (root.hidden) return 0;
    if (!root.getClientRects().length) return null;
    const rows = [...root.children].filter((c) => !c.hidden);
    return rows.reduce((h, c) => h + c.offsetHeight, 0) + 6 * Math.max(0, rows.length - 1);
}

// Sizes a node to what its inputs and editors need, so an editor never clips or leaves a gap.
function fitNode(node) {
    const height = node.computeSize()[1];
    if (Math.abs(height - node.size[1]) > 1) node.setSize([node.size[0], height]);
}

function connectedSlots(node, group) {
    // Autogrow names its sockets "images.image_N"; the backend receives them keyed "image_N".
    return (node.inputs ?? []).filter((i) => i.name.startsWith(`${group}.`) && i.link != null).map((i) => i.name.slice(group.length + 1));
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

// A ruler step from the 1-2-5 series that puts at most `ticks` ticks across `length` frames.
function tickStep(length, ticks) {
    const step = 10 ** Math.max(0, Math.floor(Math.log10(length / ticks)) || 0);
    return [1, 2, 5, 10].map((k) => k * step).find((t) => length / t <= ticks) ?? 10 * step;
}

// Fits a chain beat's inputs to its beat: the model and start settings only on the first link, and image
// and video sockets only where the model reads them.
function adaptBeat(node) {
    const facts = modelFacts(node);
    const opens = upstream(node, "chain") === null;
    // A beat's node type already has only the inputs its models take; what's left is which it reads on this beat.
    const has = (name) => node.reactorDeclared?.has(name);
    const reads = (taken) => !facts || taken === "any" || (taken === "first" && opens);
    let changed = fitInputs(node, [["image", "IMAGE", reads(facts?.images)], ["video", "VIDEO", reads(facts?.videos)]].filter(([name]) => has(name)))
        | (has("references") && showGrown(node, "references", "reference_0", "REACTOR_REFERENCE", true));
    for (const widget of node.widgets ?? [])
        if (node.reactorStart.has(widget.name) && widget.hidden !== !opens) {
            widget.hidden = !opens;
            changed = true;
        }
    if (changed) {
        fitNode(node);
        node.setDirtyCanvas?.(true, true);
    }
}

// Why the model would reject each beat, keyed by index; mirrors compile_timeline.
function problems(beats, spans, facts) {
    const found = new Map();
    const add = (i, why) => found.set(i, [...(found.get(i) ?? []), why]);
    beats.forEach((b, i) => {
        if (!facts) return;
        if (b.references?.length && !facts.references) add(i, "this model takes no references");
        if (b.references?.length > 3) add(i, "a beat holds at most 3 references");
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
                for (let j = first; j < i; j++) add(j, facts.supports_cuts ? `longer than ${limit} frames without a cut; add one` : `a render on this model lasts at most ${limit} frames`);
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
// With `edit` null the lanes are read-only; otherwise it adds, selects and drags the moves, and a
// move added or resized stays within `edit.limit`.
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

// A chain drawn on a frame ruler: its beats as they will play, snapped to the model's chunks, and its
// camera moves on the lanes underneath. It only shows the chain, so it saves nothing.
function createViewer(node) {
    let selected = null;
    let signature = "";
    // Measured on each draw, since the viewer may not be laid out when it renders; the default fits an empty viewer until then.
    let height = 120;
    let laidOut = false;

    const root = el("div", { className: "reactor-tl" });
    const ruler = el("div", { className: "reactor-tl-ruler" });
    const track = el("div", { className: "reactor-tl-track" });
    const lanes = el("div", { className: "reactor-tl" });
    const playhead = el("div", { className: "reactor-tl-playhead", hidden: true });
    const panel = el("div");
    const footer = el("div", { className: "reactor-tl-row" });
    root.append(ruler, track, lanes, panel, footer);

    // What the viewer draws from; a change here, including edits upstream, triggers a redraw.
    const state = () => ({ chain: chainBeats(node), facts: modelFacts(node), width: track.clientWidth });

    function render() {
        const s = state();
        const beats = Array.isArray(s.chain) ? s.chain : [];
        const spans = layout(beats, s.facts);
        const length = spans.length ? Math.max(spans.at(-1)[1], s.facts?.frames_per_chunk ?? 1) : 240;
        const found = problems(beats, spans, s.facts);
        const pct = (frame) => `${(100 * frame) / length}%`;
        if (selected !== null && selected >= beats.length) selected = null;
        const camera = s.facts?.camera ?? {};
        const moves = chainMoves(beats, spans);

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
                className: ["reactor-tl-beat", i === selected && "selected", found.has(i) && "invalid"].filter(Boolean).join(" "),
                title: [`frames ${start} to ${end}`, ...(found.get(i) ?? [])].join("\n"),
                style: `left:${pct(start)};width:${pct(Math.max(0, end - start))}`,
            }, el("span", { textContent: b.prompt || "(empty prompt)" }), el("span", { className: "reactor-tl-length", textContent: ` ${end - start}f` }));
            block.addEventListener("pointerdown", (e) => {
                e.stopPropagation();
                selected = selected === i ? null : i;
                render();
            });
            track.append(block);
            // Shot or cut is how this beat enters from the one before, so it is drawn on their boundary.
            if (i) track.append(el("div", { className: `reactor-tl-boundary ${b.cut ? "cut" : "shot"} locked`, style: `left:${pct(start)}`,
                                            title: b.cut ? "Cut: starts fresh, with no blend." : "Shot: blends from the beat before." }));
        });

        drawLanes(lanes, moves, { camera, facts: s.facts, length, pct, found: moveProblems(moves, s.facts), selectedMove: null,
                                  pastEnd: "runs past the end; cut at the last chunk", edit: null });

        panel.replaceChildren();
        panel.className = "reactor-tl-note";
        if (selected !== null) {
            const [start, end] = spans[selected];
            panel.textContent = `Beat ${selected + 1}, frames ${start} to ${end}: ${beats[selected].prompt || "(empty prompt)"}`;
            if (found.has(selected)) panel.append(el("div", { className: "reactor-tl-problem", textContent: found.get(selected).join("; ") }));
        } else {
            panel.textContent = s.chain === null ? "Connect a chain to draw it."
                : s.chain === "unknown" ? "This timeline can't read the chain, so its beats aren't drawn."
                : "Click a beat to read its prompt. Edit a beat and its camera moves on its chain node.";
        }

        footer.replaceChildren();
        if (Array.isArray(s.chain)) footer.append(el("span", { className: "reactor-tl-note", textContent: s.facts ? `${length} frames, ${(length / s.facts.fps).toFixed(1)} s at ${s.facts.fps} fps` : `${length} frames` }));
    }

    const widget = node.addDOMWidget("timeline", "REACTOR_TIMELINE", root, {
        serialize: false,
        getMinHeight: () => height && height + 2 * widget.margin,
        // The Parameters panel draws a widget by setting its width to the panel's and never restores it.
        hideInPanel: true,
        onDraw: () => {
            const s = state();
            const next = JSON.stringify([s.chain, s.facts, s.width]);
            if (next !== signature) {
                signature = next;
                render();
            }
            // Fit the node when its content changes, and leave a size the user dragged alone; the first
            // measure only grows the node, so a loaded workflow keeps its saved height.
            const measured = contentHeight(root);
            if (measured !== null && measured !== height) {
                height = measured;
                if (laidOut) fitNode(node);
                else node.expandToFitContent();
                laidOut = true;
            }
        },
    });
    // A Reactor Render of the same chain reports frames rendered over frames planned, and the planned
    // length is this ruler's length, so the ratio places the playhead.
    const rendersThis = (id) => {
        const render = node.graph?.getNodeById(id), chain = upstream(node, "chain");
        return !!chain && render?.comfyClass === "ReactorRender" && upstream(render, "chain") === chain;
    };
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
    return widget;
}

// A chain beat's camera lanes: moves counted from the beat's own start, on the lanes of the model
// its chain feeds, and ending with the beat.
function createMoveEditor(node, inputName) {
    let value = { moves: [] };
    let selectedMove = null;
    let signature = "";
    // Measured on each draw, since the editor may not be laid out when it renders; the default fits an empty editor until then.
    let height = 120;
    let laidOut = false;
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
    const state = () => ({ facts: modelFacts(node), beats: beatsUpTo(node), width: lanes.clientWidth });

    function render() {
        const s = state();
        const camera = s.facts?.camera ?? {};
        const shown = Object.keys(camera).length > 0 || value.moves.length > 0;
        ruler.hidden = !shown;
        // A model without camera controls gets no move editor at all.
        root.hidden = !!s.facts && !shown;
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
            panel.textContent = !s.facts ? "Start this chain from a first link to add camera moves."
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
        getMinHeight: () => height && height + 2 * widget.margin,
        // The Parameters panel draws a widget by setting its width to the panel's and never restores it.
        hideInPanel: true,
        onDraw: () => {
            const s = state();
            const next = JSON.stringify([s.facts, s.beats, s.width]);
            if (next !== signature) {
                signature = next;
                render();
            }
            // Fit the node when its content changes, and leave a size the user dragged alone; the first
            // measure only grows the node, so a loaded workflow keeps its saved height.
            const measured = contentHeight(root);
            if (measured !== null && measured !== height) {
                height = measured;
                if (laidOut) fitNode(node);
                else node.expandToFitContent();
                laidOut = true;
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
        if (nodeData.name === "ReactorTimeline") {
            const onNodeCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function (...args) {
                const result = onNodeCreated?.apply(this, args);
                createViewer(this);
                return result;
            };
        }
        if (nodeData.name === "ReactorRender") {
            const onDrawForeground = nodeType.prototype.onDrawForeground;
            nodeType.prototype.onDrawForeground = function (...args) {
                // The status line otherwise shares the node's spare height with the preview.
                const status = this.widgets?.find((w) => w.name === "$$node-text-preview");
                if (status && !status.options.getMaxHeight) status.options.getMaxHeight = status.options.getMinHeight;
                return onDrawForeground?.apply(this, args);
            };
        }
        if (nodeData.name === "ReactorChainJoin") {
            const onConnectInput = nodeType.prototype.onConnectInput;
            nodeType.prototype.onConnectInput = function (slot, type, output, source, ...rest) {
                // Refuse a chain for another model here, rather than only when the workflow runs.
                const model = chainModel(source);
                const clash = model && this.inputs.some((input, i) => {
                    const other = i !== slot && input.name.startsWith("chains.") && input.link != null && chainModel(this.getInputNode(i));
                    return other && other !== model;
                });
                if (clash) {
                    app.extensionManager.toast.add({ severity: "warn", summary: "Reactor Chain Join", life: 5000,
                        detail: "That chain is for a different model than the chains already joined; join chains made for the same model." });
                    return false;
                }
                return onConnectInput?.call(this, slot, type, output, source, ...rest);
            };
        }
        if (isBeat(nodeData.name)) {
            nodeType.prototype.reactorDeclared = new Set([...Object.keys(nodeData.input?.required ?? {}), ...Object.keys(nodeData.input?.optional ?? {})]);
            const chain = nodeData.input?.optional?.chain?.[1] ?? {};
            Object.assign(MODEL_FACTS, chain.model_facts);
            nodeType.prototype.reactorModels = Object.keys(chain.model_facts ?? {});
            // The model picker and the settings read only at start, which a later link hides.
            nodeType.prototype.reactorStart = new Set(["model", ...(chain.start_settings ?? [])]);
            const onNodeCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function (...args) {
                const result = onNodeCreated?.apply(this, args);
                const frames = this.widgets?.find((w) => w.name === "frames");
                const callback = frames?.callback;
                if (frames)
                    // The node's step is its model's chunk, so a typed length snaps to whole chunks.
                    frames.callback = (value, ...rest) => {
                        frames.value = Math.max(1, Math.round(value / frames.options.step2)) * frames.options.step2;
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
            REACTOR_MOVE_EDITOR: (node, inputName) => ({ widget: createMoveEditor(node, inputName) }),
        };
    },
});
