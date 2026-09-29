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
comfyui-reactor-api. Below are two of them rendered at seed 42; click a preview for the video
(480p). `reactor_beat_chain` and `reactor_camera_moves` build the same videos the other way
round, since a chain and a drawn timeline compile to the same commands.

### `reactor_timeline_editor` — LongLive-2.0

Three beats drawn on the timeline, the last a cut.

![](example_outputs/reactor_timeline_editor_graph.png)

[![](example_outputs/reactor_timeline_editor.gif)](example_outputs/reactor_timeline_editor.mp4)

### `reactor_camera_beats` — LingBot World 2

A chain of Reactor Beats, each carrying its own camera moves: a walk forward that turns, strafes
and orbits.

![](example_outputs/reactor_camera_beats_graph.png)

[![](example_outputs/reactor_camera_beats.gif)](example_outputs/reactor_camera_beats.mp4)

## Tests

`uv run pytest -q tests`
