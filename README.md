# comfyui-reactor-api

Render videos on [Reactor](https://docs.reactor.inc) real-time video models from ComfyUI. Lay out
prompt beats and camera moves on a timeline, pick a model, and Reactor Render returns the video.

<table>
  <tr>
    <td><a href="example_outputs/reactor_timeline_editor.mp4"><img src="example_outputs/reactor_timeline_editor.webp" width="400"></a><br>LongLive-2.0: a nine-beat story with shots and cuts</td>
    <td><a href="example_outputs/reactor_camera_beats.mp4"><img src="example_outputs/reactor_camera_beats.webp" width="400"></a><br>LingBot World 2: camera moves over a reference image</td>
  </tr>
  <tr>
    <td><a href="example_outputs/reactor_visko_orbis_stable.mp4"><img src="example_outputs/reactor_visko_orbis_stable.webp" width="400"></a><br>Visko Orbis Stable: a storm morphing around one shot</td>
    <td><a href="example_outputs/reactor_helios.mp4"><img src="example_outputs/reactor_helios.webp" width="400"></a><br>Helios: a scene steered by prompt changes, from text</td>
  </tr>
</table>

Click a preview for the video (480p). All four are example workflows from this repo, rendered at
seed 42.

## What you can do with each model

Each model has an overview page on docs.reactor.inc and a prompt guide. The prompt guides apply to
the beats you write here.

### LongLive-2.0

Tell a story across many scenes from text alone. Each beat is a shot. Mark a beat as a cut to break
cleanly to a new scene, or leave it as a shot to keep the same world and move the story on.
[Overview](https://docs.reactor.inc/model-api-reference/longlive-v2/overview) ·
[Prompt guide](https://docs.reactor.inc/model-api-reference/longlive-v2/prompt-guide)

### Helios

Generate one continuous scene and steer it with a new prompt on each beat. Any beat can take a
reference image to guide it. The `sr_scale` setting upscales the output 2x or 4x.
[Overview](https://docs.reactor.inc/model-api-reference/helios/overview) ·
[Prompt guide](https://docs.reactor.inc/model-api-reference/helios/prompt-guide)

### LingBot

Walk through a world grown from a seed image. The first beat's image sets how the world looks, the
prompt steers the rest, and the movement and look lanes drive the camera.
[Overview](https://docs.reactor.inc/model-api-reference/lingbot/overview) ·
[Prompt guide](https://docs.reactor.inc/model-api-reference/lingbot/prompt-guide)

### LingBot World 2

The next generation of LingBot. Forward and sideways movement have their own lanes, you can look
horizontally and vertically, and the camera pose lane adds directed camera moves. Change the prompt
between beats to restyle the weather, lighting, or events while the reference image stays.
[Overview](https://docs.reactor.inc/model-api-reference/lingbot-world-2/overview) ·
[Prompt guide](https://docs.reactor.inc/model-api-reference/lingbot-world-2/prompt-guide)

### Visko Orbis Stable and Visko Orbis Dynamic

Film one uninterrupted shot that changes as it plays. Each new beat's prompt morphs the picture
instead of cutting. Start from text, or anchor the opening frame with an image on the first beat.
Output goes up to 4k: Stable offers 1080p, 2k and 4k, and Dynamic also offers native resolution.
[Stable overview](https://docs.reactor.inc/model-api-reference/visko-orbis-stable/overview) ·
[Stable prompt guide](https://docs.reactor.inc/model-api-reference/visko-orbis-stable/prompt-guide) ·
[Dynamic overview](https://docs.reactor.inc/model-api-reference/visko-orbis-dynamic/overview) ·
[Dynamic prompt guide](https://docs.reactor.inc/model-api-reference/visko-orbis-dynamic/prompt-guide)

| Model | Image | Cuts | Camera moves |
| --- | --- | --- | --- |
| LongLive-2.0 | — | yes | — |
| Helios | any beat | — | — |
| LingBot | first beat, required | — | yes |
| LingBot World 2 | first beat, required | — | yes |
| Visko Orbis Dynamic | first beat | — | — |
| Visko Orbis Stable | first beat | — | — |

## Install

Clone or symlink this folder into `ComfyUI/custom_nodes/`, then install `requirements.txt` into
ComfyUI's Python. Copy `config.ini.example` to `config.ini` in this folder and set your Reactor API
key in it. You can also set `REACTOR_API_KEY` in the environment ComfyUI starts from; it takes
priority over `config.ini`. The key is stored in plain text, and `config.ini` is gitignored.

## Nodes

- **Reactor Model** picks the model and shows its settings.
- **Reactor Timeline** is a visual editor. It shows beats on a frame ruler, snapped to the model's
  chunks, with camera lanes underneath for models that have camera controls.
- **Reactor Beat** is a single beat. Chain beats together instead of drawing them. Each beat has
  its own camera moves, and a move that continues onto the next beat plays as one move.
- **Reactor Render** runs the timeline and outputs a video.

## Two ways to build a timeline

**Draw it on Reactor Timeline.** This is the simple way. Add beats on the ruler, type a prompt into
each one, drag the edges to set how long it plays, and draw camera moves in the lanes underneath.
The whole video lives in one node. `reactor_timeline_editor` and `reactor_camera_moves` work this
way.

**Chain Reactor Beat nodes.** Use this for more control. Each beat is its own node, so the rest of
the graph can feed it:

- **ComfyUI nodes build the beats.** A beat's prompt, length, and image are ordinary inputs. Fill
  them from any other node, such as text built from templates, a prompt from a language model node,
  or an image you generated or edited earlier in the graph. For example, write each prompt as a
  Format Text template and feed it a character from a Custom Combo. Picking another character then
  recasts every beat. The chain compiles into the timeline when it runs.
- **Narratives can fork.** A beat's output can feed more than one next beat. Branch a shared opening
  into different endings. Give each branch its own Reactor Timeline and Reactor Render to get every
  ending, or pick one with ComfyUI's If/Else Switch (still experimental). Only the chosen ending runs.

Connect the last beat, or the switch, to Reactor Timeline's `chain` input. The chain then replaces
anything drawn on that timeline. `reactor_beat_chain`, `reactor_camera_beats`,
`reactor_camera_events` and `reactor_ad_variants` work this way.

## Examples

Each workflow in `example_workflows/` also appears in ComfyUI's template browser under
comfyui-reactor-api. The scenes come from the examples in
[reactor-team/js-sdk](https://github.com/reactor-team/js-sdk). Workflows that start from an image load
it from ComfyUI's `input` folder, so copy `example_inputs/` there first.

| Workflow | Model | Scene |
| --- | --- | --- |
| `reactor_timeline_editor` | LongLive-2.0 | Martian outpost: nine beats drawn on the timeline, shots and cuts |
| `reactor_beat_chain` | LongLive-2.0 | Wildlife montage: three Reactor Beats in a chain, joined by cuts |
| `reactor_camera_moves` | LingBot World 2 | Jet ski cruise: beats and camera moves drawn on the timeline |
| `reactor_camera_beats` | LingBot World 2 | The same jet ski cruise as a chain of beats, each with its own moves |
| `reactor_camera_events` | LingBot World 2 | The jet ski ride on one camera path, with an event you pick: meteors, dolphins, a whale or a seaplane |
| `reactor_ad_variants` | LongLive-2.0 | Park ad: one opening, three audience endings rendered in one run, and a season you pick |
| `reactor_visko_orbis_stable` | Visko Orbis Stable | Fisherman in a storm, from an image |
| `reactor_helios` | Helios | King of the Jungle, from text |

Each graph screenshot below has its workflow embedded. Drag one onto the ComfyUI canvas to load it.

`reactor_timeline_editor`:

![](example_outputs/reactor_timeline_editor_graph.png)

`reactor_camera_beats`:

![](example_outputs/reactor_camera_beats_graph.png)

## Tests

`uv run pytest -q tests`
