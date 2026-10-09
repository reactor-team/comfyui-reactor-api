import asyncio
import contextlib
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
from comfy_api.latest import ComfyExtension, InputImpl, io, ui
from server import PromptServer

from .reactor_render import live
from .reactor_render.config import connect_args, max_sessions
from .reactor_render.clip import ClipStream
from .reactor_render.session import render
from .reactor_render.timeline import MODELS, Beat, ModelSpec, Reference, Setting, Timeline, call_setup, compile_timeline, editor_moves, model_facts, join_chains

ReactorSequenceType = io.Custom("REACTOR_SEQUENCE")
ReactorReferenceType = io.Custom("REACTOR_REFERENCE")
# A browser camera, by its label; the widget lists the cameras the browser can see.
ReactorCameraType = io.Custom("REACTOR_CAMERA")
ReactorCameraDeviceType = io.Custom("REACTOR_CAMERA_DEVICE")
# A browser microphone, by its label, the same way.
ReactorMicrophoneType = io.Custom("REACTOR_MICROPHONE")
ReactorMicrophoneDeviceType = io.Custom("REACTOR_MICROPHONE_DEVICE")


@io.comfytype(io_type="REACTOR_MOVE_EDITOR")
class MoveEditor(io.ComfyTypeIO):
    """A segment's camera lane widget. Its value is `{"moves": [...]}`, each move counted from the segment's start."""
    Type = dict

    class Input(io.WidgetInput):
        def __init__(self, id: str, tooltip: str = None):
            super().__init__(id, tooltip=tooltip, default={"moves": []}, socketless=True)


def image_to_png(image) -> bytes:
    pixels = np.clip(255.0 * image[0].cpu().numpy(), 0, 255).astype(np.uint8)
    buf = bytes_io.BytesIO()
    Image.fromarray(pixels).save(buf, format="PNG")
    return buf.getvalue()


# The factory node that makes each model's references.
REFERENCE_NODES = {"Vidu S2-Avatar": "Reactor Vidu S2-Avatar Reference", "H3 Reference Turbo Realtime": "Reactor H3 Reference Turbo Realtime Picture"}


def references_input(name: str, spec: ModelSpec, tooltip: str):
    """Sockets for a model's own references, one more appearing as each is connected."""
    return io.Autogrow.Input("references", optional=True, template=io.Autogrow.TemplatePrefix(
        input=ReactorReferenceType.Input("reference", tooltip=f"A {REFERENCE_NODES[name]}, {tooltip}"),
        prefix="reference_", min=0, max=spec.max_references))


def video_bytes(video) -> bytes:
    """The video as a file in its own container and codec; save_to re-encodes only a trimmed or cropped video."""
    buf = bytes_io.BytesIO()
    video.save_to(buf)
    return buf.getvalue()


def setting_input(key: str, setting: Setting, tooltip: str):
    if setting.options:
        return io.Combo.Input(key, options=list(setting.options), default=setting.default, optional=True, tooltip=tooltip)
    if isinstance(setting.default, bool):
        return io.Boolean.Input(key, default=setting.default, optional=True, tooltip=tooltip)
    if setting.maximum is not None and isinstance(setting.default, int):
        return io.Int.Input(key, default=setting.default, min=int(setting.minimum), max=int(setting.maximum), optional=True, tooltip=tooltip)
    if setting.maximum is not None:
        return io.Float.Input(key, default=setting.default, min=0.0, max=setting.maximum, step=0.05, optional=True, tooltip=tooltip)
    return io.String.Input(key, default=setting.default, multiline=True, optional=True, tooltip=tooltip)


# Models that share a segment node, as their inputs match; every other model has its own.
SEGMENT_FAMILIES = {"Visko Orbis": ("Visko Orbis Dynamic", "Visko Orbis Stable")}
FAMILY_OF = {name: next((f for f, names in SEGMENT_FAMILIES.items() if name in names), name) for name in MODELS}
# What each model does, as the node menu groups them.
CAPABILITY = {"LongLive-2.0": "Generate video", "Helios": "Generate video", "FastH3": "Generate video", "H3 Reference Turbo Realtime": "Generate video",
              "Visko Orbis Dynamic": "Generate video", "Visko Orbis Stable": "Generate video",
              "LingBot": "Explore worlds", "LingBot World 2": "Explore worlds",
              "Sana Streaming": "Edit video", "X2": "Edit video", "Vidu S2-Editing": "Edit video",
              "Vidu S2-Avatar": "Avatars", "LTX": "Avatars"}


def segment_node(family: str) -> type[io.ComfyNode]:
    """A family's segment node: one segment of a sequence, with the inputs its models take. The first segment also picks the model and its settings."""
    models = [name for name in MODELS if FAMILY_OF[name] == family]
    spec = MODELS[models[0]]
    node_id = "Reactor" + "".join(c for c in family if c.isalnum()) + "Segment"
    display_name = f"Reactor {family} Segment"
    # A chunked model's segments last whole chunks, so the length steps a chunk at a time.
    step = spec.frames_per_chunk or 1
    length = f" Steps by one {step}-frame chunk." if spec.frames_per_chunk else ""

    class Segment(io.ComfyNode):
        @classmethod
        def define_schema(cls):
            return io.Schema(
                node_id=node_id,
                display_name=display_name,
                category=f"Reactor/{CAPABILITY[models[0]]}",
                search_aliases=["reactor segment", "sequence", *models],
                description=f"One segment of a {family} sequence. The first segment sets the model and its settings; connect the others after it, "
                            "and feed the last one to Reactor Render, and to a Reactor Sequence Visualizer to see it.",
                inputs=[
                    *([io.Combo.Input("model", options=models, tooltip="The model the sequence runs on. Read on the first segment.")] if len(models) > 1 else []),
                    *([io.String.Input("prompt", multiline=True)] if spec.prompted else []),
                    io.Float.Input("seconds", default=0, min=0, max=spec.seconds[1], step=0.5,
                                   tooltip=f"How long this segment plays, {spec.seconds[0]:g} to {spec.seconds[1]:g} seconds. "
                                           "0 plays as long as the script needs at the pace.") if spec.seconds else
                    io.Int.Input("frames", default=max(1, round(120 / step)) * step, min=1, max=100000, step=step,
                                 tooltip="How many frames of video this segment plays. It starts where the segment before it in the sequence ends"
                                         + ("; both ends round to the nearest chunk." + length if spec.frames_per_chunk else ".")),
                    *([io.Combo.Input("kind", options=["shot", "cut"],
                                      tooltip="How this segment enters from the one before: shot blends softly, cut starts fresh. The first segment just opens the video.")]
                      if spec.supports_cuts else []),
                    *([io.Image.Input("image", optional=True, tooltip="Reference image." if spec.images == "any" else "Reference image, read on the first segment.")]
                      if spec.images != "none" else []),
                    *([io.Video.Input("video", optional=True, tooltip="Source clip to edit, read on the first segment.")] if spec.videos != "none" else []),
                    *([MoveEditor.Input("moves", tooltip="Camera moves during this segment. A move ends with its segment; continue it on the next segment to keep it going.")]
                      if spec.camera else []),
                    *[setting_input(key, setting, "Read on the first segment.") for key, setting in spec.settings.items()],
                    *[setting_input(key, setting, "Holds until a later segment changes it.") for key, setting in spec.beat_settings.items()],
                    *([references_input(family, spec, "in effect during this segment.")] if spec.references else []),
                    ReactorSequenceType.Input("sequence", optional=True, tooltip="The segments before this one. Leave it empty on the first segment.",
                                           extra_dict={"model_facts": {name: model_facts(MODELS[name]) for name in models},
                                                       "start_settings": list(spec.settings)}),
                ],
                outputs=[ReactorSequenceType.Output()],
            )

        @classmethod
        def execute(cls, frames=0, prompt="", model=models[0], seconds=None, sequence=None, kind="shot", image=None, video=None, moves=None, references=None, **settings) -> io.NodeOutput:
            # A later segment's model and start settings are the first segment's, so its own are not read.
            name = model if sequence is None else sequence.model
            if name not in models:
                raise ValueError(f"{display_name} can't take {name}; use Reactor {FAMILY_OF[name]} Segment.")
            if seconds is not None:
                frames = round(seconds * MODELS[name].fps)
            beat = Beat(prompt, frames, cut=kind == "cut", image=None if image is None else image_to_png(image),
                        video=None if video is None else video_bytes(video), moves=tuple(editor_moves(moves)) if moves else (),
                        references=tuple(r for r in (references or {}).values() if r is not None),
                        settings={key: value for key, value in settings.items() if key in spec.beat_settings and value is not None})
            if sequence is None:
                return io.NodeOutput(Timeline(name, (beat,), {key: value for key, value in settings.items() if key in spec.settings and value is not None}))
            return io.NodeOutput(replace(sequence, beats=(*sequence.beats, beat)))

    Segment.__name__ = Segment.__qualname__ = node_id
    return Segment


SEGMENT_NODES = [segment_node(family) for family in dict.fromkeys(FAMILY_OF.values())]


class ReactorSequenceJoin(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ReactorSequenceJoin",
            display_name="Reactor Sequence Join",
            category="Reactor",
            search_aliases=["concat sequences"],
            description="Play Reactor sequences one after another, as one sequence. They must be for the same model, and the first sequence's first segment sets the settings.",
            inputs=[io.Autogrow.Input("sequences", template=io.Autogrow.TemplatePrefix(
                ReactorSequenceType.Input("sequence", tooltip="A sequence, from its last segment or another Reactor Sequence Join. Each plays after the one before; its first segment enters by its own shot or cut."),
                prefix="sequence_", min=2, max=32))],
            outputs=[ReactorSequenceType.Output()],
        )

    @classmethod
    def execute(cls, sequences) -> io.NodeOutput:
        return io.NodeOutput(join_chains([sequence for sequence in sequences.values() if sequence is not None]))


class ReactorSequenceVisualizer(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ReactorSequenceVisualizer",
            display_name="Reactor Sequence Visualizer",
            category="Reactor",
            search_aliases=["reactor timeline", "timeline visualizer"],
            description="Draws a sequence's segments and camera moves on a frame ruler, snapped to the model's chunks as they will play. "
                        "It only shows the sequence; edit a segment on its own node.",
            inputs=[ReactorSequenceType.Input("sequence", tooltip="The sequence to draw, such as its last segment or a Reactor Sequence Join.")],
        )

    @classmethod
    def execute(cls, sequence) -> io.NodeOutput:
        return io.NodeOutput()


class ReactorViduS2AvatarReference(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ReactorViduS2AvatarReference",
            display_name="Reactor Vidu S2-Avatar Reference",
            category="Reactor/Avatars",
            search_aliases=["reactor reference", "object", "garment", "outfit", "background"],
            description="An image for a Vidu S2-Avatar character to take on: an object to hold, an outfit to wear, or a background. "
                        "Connect it to each segment it lasts for; it goes away at the first segment without it.",
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


class ReactorH3ReferenceTurboRealtimePicture(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ReactorH3ReferenceTurboRealtimePicture",
            display_name="Reactor H3 Reference Turbo Realtime Picture",
            category=f"Reactor/{CAPABILITY['H3 Reference Turbo Realtime']}",
            search_aliases=["reactor reference", "reactor picture", "H3 Reference Turbo Realtime", "character", "object", "setting"],
            description="A reference picture for an H3 Reference Turbo Realtime clip to draw on: a character, an object or a place. "
                        "It guides the look rather than fixing a frame. Connect it to each segment it guides; "
                        "a segment's prompt calls its first connected picture Picture 1, the next Picture 2, and so on.",
            inputs=[io.Image.Input("image", tooltip="JPEG, PNG or WebP; only the first image of a batch is used.")],
            outputs=[ReactorReferenceType.Output()],
        )

    @classmethod
    def execute(cls, image) -> io.NodeOutput:
        return io.NodeOutput(Reference("H3 Reference Turbo Realtime", image_to_png(image), "image"))


class ReactorRender(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ReactorRender",
            display_name="Reactor Render",
            category="Reactor",
            description="Renders a Reactor sequence on its model, with the API key from the REACTOR_API_KEY environment variable or this plugin's config.ini.",
            inputs=[
                ReactorSequenceType.Input("sequence", tooltip="The last segment of a sequence, or a Reactor Sequence Join."),
                io.Int.Input("seed", default=42, min=0, max=2**31 - 1, control_after_generate=True),
            ],
            outputs=[io.Video.Output()],
            hidden=[io.Hidden.unique_id],
        )

    @classmethod
    async def execute(cls, sequence, seed) -> io.NodeOutput:
        connect = connect_args()
        spec = MODELS[sequence.model]
        beats = [replace(b, image=spec.fit_png(b.image)) if b.image is not None else b for b in sequence.beats]
        plan = compile_timeline(sequence.model, beats, seed, sequence.settings)
        out_path = os.path.join(folder_paths.get_temp_directory(), f"reactor_{uuid.uuid4().hex}.mp4")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        pbar = comfy.utils.ProgressBar(1)
        status = lambda text: PromptServer.instance.send_progress_text(f"Status: {text}", cls.hidden.unique_id)
        async with one_session(status):
            await render(spec, plan, out_path,
                         # The preview widget never scales an image up, so frames go at full size to fill the node.
                         on_progress=lambda done, total, frame: pbar.update_absolute(
                             done, total, None if frame is None else ("JPEG", Image.fromarray(frame), max(frame.shape[:2]))),
                         on_status=status,
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


# ComfyUI runs async nodes side by side, and each parallel session takes its own share of Reactor's
# generation capacity, so sessions past the limit queue behind the running ones.
SESSIONS = asyncio.Semaphore(max_sessions())


@contextlib.asynccontextmanager
async def one_session(on_status=None):
    """Holds one of this server's Reactor sessions, waiting for a running one to finish and honoring Cancel meanwhile."""
    if SESSIONS.locked() and on_status:
        on_status("Waiting for another Reactor session to finish")
    while True:
        try:
            await asyncio.wait_for(SESSIONS.acquire(), 0.5)
            break
        # Not the builtin TimeoutError, which asyncio's only aliases from Python 3.11; this plugin supports 3.10.
        except asyncio.TimeoutError:
            comfy.model_management.throw_exception_if_processing_interrupted()
    try:
        yield
    finally:
        SESSIONS.release()


async def live_output(mode: str, model: str, prompt: str, setup: list[tuple[str, dict]],
                      clip: ClipStream | None, filename_prefix: str, camera: str | None = None,
                      settings: dict[str, object] | None = None, microphone: str | None = None) -> io.NodeOutput:
    """A new take recorded in a live run, saved under the output directory like SaveVideo's files."""
    sid = PromptServer.instance.client_id
    if sid is None:
        raise RuntimeError(live.OPEN_TAB_ERROR)
    spec = MODELS[model]
    # The browser asks its camera for this size, a 16:9 frame at the model's native size, and holds
    # it: a live session dies on a resolution change mid-chunk.
    input_size = spec.frame_size(1920, 1080) if mode == "style" and clip is None else None
    # A started prompt drives generation; an avatar's opening line may be empty, so "call" is exempt.
    if spec.prompted and mode != "call" and not prompt.strip():
        raise ValueError(f"{model} needs a text prompt: enter one in the Realtime node's prompt input.")
    # A missing API key fails the node here, before it queues for a session slot.
    connect = connect_args()
    async with one_session():
        # Claim the take's filename inside the session window: scanned from disk, it is only free
        # while no other in-flight take can pick the same one.
        folder, filename, counter, subfolder, _ = folder_paths.get_save_image_path(filename_prefix, folder_paths.get_output_directory())
        file = f"{filename}_{counter:05}_.mp4"
        path = os.path.join(folder, file)
        run = live.LiveRun(mode, spec, path, prompt, setup, input_size, connect, clip, settings)
        live.RUNS[run.run_id] = run
        try:
            PromptServer.instance.send_sync("reactor.live.open",
                                            {"run_id": run.run_id, "mode": mode,
                                             "title": "Reactor Realtime", "camera": camera, "microphone": microphone}, sid)
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


class ReactorMicrophoneCapture(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="ReactorMicrophoneCapture",
            display_name="Reactor Microphone Capture",
            category="Reactor",
            description="Picks the microphone on this browser that you talk through on a Reactor Realtime call.",
            inputs=[ReactorMicrophoneDeviceType.Input("microphone", tooltip="The microphone to talk through. Default is the browser's default microphone.")],
            outputs=[ReactorMicrophoneType.Output()],
        )

    @classmethod
    def execute(cls, microphone) -> io.NodeOutput:
        return io.NodeOutput(microphone)


def live_option(name: str) -> io.DynamicCombo.Option:
    """A model's entry in Reactor Realtime's model picker: the inputs that model takes to start a live run."""
    spec = MODELS[name]
    source = spec.pattern == "source"
    return io.DynamicCombo.Option(name, [
        *([io.String.Input("prompt", multiline=True, tooltip="What you say to the character first; may be empty." if spec.pattern == "call"
                           else "The script the first take speaks. Required. Apply a new one in the window for the next take." if spec.pattern == "takes"
                           else "The prompt the video starts from. Required. Change it in the window as it plays.")] if spec.prompted else []),
        *([io.Image.Input("image", optional=not spec.image_required,
                          tooltip="The person the character is made from." if spec.pattern == "call" else "Reference image.")]
          if spec.images != "none" else []),
        *([references_input(name, spec, "kept by every clip.")] if spec.references and spec.pattern != "call" else []),
        *([io.Video.Input("video", optional=True, tooltip="Source clip to restyle, looped until you press Done."),
           ReactorCameraType.Input("camera", optional=True, tooltip="A Reactor Camera Capture, to stream a camera instead of a clip.")]
          if source else []),
        *([ReactorMicrophoneType.Input("microphone", optional=True,
                                       tooltip="A Reactor Microphone Capture, to talk through a chosen microphone. Default is the browser's default microphone.")]
          if spec.pattern == "call" else []),
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
            search_aliases=["reactor live", *dict.fromkeys(CAPABILITY[name] for name, spec in MODELS.items() if spec.live),
                            *(name for name, spec in MODELS.items() if spec.live)],
            description="Runs a Reactor model in real time: press Run and a window opens in your browser tab where you steer the model as it plays. "
                        "Pick a model and it shows that model's inputs. A video-to-video model restyles a clip or a camera; you talk with an "
                        "avatar out loud or by typed message; any other model generates from the prompt and image, "
                        "with keyboard controls on a world model. Each run records a take; set the seed's control to fixed and a rerun "
                        "with no input changed keeps the last take instead of starting a new session.",
            not_idempotent=True,
            is_output_node=True,
            inputs=[
                io.DynamicCombo.Input("model", options=[live_option(name) for name, spec in MODELS.items() if spec.live],
                                      tooltip="The model to run in real time, with the inputs and settings it takes."),
                io.Int.Input("seed", default=42, min=0, max=2**31 - 1,
                             tooltip="A new seed starts a new session and records a new take. Fixed, with no input changed, a rerun keeps the last take."),
                io.String.Input("filename_prefix", default="reactor/realtime",
                                tooltip="Where each take is saved, under ComfyUI's output folder."),
            ],
            outputs=[io.Video.Output()],
        )

    @classmethod
    async def execute(cls, model, seed, filename_prefix) -> io.NodeOutput:
        name, prompt, image, video, camera = model["model"], model.get("prompt", ""), model.get("image"), model.get("video"), model.get("camera")
        spec = MODELS[name]
        settings = {key: model[key] for key in spec.settings if model.get(key) is not None}
        beat_settings = {key: model[key] for key in spec.beat_settings if model.get(key) is not None}
        png = None if image is None else spec.fit_png(image_to_png(image))
        if spec.pattern == "call":
            return await live_output("call", name, prompt, call_setup(name, png, settings), None, filename_prefix,
                                     microphone=model.get("microphone"))
        if spec.pattern != "source":
            beat = Beat(prompt, 1, image=png, references=tuple(r for r in (model.get("references") or {}).values() if r is not None), settings=beat_settings)
            return await live_output("drive", name, prompt, live.drive_setup(name, beat, seed, settings), None, filename_prefix)
        if (video is None) == (camera is None):
            raise ValueError(f"{name} edits one source: connect a video or a Reactor Camera Capture.")
        clip = None
        if video is not None:
            # Streamed from the compressed video, never get_components(), which holds every frame at full size as float32.
            clip = await ClipStream.open(spec, await asyncio.to_thread(video_bytes, video), loop=True)
            if clip is None:
                raise ValueError(f"{name}'s source video has no frames.")
        try:
            return await live_output("style", name, prompt, live.style_setup(spec, prompt, seed, png, beat_settings), clip,
                                     filename_prefix, camera, beat_settings)
        finally:
            if clip is not None:
                await clip.close()


class ReactorExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [*SEGMENT_NODES, ReactorSequenceJoin, ReactorSequenceVisualizer, ReactorViduS2AvatarReference, ReactorH3ReferenceTurboRealtimePicture, ReactorRender, ReactorCameraCapture, ReactorMicrophoneCapture, ReactorRealtime]


async def comfy_entrypoint() -> ReactorExtension:
    return ReactorExtension()
