import configparser
import io as bytes_io
import os
import uuid

import numpy as np
from PIL import Image
from typing_extensions import override

import comfy.model_management
import comfy.utils
import folder_paths
from comfy.cli_args import args
from comfy_api.latest import ComfyExtension, InputImpl, io

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
                MoveEditor.Input("moves", tooltip="Camera moves during this beat, for models with camera controls. A move ends with its beat; continue it on the next beat to keep it going."),
                ReactorBeatsType.Input("chain", optional=True),
            ],
            outputs=[ReactorBeatsType.Output()],
        )

    @classmethod
    def execute(cls, prompt, frames, kind, moves, image=None, chain=None) -> io.NodeOutput:
        beat = Beat(prompt, frames, cut=kind == "cut", image=None if image is None else image_to_png(image),
                    moves=tuple(editor_moves(moves)))
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
        # REACTOR_LOCAL=1 targets a model served by `reactor run` on this machine, which needs no key.
        if os.environ.get("REACTOR_LOCAL") == "1":
            connect = {"local": True}
            if os.environ.get("REACTOR_API_URL"):
                connect["api_url"] = os.environ["REACTOR_API_URL"]
        elif key := os.environ.get("REACTOR_API_KEY") or saved_api_key():
            connect = {"api_key": key}
        else:
            raise RuntimeError(f"Set REACTOR_API_KEY in {CONFIG_PATH} or in the environment ComfyUI starts from.")
        plan = compile_timeline(timeline.model, list(timeline.beats), seed, timeline.settings, list(timeline.moves))
        out_path = os.path.join(folder_paths.get_temp_directory(), f"reactor_{uuid.uuid4().hex}.mp4")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        pbar = comfy.utils.ProgressBar(1)
        await render(MODELS[timeline.model], plan, out_path,
                     on_progress=lambda done, total, frame: pbar.update_absolute(
                         done, total, None if frame is None else ("JPEG", Image.fromarray(frame), args.preview_size)),
                     check_interrupt=comfy.model_management.throw_exception_if_processing_interrupted,
                     **connect)
        return io.NodeOutput(InputImpl.VideoFromFile(out_path))


class ReactorExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [ReactorModel, ReactorBeat, ReactorTimeline, ReactorRender]


async def comfy_entrypoint() -> ReactorExtension:
    return ReactorExtension()
