# Changelog

Each release has a section headed `## <version>`, matching `version` in `pyproject.toml`.
The publish workflow sends the section for the version it publishes to the Comfy Registry as
that version's changelog. Earlier versions are in the git history.

## 0.5.0

- Add Pika H3 Flex, which makes clips with sound from a prompt, up to nine reference pictures and
  three sounds of at most 15 seconds together, or a first and last frame. It works in Reactor
  Realtime and in a segment node.
- Add EdenLive, which restyles a plain draft video, such as a rendered 3D scene, from a prompt and
  a reference image. It works in Reactor Realtime and in a segment node.
- In Reactor Realtime, Pika's next clip now plays straight after the one before, with no pause.
- After an update, the browser loads the new version of the Reactor Realtime window instead of an
  old cached copy.
- Start ComfyUI with `REACTOR_DEV_MODE=1` to log each Reactor session's events to the console
  when checking a model or working on the plugin.

## 0.4.0

- Add XMAX X2.1, a video-editing model that takes a source video, a prompt, an optional
  reference image and up to eight drag points. It works in Reactor Realtime and in a segment node.
- Add the Reactor XMAX X2.1 Drag node: one finger, from a start point to an end point, on a segment.
- Reactor Realtime takes several touches at once, so you can drag more than one point on the video.
- Add examples: swap a character or an outfit from an image, pose a still by dragging points,
  and a real-time drag to pose a robot.
