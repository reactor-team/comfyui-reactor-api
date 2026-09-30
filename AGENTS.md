# AGENTS.md

## Two ways to use the plugin: live and scripted

The plugin has two front ends. Keep them separate.

- **Reactor Realtime is the live, interactive path.** It stands alone: a `model` picker (a
  `DynamicCombo` built from `MODELS`) shows only the inputs and settings the picked model takes, and
  the node outputs the take as a video. It never takes a sequence. A model the live window can drive
  gets its controls from its model data, not from a special case in the node.
- **Segment nodes and Reactor Render are the scripted, declarative path.** A video is a sequence of
  segments set up ahead of time, one segment node per segment. Every segment input is an ordinary ComfyUI input,
  so the rest of the graph can build prompts and images, fork a story, or switch endings, and Render
  outputs a normal video. Reactor Sequence Visualizer and Sequence Join work on sequences only.

A new model capability lands in its model data first, so both paths pick it up; add it to the node
of a path only when that path can use it.

## Where a model's capability lives: shared nodes, model data, or a model's own nodes

The plugin drives many Reactor models through a few shared nodes and each model's own nodes. Put
each capability in one of four places, depending on how many models share it and whether they
share its shape.

1. **Shared nodes**: Sequence Join, Sequence Visualizer, Render, Realtime, Camera Capture and Microphone Capture.
   They hold only what most models have in the same shape: a prompt per segment, a segment's length, an
   image, a source video, camera moves, a seed. A shared node describes its inputs in
   general terms. It never branches on a model's name. Its tooltips name no model, though they
   may give a model's own node as an example of what plugs in.
2. **Model data**: `MODELS` in `reactor_render/timeline.py`. This covers how a model is driven
   (`pattern`), what it accepts, its frame grid, its camera lanes, its `settings` and its
   `beat_settings`. Shared code reads `ModelSpec` fields, never the model's name. An option fixed
   for the whole run, such as a voice, is in `settings`, and is read only on a sequence's first segment.
   An option the model takes mid-run, such as an image strength, is in `beat_settings`, and holds
   until a later segment changes it.
3. **A model's segment node**: every segment of a sequence is its model's own node, such as Reactor
   Helios Segment, built by `segment_node` from the model's data. It shows only the inputs that model
   takes, including its `settings` and `beat_settings`, and outputs the shared `REACTOR_SEQUENCE` that
   Sequence Join, Sequence Visualizer and Render read. The first segment (nothing on its `sequence`
   input) picks the model and its settings. Models whose inputs match share a node
   (`SEGMENT_FAMILIES`) with a `model` combo, which rejects a model outside its family. A segment is
   always built by its model's node, but a node that works on whole sequences, such as Sequence
   Join, Render or the Sequence Visualizer, is shared. It takes any `REACTOR_SEQUENCE` and never
   reads which model's node built a segment.
4. **A model's own factory node**: for a value that one model takes and that needs several fields
   of its own, which a single segment input can't hold. Reactor Vidu S2-Avatar Reference is the
   example. It outputs a shared type (`REACTOR_REFERENCE`), so it plugs into generic sockets. Shared nodes carry the
   value without reading it, and only that model's compiler reads its fields. The value records
   the model it was made for, and a render for any other model rejects it.

To choose:

- A per-segment input → a field in the model's data, so its segment node shows it. A single value the
  model takes mid-run is a `beat_settings` entry.
- A single value that applies to the whole run → a `settings` entry, read on the first segment.
- A value with several fields of its own → that model's factory node, into a segment input.
- Every segment node has frames, and a prompt unless its model takes none (`prompted`). Other inputs
  appear only on the nodes whose models take them.
- A shared node takes something only when most models take it in the same shape. Until then it
  stays with the model.

Name a model's own node `Reactor <Model> <Thing>`, with node_id `Reactor<Model><Thing>`. File it
under its model's capability, `Reactor/<capability>` from `CAPABILITY` in `nodes.py` (Generate
video, Explore worlds, Edit video, Avatars), not under the model's name, and give it the model's
name as a search alias. A new model gets a `CAPABILITY` entry; Reactor Realtime's search aliases
pick up its name and capability from there. Shared nodes stay at `Reactor`.

"Segment" and "sequence" are the user-facing words. Internal names such as `Beat`, `beats` and
`beat_settings` predate them and may stay.

## How dynamic inputs follow the picked model

Nodes change shape with the model, and every change is driven by model data:

- **A segment node's** inputs are fixed by its model, so the frontend only hides first-segment inputs
  (the model, the start `settings`, and an image or video read at start) on later segments, and snaps
  lengths to the chunk grid. A new whole-run option is a `Setting`, never a new node or a new input
  on a shared node.
- **Sequence Visualizer** reads `model_facts`, which each segment node sends to the frontend on its
  `sequence` input, for the model its sequence starts with. It only draws the sequence, snapped to the model's
  chunks, with camera lanes where the model has them. The frontend checks facts, never a model's
  name.
- **Realtime's** inputs come from its `model` picker: each live model's option lists the inputs its
  `ModelSpec` says it takes (`live_option` in `nodes.py`), so ComfyUI swaps them when the model
  changes, with no frontend code. A new capability that a socket
  depends on gets a `ModelSpec` field and a matching entry in `model_facts`.
- **Runners** differ by `pattern` (chunked, source, clips, call), which says how a model is driven.
  Code for a pattern may assume that pattern's commands. It must not assume any one model's
  settings beyond what the model's data declares.

## Only public information

Everything here must be something that can be learned from docs.reactor.inc or by watching the
session's traffic. Measured behaviour, such as a chunk length that differs from the docs, is fine;
say it was measured. Don't include private repository names or links, internal service names, or
ticket IDs. This applies to code, comments, the README and commit messages.

## Tests

`uv run pytest -q tests`
