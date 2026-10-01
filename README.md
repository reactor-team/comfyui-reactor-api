# comfyui-reactor-api

Run [Reactor](https://docs.reactor.inc) real-time video models in ComfyUI, in a window you steer
as they play or as a scripted render.

<img src="example_outputs/reactor_realtime_window.jpg" width="640" alt="The Reactor Realtime window driving a LingBot World 2 world with the keyboard">

In ComfyUI, LingBot World 2 plays in real time in the Reactor Realtime window, and W/A/S/D and the
arrow keys move the camera through the world as it generates. Press Done and the take comes out of
the node as a video.

## Install

Clone or symlink this folder into `ComfyUI/custom_nodes/` and install `requirements.txt` into
ComfyUI's Python. Copy `config.ini.example` to `config.ini` and set your Reactor API key, or set
`REACTOR_API_KEY` in ComfyUI's environment (it takes priority).

Copy `example_inputs/` into ComfyUI's `input` folder so the examples find their images.

## Examples

All of these are in ComfyUI's template browser under comfyui-reactor-api. Real-time examples play in the
Reactor Realtime window; press Done to output the take as a video. Edit previews show the source on
the left and the edit on the right. Drop a preview on the canvas to load its workflow.

| Preview | Workflow | Model | Runs |
| --- | --- | --- | --- |
| <img src="example_outputs/reactor_realtime_world.webp" width="240"> | [Real-time world driven with the keyboard](<example_workflows/Explore worlds - Real-time world driven with the keyboard (LingBot World 2).json>) | LingBot World 2 | Real time |
| <img src="example_outputs/reactor_avatar.webp" width="240"> | [Real-time conversation, your voice in and video out](<example_workflows/Avatars - Real-time conversation, your voice in and video out (Vidu S2-Avatar).json>) | Vidu S2-Avatar | Real time |
| <img src="example_outputs/reactor_realtime_camera_restyle.webp" width="360"> | [Real-time camera restyled from an image](<example_workflows/Edit video - Real-time camera restyled from an image (Vidu S2-Editing).json>) | Vidu S2-Editing | Real time |
| <img src="example_outputs/reactor_realtime_camera_tryon.webp" width="360"> | [Real-time camera dressed in an outfit from an image](<example_workflows/Edit video - Real-time camera dressed in an outfit from an image (Vidu S2-Editing).json>) | Vidu S2-Editing | Real time |
| <img src="example_outputs/reactor_realtime_camera_swap.webp" width="360"> | [Real-time camera recast as a character from an image](<example_workflows/Edit video - Real-time camera recast as a character from an image (Vidu S2-Editing).json>) | Vidu S2-Editing | Real time |
| <img src="example_outputs/reactor_realtime_camera_edit.webp" width="360"> | [Real-time camera edited by a prompt](<example_workflows/Edit video - Real-time camera edited by a prompt (X2).json>) | X2 | Real time |
| <img src="example_outputs/reactor_realtime_prompt.webp" width="240"> | [Real-time video from a prompt](<example_workflows/Generate video - Real-time video from a prompt (LongLive-2.0).json>) | LongLive-2.0 | Real time |
| <img src="example_outputs/reactor_realtime_video_file.webp" width="360"> | [Real-time video file edited by a prompt](<example_workflows/Edit video - Real-time video file edited by a prompt (X2).json>) | X2 | Real time |
| <img src="example_outputs/reactor_segment_cuts.webp" width="240"> | [Segments joined by cuts](<example_workflows/Generate video - Segments joined by cuts (LongLive-2.0).json>) | LongLive-2.0 | Render |
| <img src="example_outputs/reactor_nine_segments.webp" width="240"> | [Nine segments of shots and cuts](<example_workflows/Generate video - Nine segments of shots and cuts (LongLive-2.0).json>) | LongLive-2.0 | Render |
| <img src="example_outputs/reactor_helios.webp" width="240"> | [One scene steered by prompts](<example_workflows/Generate video - One scene steered by prompts (Helios).json>) | Helios | Render |
| <img src="example_outputs/reactor_visko_orbis_stable.webp" width="240"> | [A storm morphing around one shot](<example_workflows/Generate video - A storm morphing around one shot (Visko Orbis Stable).json>) | Visko Orbis Stable | Render |
| <img src="example_outputs/reactor_camera_moves.webp" width="240"> | [Camera moves across a sequence](<example_workflows/Explore worlds - Camera moves across a sequence (LingBot World 2).json>) | LingBot World 2 | Render |
| <img src="example_outputs/reactor_vidu_edit.webp" width="360"> | [Restyle, re-dress and recast a video](<example_workflows/Edit video - Restyle, re-dress and recast a video (Vidu S2-Editing).json>) | Vidu S2-Editing | Render |

## Nodes

- **Reactor Realtime**: runs a model in real time. Pick the model, queue it, and steer it in the window:
  edit the prompt and press Apply, drive the camera with W/A/S/D and the arrow keys, or talk to an
  avatar. Press Done to output the take as a video.
- **Reactor <model> Segment**: one segment of a scripted sequence, with that model's inputs. Chain
  segments through `sequence`. The first segment holds the model's settings. Since every input is a
  normal ComfyUI input, other nodes can write the prompts, make the images, or branch the story.
- **Reactor Render**: renders a sequence and outputs a video.
- **Reactor Sequence Join**: plays sequences for the same model one after another.
- **Reactor Sequence Visualizer**: draws a sequence's segments and camera moves on a frame ruler.
- **Reactor Vidu S2-Avatar Reference**: tags an image as an object, outfit or background for an
  avatar segment.
- **Reactor Camera Capture** / **Microphone Capture**: pick this browser's camera or microphone for
  Reactor Realtime.

## Models

| Model | What it does | Takes | Prompt guide |
| --- | --- | --- | --- |
| [LongLive-2.0](https://docs.reactor.inc/model-api-reference/longlive-v2/overview) | A story across shots and cuts, from text | prompt | [Prompt guide](https://docs.reactor.inc/model-api-reference/longlive-v2/prompt-guide) |
| [Helios](https://docs.reactor.inc/model-api-reference/helios/overview) | One continuous scene steered by prompts | prompt, image on any segment | [Prompt guide](https://docs.reactor.inc/model-api-reference/helios/prompt-guide) |
| [Visko Orbis Stable / Dynamic](https://docs.reactor.inc/model-api-reference/visko-orbis-stable/overview) | One unbroken shot that morphs, with sound | prompt, optional first image | [Stable](https://docs.reactor.inc/model-api-reference/visko-orbis-stable/prompt-guide), [Dynamic](https://docs.reactor.inc/model-api-reference/visko-orbis-dynamic/prompt-guide) |
| [LingBot](https://docs.reactor.inc/model-api-reference/lingbot/overview) | A world you walk through | first image, camera moves | [Prompt guide](https://docs.reactor.inc/model-api-reference/lingbot/prompt-guide) |
| [LingBot World 2](https://docs.reactor.inc/model-api-reference/lingbot-world-2/overview) | LingBot with more camera control | first image, camera moves | [Prompt guide](https://docs.reactor.inc/model-api-reference/lingbot-world-2/prompt-guide) |
| [Sana Streaming](https://docs.reactor.inc/model-api-reference/sana-streaming/overview) | Edit a video file from a prompt | video (Render only) | [Prompt guide](https://docs.reactor.inc/model-api-reference/sana-streaming/prompt-guide) |
| [X2](https://docs.reactor.inc/model-api-reference/x2/overview) | Edit a video or camera from a prompt | video, optional image | [Prompt guide](https://docs.reactor.inc/model-api-reference/x2/prompt-guide) |
| [Vidu S2-Editing](https://docs.reactor.inc/model-api-reference/vidu-s2-editing/overview) | Restyle, re-dress or recast from an image | video, image | [Prompt guide](https://docs.reactor.inc/model-api-reference/vidu-s2-editing/prompt-guide) |
| [Vidu S2-Avatar](https://docs.reactor.inc/model-api-reference/vidu-s2-avatar/overview) | Talk with a character made from a photo | first image, persona | [Prompt guide](https://docs.reactor.inc/model-api-reference/vidu-s2-avatar/prompt-guide) |

Read the model's prompt guide before writing prompts: it applies to the prompts you write here, and each example's note links it.

## Notes

- ComfyUI runs one Reactor session at a time. Set `MAX_CONCURRENT` under `[Sessions]` in
  `config.ini` and restart to run more.
- In Reactor Realtime, a fixed `seed` with unchanged inputs reuses the last take. Set it to randomize to
  record a new take each time.
- For Vidu S2-Avatar, wear headphones so the character doesn't hear itself.
- Reactor Realtime takes are saved under `reactor/realtime`.

## Tests

`uv run pytest -q tests`
