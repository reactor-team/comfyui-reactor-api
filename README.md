# comfyui-reactor-api

Run [Reactor](https://docs.reactor.inc) real-time video models from ComfyUI, interactively or as a scripted
render.

## Start here

1. Install the plugin (see [Install](#install)) and set your Reactor API key.
2. Copy this plugin's `example_inputs/` folder into ComfyUI's `input` folder. Example 05 also loads
   `example_outputs/reactor_camera_segments.mp4`, so copy that there too.
3. Open ComfyUI's template browser and pick **01 Generate video - Realtime video from a prompt** under
   comfyui-reactor-api. Queue it, and change the prompt in the window as the video plays.

## Two ways to use it

- **Realtime: Reactor Realtime.** For interactive use. Pick a model on the node and it shows the inputs
  that model takes: a prompt, an image, a clip or camera, and its settings. Queue it and the model
  plays in a window where you steer it: change the prompt, drive the camera from the keyboard, or
  talk to an avatar. Press Done and the take comes out of the node as a video.
- **Scripted: segment nodes and Reactor Render.** For a sequence you define ahead of time. Each
  segment is a model's segment node, with its prompt, length, image and camera moves, and the
  segments play in sequence order. Every input is an ordinary ComfyUI input, so other nodes can write
  the prompts, make the images, fork the story or pick an ending, and the render is a video like any
  other node's output.

## Showcase

<table>
  <tr>
    <td><a href="example_outputs/reactor_segment_cuts.mp4"><img src="example_outputs/reactor_segment_cuts.webp" width="400"></a><br>06 · LongLive-2.0: three segments joined by cuts</td>
    <td><a href="example_outputs/reactor_nine_segments.mp4"><img src="example_outputs/reactor_nine_segments.webp" width="400"></a><br>07 · LongLive-2.0: nine segments of shots and cuts</td>
  </tr>
  <tr>
    <td><a href="example_outputs/reactor_helios.mp4"><img src="example_outputs/reactor_helios.webp" width="400"></a><br>08 · Helios: a scene steered by prompts, from text</td>
    <td><a href="example_outputs/reactor_visko_orbis_stable.mp4"><img src="example_outputs/reactor_visko_orbis_stable.webp" width="400"></a><br>09 · Visko Orbis Stable: a storm morphing around one shot</td>
  </tr>
  <tr>
    <td><a href="example_outputs/reactor_camera_moves.mp4"><img src="example_outputs/reactor_camera_moves.webp" width="400"></a><br>10 · LingBot World 2: camera moves across a sequence</td>
    <td><a href="example_outputs/reactor_camera_segments.mp4"><img src="example_outputs/reactor_camera_segments.webp" width="400"></a><br>11 · LingBot World 2: a camera move on each segment</td>
  </tr>
  <tr>
    <td><a href="example_outputs/reactor_camera_events.mp4"><img src="example_outputs/reactor_camera_events.webp" width="400"></a><br>12 · LingBot World 2: one camera path, an event you pick</td>
    <td><a href="example_outputs/reactor_ad_variants.mp4"><img src="example_outputs/reactor_ad_variants.webp" width="400"></a><br>13 · LongLive-2.0: one opening, three endings</td>
  </tr>
  <tr>
    <td><a href="example_outputs/reactor_vidu_edit.mp4"><img src="example_outputs/reactor_vidu_edit.webp" width="400"></a><br>14 · Vidu S2-Editing: restyle, re-dress and recast a video</td>
  </tr>
</table>

Each preview is a 12-second excerpt from the example workflow with that number, and it has the
workflow embedded: save the preview and drop it on the ComfyUI canvas to load it. Click a preview for
the whole render (480p).

## What you can do with each model

The segment nodes are grouped in ComfyUI's node menu by what the models do: Reactor › Generate video,
Explore worlds, Edit video and Avatars. Each model has an overview page on docs.reactor.inc and a
prompt guide. The prompt guides apply to the segments you write here.

### Generate video

#### LongLive-2.0

Tell a story across many scenes from text alone. Each segment is a shot. Mark a segment as a cut to start
fresh, or leave it as a shot to keep the same world and move the story on.
[Overview](https://docs.reactor.inc/model-api-reference/longlive-v2/overview) ·
[Prompt guide](https://docs.reactor.inc/model-api-reference/longlive-v2/prompt-guide)

#### Helios

Generate one continuous scene and steer it with a new prompt on each segment. Any segment can take a
reference image to guide it. On Reactor Helios Segment, each segment's `image_strength` sets how closely
the scene holds to the image.
[Overview](https://docs.reactor.inc/model-api-reference/helios/overview) ·
[Prompt guide](https://docs.reactor.inc/model-api-reference/helios/prompt-guide)

#### Visko Orbis Stable and Visko Orbis Dynamic

Film one uninterrupted shot that changes as it plays. Each new segment's prompt morphs the picture
instead of cutting. Start from text, or anchor the opening frame with an image on the first segment.
Dynamic renders at its native 832x480 and Stable at its smallest size, 1080p.
The video has sound, made from the picture or described by each segment's `audio_prompt` on Reactor
Visko Orbis Segment. Turn `audio` off on the first segment to leave it out, which makes each chunk
cheaper to generate.
[Stable overview](https://docs.reactor.inc/model-api-reference/visko-orbis-stable/overview) ·
[Stable prompt guide](https://docs.reactor.inc/model-api-reference/visko-orbis-stable/prompt-guide) ·
[Dynamic overview](https://docs.reactor.inc/model-api-reference/visko-orbis-dynamic/overview) ·
[Dynamic prompt guide](https://docs.reactor.inc/model-api-reference/visko-orbis-dynamic/prompt-guide)

### Explore worlds

#### LingBot

Walk through a world grown from a seed image. The first segment's image sets how the world looks, the
prompt steers the rest, and the movement and look lanes drive the camera.
[Overview](https://docs.reactor.inc/model-api-reference/lingbot/overview) ·
[Prompt guide](https://docs.reactor.inc/model-api-reference/lingbot/prompt-guide)

#### LingBot World 2

The next generation of LingBot. Forward and sideways movement have their own lanes, you can look
horizontally and vertically, and the camera pose lane adds directed camera moves. Change the prompt
between segments to restyle the weather, lighting, or events while the reference image stays.
[Overview](https://docs.reactor.inc/model-api-reference/lingbot-world-2/overview) ·
[Prompt guide](https://docs.reactor.inc/model-api-reference/lingbot-world-2/prompt-guide)

### Edit video

#### Sana Streaming

Edit a video file from a prompt. The first segment takes the clip, and each segment's prompt is an
edit: a later segment changes the edit from its first frame. Everything the prompt doesn't mention
carries through from the clip.
[Overview](https://docs.reactor.inc/model-api-reference/sana-streaming/overview) ·
[Prompt guide](https://docs.reactor.inc/model-api-reference/sana-streaming/prompt-guide)

#### X2

Edit a video or a camera from a prompt, with an optional reference image for a character or object to
insert or swap in. The first segment takes the clip and the image; each segment's prompt changes the edit.
[Overview](https://docs.reactor.inc/model-api-reference/x2/overview) ·
[Prompt guide](https://docs.reactor.inc/model-api-reference/x2/prompt-guide)

#### Vidu S2-Editing

Edit a video or a camera from one reference image, with no prompt. `editing_type` says how the image
is used: `style_transfer` restyles the whole scene, `virtual_tryon` puts on an item the person
wears, `subject_replacement` changes how the person looks, and `background_replacement` swaps the
scene behind them. The first segment takes the clip. A later segment that changes the image or
`editing_type` switches the look from its first frame. The edit has a time limit, and a video
that runs past it ends there.
[Overview](https://docs.reactor.inc/model-api-reference/vidu-s2-editing/overview) ·
[Prompt guide](https://docs.reactor.inc/model-api-reference/vidu-s2-editing/prompt-guide)

### Avatars

#### Vidu S2-Avatar

Hold a conversation with a character made from a photo. The first segment's image is the person, and
each segment's prompt is said to them in turn. The character answers aloud, and the next segment starts
once it goes quiet and the segment's frames have played, so a segment can run longer than drawn but never
shorter. Set `persona` on the first segment to describe who they are; `voice` and `greeting` are optional,
and `greeting` is an instruction such as "Say hello and wave." Without a greeting the character waits
for the first segment, and the video starts on its answer. Only the first segment takes an image.
To give the character an object, outfit or background, connect a Reactor Vidu S2-Avatar Reference
to each segment it lasts for; it goes away at the first segment without it. A reference takes a while to show, so give the
segment that adds one a long hold. To cut between two characters, render each turn on its own and join the
turns with ComfyUI's Concatenate Video, as example 15 does.
[Overview](https://docs.reactor.inc/model-api-reference/vidu-s2-avatar/overview) ·
[Prompt guide](https://docs.reactor.inc/model-api-reference/vidu-s2-avatar/prompt-guide)

| Model | Image | Video | Cuts | Camera moves |
| --- | --- | --- | --- | --- |
| LongLive-2.0 | — | — | yes | — |
| Helios | any segment | — | — | — |
| LingBot | first segment, required | — | — | yes |
| LingBot World 2 | first segment, required | — | — | yes |
| Visko Orbis Dynamic | first segment | — | — | — |
| Visko Orbis Stable | first segment | — | — | — |
| Sana Streaming | — | first segment, required | — | — |
| X2 | first segment | first segment, required | — | — |
| Vidu S2-Editing | any segment, required on the first | first segment, required | — | — |
| Vidu S2-Avatar | first segment, required; its references on any segment | — | — | — |

## Install

Clone or symlink this folder into `ComfyUI/custom_nodes/`, then install `requirements.txt` into
ComfyUI's Python. Copy `config.ini.example` to `config.ini` in this folder and set your Reactor API
key in it. You can also set `REACTOR_API_KEY` in the environment ComfyUI starts from; it takes
priority over `config.ini`. The key is stored in plain text, and `config.ini` is gitignored.

ComfyUI runs one Reactor session at a time: when a workflow has several Renders, or a Render and a
Realtime, each waits for the one before it to finish. `MAX_CONCURRENT` under `[Sessions]` in
`config.ini` raises the limit; restart ComfyUI after changing it.

## Nodes

- **Reactor <model> Segment**, such as Reactor Helios Segment, is one segment of a sequence for that
  model, with only the inputs the model takes. The segment nodes are filed by capability under
  Reactor, and searching "reactor segment" or a model's name finds them. Connect segments one after
  another, one node per segment, through their `sequence` input. The first segment also sets the
  model's settings, such as `audio` or `persona`; later segments hide them and change only the
  settings a model takes mid-run, such as `image_strength`, which hold until changed. Reactor Visko
  Orbis Segment covers both Visko Orbis models and picks one on its first segment. On the LingBots,
  each segment has its own camera moves, and a move that continues onto the next segment plays as
  one move.
- **Reactor Sequence Join** plays sequences one after another, as one sequence. They must be for the
  same model, and the first sequence's settings are used.
- **Reactor Sequence Visualizer** draws a sequence on a frame ruler: its segments, snapped to the
  model's chunks as they will play, with the camera moves underneath for models that have camera
  controls. It only shows the sequence; edit a segment on its own node.
- **Reactor Vidu S2-Avatar Reference** tags an image as an object, outfit (`garment`) or background,
  with an optional sentence saying what happens, such as "He holds up the crystal ball." A segment
  holds up to 3.
- **Reactor Render** renders a sequence, from its last segment or a Reactor Sequence Join, and
  outputs a video.
- **Reactor Realtime** runs a model in real time. Its `model` picker shows only the inputs and settings the
  picked model takes. You steer it while it plays, and each take is saved as a video. Searching a
  model's name or capability finds it.
- **Reactor Camera Capture** picks a camera on this browser to stream into Reactor Realtime.
- **Reactor Microphone Capture** picks the microphone on this browser that you talk through on a
  Reactor Realtime call.

## Building a video from segments

Each segment is its own node, so the rest of the graph can feed it:

- **ComfyUI nodes build the segments.** A segment's prompt, length, and image are ordinary inputs.
  Fill them from any other node, such as text built from templates, a prompt from a language model
  node, or an image you generated or edited earlier in the graph. For example, write each prompt as a
  Format Text template and feed it a character from a Custom Combo. Picking another character then
  recasts every segment.
- **Narratives can fork.** A segment's output can feed more than one next segment. Branch a shared
  opening into different endings. Give each branch its own Reactor Render to get every ending, or
  pick one with ComfyUI's If/Else Switch (still experimental). Only the chosen ending runs.

The first segment sets the model and its settings; later segments take them from it. Then connect
the last segment, or the switch, to Reactor Render. Connect it to a Reactor Sequence Visualizer as
well to see the segments and camera moves laid out as they will play.

## Realtime

Reactor Realtime takes no sequence: pick the model on the node and fill in the inputs it shows. It
opens a window in ComfyUI that plays the model's output in real time. Edit the prompt there
and press Apply to change it mid-take; press Done to save the take under `reactor/realtime`, or
Cancel to drop it. With more than one camera, the window can switch cameras mid-take.
Each queue records a new take while `seed` is set to randomize. With the seed fixed and no input
changed, a rerun keeps the last take instead of opening the window.

- **Video-to-video** models edit a source you stream in. Connect a Reactor Camera Capture to stream
  a camera, or connect a video to stream a file, which loops until you press Done. X2 also takes a
  reference image. Vidu S2-Editing takes no prompt: its window has an `editing_type` picker and a
  new-image picker in place of the prompt box, and Apply switches the look. Sana Streaming isn't
  available in Realtime yet; render it with Reactor Render.
- **Generating** models start from a prompt, and from an image where the model takes one. For
  models with camera lanes, click the video and drive with the keys shown under it: W, A, S and D move, the arrow keys look,
  and Q and E orbit on LingBot World 2. Models without camera lanes are steered by prompt alone.
- **Vidu S2-Avatar** holds a conversation with you. Connect the person's image and set
  `persona`. Talk to the character out loud, or type a message and press Send; it answers either
  way. The window asks for microphone access and uses the browser's default microphone; connect a Reactor
  Microphone Capture to pick another. Wear headphones so the character doesn't hear itself.
  The take records the character only, not your voice.

Realtime prompts work best written to each model's prompt guide:
[X2](https://docs.reactor.inc/model-api-reference/x2/prompt-guide) ·
[LingBot World 2](https://docs.reactor.inc/model-api-reference/lingbot-world-2/prompt-guide).

The window joins the take's Reactor session from this browser over WebRTC, so the preview is the
model's own output at full size, and a camera streams straight from this browser to Reactor. ComfyUI
records the take from the same session.

## Examples

Each workflow in `example_workflows/` also appears in ComfyUI's template browser under
comfyui-reactor-api, with a thumbnail. The scenes come from the examples in
[reactor-team/js-sdk](https://github.com/reactor-team/js-sdk). Copy `example_inputs/` into ComfyUI's
`input` folder first; 05 also loads `example_outputs/reactor_camera_segments.mp4` from there.

| # | Workflow | Group | Model |
| --- | --- | --- | --- |
| 01 | Realtime video from a prompt | Generate video | LongLive-2.0 |
| 02 | Realtime camera edited by a prompt | Edit video | X2 |
| 03 | Realtime camera restyled from an image | Edit video | Vidu S2-Editing |
| 04 | Realtime world driven with the keyboard | Explore worlds | LingBot World 2 |
| 05 | Realtime video file edited by a prompt | Edit video | X2 |
| 06 | Segments joined by cuts | Generate video | LongLive-2.0 |
| 07 | Nine segments of shots and cuts | Generate video | LongLive-2.0 |
| 08 | One scene steered by prompts | Generate video | Helios |
| 09 | A storm morphing around one shot | Generate video | Visko Orbis Stable |
| 10 | Camera moves across a sequence | Explore worlds | LingBot World 2 |
| 11 | A camera move on each segment | Explore worlds | LingBot World 2 |
| 12 | One camera path, an event you pick | Explore worlds | LingBot World 2 |
| 13 | One opening, three endings | Generate video | LongLive-2.0 |
| 14 | Restyle, re-dress and recast a video | Edit video | Vidu S2-Editing |
| 15 | Two characters in conversation | Avatars | Vidu S2-Avatar |

## Tests

`uv run pytest -q tests`
