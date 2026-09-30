import asyncio
import configparser
import io as bytes_io
import logging
import os
import uuid
from dataclasses import replace

import av
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
# A browser camera, by its label; the widget lists the cameras the browser can see.
ReactorCameraType = io.Custom("REACTOR_CAMERA")
ReactorCameraDeviceType = io.Custom("REACTOR_CAMERA_DEVICE")


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

    async def relay(message):
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
        if msg.type == WSMsgType.TEXT:
            run.receive(msg.data)
    run.disconnected()
    return ws


async def live_output(mode: str, model: str, prompt: str, setup: list[tuple[str, dict]],
                      clip: list[np.ndarray] | None, filename_prefix: str, camera: str | None = None) -> io.NodeOutput:
    """A new take recorded in a live run, saved under the output directory like SaveVideo's files."""
    sid = PromptServer.instance.client_id
    if sid is None:
        raise RuntimeError(live.OPEN_TAB_ERROR)
    folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(filename_prefix, folder_paths.get_output_directory())
    file = f"{filename}_{counter:05}_.mp4"
    path = os.path.join(folder, file)
    spec = MODELS[model]
    # The browser asks its camera for this size, a 16:9 frame at the model's native size, and holds
    # it: a live session dies on a resolution change mid-chunk.
    input_size = spec.frame_size(1920, 1080) if mode == "style" and clip is None else None
    run = live.LiveRun(mode, spec, path, prompt, setup, input_size, connect_args(), clip)
    live.RUNS[run.run_id] = run
    try:
        PromptServer.instance.send_sync("reactor.live.open",
                                        {"run_id": run.run_id, "mode": mode,
                                         "title": "Reactor Realtime", "camera": camera}, sid)
        await run.run(comfy.model_management.throw_exception_if_processing_interrupted)
    finally:
        live.RUNS.pop(run.run_id, None)
    return io.NodeOutput(InputImpl.VideoFromFile(path), ui=ui.PreviewVideo([ui.SavedResult(file, subfolder, io.FolderType.output)]))


class ReactorCameraCapture(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ReactorCameraCapture",
            display_name="Reactor Camera Capture",
            category="Reactor",
            description="Streams a camera on this browser into Reactor Realtime as the source for a video-to-video model.",
            inputs=[ReactorCameraDeviceType.Input("camera", tooltip="The camera to stream. Default is the browser's default camera.")],
            outputs=[ReactorCameraType.Output()],
        )

    @classmethod
    def execute(cls, camera) -> io.NodeOutput:
        return io.NodeOutput(camera)


class ReactorRealtime(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ReactorRealtime",
            display_name="Reactor Realtime",
            category="Reactor",
            description="Runs a Reactor model live in a modal in your browser tab, where you change the prompt as it plays. "
                        "A video-to-video model restyles the camera or video you connect; any other model generates "
                        "from the prompt and image, with keyboard controls on a world model. Each run records a take.",
            not_idempotent=True,
            is_output_node=True,
            inputs=[
                ReactorModelType.Input("model"),
                io.String.Input("prompt", multiline=True),
                io.Image.Input("image", optional=True, tooltip="The image the video starts from, or X2's reference image."),
                io.Video.Input("video", optional=True, tooltip="A source video for video-to-video models, looped until you press Done."),
                ReactorCameraType.Input("camera", optional=True, tooltip="A Reactor Camera Capture, to stream a camera into a video-to-video model instead of a video."),
                io.Int.Input("seed", default=42, min=0, max=2**31 - 1),
                io.String.Input("filename_prefix", default="reactor/realtime",
                                tooltip="Where each take is saved, under ComfyUI's output folder."),
            ],
            outputs=[io.Video.Output()],
        )

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        # Every queue records a new take.
        return uuid.uuid4().hex

    @classmethod
    async def execute(cls, model, prompt, seed, filename_prefix, image=None, video=None, camera=None) -> io.NodeOutput:
        name, settings = model
        spec = MODELS[name]
        if not spec.live:
            raise ValueError(f"{name} isn't available in Reactor Realtime yet; render it with Reactor Render instead.")
        if image is not None and spec.images == "none":
            logging.warning("%s has no image input; ignoring it.", name)
            image = None
        png = None if image is None else spec.fit_png(image_to_png(image))
        if spec.pattern != "source":
            if video is not None or camera is not None:
                logging.warning("%s does not take a source video or camera; ignoring it.", name)
            return await live_output("drive", name, prompt, live.drive_setup(name, prompt, seed, png, settings), None, filename_prefix)
        if video is None and camera is None:
            raise ValueError(f"{name} needs a source: connect a Load Video or a Reactor Camera Capture.")
        if video is not None and camera is not None:
            raise ValueError(f"{name} takes one source: disconnect the Load Video or the Reactor Camera Capture.")
        clip = None
        if video is not None:
            mp4 = video_to_mp4(video)
            # Decoded off the loop: a long clip would stall the server's other requests.
            clip = await asyncio.to_thread(lambda: [np.asarray(spec.fit(f.to_image())) for f in av.open(bytes_io.BytesIO(mp4)).decode(video=0)])
        return await live_output("style", name, prompt, live.style_setup(spec, prompt, seed, png), clip, filename_prefix, camera)


class ReactorExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [ReactorModel, ReactorBeat, ReactorTimeline, ReactorRender, ReactorCameraCapture, ReactorRealtime]


async def comfy_entrypoint() -> ReactorExtension:
    return ReactorExtension()
