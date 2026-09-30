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
from comfy_api.latest import ComfyExtension, InputImpl, Types, io, ui
from server import PromptServer

from .reactor_render import live
from .reactor_render.session import render
from .reactor_render.timeline import MODELS, Beat, Reference, Setting, Timeline, call_setup, compile_timeline, editor_moves, model_facts, join_chains

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")

ReactorChainType = io.Custom("REACTOR_CHAIN")
ReactorReferenceType = io.Custom("REACTOR_REFERENCE")
# A browser camera, by its label; the widget lists the cameras the browser can see.
ReactorCameraType = io.Custom("REACTOR_CAMERA")
ReactorCameraDeviceType = io.Custom("REACTOR_CAMERA_DEVICE")


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


def setting_input(key: str, setting: Setting, tooltip: str):
    if setting.options:
        return io.Combo.Input(key, options=list(setting.options), default=setting.default, optional=True, tooltip=tooltip)
    if isinstance(setting.default, bool):
        return io.Boolean.Input(key, default=setting.default, optional=True, tooltip=tooltip)
    if setting.maximum is not None:
        return io.Float.Input(key, default=setting.default, min=0.0, max=setting.maximum, step=0.05, optional=True, tooltip=tooltip)
    return io.String.Input(key, default=setting.default, multiline=True, optional=True, tooltip=tooltip)


# Models that share a chain node, as their inputs match; every other model has its own.
CHAIN_FAMILIES = {"Visko Orbis": ("Visko Orbis Dynamic", "Visko Orbis Stable")}
FAMILY_OF = {name: next((f for f, names in CHAIN_FAMILIES.items() if name in names), name) for name in MODELS}


def chain_node(family: str) -> type[io.ComfyNode]:
    """A family's chain node: one link of a chain, a beat with the inputs its models take. The first link also picks the model and its settings."""
    models = [name for name in MODELS if FAMILY_OF[name] == family]
    spec = MODELS[models[0]]
    node_id = "Reactor" + "".join(c for c in family if c.isalnum()) + "Chain"
    display_name = f"Reactor {family} Chain"
    # A chunked model's beats last whole chunks, so the length steps a chunk at a time.
    step = spec.frames_per_chunk or 1
    length = f" Steps by one {step}-frame chunk." if spec.frames_per_chunk else ""

    class Chain(io.ComfyNode):
        @classmethod
        def define_schema(cls):
            return io.Schema(
                node_id=node_id,
                display_name=display_name,
                category=f"Reactor/{family}",
                search_aliases=["reactor chain", "beat"],
                description=f"One beat of a {family} chain. The first link sets the model and its settings; chain the others after it, "
                            "and feed the last one to Reactor Render, and to a Reactor Timeline to see it.",
                inputs=[
                    *([io.Combo.Input("model", options=models, tooltip="The model the chain runs on. Read on the first link.")] if len(models) > 1 else []),
                    *([io.String.Input("prompt", multiline=True)] if spec.prompted else []),
                    io.Int.Input("frames", default=max(1, round(120 / step)) * step, min=1, max=100000, step=step,
                                 tooltip="How many frames of video this beat plays. It starts where the beat before it in the chain ends"
                                         + ("; both ends round to the nearest chunk." + length if spec.frames_per_chunk else ".")),
                    *([io.Combo.Input("kind", options=["shot", "cut"],
                                      tooltip="How this beat enters from the one before: shot blends softly, cut starts fresh. The first beat just opens the video.")]
                      if spec.supports_cuts else []),
                    *([io.Image.Input("image", optional=True, tooltip="Reference image." if spec.images == "any" else "Reference image, read on the first beat.")]
                      if spec.images != "none" else []),
                    *([io.Video.Input("video", optional=True, tooltip="Source clip to edit, read on the first beat.")] if spec.videos != "none" else []),
                    *([MoveEditor.Input("moves", tooltip="Camera moves during this beat. A move ends with its beat; continue it on the next beat to keep it going.")]
                      if spec.camera else []),
                    *[setting_input(key, setting, "Read on the first link.") for key, setting in spec.settings.items()],
                    *[setting_input(key, setting, "Holds until a later link changes it.") for key, setting in spec.beat_settings.items()],
                    *([io.Autogrow.Input("references", optional=True, template=io.Autogrow.TemplatePrefix(
                        input=ReactorReferenceType.Input("reference", tooltip=f"A Reactor {family} Reference, in effect during this beat."),
                        prefix="reference_", min=0, max=3))] if spec.references else []),
                    ReactorChainType.Input("chain", optional=True, tooltip="The links before this one. Leave it empty on the first link.",
                                           extra_dict={"model_facts": {name: model_facts(MODELS[name]) for name in models},
                                                       "start_settings": list(spec.settings)}),
                ],
                outputs=[ReactorChainType.Output()],
            )

        @classmethod
        def execute(cls, frames, prompt="", model=models[0], chain=None, kind="shot", image=None, video=None, moves=None, references=None, **settings) -> io.NodeOutput:
            # A later link's model and start settings are the first link's, so its own are not read.
            name = model if chain is None else chain.model
            if name not in models:
                raise ValueError(f"{display_name} can't take {name}; use Reactor {FAMILY_OF[name]} Chain.")
            beat = Beat(prompt, frames, cut=kind == "cut", image=None if image is None else image_to_png(image),
                        video=None if video is None else video_to_mp4(video), moves=tuple(editor_moves(moves)) if moves else (),
                        references=tuple(r for r in (references or {}).values() if r is not None),
                        settings={key: value for key, value in settings.items() if key in spec.beat_settings and value is not None})
            if chain is None:
                return io.NodeOutput(Timeline(name, (beat,), {key: value for key, value in settings.items() if key in spec.settings and value is not None}))
            return io.NodeOutput(replace(chain, beats=(*chain.beats, beat)))

    Chain.__name__ = Chain.__qualname__ = node_id
    return Chain


CHAIN_NODES = [chain_node(family) for family in dict.fromkeys(FAMILY_OF.values())]


class ReactorChainJoin(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ReactorChainJoin",
            display_name="Reactor Chain Join",
            category="Reactor",
            search_aliases=["concat chains"],
            description="Play Reactor chains one after another, as one chain. They must be for the same model, and the first chain's first link sets the settings.",
            inputs=[io.Autogrow.Input("chains", template=io.Autogrow.TemplatePrefix(
                ReactorChainType.Input("chain", tooltip="A chain, from its last link or another Reactor Chain Join. Each plays after the one before; its first beat enters by its own shot or cut."),
                prefix="chain_", min=2, max=32))],
            outputs=[ReactorChainType.Output()],
        )

    @classmethod
    def execute(cls, chains) -> io.NodeOutput:
        return io.NodeOutput(join_chains([chain for chain in chains.values() if chain is not None]))


class ReactorTimeline(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ReactorTimeline",
            display_name="Reactor Timeline",
            category="Reactor",
            description="Draws a chain's beats and camera moves on a frame ruler, snapped to the model's chunks as they will play. "
                        "It only shows the chain; edit a beat on its chain node.",
            inputs=[ReactorChainType.Input("chain", tooltip="The chain to draw, such as its last link or a Reactor Chain Join.")],
        )

    @classmethod
    def execute(cls, chain) -> io.NodeOutput:
        return io.NodeOutput()


class ReactorViduS2AvatarReference(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ReactorViduS2AvatarReference",
            display_name="Reactor Vidu S2-Avatar Reference",
            category="Reactor/Vidu S2-Avatar",
            search_aliases=["reactor reference", "object", "garment", "outfit", "background"],
            description="An image for a Vidu S2-Avatar character to take on: an object to hold, an outfit to wear, or a background. "
                        "Connect it to each beat it lasts for; it goes away at the first beat without it.",
            inputs=[
                io.Image.Input("image", tooltip="For an object or outfit, use a plain background, so the character takes the item and not its surroundings."),
                io.Combo.Input("kind", options=["object", "garment", "background"],
                               tooltip="What the image is: an object to hold, a garment to wear, or a background to stand in."),
                io.String.Input("text", default="", multiline=True, optional=True,
                                tooltip="One plain sentence saying what happens, such as \"He holds up the crystal ball.\" Up to 200 characters are sent."),
            ],
            outputs=[ReactorReferenceType.Output()],
        )

    @classmethod
    def execute(cls, image, kind, text="") -> io.NodeOutput:
        return io.NodeOutput(Reference("Vidu S2-Avatar", image_to_png(image), kind, text.strip()))


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
            description="Renders a Reactor chain on its model, with the API key from the REACTOR_API_KEY environment variable or this plugin's config.ini.",
            inputs=[
                ReactorChainType.Input("chain", tooltip="The last beat of a chain, or a Reactor Chain Join."),
                io.Int.Input("seed", default=42, min=0, max=2**31 - 1, control_after_generate=True),
            ],
            outputs=[io.Video.Output()],
            hidden=[io.Hidden.unique_id],
        )

    @classmethod
    async def execute(cls, chain, seed) -> io.NodeOutput:
        connect = connect_args()
        spec = MODELS[chain.model]
        beats = [replace(b, image=spec.fit_png(b.image)) if b.image is not None else b for b in chain.beats]
        plan = compile_timeline(chain.model, beats, seed, chain.settings)
        out_path = os.path.join(folder_paths.get_temp_directory(), f"reactor_{uuid.uuid4().hex}.mp4")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        pbar = comfy.utils.ProgressBar(1)
        await render(spec, plan, out_path,
                     # The preview widget never scales an image up, so frames go at full size to fill the node.
                     on_progress=lambda done, total, frame: pbar.update_absolute(
                         done, total, None if frame is None else ("JPEG", Image.fromarray(frame), max(frame.shape[:2]))),
                     on_status=lambda text: PromptServer.instance.send_progress_text(f"Status: {text}", cls.hidden.unique_id),
                     check_interrupt=comfy.model_management.throw_exception_if_processing_interrupted,
                     **connect)
        PromptServer.instance.send_progress_text("Status: Completed", cls.hidden.unique_id)
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
                      clip: list[np.ndarray] | None, filename_prefix: str, camera: str | None = None,
                      settings: dict[str, object] | None = None) -> io.NodeOutput:
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
    run = live.LiveRun(mode, spec, path, prompt, setup, input_size, connect_args(), clip, settings)
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


def live_option(name: str) -> io.DynamicCombo.Option:
    """A model's entry in Reactor Realtime's model picker: the inputs that model takes to start a live run."""
    spec = MODELS[name]
    source = spec.pattern == "source"
    return io.DynamicCombo.Option(name, [
        *([io.String.Input("prompt", multiline=True, tooltip="What you say to the character first." if spec.pattern == "call"
                           else "The prompt the video starts from. Change it in the window as it plays.")] if spec.prompted else []),
        *([io.Image.Input("image", optional=not spec.image_required,
                          tooltip="The person the character is made from." if spec.pattern == "call" else "Reference image.")]
          if spec.images != "none" else []),
        *([io.Video.Input("video", optional=True, tooltip="Source clip to restyle, looped until you press Done."),
           ReactorCameraType.Input("camera", optional=True, tooltip="A Reactor Camera Capture, to stream a camera instead of a clip.")]
          if source else []),
        *[setting_input(key, setting, "Read at start.") for key, setting in spec.settings.items()],
        *[setting_input(key, setting, "Read at start." if spec.prompted else "Change it in the window as it plays.")
          for key, setting in spec.beat_settings.items()],
    ])


class ReactorRealtime(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ReactorRealtime",
            display_name="Reactor Realtime",
            category="Reactor",
            description="Runs a Reactor model live in a modal in your browser tab, and you change the prompt as it plays. "
                        "Pick a model and it shows that model's inputs. A video-to-video model restyles a clip or a camera; you talk with an "
                        "avatar out loud or by typed message; any other model generates from the prompt and image, "
                        "with keyboard controls on a world model. Each run records a take.",
            not_idempotent=True,
            is_output_node=True,
            inputs=[
                io.DynamicCombo.Input("model", options=[live_option(name) for name, spec in MODELS.items() if spec.live],
                                      tooltip="The model to run live, with the inputs and settings it takes."),
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
    async def execute(cls, model, seed, filename_prefix) -> io.NodeOutput:
        name, prompt, image, video, camera = model["model"], model.get("prompt", ""), model.get("image"), model.get("video"), model.get("camera")
        spec = MODELS[name]
        settings = {key: model[key] for key in spec.settings if model.get(key) is not None}
        beat_settings = {key: model[key] for key in spec.beat_settings if model.get(key) is not None}
        png = None if image is None else spec.fit_png(image_to_png(image))
        if spec.pattern == "call":
            return await live_output("call", name, prompt, call_setup(name, png, settings), None, filename_prefix)
        if spec.pattern != "source":
            beat = Beat(prompt, 1, image=png, settings=beat_settings)
            return await live_output("drive", name, prompt, live.drive_setup(name, beat, seed, settings), None, filename_prefix)
        if (video is None) == (camera is None):
            raise ValueError(f"{name} edits one source: connect a video or a Reactor Camera Capture.")
        clip = None
        if video is not None:
            # Converted off the loop: a long clip would stall the server's other requests.
            clip = await asyncio.to_thread(lambda: [np.asarray(spec.fit(Image.fromarray(np.clip(255.0 * f.cpu().numpy(), 0, 255).astype(np.uint8))))
                                                    for f in video.get_components().images])
        return await live_output("style", name, prompt, live.style_setup(spec, prompt, seed, png, beat_settings), clip, filename_prefix,
                                 camera, beat_settings)


class ReactorExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [*CHAIN_NODES, ReactorChainJoin, ReactorTimeline, ReactorViduS2AvatarReference, ReactorRender, ReactorCameraCapture, ReactorRealtime]


async def comfy_entrypoint() -> ReactorExtension:
    return ReactorExtension()
