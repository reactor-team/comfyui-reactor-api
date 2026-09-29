import asyncio
import configparser
import io as bytes_io
import logging
import os
import uuid
from dataclasses import replace

import numpy as np
from aiohttp import WSMsgType, web
from PIL import Image
from typing_extensions import override

import comfy.model_management
import comfy.utils
import folder_paths
from comfy.cli_args import args
from comfy_api.latest import ComfyExtension, InputImpl, Types, io, ui
from server import PromptServer

from .reactor_render import live
from .reactor_render.session import render
from .reactor_render.timeline import MODELS, Beat, Timeline, compile_timeline, editor_beats, editor_moves, model_facts

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")

ReactorTimelineType = io.Custom("REACTOR_TIMELINE")
ReactorBeatsType = io.Custom("REACTOR_BEATS")
ReactorModelType = io.Custom("REACTOR_MODEL")


@io.comfytype(io_type="REACTOR_BEAT_EDITOR")
class BeatEditor(io.ComfyTypeIO):
    """The timeline editor widget. Its value is `{"beats": [...], "moves": [...]}`: a list value would read as a link."""
    Type = dict

    class Input(io.WidgetInput):
        def __init__(self, id: str, tooltip: str = None):
            super().__init__(id, tooltip=tooltip, default={"beats": [], "moves": []}, socketless=True)


@io.comfytype(io_type="REACTOR_MOVE_EDITOR")
class MoveEditor(io.ComfyTypeIO):
    """A beat's camera lane widget. Its value is `{"moves": [...]}`, each move counted from the beat's start."""
    Type = dict

    class Input(io.WidgetInput):
        def __init__(self, id: str, tooltip: str = None):
            super().__init__(id, tooltip=tooltip, default={"moves": []}, socketless=True)


def image_to_png(image) -> bytes:
    pixels = np.clip(255.0 * image[0].cpu().numpy(), 0, 255).astype(np.uint8)
    buf = bytes_io.BytesIO()
    Image.fromarray(pixels).save(buf, format="PNG")
    return buf.getvalue()


def video_to_mp4(video) -> bytes:
    buf = bytes_io.BytesIO()
    video.save_to(buf, format=Types.VideoContainer.MP4, codec=Types.VideoCodec.H264)
    return buf.getvalue()


class ReactorModel(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ReactorModel",
            display_name="Reactor Model",
            category="Reactor",
            description="Picks the Reactor model a render runs on, and that model's settings.",
            inputs=[io.DynamicCombo.Input("model", options=[
                io.DynamicCombo.Option(name, [io.Combo.Input(key, options=list(setting.options), default=setting.default, optional=True)
                                              for key, setting in spec.settings.items()])
                for name, spec in MODELS.items()],
                extra_dict={"model_facts": {name: model_facts(spec) for name, spec in MODELS.items()}})],
            outputs=[ReactorModelType.Output()],
        )

    @classmethod
    def execute(cls, model) -> io.NodeOutput:
        # A setting missing from an older saved prompt is left to the model's own default.
        name = model["model"]
        return io.NodeOutput((name, {key: model[key] for key in MODELS[name].settings if key in model}))


class ReactorBeat(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ReactorBeat",
            display_name="Reactor Beat",
            category="Reactor",
            description="One beat of a Reactor timeline. Chain beats together and feed the last one to Reactor Timeline.",
            inputs=[
                io.String.Input("prompt", multiline=True),
                io.Int.Input("frames", default=120, min=1, max=100000,
                             tooltip="How many frames of video this beat plays. It starts where the beat before it in the chain ends; both ends round to the nearest chunk."),
                io.Combo.Input("kind", options=["shot", "cut"],
                               tooltip="How this beat enters from the one before: shot blends softly, cut starts a fresh scene. The first beat just opens the video."),
                io.Image.Input("image", optional=True, tooltip="Reference image, for models that take one."),
                io.Video.Input("video", optional=True, tooltip="Source clip to edit, for video-to-video models. Only the first beat's is used."),
                MoveEditor.Input("moves", tooltip="Camera moves during this beat, for models with camera controls. A move ends with its beat; continue it on the next beat to keep it going."),
                ReactorBeatsType.Input("chain", optional=True),
            ],
            outputs=[ReactorBeatsType.Output()],
        )

    @classmethod
    def execute(cls, prompt, frames, kind, moves, image=None, video=None, chain=None) -> io.NodeOutput:
        beat = Beat(prompt, frames, cut=kind == "cut", image=None if image is None else image_to_png(image),
                    video=None if video is None else video_to_mp4(video), moves=tuple(editor_moves(moves)))
        return io.NodeOutput((*(chain or ()), beat))


class ReactorTimeline(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ReactorTimeline",
            display_name="Reactor Timeline",
            category="Reactor",
            description="Lay out the beats of a Reactor render on a timeline, snapped to the model's chunks.",
            inputs=[
                ReactorModelType.Input("model"),
                BeatEditor.Input("beats"),
                ReactorBeatsType.Input("chain", optional=True, tooltip="Beats from a Reactor Beat chain. When connected, they and their camera moves are the timeline, and the beats and moves drawn here are not used."),
                io.Autogrow.Input("images", optional=True, template=io.Autogrow.TemplatePrefix(
                    input=io.Image.Input("image", tooltip="Reference image a beat can pick, for models that take one."),
                    prefix="image_", min=0, max=100)),
            ],
            outputs=[ReactorTimelineType.Output()],
        )

    @classmethod
    def execute(cls, model, beats, chain=None, images=None) -> io.NodeOutput:
        name, settings = model
        if chain is not None:
            return io.NodeOutput(Timeline(name, tuple(chain), settings))
        pngs = {slot: image_to_png(image) for slot, image in (images or {}).items() if image is not None}
        return io.NodeOutput(Timeline(name, tuple(editor_beats(beats, pngs)), settings, tuple(editor_moves(beats))))


def saved_api_key():
    """REACTOR_API_KEY under [API] in this plugin's config.ini, laid out as in config.ini.example."""
    config = configparser.ConfigParser()
    config.read(CONFIG_PATH)
    return config.get("API", "REACTOR_API_KEY", fallback="").strip() or None


def connect_args() -> dict:
    """The Reactor client's authentication, from REACTOR_API_KEY or this plugin's config.ini."""
    # REACTOR_LOCAL=1 targets a model served by `reactor run` on this machine, which needs no key.
    if os.environ.get("REACTOR_LOCAL") == "1":
        connect = {"local": True}
        if os.environ.get("REACTOR_API_URL"):
            connect["api_url"] = os.environ["REACTOR_API_URL"]
        return connect
    if key := os.environ.get("REACTOR_API_KEY") or saved_api_key():
        return {"api_key": key}
    raise RuntimeError(f"Set REACTOR_API_KEY in {CONFIG_PATH} or in the environment ComfyUI starts from.")


class ReactorRender(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ReactorRender",
            display_name="Reactor Render",
            category="Reactor",
            description="Renders a timeline on a Reactor model, with the API key from the REACTOR_API_KEY environment variable or this plugin's config.ini.",
            inputs=[
                ReactorTimelineType.Input("timeline"),
                io.Int.Input("seed", default=42, min=0, max=2**31 - 1, control_after_generate=True),
            ],
            outputs=[io.Video.Output()],
        )

    @classmethod
    async def execute(cls, timeline, seed) -> io.NodeOutput:
        connect = connect_args()
        spec = MODELS[timeline.model]
        beats = [replace(b, image=spec.fit_png(b.image)) if b.image is not None else b for b in timeline.beats]
        plan = compile_timeline(timeline.model, beats, seed, timeline.settings, list(timeline.moves))
        out_path = os.path.join(folder_paths.get_temp_directory(), f"reactor_{uuid.uuid4().hex}.mp4")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        pbar = comfy.utils.ProgressBar(1)
        await render(spec, plan, out_path,
                     on_progress=lambda done, total, frame: pbar.update_absolute(
                         done, total, None if frame is None else ("JPEG", Image.fromarray(frame), args.preview_size)),
                     check_interrupt=comfy.model_management.throw_exception_if_processing_interrupted,
                     **connect)
        return io.NodeOutput(InputImpl.VideoFromFile(out_path))


@PromptServer.instance.routes.get("/reactor/live/{run_id}")
async def live_socket(request):
    """The browser modal's end of a live take: websocket messages relayed to and from the run."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    run = live.RUNS.get(request.match_info["run_id"])
    if run is None:
        await ws.close(code=4404)
        return ws
    loop = asyncio.get_running_loop()
    pending = {"frame": None, "task": None}

    async def relay(message):
        if isinstance(message, bytes):
            # A preview frame replaces one not yet sent: only the newest is ever worth sending.
            pending["frame"] = message
            if pending["task"] is not None:
                return
            pending["task"] = asyncio.current_task()
            try:
                while pending["frame"] is not None:
                    frame, pending["frame"] = pending["frame"], None
                    await ws.send_bytes(frame)
            finally:
                pending["task"] = None
        else:
            await ws.send_str(message)
            if run.ended:
                await ws.close()

    def send(message):
        """The thread-safe sender the run emits through; a dead socket is silent."""
        future = asyncio.run_coroutine_threadsafe(relay(message), loop)
        future.add_done_callback(lambda f: f.cancelled() or f.exception())

    if not run.connected(send):
        await ws.close(code=4409)
        return ws
    async for msg in ws:
        if msg.type in (WSMsgType.TEXT, WSMsgType.BINARY):
            run.receive(msg.data)
    run.disconnected()
    return ws


async def live_output(mode: str, model: str, prompt: str, setup: list[tuple[str, dict]],
                      filename_prefix: str) -> io.NodeOutput:
    """A new take recorded in a live run, saved under the output directory like SaveVideo's files."""
    sid = PromptServer.instance.client_id
    if sid is None:
        raise RuntimeError(live.OPEN_TAB_ERROR)
    folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(filename_prefix, folder_paths.get_output_directory())
    file = f"{filename}_{counter:05}_.mp4"
    path = os.path.join(folder, file)
    spec = MODELS[model]
    # The browser encodes its webcam at exactly this size, a 16:9 frame at the model's native size:
    # a live session dies on a resolution change mid-chunk.
    input_size = spec.frame_size(1920, 1080) if mode == "style" else None
    run = live.LiveRun(mode, spec, path, prompt, setup, input_size, connect_args())
    live.RUNS[run.run_id] = run
    try:
        PromptServer.instance.send_sync("reactor.live.open",
                                        {"run_id": run.run_id, "mode": mode,
                                         "title": f"Reactor Live — {model}"}, sid)
        await run.run(comfy.model_management.throw_exception_if_processing_interrupted)
    finally:
        live.RUNS.pop(run.run_id, None)
        run.close()
    return io.NodeOutput(InputImpl.VideoFromFile(path), ui=ui.PreviewVideo([ui.SavedResult(file, subfolder, io.FolderType.output)]))


class ReactorLiveStyle(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ReactorLiveStyle",
            display_name="Reactor Live Style",
            category="Reactor",
            description="Restyles your webcam live: a modal opens in your browser tab, and the run records a take.",
            not_idempotent=True,
            is_output_node=True,
            inputs=[
                io.Combo.Input("model", options=[name for name, spec in MODELS.items() if spec.source_track], default="X2"),
                io.String.Input("prompt", multiline=True),
                io.Image.Input("reference_image", optional=True, tooltip="Reference image, for X2."),
                io.Int.Input("seed", default=42, min=0, max=2**31 - 1),
                io.String.Input("filename_prefix", default="reactor/live_style",
                                tooltip="Where each take is saved, under ComfyUI's output folder."),
            ],
            outputs=[io.Video.Output()],
        )

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        # Every queue records a new take.
        return uuid.uuid4().hex

    @classmethod
    async def execute(cls, model, prompt, seed, filename_prefix, reference_image=None) -> io.NodeOutput:
        spec = MODELS[model]
        if reference_image is not None and spec.images == "none":
            logging.warning("%s has no reference image; ignoring it.", model)
            reference_image = None
        image = None if reference_image is None else spec.fit_png(image_to_png(reference_image))
        return await live_output("style", model, prompt, live.style_setup(spec, prompt, seed, image), filename_prefix)


class ReactorRealtimeControl(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ReactorRealtimeControl",
            display_name="Reactor Realtime Control",
            category="Reactor",
            description="Generates live from a prompt: a modal opens in your browser tab where you change the prompt as it plays, and drive with the keyboard on a world model. The run records a take.",
            not_idempotent=True,
            is_output_node=True,
            inputs=[
                ReactorModelType.Input("model"),
                io.String.Input("prompt", multiline=True),
                io.Image.Input("image", optional=True, tooltip="The image the video starts from."),
                io.Int.Input("seed", default=42, min=0, max=2**31 - 1),
                io.String.Input("filename_prefix", default="reactor/realtime_control",
                                tooltip="Where each take is saved, under ComfyUI's output folder."),
            ],
            outputs=[io.Video.Output()],
        )

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return uuid.uuid4().hex

    @classmethod
    async def execute(cls, model, prompt, seed, filename_prefix, image=None) -> io.NodeOutput:
        name, settings = model
        spec = MODELS[name]
        if spec.pattern != "chunked":
            raise ValueError(f"{name} restyles a video; use Reactor Live Style.")
        if image is not None and spec.images == "none":
            logging.warning("%s has no image input; ignoring it.", name)
            image = None
        png = None if image is None else spec.fit_png(image_to_png(image))
        return await live_output("drive", name, prompt, live.drive_setup(name, prompt, seed, png, settings), filename_prefix)


class ReactorExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [ReactorModel, ReactorBeat, ReactorTimeline, ReactorRender, ReactorLiveStyle, ReactorRealtimeControl]


async def comfy_entrypoint() -> ReactorExtension:
    return ReactorExtension()
