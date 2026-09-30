import io
import itertools
import logging
import math
from dataclasses import dataclass, field, replace

from PIL import Image, ImageOps


@dataclass(frozen=True)
class Beat:
    """A timeline beat: for `frames` frames of video, the video follows `prompt`. Beats play in order.

    `cut` asks for a hard scene break from the beat before instead of a soft transition. `image`
    is PNG bytes for models that condition on a reference image. `video` is the source clip as
    MP4 bytes, for video-to-video models, and only the first beat's is read. `moves` are camera
    moves counted from the beat's own start and cut at its end.
    """
    prompt: str
    frames: int
    cut: bool = False
    image: bytes | None = None
    video: bytes | None = None
    moves: tuple["Move", ...] = ()


@dataclass(frozen=True)
class Move:
    """A camera move: from frame `start_frame`, for `frames` frames, the camera lane `lane` holds `value`.

    Moves run on their own clock beside the beats, and moves on different lanes may overlap. `speed`
    is how fast a rotation turns, for lanes that have one; unset, it turns at the default.
    """
    lane: str
    value: str | float
    start_frame: int
    frames: int
    speed: float | None = None


@dataclass(frozen=True)
class Timeline:
    """A whole render request: the model, its settings, its beats in play order, and its camera moves."""
    model: str
    beats: tuple[Beat, ...]
    settings: dict[str, str] = field(default_factory=dict)
    moves: tuple[Move, ...] = ()


@dataclass(frozen=True)
class Setting:
    """A model option picked on Reactor Model and sent before `start` as `command` with one field."""
    command: str
    options: tuple[str, ...]
    default: str


@dataclass(frozen=True)
class Lane:
    """A camera control the model holds as state: a value sent with `command` lasts until the next one.

    `options` maps each value a move can pick to what is sent for it; a lane with no options takes a
    number up to `maximum`. `idle` is sent when no move covers a chunk. `speed` names the lane a
    move's speed is sent on; that lane is set only through those moves and holds between them.
    """
    command: str
    options: dict[str, object]
    idle: object
    maximum: float | None = None
    speed: str | None = None


@dataclass(frozen=True)
class ModelSpec:
    """A Reactor model as the plugin drives it.

    `pattern` is how the model is driven: "chunked" models generate from prompts and report each
    chunk, "source" models transform a video the client sends, and "clips" models build and play
    queued clips. `images` is which beats may carry an image: "none", "first" (read only at
    start), or "any". `frames_per_chunk` is set only for models whose timeline is scheduled by chunk.
    `session_frames` is whether `chunk_complete.frames_emitted` counts the whole session rather
    than the one chunk. `image_required` is whether the opening beat must carry an image.
    `videos` is which beats may carry one, "none" or "first", as `images` is. `video_required`
    is whether the opening beat must carry one. `source_track` names the inbound track a
    "source" model's live source is pushed to. `first_chunk_frames` is the length of a scene's
    first chunk when it differs from the rest. `size` is the native frame size images and source
    frames are fitted to; with `keeps_aspect`, only its short side is fixed and the source's aspect holds.
    `starts` is whether the model waits for a `start` command before generating. `live` is whether
    Reactor Realtime offers the model.
    `settings` maps each setting's command field to the setting. `camera` maps each camera lane,
    named by its command's field, to the lane. `prompt_command` changes the prompt mid-run.
    """
    slug: str
    pattern: str
    fps: float
    images: str
    supports_cuts: bool
    frames_per_chunk: int | None = None
    max_scene_chunks: int | None = None
    session_frames: bool = False
    image_required: bool = False
    videos: str = "none"
    video_required: bool = False
    source_track: str | None = None
    first_chunk_frames: int | None = None
    size: tuple[int, int] = (1280, 704)
    keeps_aspect: bool = False
    starts: bool = True
    live: bool = True
    settings: dict[str, Setting] = field(default_factory=dict)
    camera: dict[str, Lane] = field(default_factory=dict)
    prompt_command: str = "set_prompt"

    def frame_size(self, width: int, height: int) -> tuple[int, int]:
        """The size a width x height input is sent at."""
        if not self.keeps_aspect:
            return self.size
        scale = min(self.size) / min(width, height)
        return round(width * scale / 2) * 2, round(height * scale / 2) * 2

    def fit(self, image: Image.Image) -> Image.Image:
        """The image scaled to cover `frame_size` and center-cropped to it."""
        return ImageOps.fit(image.convert("RGB"), self.frame_size(*image.size), Image.LANCZOS)

    def fit_png(self, png: bytes) -> bytes:
        buf = io.BytesIO()
        self.fit(Image.open(io.BytesIO(png))).save(buf, format="PNG")
        return buf.getvalue()

    def chunks_for(self, frames: float) -> int:
        """How many chunks of a scene come nearest to `frames` of video, halves rounding up as the editor's do."""
        first = self.first_chunk_frames or self.frames_per_chunk
        return 0 if frames < first / 2 else 1 + math.floor((frames - first) / self.frames_per_chunk + 0.5)

    def frames_in(self, chunks: int) -> int:
        """The frames of video in a scene's first `chunks` chunks."""
        return 0 if chunks == 0 else (self.first_chunk_frames or self.frames_per_chunk) + (chunks - 1) * self.frames_per_chunk


VISKO_RESOLUTION = ("native", "1080p", "2k", "4k")


def choices(*values: str) -> dict[str, str]:
    return {v: v for v in values}


# One 6-value pose, `[rx, ry, rz, tx, ty, tz]`, applies to every frame. Signs measured on cloud sessions:
# +ry yaws right, -tx moves left, +tz moves in, -ty moves up.
POSES = {
    "orbit_left": [0, 0.04, 0, -0.35, 0, 0],
    "orbit_right": [0, -0.04, 0, 0.35, 0, 0],
    "dolly_in": [0, 0, 0, 0, 0, 1],
    "dolly_out": [0, 0, 0, 0, 0, -1],
    "crane_up": [0, 0, 0, 0, -1, 0],
    "crane_down": [0, 0, 0, 0, 1, 0],
}

LOOK = {"look_horizontal": Lane("set_look_horizontal", choices("left", "right"), "idle", speed="rotation_speed_deg"),
        "look_vertical": Lane("set_look_vertical", choices("up", "down"), "idle", speed="rotation_speed_deg"),
        "rotation_speed_deg": Lane("set_rotation_speed_deg", {}, 5.0, maximum=30.0)}
LINGBOT_CAMERA = {"movement": Lane("set_movement", choices("forward", "back", "strafe_left", "strafe_right"), "idle"), **LOOK}
LINGBOT_WORLD_2_CAMERA = {"camera_pose": Lane("set_camera_pose", POSES, []),
                          "move_longitudinal": Lane("set_move_longitudinal", choices("forward", "back"), "idle"),
                          "move_lateral": Lane("set_move_lateral", choices("strafe_left", "strafe_right"), "idle"),
                          **LOOK}


MODELS = {
    # Measured on cloud sessions: a scene's first chunk is 29 frames and every later one 32, where the docs say 29,
    # and `frames_emitted` counts the whole session.
    "LongLive-2.0": ModelSpec("reactor/longlive-v2", "chunked", 24.0, "none", True, frames_per_chunk=32, max_scene_chunks=48,
                              session_frames=True, first_chunk_frames=29, size=(1280, 704), prompt_command="set_shot"),
    # TODO: Helios publishes no frame rate, so the 24 the output file plays at is a placeholder.
    "Helios": ModelSpec("reactor/helios", "chunked", 24.0, "any", False, frames_per_chunk=33, size=(640, 384),
                        settings={"sr_scale": Setting("set_sr_scale", ("off", "2x", "4x"), "2x")}),
    # Measured on cloud sessions, for both LingBots: a run's first chunk is 17 frames and every later one 24.
    # `max_scene_chunks` is a run, after which the model restarts from its image.
    # TODO: LingBot's docs say 16 fps, but frames arrive at about 38, so the output file's rate is unsettled.
    "LingBot": ModelSpec("reactor/lingbot", "chunked", 16.0, "first", False, frames_per_chunk=24, max_scene_chunks=300,
                         image_required=True, first_chunk_frames=17, size=(1664, 960), camera=LINGBOT_CAMERA),
    # The docs say about 12 frames a chunk; 17 then 24 is what cloud sessions emit.
    "LingBot World 2": ModelSpec("reactor/lingbot-world-2", "chunked", 48.0, "first", False, frames_per_chunk=24,
                                 max_scene_chunks=10000, image_required=True, first_chunk_frames=17, size=(1664, 960),
                                 camera=LINGBOT_WORLD_2_CAMERA),
    # `max_scene_chunks` is `generation_started.max_chunks` on cloud sessions at `2k`; the Dynamic docs say 229.
    "Visko Orbis Dynamic": ModelSpec("reactor/visko-orbis-dynamic", "chunked", 18.0, "first", False, frames_per_chunk=33,
                                     max_scene_chunks=2000, size=(832, 480), settings={"resolution": Setting("set_resolution", VISKO_RESOLUTION, "2k")}),
    "Visko Orbis Stable": ModelSpec("reactor/visko-orbis-stable", "chunked", 18.0, "first", False, frames_per_chunk=33,
                                    max_scene_chunks=2000, size=(832, 480), settings={"resolution": Setting("set_resolution", VISKO_RESOLUTION[1:], "2k")}),
    # TODO: SANA-Streaming's docs publish no frame rate, so the 24 the output file plays at is
    # a placeholder.
    "Sana Streaming": ModelSpec("reactor/sana-streaming", "source", 24.0, "none", False, videos="first", video_required=True,
                                source_track="camera", size=(1280, 704), live=False),
    "X2": ModelSpec("xmax/x2", "source", 24.0, "first", False, videos="first", video_required=True, source_track="source",
                size=(1472, 832), keeps_aspect=True, starts=False),
}


def model_facts(spec: ModelSpec) -> dict:
    """What the timeline editor needs to draw a model's chunk grid and flag beats it would reject."""
    return {
        "fps": spec.fps,
        "frames_per_chunk": spec.frames_per_chunk,
        "first_chunk_frames": spec.first_chunk_frames or spec.frames_per_chunk,
        "supports_cuts": spec.supports_cuts,
        "max_scene_chunks": spec.max_scene_chunks,
        "images": spec.images,
        "image_required": spec.image_required,
        "videos": spec.videos,
        "video_required": spec.video_required,
        "camera": {name: {"options": list(lane.options), "idle": lane.idle, "maximum": lane.maximum,
                          "speed": lane.speed and {"idle": spec.camera[lane.speed].idle, "maximum": spec.camera[lane.speed].maximum}}
                   for name, lane in spec.camera.items() if name not in speed_lanes(spec)},
    }


def editor_beats(value: dict, images: dict[str, bytes]) -> list[Beat]:
    """The beats stored in a timeline editor's widget value. `images` maps a connected slot name to PNG bytes."""
    beats = []
    for b in value["beats"]:
        slot = b["image"]
        if slot is not None and slot not in images:
            raise ValueError(f"Beat {len(beats) + 1} uses {slot}, which has no image connected.")
        beats.append(Beat(b["prompt"], int(b["frames"]), cut=b["cut"], image=None if slot is None else images[slot]))
    return beats


def editor_moves(value: dict) -> list[Move]:
    """The camera moves stored in a timeline editor's widget value. A value saved before moves existed has none."""
    return [Move(m["lane"], m["value"], int(m["start_frame"]), int(m["frames"]), m.get("speed")) for m in value.get("moves", [])]


@dataclass
class Plan:
    """Commands that render a timeline on one model.

    `setup` is sent in order before the model runs. `timed` holds `(chunk, command, data)` to send
    once `chunk` chunks have completed, for beats the model cannot schedule itself. `chunks` is how
    many chunks of video to capture. `source` is the clip a "source" runner streams, as MP4 bytes;
    for a "source" model `chunks` and the first element of each `timed` entry count source frames
    pushed, as a source model reports no chunks. A command value holding `bytes` is a file to
    upload first.
    """
    setup: list[tuple[str, dict]]
    chunks: int
    timed: list[tuple[int, str, dict]] = field(default_factory=list)
    source: bytes | None = None


def compile_timeline(model: str, beats: list[Beat], seed: int, settings: dict[str, str] | None = None,
                     moves: list[Move] | None = None) -> Plan:
    spec = MODELS[model]
    if not beats:
        raise ValueError("The timeline has no beats.")
    for i, beat in enumerate(beats):
        if beat.image is not None and spec.images == "none":
            raise ValueError(f"{model} does not take reference images.")
        if beat.video is not None and spec.videos == "none":
            raise ValueError(f"{model} does not take a source video.")
        # The first beat opens the video, so there is nothing for it to cut from.
        if i and beat.cut and not spec.supports_cuts:
            raise ValueError(f"{model} has no hard cuts; use a shot beat instead.")
    if spec.image_required and beats[0].image is None:
        raise ValueError(f"{model} needs an image on the first beat.")
    if spec.video_required and beats[0].video is None:
        raise ValueError(f"{model} needs a video on the first beat.")
    if spec.images == "first":
        beats = list(beats)
        for i, start in enumerate(itertools.accumulate(b.frames for b in beats[:-1]), 1):
            if beats[i].image is not None:
                logging.warning("%s reads its image only at start; dropping the image on the beat at frame %d.", model, start)
                beats[i] = replace(beats[i], image=None)
    if spec.videos == "first":
        beats = list(beats)
        for i, start in enumerate(itertools.accumulate(b.frames for b in beats[:-1]), 1):
            if beats[i].video is not None:
                logging.warning("%s reads its video only at start; dropping the video on the beat at frame %d.", model, start)
                beats[i] = replace(beats[i], video=None)
    moves = list(moves or [])
    if any(beat.moves for beat in beats):
        total, starts = scheduled_chunks(spec, beats)
        for beat, first, end in zip(beats, starts, starts[1:] + [total]):
            offset, stop = spec.frames_in(first), spec.frames_in(end)
            for m in beat.moves:
                start = offset + m.start_frame
                if start >= stop:
                    logging.warning("%s: dropping the %s move at frame %d of a beat, after the beat ends.", model, m.lane, m.start_frame)
                    continue
                moves.append(replace(m, start_frame=start, frames=min(start + m.frames, stop) - start))
    plan = COMPILERS[model](model, spec, beats, seed)

    # A model without `start` ("source") takes its settings and camera commands at setup's end.
    def at_start() -> int:
        return plan.setup.index(("start", {})) if ("start", {}) in plan.setup else len(plan.setup)

    plan.setup[at_start():at_start()] = [(spec.settings[name].command, {name: value}) for name, value in (settings or {}).items()]
    for chunk, command, data in camera_commands(model, spec, moves, plan.chunks):
        if chunk == 0:
            plan.setup.insert(at_start(), (command, data))
        else:
            # A live change applies from the chunk after the one generating, so it goes out a chunk early.
            plan.timed.append((chunk - 1, command, data))
    return plan


def camera_commands(model: str, spec: ModelSpec, moves: list[Move], chunks: int) -> list[tuple[int, str, dict]]:
    """`(chunk, command, data)` for each chunk where a camera lane's value changes, in chunk order.

    Each lane holds the value of the move covering a chunk, or its idle value; a move's edges snap to
    chunks as a beat's do, and a move past the last chunk is cut there.
    """
    held: dict[str, list] = {}
    for move in moves:
        if not spec.camera:
            raise ValueError(f"{model} has no camera controls.")
        if move.lane not in spec.camera:
            raise ValueError(f"{model} has no {move.lane} camera control.")
        lane = spec.camera[move.lane]
        if lane.options and move.value not in lane.options:
            raise ValueError(f"{move.value} is not a {move.lane} move.")
        first = spec.chunks_for(move.start_frame)
        end = min(spec.chunks_for(move.start_frame + move.frames), chunks)
        if first >= chunks:
            logging.warning("%s: dropping the %s move at frame %d, after the timeline ends.", model, move.lane, move.start_frame)
            continue
        if end <= first:
            raise ValueError(f"The {move.lane} move at frame {move.start_frame} lasts less than one {spec.frames_per_chunk}-frame chunk.")
        values = held.setdefault(move.lane, [None] * chunks)
        if any(v is not None for v in values[first:end]):
            raise ValueError(f"Two {move.lane} moves overlap at frame {move.start_frame}.")
        values[first:end] = [lane.options[move.value] if lane.options else float(move.value)] * (end - first)
        if lane.speed:
            speed = spec.camera[lane.speed].idle if move.speed is None else float(move.speed)
            speeds = held.setdefault(lane.speed, [None] * chunks)
            if any(v is not None and v != speed for v in speeds[first:end]):
                raise ValueError(f"Two moves at frame {move.start_frame} turn at different speeds.")
            speeds[first:end] = [speed] * (end - first)
    commands = []
    for name, values in held.items():
        lane, previous = spec.camera[name], spec.camera[name].idle
        holds = name in speed_lanes(spec)
        for chunk, value in enumerate(values):
            if value is None:
                value = previous if holds else lane.idle
            if value != previous:
                commands.append((chunk, lane.command, {name: value}))
            previous = value
    return sorted(commands, key=lambda c: c[0])


def speed_lanes(spec: ModelSpec) -> set[str]:
    return {lane.speed for lane in spec.camera.values() if lane.speed}


def scheduled_chunks(spec: ModelSpec, beats: list[Beat]) -> tuple[int, list[int]]:
    """The render's length in chunks, and the chunk each beat starts on.

    Each boundary is the chunk whose video length comes nearest the running total of durations,
    so rounding never accumulates along the timeline. A cut starts a new scene, and a scene opens
    with `first_chunk_frames`.
    """
    bounds, scene_chunk, scene_frames, total = [0], 0, 0, 0
    for i, beat in enumerate(beats):
        if i and beat.cut:
            scene_chunk, scene_frames = bounds[-1], scene_frames + spec.frames_in(bounds[-1] - scene_chunk)
        total += beat.frames
        bounds.append(scene_chunk + spec.chunks_for(total - scene_frames))
    for i, (start, end) in enumerate(zip(bounds, bounds[1:])):
        if end <= start:
            raise ValueError(f"Beat {i + 1} lasts {beats[i].frames} frames, less than one {spec.frames_per_chunk}-frame chunk.")
    return bounds[-1], bounds[:-1]


def compile_longlive(model: str, spec: ModelSpec, beats: list[Beat], seed: int) -> Plan:
    chunks, at = scheduled_chunks(spec, beats)
    bounds = [0] + [c for b, c in zip(beats[1:], at[1:]) if b.cut] + [chunks]
    for start, end in zip(bounds, bounds[1:]):
        if end - start > spec.max_scene_chunks:
            raise ValueError(f"A {model} scene can last at most {spec.frames_in(spec.max_scene_chunks)} frames; add a cut beat to go longer.")
    plan = Plan(setup=[("set_seed", {"seed": seed}), ("set_shot", {"prompt": beats[0].prompt})], chunks=chunks)
    for beat, chunk in zip(beats[1:], at[1:]):
        command = "schedule_scene_cut" if beat.cut else "schedule_shot"
        plan.setup.append((command, {"prompt": beat.prompt, "at_session_chunk": chunk}))
    plan.setup.append(("start", {}))
    return plan


def compile_helios(model: str, spec: ModelSpec, beats: list[Beat], seed: int) -> Plan:
    chunks, at = scheduled_chunks(spec, beats)
    plan = Plan(setup=[("set_seed", {"seed": seed})], chunks=chunks)
    first = beats[0]
    if first.image is None:
        plan.setup.append(("set_prompt", {"prompt": first.prompt}))
    else:
        plan.setup.append(("set_conditioning", {"prompt": first.prompt, "image": first.image}))
    for beat, chunk in zip(beats[1:], at[1:]):
        if beat.image is None:
            plan.setup.append(("schedule_prompt", {"prompt": beat.prompt, "chunk": chunk}))
        else:
            # schedule_prompt carries no image, so an image beat is sent live. A live change applies
            # from the chunk after the one generating, so it goes out a chunk early.
            plan.timed.append((chunk - 1, "set_conditioning", {"prompt": beat.prompt, "image": beat.image}))
    plan.setup.append(("start", {}))
    return plan


def compile_live(model: str, spec: ModelSpec, beats: list[Beat], seed: int) -> Plan:
    chunks, at = scheduled_chunks(spec, beats)
    if chunks > spec.max_scene_chunks:
        raise ValueError(f"A {model} render can last at most {spec.frames_in(spec.max_scene_chunks)} frames.")
    plan = Plan(setup=[("set_seed", {"seed": seed})], chunks=chunks)
    if beats[0].image is not None:
        plan.setup.append(("set_image", {"image": beats[0].image}))
    plan.setup += [("set_prompt", {"prompt": beats[0].prompt}), ("start", {})]
    # These models have no schedule command, so each later beat is sent live, a chunk early.
    plan.timed = [(chunk - 1, "set_prompt", {"prompt": beat.prompt}) for beat, chunk in zip(beats[1:], at[1:])]
    return plan


def source_prompts(beats: list[Beat]) -> list[tuple[int, str, dict]]:
    """Each later beat's prompt, sent live as the source reaches the beat's first frame."""
    starts = itertools.accumulate(beat.frames for beat in beats[:-1])
    return [(start, "set_prompt", {"prompt": beat.prompt}) for start, beat in zip(starts, beats[1:])]


def compile_sana(model: str, spec: ModelSpec, beats: list[Beat], seed: int) -> Plan:
    # The clip goes up the live `camera` track: the deployed model has no file mode (`set_mode`, `set_video`).
    return Plan(setup=[("set_seed", {"seed": seed}), ("set_prompt", {"prompt": beats[0].prompt}), ("start", {})],
                chunks=sum(beat.frames for beat in beats), timed=source_prompts(beats), source=beats[0].video)


def compile_x2(model: str, spec: ModelSpec, beats: list[Beat], seed: int) -> Plan:
    plan = Plan(setup=[("set_keep_backlog", {"keep_backlog": True})], chunks=sum(beat.frames for beat in beats),
                source=beats[0].video)
    if beats[0].image is not None:
        plan.setup.append(("set_reference_image", {"reference_image": beats[0].image}))
    plan.setup.append(("set_prompt", {"prompt": beats[0].prompt}))
    plan.timed = source_prompts(beats)
    return plan


COMPILERS = {"LongLive-2.0": compile_longlive, "Helios": compile_helios, "LingBot": compile_live,
             "LingBot World 2": compile_live, "Visko Orbis Dynamic": compile_live, "Visko Orbis Stable": compile_live,
             "Sana Streaming": compile_sana, "X2": compile_x2}
