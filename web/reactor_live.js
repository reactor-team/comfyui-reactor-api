import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const HINT = "Press Run to start a real-time session: the model plays in a window and you steer it as it runs. "
    + "Each run records a take. Set the seed's control after generate to fixed to keep the last take instead of starting a new session.";

const STYLE = `
.reactor-live-node-help { box-sizing: border-box; padding: 4px 6px; color: var(--descrip-text, #999); font: 11px sans-serif; line-height: 1.35; overflow: hidden; }
.reactor-live-backdrop { position: fixed; inset: 0; z-index: 1000; display: flex; align-items: center; justify-content: center; background: color-mix(in srgb, var(--bg-color, #202020) 65%, transparent); }
.reactor-live { box-sizing: border-box; width: min(920px, 94vw); max-height: 92vh; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; padding: 12px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--comfy-menu-bg); color: var(--input-text); font: 12px sans-serif; }
.reactor-live-title { font-size: 14px; font-weight: bold; }
.reactor-live-preview { position: relative; align-self: center; width: 100%; background: #000; border: 1px solid var(--border-color); border-radius: 4px; overflow: hidden; }
.reactor-live-preview.drive { cursor: pointer; }
.reactor-live-preview.drive:focus { outline: 2px solid var(--p-primary-color, #3b82f6); outline-offset: 2px; cursor: default; }
.reactor-live-hint { position: absolute; left: 50%; bottom: 10px; transform: translateX(-50%); padding: 3px 10px; border-radius: 3px; background: rgb(0 0 0 / 0.65); color: #fff; pointer-events: none; }
.reactor-live-preview:focus .reactor-live-hint { display: none; }
.reactor-live-output { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; }
.reactor-live-self { position: absolute; right: 6px; bottom: 6px; width: 22%; min-width: 120px; border: 1px solid var(--border-color); border-radius: 3px; }
.reactor-live-status { min-height: 15px; opacity: 0.8; }
.reactor-live-status:empty { display: none; }
.reactor-live-status.error { color: #e05252; opacity: 1; }
.reactor-live-keys { display: flex; gap: 24px; justify-content: center; }
.reactor-live-pad { display: grid; grid-template-columns: repeat(3, 30px); gap: 4px; }
.reactor-live-key { display: flex; align-items: center; justify-content: center; height: 30px; border: 1px solid var(--border-color); border-radius: 4px; background: var(--comfy-input-bg); opacity: 0.6; transition: background 80ms, opacity 80ms; }
.reactor-live-key { grid-row: 2; }
.reactor-live-key.up { grid-row: 1; }
.reactor-live-key.on { opacity: 1; background: var(--p-primary-color, #3b82f6); border-color: var(--p-primary-color, #3b82f6); color: #fff; }
.reactor-live-stats { opacity: 0.6; font-variant-numeric: tabular-nums; }
.reactor-live-look { display: flex; gap: 12px; align-items: flex-start; flex-wrap: wrap; }
.reactor-live-figure { display: flex; flex-direction: column; gap: 4px; width: 112px; text-align: center; }
.reactor-live-tile { box-sizing: border-box; width: 112px; height: 112px; display: flex; align-items: center; justify-content: center; padding: 0; overflow: hidden; border: 1px solid var(--border-color); border-radius: 4px; background: var(--comfy-input-bg); }
.reactor-live-tile img { width: 100%; height: 100%; object-fit: cover; }
.reactor-live .reactor-live-tile.new { border-style: dashed; padding: 8px; }
.reactor-live-tile.new.over { border-color: var(--p-primary-color, #3b82f6); }
.reactor-live-tile.new:has(img:not([hidden])) { padding: 0; border-style: solid; }
.reactor-live-caption { opacity: 0.7; }
.reactor-live-look-side { display: flex; flex-direction: column; gap: 8px; flex: 1; min-width: 220px; }
.reactor-live-segment { display: flex; flex-wrap: wrap; }
.reactor-live .reactor-live-segment button { border-radius: 0; margin-left: -1px; }
.reactor-live .reactor-live-segment button:first-child { border-radius: 3px 0 0 3px; margin-left: 0; }
.reactor-live .reactor-live-segment button:last-child { border-radius: 0 3px 3px 0; }
.reactor-live .reactor-live-segment button.on { position: relative; background: var(--p-primary-color, #3b82f6); border-color: var(--p-primary-color, #3b82f6); color: #fff; }
.reactor-live textarea, .reactor-live button, .reactor-live select { background: var(--comfy-input-bg); color: var(--input-text); border: 1px solid var(--border-color); border-radius: 3px; font: inherit; }
.reactor-live textarea { min-height: 52px; resize: vertical; }
.reactor-live button { padding: 4px 12px; cursor: pointer; }
.reactor-live button:disabled { cursor: default; opacity: 0.5; }
.reactor-live-row { display: flex; gap: 8px; align-items: center; }
.reactor-live-row.end { justify-content: flex-end; }
.reactor-live-camera { margin-right: auto; max-width: 50%; }
`;

function el(tag, attrs = {}, ...children) {
    const node = Object.assign(document.createElement(tag), attrs);
    node.append(...children);
    return node;
}

function openLive({ run_id, mode, title, camera, microphone }) {
    const backdrop = el("div", { className: "reactor-live-backdrop" });
    const header = el("div", { className: "reactor-live-title", textContent: title });
    const output = el("video", { className: "reactor-live-output", autoplay: true, muted: true, playsInline: true });
    const sound = el("audio", { autoplay: true });
    const preview = el("div", { className: "reactor-live-preview" }, output, sound);
    const status = el("div", { className: "reactor-live-status", textContent: "Connecting…" });
    // Keycaps for the drive keys, lit while held.
    const caps = new Map();
    // Each pad is a top row at columns 1-3, where null leaves a gap, over a full bottom row.
    const pad = (top, bottom) => el("div", { className: "reactor-live-pad" },
        ...top.flatMap((key, i) => (key ? [cap(key, "reactor-live-key up", i + 1)] : [])), ...bottom.map((key) => cap(key, "reactor-live-key")));
    function cap([code, label], className, column) {
        const node = el("div", { className, textContent: label });
        if (column) node.style.gridColumn = column;
        caps.set(code, node);
        return node;
    }
    const legend = el("div", { className: "reactor-live-keys", hidden: true },
                      pad([["KeyQ", "Q"], ["KeyW", "W"], ["KeyE", "E"]], [["KeyA", "A"], ["KeyS", "S"], ["KeyD", "D"]]),
                      pad([null, ["ArrowUp", "↑"], null], [["ArrowLeft", "←"], ["ArrowDown", "↓"], ["ArrowRight", "→"]]));
    const stats = el("div", { className: "reactor-live-stats" });
    const prompt = el("textarea");
    // The Look panel, for a model steered by an image and settings instead of a prompt, filled from the config.
    const switcher = el("div", { className: "reactor-live-look", hidden: true });
    const apply = el("button", { textContent: "Apply", disabled: true });
    const applyRow = el("div", { className: "reactor-live-row end" }, apply);
    const done = el("button", { textContent: "Done" });
    const cancel = el("button", { textContent: "Cancel" });
    const buttons = el("div", { className: "reactor-live-row" }, done, cancel);
    backdrop.append(el("div", { className: "reactor-live" }, header, preview, status, legend, prompt, switcher, applyRow, buttons, stats));
    document.body.append(backdrop);

    const url = new URL(api.apiURL(`/reactor/live/${run_id}`), location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(url);

    let stream = null;       // the camera or microphone, when this browser publishes it
    let held = null;
    let lanes = [];          // the model's drive lanes, from the config
    let promptCommand = null;
    let promptField = null;
    let look = null;         // the Look panel's image field, next image and setting pickers, for a model without a prompt
    let started = false;     // setup is done, and this browser sends the model its commands
    let markStarted;
    const startedSignal = new Promise((resolve) => { markStarted = resolve; });
    const lastSent = new Map();  // each lane's field to the value last sent
    let reactor = null;      // this browser's own client in the session
    let input = null;        // the camera size and rate the server asked for
    let own = null;          // this browser's connection stats: the preview it receives, the camera it sends
    let ended = false;       // an "ended" message arrived; the socket close after it is expected
    let staying = false;     // the modal stays open on an error or a take that needs pasting
    let closed = false;
    let tornDown = false;

    const send = (object) => { if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(object)); };

    // What to show for an SDK error: the sentence in the Reactor API reply it carries, with its link, or its own message.
    function errorText(e) {
        const text = e?.message ?? String(e);
        try {
            const body = JSON.parse(text.slice(text.indexOf("{")));
            if (body?.message) return [body.message, body.url].filter(Boolean).join(" ");
        } catch {}
        return text;
    }

    function stopKeys() {
        window.removeEventListener("keydown", onKeyDown, true);
        window.removeEventListener("keyup", onKeyUp, true);
        preview.removeEventListener("blur", releaseKeys);
        held = null;
        for (const cap of caps.values()) cap.classList.remove("on");
    }

    function teardown() {
        if (tornDown) return;
        tornDown = true;
        stopKeys();
        window.removeEventListener("paste", onPaste);
        if (stream) for (const track of stream.getTracks()) track.stop();
        reactor?.disconnect().catch(() => {});
        socket.close();
    }

    function close() {
        if (closed) return;
        closed = true;
        teardown();
        backdrop.remove();
    }

    // The run is over but the user has something to read: an error, or a take to paste by hand.
    function stay(text, error = false) {
        staying = true;
        status.textContent = text;
        status.classList.toggle("error", error);
        buttons.replaceChildren(el("button", { textContent: "Close", onclick: close }));
    }

    function cancelRun() {
        send({ type: "cancel" });
        close();
    }

    // While the modal is open, keys outside its text fields stop here so ComfyUI's hotkeys never
    // fire; the drive keys drive only while the preview has focus, and a blur releases them all.
    function command(name, data) {
        reactor?.sendCommand(name, data).then((reply) => {
            if (reply?.type === "command_error") status.textContent = `Reactor rejected ${name}: ${reply.data?.reason ?? "no reason given"}`;
        }, (e) => { status.textContent = `Sending ${name} failed: ${errorText(e)}`; });
    }

    // Each lane follows the first of its key pairs with exactly one key held, and rests at idle otherwise.
    function drive() {
        for (const [code, cap] of caps) cap.classList.toggle("on", held.has(code));
        if (!started) return;
        for (const lane of lanes) {
            let value = lane.idle;
            for (const [low, high, negative, positive] of lane.axes) {
                const axis = held.has(high) - held.has(low);
                if (axis) {
                    value = axis > 0 ? positive : negative;
                    break;
                }
            }
            if (JSON.stringify(lastSent.get(lane.field) ?? lane.idle) === JSON.stringify(value)) continue;
            lastSent.set(lane.field, value);
            command(lane.command, { [lane.field]: value });
        }
    }
    function onKeyDown(e) {
        if (e.key === "Escape") {
            e.preventDefault();
            e.stopImmediatePropagation();
            cancelRun();
            return;
        }
        if (e.target?.closest?.("textarea, input")) return;
        e.stopImmediatePropagation();
        if (!held || document.activeElement !== preview || !caps.has(e.code)) return;
        e.preventDefault();
        if (e.repeat || held.has(e.code)) return;
        held.add(e.code);
        drive();
    }
    function onKeyUp(e) {
        if (!e.target?.closest?.("textarea, input")) e.stopImmediatePropagation();
        if (held?.delete(e.code)) drive();
    }
    function releaseKeys() {
        if (!held?.size) return;
        held.clear();
        drive();
    }
    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("keyup", onKeyUp, true);

    // The session, joined from this browser: it plays the model's output as the preview and, for
    // a camera source, publishes the camera itself.

    const openCamera = (deviceId) => navigator.mediaDevices.getUserMedia({
        video: { deviceId: deviceId && { exact: deviceId }, width: { ideal: input.width }, height: { ideal: input.height },
                 frameRate: { ideal: input.fps } },
        audio: false,
    });

    async function startCamera() {
        // The node names its camera by label; an unknown label falls back to the default camera.
        const wanted = camera && camera !== DEFAULT_DEVICE
            && (await navigator.mediaDevices.enumerateDevices()).find((device) => device.kind === "videoinput" && device.label === camera);
        const media = await openCamera(wanted?.deviceId);
        if (tornDown) return media.getTracks().forEach((track) => track.stop());
        stream = media;
        preview.append(el("video", { className: "reactor-live-self", autoplay: true, muted: true, playsInline: true, srcObject: stream }));
    }

    async function addPicker(sender) {
        // Device labels are only readable once camera access is granted, so the picker comes after the first open.
        const cameras = (await navigator.mediaDevices.enumerateDevices()).filter((device) => device.kind === "videoinput");
        if (cameras.length < 2) return;
        const current = stream.getVideoTracks()[0]?.getSettings().deviceId;
        const picker = el("select", { className: "reactor-live-camera", title: "Camera" },
                          ...cameras.map((camera, i) => el("option", { value: camera.deviceId, textContent: camera.label || `Camera ${i + 1}`,
                                                                         selected: camera.deviceId === current })));
        picker.addEventListener("change", async () => {
            const next = await openCamera(picker.value).catch(() => null);
            if (!next) return void (status.textContent = "That camera is unavailable.");
            if (tornDown) return next.getTracks().forEach((track) => track.stop());
            await sender.replaceTrack(next.getVideoTracks()[0]);
            for (const track of stream.getTracks()) track.stop();
            stream = next;
            preview.querySelector(".reactor-live-self").srcObject = next;
        });
        buttons.prepend(picker);
    }

    async function startMic() {
        const wanted = microphone && microphone !== DEFAULT_DEVICE
            && (await navigator.mediaDevices.enumerateDevices()).find((device) => device.kind === "audioinput" && device.label === microphone);
        const media = await navigator.mediaDevices.getUserMedia({
            audio: { deviceId: wanted && { exact: wanted.deviceId }, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
            video: false,
        });
        if (tornDown) return media.getTracks().forEach((track) => track.stop());
        stream = media;
    }

    // The session can miss this browser's first request for the model's output, and then the preview stays
    // black for the whole take while the recording is fine; ask again while no video has arrived.
    async function keepPreview(names) {
        await startedSignal;
        for (let attempt = 1; attempt <= PREVIEW_RETRIES; attempt++) {
            await new Promise((resolve) => setTimeout(resolve, PREVIEW_WAIT_MS));
            if (tornDown || output.videoWidth) return;
            console.warn(`Reactor: no preview video yet, asking for it again (${attempt} of ${PREVIEW_RETRIES}).`);
            for (const name of names) await reactor.resumeTrack(name);
        }
    }

    async function joinSession(join) {
        const mic = join.tracks.find((track) => track.name === join.publish)?.kind === "audio";
        if (join.publish) {
            try {
                await (mic ? startMic() : startCamera());
            } catch {
                throw new Error(mic ? "The microphone is unavailable; allow microphone access and run again."
                                    : "The camera is unavailable; allow camera access and run again.");
            }
            if (tornDown) return;
        }
        const { Reactor } = await import("./vendor/reactor-sdk.mjs");
        if (tornDown) return;
        reactor = new Reactor({ modelName: join.model, local: join.local, modelTracks: join.tracks });
        reactor.on("trackReceived", (name, track, media) => {
            if (name === join.publish) return;
            if (track.kind === "video") output.srcObject = media;
            else sound.srcObject = media;
        });
        reactor.on("statsUpdate", (stats) => { own = stats; });
        let joined = false;
        // Why the session went away, when Reactor or the SDK said: the reason Reactor ended it, or the last error.
        let lost = null;
        reactor.on("runtimeMessage", (message) => {
            if (message?.type === "sessionEnded") lost = message.data?.reason || "Reactor ended the session.";
        });
        reactor.on("error", (e) => {
            console.error("Reactor:", e);
            if (e?.recoverable || tornDown) return;
            lost ??= errorText(e);
            status.textContent = lost;
            status.classList.add("error");
        });
        const ready = new Promise((resolve) => reactor.on("statusChanged", (s) => {
            if (s === "ready") resolve();
            // This browser's side dropped: end the run on the server too, so neither side outlives the other.
            if (s === "disconnected" && joined && !tornDown) {
                const error = lost ?? "This browser lost its connection to the Reactor session.";
                send({ type: "cancel", error });
                teardown();
                stay(error, true);
            }
        }));
        try {
            await reactor.connect(join.jwt ?? undefined, { sessionId: join.session_id });
            await ready;
            joined = true;
        } catch (e) {
            console.error("Reactor: this browser could not join the session.", e);
            throw new Error(`This browser could not join the Reactor session: ${errorText(e)}`);
        }
        keepPreview(join.tracks.filter((track) => track.direction === "recvonly").map((track) => track.name)).catch(() => {});
        if (!join.publish || tornDown) return;
        const track = mic ? stream.getAudioTracks()[0] : stream.getVideoTracks()[0];
        // The SDK gives up on a publish after a fixed 10 s, which a slow link can miss; try again before failing.
        for (let attempt = 1; ; attempt++) {
            try {
                await reactor.publishTrack(join.publish, track);
                break;
            } catch (e) {
                if (attempt === 3 || tornDown || !/timed out/.test(e?.message ?? e)) throw e;
                console.warn(`Reactor: publishing ${join.publish} timed out, retrying.`, e);
                await reactor.unpublishTrack(join.publish);
            }
        }
        if (tornDown) return;
        const sender = reactor.getPeerConnection()?.getSenders().find((s) => s.track === track);
        if (sender && !mic) {
            // The model's input size is fixed for the session: drop frames on a slow link, never resolution.
            const params = sender.getParameters();
            params.degradationPreference = "maintain-resolution";
            await sender.setParameters(params).catch(() => {});
            addPicker(sender).catch(() => {});
        }
        send({ type: "published" });
    }

    // Message flow.

    socket.addEventListener("message", (event) => {
        const message = JSON.parse(event.data);
        if (message.type === "config") {
            input = message.input;
            const { width, height } = message.preview;
            // The spacer holds the aspect ratio; the video stretches over it.
            preview.append(el("div", { style: `width:100%;aspect-ratio:${width}/${height}` }));
            preview.style.maxWidth = `${width}px`;
            // Only the keys this model's camera lanes answer to are shown, and a pad left empty goes too.
            lanes = message.lanes;
            promptCommand = message.prompt_command;
            promptField = message.prompt_field;
            if (mode === "call") {
                apply.textContent = "Send";
                // The placeholder goes once there is text, so the two ways to talk are also said above the box.
                prompt.before(el("div", { textContent: "Talk to the character out loud, or type a message and press Send." }));
                prompt.placeholder = "Type a message for the character…";
            }
            const keys = new Set(lanes.flatMap((lane) => lane.axes.flatMap(([low, high]) => [low, high])));
            for (const [code, node] of caps) if (!keys.has(code)) {
                node.remove();
                caps.delete(code);
            }
            for (const pad of [...legend.children]) if (!pad.children.length) pad.remove();
            prompt.value = message.prompt;
            if (message.switch) {
                prompt.hidden = true;
                applyRow.hidden = true;
                look = buildLook(message.switch);
            }
            if (caps.size) {
                legend.hidden = false;
                preview.tabIndex = 0;
                preview.classList.add("drive");
                preview.append(el("div", { className: "reactor-live-hint", textContent: "Click the video to drive" }));
                held = new Set();
                preview.addEventListener("blur", releaseKeys);
                preview.focus();
            }
        } else if (message.type === "join") {
            joinSession(message).catch((e) => {
                // A run already torn down (the socket closed first) keeps its own message.
                if (!tornDown) {
                    teardown();
                    stay(e.message, true);
                }
            });
        } else if (message.type === "status") {
            status.textContent = message.text;
        } else if (message.type === "started") {
            started = true;
            markStarted();
            apply.disabled = false;
            look?.refresh();
            if (held) drive();
        } else if (message.type === "stats") {
            const mbps = (bps) => `${(bps / 1e6).toFixed(1)} Mbps`;
            stats.textContent = [
                message.rtt_ms != null && `RTT ${Math.round(message.rtt_ms)} ms`,
                message.fps != null && `model ${Math.round(message.fps)} fps`,
                own?.framesPerSecond != null && `preview ${Math.round(own.framesPerSecond)} fps`,
                message.in_bps != null && `in ${mbps(message.in_bps)}`,
                mode === "style" && (stream ? own?.outgoingBitrate : message.out_bps) != null
                    && `out ${mbps(stream ? own.outgoingBitrate : message.out_bps)}`,
                message.loss != null && `loss ${(message.loss * 100).toFixed(1)}%`,
            ].filter(Boolean).join(" · ");
        } else if (message.type === "ended") {
            ended = true;
            teardown();
            if (message.error) stay(message.error, true);
            else close();
        }
    });
    socket.addEventListener("close", (event) => {
        if (ended || closed || staying) return;
        // The server cancels the run on a closed socket; leave the session with it.
        teardown();
        if (event.code === 4404) stay("This run is unknown to the server.", true);
        else if (event.code === 4409) stay("This run already has another browser connected.", true);
        else stay("Connection lost.", true);
    });

    apply.onclick = async () => {
        if (look) return void switchTo();
        command(promptCommand, { [promptField]: prompt.value });
        // A message to the character is gone once sent; a prompt stays to be edited.
        if (mode === "call") prompt.value = "";
    };
    // Each option value reads as its label: `style_transfer` is "Style transfer".
    const optionLabel = (option) => option.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());

    // The reference in use, a tile for the next one (dropped, pasted or picked), and each setting's options.
    function buildLook({ image, reference, settings }) {
        const current = el("img", { alt: "", hidden: !reference, src: reference ?? "" });
        const chosen = el("img", { alt: "", hidden: true });
        const hint = el("span", { textContent: "Drop, paste or click for a new image" });
        const file = el("input", { type: "file", accept: "image/png,image/jpeg,image/webp", hidden: true });
        const tile = el("button", { className: "reactor-live-tile new", title: "A new reference image" }, chosen, hint);
        const panel = { image, next: null, busy: false };
        panel.choose = (next) => {
            if (!next?.type.startsWith("image/") || panel.busy) return;
            if (chosen.src) URL.revokeObjectURL(chosen.src);
            panel.next = next;
            chosen.src = URL.createObjectURL(next);
            chosen.hidden = false;
            hint.hidden = true;
            panel.refresh();
        };
        panel.pickers = settings.map((setting) => {
            const picker = { setting, value: setting.value, sent: setting.value };
            picker.buttons = setting.options.map((option) => el("button", {
                textContent: optionLabel(option), title: option,
                onclick: () => { picker.value = option; panel.refresh(); },
            }));
            return picker;
        });
        panel.refresh = () => {
            for (const picker of panel.pickers) picker.buttons.forEach((button, i) => button.classList.toggle("on", picker.setting.options[i] === picker.value));
            const changed = panel.next || panel.pickers.some((picker) => picker.value !== picker.sent);
            apply.disabled = !started || panel.busy || !changed;
        };
        // After a switch the chosen image becomes the one in use.
        panel.commit = () => {
            for (const picker of panel.pickers) picker.sent = picker.value;
            if (panel.next) {
                if (current.src.startsWith("blob:")) URL.revokeObjectURL(current.src);
                current.src = chosen.src;
                current.hidden = false;
                chosen.removeAttribute("src");
                chosen.hidden = true;
                hint.hidden = false;
                panel.next = null;
            }
            file.value = "";
            panel.refresh();
        };
        tile.onclick = () => file.click();
        file.onchange = () => panel.choose(file.files[0]);
        tile.ondragover = (e) => { e.preventDefault(); tile.classList.add("over"); };
        tile.ondragleave = () => tile.classList.remove("over");
        tile.ondrop = (e) => {
            e.preventDefault();
            tile.classList.remove("over");
            panel.choose(e.dataTransfer.files[0]);
        };
        window.addEventListener("paste", onPaste);
        const figure = (node, caption) => el("div", { className: "reactor-live-figure" }, node, el("div", { className: "reactor-live-caption", textContent: caption }));
        switcher.append(figure(el("div", { className: "reactor-live-tile" }, current), "In use"), figure(tile, "Next"), file,
                        el("div", { className: "reactor-live-look-side" },
                           ...panel.pickers.map(({ buttons }) => el("div", { className: "reactor-live-segment" }, ...buttons)),
                           el("div", { className: "reactor-live-row" }, apply)));
        switcher.hidden = false;
        panel.refresh();
        return panel;
    }
    function onPaste(e) {
        const next = [...(e.clipboardData?.files ?? [])].find((file) => file.type.startsWith("image/"));
        if (!next || !look) return;
        e.preventDefault();
        look.choose(next);
    }

    // Sends only what changed, once, and waits for the model's reply: it keeps the previous look when a switch fails.
    async function switchTo() {
        const data = {};
        for (const picker of look.pickers) if (picker.value !== picker.sent) data[picker.setting.field] = picker.value;
        look.busy = true;
        apply.textContent = "Switching…";
        look.refresh();
        try {
            if (look.next) data[look.image] = await reactor.uploadFile(look.next);
            const reply = await reactor.sendCommand(promptCommand, data);
            if (reply?.type === "command_error") throw new Error(reply.data?.reason ?? "no reason given");
            look.commit();
            status.textContent = "Switched; the new look shows in a moment.";
        } catch (e) {
            status.textContent = `The switch failed: ${errorText(e)}`;
        } finally {
            look.busy = false;
            apply.textContent = "Apply";
            look.refresh();
        }
    }
    done.onclick = () => {
        done.disabled = true;
        send({ type: "done" });
        status.textContent = "Saving the take…";
    };
    cancel.onclick = cancelRun;

    return { cancel: cancelRun };
}

const DEFAULT_DEVICE = "Default";
const PREVIEW_WAIT_MS = 4000;
const PREVIEW_RETRIES = 3;
let current = null;

// The device combo on Reactor Camera Capture and Microphone Capture. Labels are readable only once
// this page has access to that kind of device, so opening the list asks for it the first time.
const deviceWidget = (kind) => (node, inputName) => {
    let labels = [DEFAULT_DEVICE];
    const refresh = async (ask) => {
        let devices = (await navigator.mediaDevices.enumerateDevices()).filter((device) => device.kind === kind);
        if (ask && devices.length && !devices.some((device) => device.label)) {
            const media = await navigator.mediaDevices.getUserMedia({ video: kind === "videoinput", audio: kind === "audioinput" });
            for (const track of media.getTracks()) track.stop();
            devices = (await navigator.mediaDevices.enumerateDevices()).filter((device) => device.kind === kind);
        }
        labels = [DEFAULT_DEVICE, ...new Set(devices.map((device) => device.label).filter(Boolean))];
    };
    refresh(false).catch(() => {});
    const widget = node.addWidget("combo", inputName, DEFAULT_DEVICE, () => {}, {
        values: () => {
            refresh(true).catch(() => {});
            return labels.includes(widget.value) ? labels : [...labels, widget.value];
        },
    });
    return { widget };
};

app.registerExtension({
    name: "reactor.live",
    getCustomWidgets() {
        return { REACTOR_CAMERA_DEVICE: deviceWidget("videoinput"), REACTOR_MICROPHONE_DEVICE: deviceWidget("audioinput") };
    },
    setup() {
        document.head.append(el("style", { textContent: STYLE }));
        api.addEventListener("reactor.live.open", ({ detail }) => {
            // One live run at a time: closing the open run's socket is its cancel.
            current?.cancel();
            current = openLive(detail);
        });
    },
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== "ReactorRealtime") return;
        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function (...args) {
            const result = onNodeCreated?.apply(this, args);
            // The session window opens only once the run starts, so the node says what Run does.
            const hint = el("div", { className: "reactor-live-node-help", textContent: HINT });
            // The option alone still leaves the widget in widgets_values; the widget's own flag keeps it out.
            this.addDOMWidget("hint", "REACTOR_REALTIME_HINT", hint, { serialize: false, hideInPanel: true, getMinHeight: () => 64 }).serialize = false;
            return result;
        };
    },
});
