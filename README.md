# comfyui-reactor-api

ComfyUI nodes that render timelines on Reactor real-time video models. Lay out prompt beats and
camera moves, in frames, and Reactor Render plays them on the chosen model and returns the video.

## Install

Clone or symlink this folder into `ComfyUI/custom_nodes/` and install `requirements.txt` into
ComfyUI's Python. Then copy `config.ini.example` to `config.ini` in this folder and set your Reactor
API key in it. `REACTOR_API_KEY` in the environment ComfyUI starts from also works, and wins over
`config.ini`. The key is stored in plain text; `config.ini` is gitignored.

## Nodes

- **Reactor Model** picks the model and shows its settings.
- **Reactor Timeline** is a visual editor: beats on a frame ruler, snapped to the model's chunks,
  with camera lanes underneath for models that have camera controls.
- **Reactor Beat** is one beat, chained to others as an alternative to drawing them. Each beat
  carries its own camera moves; a move continued on the next beat plays as one move.
- **Reactor Render** runs the timeline and outputs a video.

## Supported models

| Model | Image | Cuts | Camera moves |
| --- | --- | --- | --- |
| LongLive-2.0 | — | yes | — |
| Helios | any beat | — | — |
| LingBot | first beat, required | — | yes |
| LingBot World 2 | first beat, required | — | yes |
| Visko Orbis Dynamic | first beat | — | — |
| Visko Orbis Stable | first beat | — | — |

## Examples

Each workflow in `example_workflows/` is also in ComfyUI's template browser under
comfyui-reactor-api. Their scenes come from the examples in
[reactor-team/js-sdk](https://github.com/reactor-team/js-sdk). The workflows that start from an image
load it from ComfyUI's `input` folder, so copy `example_inputs/` there first.

| Workflow | Model | Scene |
| --- | --- | --- |
| `reactor_timeline_editor` | LongLive-2.0 | Martian outpost: nine beats drawn on the timeline, shots and cuts |
| `reactor_beat_chain` | LongLive-2.0 | Wildlife montage: three Reactor Beats in a chain, joined by cuts |
| `reactor_camera_moves` | LingBot World 2 | Jet ski cruise: beats and camera moves drawn on the timeline |
| `reactor_camera_beats` | LingBot World 2 | The same jet ski cruise as a chain of beats, each with its own moves |
| `reactor_visko_orbis_stable` | Visko Orbis Stable | Fisherman in a storm, from an image |
| `reactor_helios` | Helios | King of the Jungle, from text |

Below are two of them rendered at seed 42; click a preview for the video (480p). Each graph
screenshot has its workflow embedded, so you can drag the image straight onto the ComfyUI canvas to
load it.

### `reactor_timeline_editor` — LongLive-2.0

A drone reveal of an astronaut's hab, in to suit up, a wave hello, then off into the desert.

![](example_outputs/reactor_timeline_editor_graph.png)

[![](example_outputs/reactor_timeline_editor.webp)](example_outputs/reactor_timeline_editor.mp4)

### `reactor_camera_beats` — LingBot World 2

A ride forward that turns and strafes into the sunset, then stops and orbits the rider as meteors
fall.

![](example_outputs/reactor_camera_beats_graph.png)

[![](example_outputs/reactor_camera_beats.webp)](example_outputs/reactor_camera_beats.mp4)

## Tests

`uv run pytest -q tests`
