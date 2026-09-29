import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

const STYLE = `
.reactor-live-backdrop { position: fixed; inset: 0; z-index: 1000; display: flex; align-items: center; justify-content: center; background: color-mix(in srgb, var(--bg-color, #202020) 65%, transparent); }
.reactor-live { box-sizing: border-box; width: min(920px, 94vw); max-height: 92vh; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; padding: 12px; border: 1px solid var(--border-color); border-radius: 6px; background: var(--comfy-menu-bg); color: var(--input-text); font: 12px sans-serif; }
.reactor-live-title { font-size: 14px; font-weight: bold; }
.reactor-live-preview { position: relative; align-self: center; width: 100%; background: #000; border: 1px solid var(--border-color); border-radius: 4px; overflow: hidden; }
.reactor-live-preview.drive { cursor: pointer; }
.reactor-live-preview.drive:focus { outline: 2px solid var(--p-primary-color, #3b82f6); outline-offset: 2px; cursor: default; }
.reactor-live-hint { position: absolute; left: 50%; bottom: 10px; transform: translateX(-50%); padding: 3px 10px; border-radius: 3px; background: rgb(0 0 0 / 0.65); color: #fff; pointer-events: none; }
.reactor-live-preview:focus .reactor-live-hint { display: none; }
.reactor-live-preview canvas { position: absolute; inset: 0; width: 100%; height: 100%; }
.reactor-live-preview video { position: absolute; right: 6px; bottom: 6px; width: 22%; min-width: 120px; border: 1px solid var(--border-color); border-radius: 3px; }
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
.reactor-live textarea, .reactor-live button { background: var(--comfy-input-bg); color: var(--input-text); border: 1px solid var(--border-color); border-radius: 3px; font: inherit; }
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

function openLive({ run_id, mode, title, camera }) {
    const backdrop = el("div", { className: "reactor-live-backdrop" });
    const header = el("div", { className: "reactor-live-title", textContent: title });
    const canvas = el("canvas");
    const preview = el("div", { className: "reactor-live-preview" }, canvas);
    const status = el("div", { className: "reactor-live-status", textContent: "Connecting…" });
    // Keycaps for the drive keys, lit while the server reports driving the model with them.
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
    const apply = el("button", { textContent: "Apply", disabled: true });
    const done = el("button", { textContent: "Done" });
    const cancel = el("button", { textContent: "Cancel" });
    const buttons = el("div", { className: "reactor-live-row" }, done, cancel);
    const view = canvas.getContext("2d");
    backdrop.append(el("div", { className: "reactor-live" }, header, preview, status, legend, prompt,
                         el("div", { className: "reactor-live-row end" }, apply), buttons, stats));
    document.body.append(backdrop);

    const url = new URL(api.apiURL(`/reactor/live/${run_id}`), location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.searchParams.set("preview_mbps", app.extensionManager.setting.get(PREVIEW_BITRATE));
    url.searchParams.set("preview_width", app.extensionManager.setting.get(PREVIEW_WIDTH));
    const socket = new WebSocket(url);
    socket.binaryType = "arraybuffer";

    let decoder = null;      // null until config, and after a decode error until the next keyframe
    let previewSize = null;
    let needKey = true;
    let requestedKey = 0;
    let encoder = null;
    let stream = null;
    let video = null;
    let wantKey = true;      // the next webcam frame goes out as a keyframe
    let held = null;
    let ended = false;       // an "ended" message arrived; the socket close after it is expected
    let staying = false;     // the modal stays open on an error or a take that needs pasting
    let closed = false;
    let tornDown = false;
    let drawn = 0;           // preview frames drawn since the last stats message
    let statsAt = performance.now();

    const send = (object) => { if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(object)); };

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
        video = null;
        if (stream) for (const track of stream.getTracks()) track.stop();
        if (encoder?.state === "configured") encoder.close();
        if (decoder?.state === "configured") decoder.close();
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
    const sendKeys = () => send({ type: "keys", held: [...held] });
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
        sendKeys();
    }
    function onKeyUp(e) {
        if (!e.target?.closest?.("textarea, input")) e.stopImmediatePropagation();
        if (held?.delete(e.code)) sendKeys();
    }
    function releaseKeys() {
        if (!held?.size) return;
        held.clear();
        sendKeys();
    }
    window.addEventListener("keydown", onKeyDown, true);
    window.addEventListener("keyup", onKeyUp, true);

    // Preview, server to browser.

    const requestKey = () => {
        if (performance.now() - requestedKey < 500) return;
        requestedKey = performance.now();
        send({ type: "keyframe" });
    };

    function newDecoder() {
        const next = new VideoDecoder({
            output: (frame) => {
                try { view.drawImage(frame, 0, 0, canvas.width, canvas.height); drawn++; } finally { frame.close(); }
            },
            error: () => {
                if (decoder !== next) return;
                // An errored decoder never recovers; resync from the next keyframe instead.
                decoder = null;
                needKey = true;
                requestKey();
            },
        });
        next.configure({ codec: "avc1.42E01F", optimizeForLatency: true });
        return next;
    }

    function decodePreview(data) {
        const header = new DataView(data);
        if (header.byteLength < 9) return;
        const key = header.getUint8(0) === 1;
        if (!decoder) {
            if (!previewSize) return;
            if (!key) { requestKey(); return; }
            decoder = newDecoder();
        }
        if (!key && (needKey || decoder.decodeQueueSize > 2)) {
            // Behind on deltas: every delta until the next keyframe is undecodable anyway.
            needKey = true;
            requestKey();
            return;
        }
        // The frame timestamp is 8 bytes of unsigned big-endian microseconds.
        let timestamp = 0;
        for (let i = 1; i <= 8; i++) timestamp = timestamp * 256 + header.getUint8(i);
        try {
            decoder.decode(new EncodedVideoChunk({ type: key ? "key" : "delta", timestamp, data: new Uint8Array(data, 9) }));
            if (key) needKey = false;
        } catch {
            needKey = true;
            requestKey();
        }
    }

    // Webcam, browser to server (style mode only).

    async function startCamera(input) {
        const grab = el("canvas", { width: input.width, height: input.height }).getContext("2d");
        let keyAt = 0;
        let frames = 0;
        let nextFrame = -Infinity;
        encoder = new VideoEncoder({
            output: (chunk) => {
                const data = new Uint8Array(9 + chunk.byteLength);
                const header = new DataView(data.buffer);
                header.setUint8(0, chunk.type === "key" ? 1 : 0);
                let t = BigInt(chunk.timestamp);
                for (let i = 8; i >= 1; i--) {
                    header.setUint8(i, Number(t & 0xffn));
                    t >>= 8n;
                }
                chunk.copyTo(data.subarray(9));
                if (socket.readyState === WebSocket.OPEN) socket.send(data);
            },
            error: () => {
                teardown();
                stay("The webcam encoder failed.", true);
            },
        });
        encoder.configure({
            // Baseline level 4.0: X2's 1472x832 input is past level 3.1's 1280x720 ceiling.
            codec: "avc1.42E028",
            width: input.width,
            height: input.height,
            framerate: input.fps,
            bitrate: 2_500_000,
            latencyMode: "realtime",
            avc: { format: "annexb" },
        });
        const open = (deviceId) => navigator.mediaDevices.getUserMedia({
            video: { deviceId: deviceId && { exact: deviceId }, width: { ideal: input.width }, height: { ideal: input.height },
                     frameRate: { ideal: input.fps } },
            audio: false,
        });
        // The node names its camera by label; an unknown label falls back to the default camera.
        const wanted = camera && camera !== DEFAULT_CAMERA
            && (await navigator.mediaDevices.enumerateDevices()).find((device) => device.kind === "videoinput" && device.label === camera);
        const media = await open(wanted?.deviceId);
        if (closed) {
            for (const track of media.getTracks()) track.stop();
            return;
        }
        stream = media;
        video = el("video", { autoplay: true, muted: true, playsInline: true, srcObject: stream });
        preview.append(video);
        // Device labels are only readable once camera access is granted, so the picker comes after the first open.
        const cameras = (await navigator.mediaDevices.enumerateDevices()).filter((device) => device.kind === "videoinput");
        if (cameras.length > 1) {
            const current = stream.getVideoTracks()[0]?.getSettings().deviceId;
            const picker = el("select", { className: "reactor-live-camera", title: "Camera" },
                              ...cameras.map((camera, i) => el("option", { value: camera.deviceId, textContent: camera.label || `Camera ${i + 1}`,
                                                                             selected: camera.deviceId === current })));
            picker.addEventListener("change", async () => {
                const next = await open(picker.value).catch(() => null);
                if (!next) return void (status.textContent = "That camera is unavailable.");
                if (closed) return next.getTracks().forEach((track) => track.stop());
                for (const track of stream.getTracks()) track.stop();
                stream = next;
                video.srcObject = next;
                wantKey = true;
            });
            buttons.prepend(picker);
        }
        const onFrame = (cb) => (video.requestVideoFrameCallback ? video.requestVideoFrameCallback(cb) : requestAnimationFrame(cb));
        const pump = () => {
            if (!video) return;
            encode(performance.now());
            onFrame(pump);
        };
        onFrame(pump);

        function encode(now) {
            if (encoder.state !== "configured") return;
            // rVFC fires at the camera's or display's rate, with jitter; feed the encoder on a
            // schedule at the rate the server asked for, so a frame a hair early is not dropped.
            if (now < nextFrame - 1) return;
            nextFrame = Math.max(nextFrame + 1000 / input.fps, now);
            // Drop before encoding, never after: a skipped frame leaves the stream decodable.
            if (encoder.encodeQueueSize >= 2 || socket.bufferedAmount >= 1048576) return;
            const { videoWidth: vw, videoHeight: vh } = video;
            if (!vw || !vh) return;
            // Cover-crop: the server gets a constant width×height whatever the camera sends.
            const scale = Math.max(input.width / vw, input.height / vh);
            const sw = input.width / scale, sh = input.height / scale;
            grab.drawImage(video, (vw - sw) / 2, (vh - sh) / 2, sw, sh, 0, 0, input.width, input.height);
            // This timestamp counts encoded frames, so a dropped frame leaves a gap; the
            // preview's comes from the server.
            const timestamp = Math.round((frames * 1e6) / input.fps);
            const key = wantKey || timestamp - keyAt >= 2e6;
            const frame = new VideoFrame(grab.canvas, { timestamp });
            encoder.encode(frame, { keyFrame: key });
            frame.close();
            if (key) {
                wantKey = false;
                keyAt = timestamp;
            }
            frames++;
        }
    }

    // Message flow.

    socket.addEventListener("message", (event) => {
        if (typeof event.data !== "string") {
            decodePreview(event.data);
            return;
        }
        const message = JSON.parse(event.data);
        if (message.type === "config") {
            previewSize = message.preview;
            canvas.width = previewSize.width;
            canvas.height = previewSize.height;
            // The spacer holds the aspect ratio; the canvas stretches over it.
            preview.append(el("div", { style: `width:100%;aspect-ratio:${previewSize.width}/${previewSize.height}` }));
            preview.style.maxWidth = `${previewSize.width}px`;
            decoder = newDecoder();
            // Only the keys this model's camera lanes answer to are shown, and a pad left empty goes too.
            for (const [code, node] of caps) if (!message.keys.includes(code)) {
                node.remove();
                caps.delete(code);
            }
            for (const pad of [...legend.children]) if (!pad.children.length) pad.remove();
            prompt.value = message.prompt;
            apply.disabled = false;
            if (caps.size) {
                legend.hidden = false;
                preview.tabIndex = 0;
                preview.classList.add("drive");
                preview.append(el("div", { className: "reactor-live-hint", textContent: "Click the video to drive" }));
                held = new Set();
                preview.addEventListener("blur", releaseKeys);
                preview.focus();
            }
            if (mode === "style" && message.input)
                startCamera(message.input).catch(() => {
                    if (!closed) {
                        teardown();
                        stay("The camera is unavailable; allow camera access and run again.", true);
                    }
                });
        } else if (message.type === "status") {
            status.textContent = message.text;
        } else if (message.type === "keys") {
            const lit = new Set(message.held);
            for (const [code, cap] of caps) cap.classList.toggle("on", lit.has(code));
        } else if (message.type === "stats") {
            const now = performance.now();
            const previewFps = Math.round((drawn * 1000) / (now - statsAt));
            drawn = 0;
            statsAt = now;
            const mbps = (bps) => `${(bps / 1e6).toFixed(1)} Mbps`;
            stats.textContent = [
                message.rtt_ms != null && `RTT ${Math.round(message.rtt_ms)} ms`,
                message.fps != null && `model ${Math.round(message.fps)} fps`,
                `preview ${previewFps} fps`,
                message.in_bps != null && `in ${mbps(message.in_bps)}`,
                mode === "style" && message.out_bps != null && `out ${mbps(message.out_bps)}`,
                message.loss != null && `loss ${(message.loss * 100).toFixed(1)}%`,
            ].filter(Boolean).join(" · ");
        } else if (message.type === "keyframe") {
            // The server lost sync; the next webcam frame goes out as a keyframe.
            wantKey = true;
        } else if (message.type === "ended") {
            ended = true;
            teardown();
            if (message.error) stay(message.error, true);
            else close();
        }
    });
    socket.addEventListener("close", (event) => {
        if (ended || closed || staying) return;
        if (event.code === 4404) stay("This run is unknown to the server.", true);
        else if (event.code === 4409) stay("This run already has another browser connected.", true);
        else stay("Connection lost.", true);
    });

    apply.onclick = () => send({ type: "prompt", prompt: prompt.value });
    done.onclick = () => {
        done.disabled = true;
        send({ type: "done" });
        status.textContent = "Saving the take…";
    };
    cancel.onclick = cancelRun;

    // No WebCodecs, no run: cancel as soon as the socket will take it, and say why.
    if (typeof VideoDecoder === "undefined" || (mode === "style" && typeof VideoEncoder === "undefined")) {
        stay("This browser can't stream H.264 with WebCodecs; use Chrome, Edge or Safari.", true);
        const abandon = () => {
            send({ type: "cancel" });
            teardown();
        };
        if (socket.readyState === WebSocket.OPEN) abandon();
        else socket.addEventListener("open", abandon, { once: true });
    }

    return { cancel: cancelRun };
}

const PREVIEW_BITRATE = "Reactor.Realtime.PreviewBitrate";
const PREVIEW_WIDTH = "Reactor.Realtime.PreviewWidth";
const DEFAULT_CAMERA = "Default";
let current = null;

// The camera combo on Reactor Camera Capture. Labels are readable only once this page has camera
// access, so opening the list asks for it the first time.
function cameraWidget(node, inputName) {
    let labels = [DEFAULT_CAMERA];
    const refresh = async (ask) => {
        let cameras = (await navigator.mediaDevices.enumerateDevices()).filter((device) => device.kind === "videoinput");
        if (ask && cameras.length && !cameras.some((device) => device.label)) {
            const media = await navigator.mediaDevices.getUserMedia({ video: true, audio: false });
            for (const track of media.getTracks()) track.stop();
            cameras = (await navigator.mediaDevices.enumerateDevices()).filter((device) => device.kind === "videoinput");
        }
        labels = [DEFAULT_CAMERA, ...new Set(cameras.map((device) => device.label).filter(Boolean))];
    };
    refresh(false).catch(() => {});
    const widget = node.addWidget("combo", inputName, DEFAULT_CAMERA, () => {}, {
        values: () => {
            refresh(true).catch(() => {});
            return labels.includes(widget.value) ? labels : [...labels, widget.value];
        },
    });
    return { widget };
}

app.registerExtension({
    name: "reactor.live",
    settings: [{
        id: PREVIEW_BITRATE,
        category: ["Reactor", "Realtime", "Preview bitrate"],
        name: "Realtime preview bitrate (Mbps)",
        tooltip: "The most the live preview uses between ComfyUI and this browser. Lower it on a slow link to a remote ComfyUI. Saved takes are unaffected.",
        type: "number",
        defaultValue: 3,
        attrs: { min: 0.5, max: 20, step: 0.5 },
    }, {
        id: PREVIEW_WIDTH,
        category: ["Reactor", "Realtime", "Preview resolution"],
        name: "Realtime preview resolution",
        tooltip: "The widest the live preview is sent; larger model output is scaled down to it. Full sends the model's own size, at more CPU and bandwidth. Saved takes are unaffected.",
        type: "combo",
        defaultValue: 832,
        options: [{ value: 0, text: "Full" }, { value: 1280, text: "1280 px" }, { value: 832, text: "832 px" },
                  { value: 640, text: "640 px" }, { value: 480, text: "480 px" }],
    }],
    getCustomWidgets() {
        return { REACTOR_CAMERA_DEVICE: cameraWidget };
    },
    setup() {
        document.head.append(el("style", { textContent: STYLE }));
        api.addEventListener("reactor.live.open", ({ detail }) => {
            // One live run at a time: closing the open run's socket is its cancel.
            current?.cancel();
            current = openLive(detail);
        });
    },
});
