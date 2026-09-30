# AGENTS.md

## Two ways to use the plugin: live and scripted

The plugin has two front ends. Keep them separate.

- **Reactor Realtime is the live, interactive path.** It stands alone: a `model` picker (a
  `DynamicCombo` built from `MODELS`) shows only the inputs and settings the picked model takes, and
  the node outputs the take as a video. It never takes a chain. A model the live window can drive
  gets its controls from its model data, not from a special case in the node.
- **Chain nodes and Reactor Render are the scripted, declarative path.** A video is a sequence of
  beats set up ahead of time, one chain node per beat. Every beat input is an ordinary ComfyUI input,
  so the rest of the graph can build prompts and images, fork a story, or switch endings, and Render
  outputs a normal video. Reactor Timeline and Chain Join work on chains only.

A new model capability lands in its model data first, so both paths pick it up; add it to the node
of a path only when that path can use it.

## Where a model's capability lives: shared nodes, model data, or a model's own nodes

The plugin drives many Reactor models through a few shared nodes and each model's own nodes. Put
each capability in one of four places, depending on how many models share it and whether they
share its shape.

1. **Shared nodes**: Chain Join, Timeline, Render, Realtime and Camera Capture.
   They hold only what most models have in the same shape: a prompt per beat, a beat's length, an
   image, a source video, camera moves, a seed. A shared node describes its inputs in
   general terms. It never branches on a model's name. Its tooltips name no model, though they
   may give a model's own node as an example of what plugs in.
2. **Model data**: `MODELS` in `reactor_render/timeline.py`. This covers how a model is driven
   (`pattern`), what it accepts, its frame grid, its camera lanes, its `settings` and its
   `beat_settings`. Shared code reads `ModelSpec` fields, never the model's name. An option fixed
   for the whole run, such as a voice, is in `settings`, and is read only on a chain's first link.
   An option the model takes mid-run, such as an image strength, is in `beat_settings`, and holds
   until a later link changes it.
3. **A model's chain node**: every beat of a chain is its model's own node, such as Reactor Helios
   Chain, built by `chain_node` from the model's data. It shows only the inputs that model takes,
   including its `settings` and `beat_settings`, and outputs the shared `REACTOR_CHAIN` that Chain
   Join, Timeline and Render read. The first link (nothing on its `chain` input) picks
   the model and its settings. Models whose inputs match share a node (`CHAIN_FAMILIES`) with a
   `model` combo, which rejects a model outside its family. Nodes are filed under
   `Reactor/<model>` and carry the search alias "reactor chain". A beat is always built by its model's node, but a node that works on whole
   chains, such as Chain Join, Render or Timeline's `chain` input, is shared. It takes any
   `REACTOR_CHAIN` and never reads which model's node built a beat.
4. **A model's own factory node**: for a value that one model takes and that needs several fields
   of its own, which a single chain input can't hold. Reactor Vidu S2-Avatar Reference is the
   example. It outputs a shared type (`REACTOR_REFERENCE`), so it plugs into generic sockets. Shared nodes carry the
   value without reading it, and only that model's compiler reads its fields. The value records
   the model it was made for, and a render for any other model rejects it.

To choose:

- A per-beat input → a field in the model's data, so its chain node shows it. A single value the
  model takes mid-run is a `beat_settings` entry.
- A single value that applies to the whole run → a `settings` entry, read on the first link.
- A value with several fields of its own → that model's factory node, into a chain input.
- Every chain node has prompt and frames. Other inputs appear only on the nodes whose models take
  them.
- A shared node takes something only when most models take it in the same shape. Until then it
  stays with the model.

Name a model's own node `Reactor <Model> <Thing>`, with node_id `Reactor<Model><Thing>` and
category `Reactor/<Model>`.

## How dynamic inputs follow the picked model

Nodes change shape with the model, and every change is driven by model data:

- **A chain node's** inputs are fixed by its model, so the frontend only hides first-link inputs
  (the model, the start `settings`, and an image or video read at start) on later links, and snaps
  lengths to the chunk grid. A new whole-run option is a `Setting`, never a new node or a new input
  on a shared node.
- **Timeline** reads `model_facts`, which each chain node sends to the frontend on its `chain`
  input, for the model its chain starts with. It only draws the chain, snapped to the model's
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
