import pytest

from reactor_render.timeline import MODELS, POSES, Beat, Move, compile_timeline, editor_beats, editor_moves, model_facts

# LongLive: a scene opens with a 29-frame chunk, then 32 frames a chunk. A beat's start is the chunk
# whose video comes nearest its running total of frames: 96 -> 3, 175 -> 6.


def test_longlive_opens_with_set_shot_and_schedules_the_rest():
    plan = compile_timeline("LongLive-2.0", [Beat("a", 96), Beat("b", 79), Beat("c", 79, cut=True)], seed=7)
    assert plan.chunks == 8
    assert plan.setup == [
        ("set_seed", {"seed": 7}),
        ("set_shot", {"prompt": "a"}),
        ("schedule_shot", {"prompt": "b", "at_session_chunk": 3}),
        ("schedule_scene_cut", {"prompt": "c", "at_session_chunk": 6}),
        ("start", {}),
    ]
    assert plan.timed == []


def test_beats_start_where_the_previous_beat_ends():
    plan = compile_timeline("LongLive-2.0", [Beat("long", 120), Beat("short", 48)], seed=0)
    assert plan.setup[1] == ("set_shot", {"prompt": "long"})
    assert plan.setup[2] == ("schedule_shot", {"prompt": "short", "at_session_chunk": 4})
    assert plan.chunks == 5


def test_starts_round_cumulatively_so_the_grid_does_not_drift():
    # 45 frames is 1.4 chunks: rounding each beat alone would make every beat one chunk long.
    plan = compile_timeline("LongLive-2.0", [Beat(str(i), 45) for i in range(10)], seed=0)
    assert [d["at_session_chunk"] for _, d in plan.setup[2:-1]] == [2, 3, 4, 6, 7, 9, 10, 11, 13]
    assert plan.chunks == 14


def test_a_beat_shorter_than_a_chunk_raises():
    with pytest.raises(ValueError, match="Beat 2 lasts 12 frames, less than one 32-frame chunk"):
        compile_timeline("LongLive-2.0", [Beat("a", 48), Beat("b", 12)], seed=0)


def test_a_cut_opening_the_video_is_just_the_opening_shot():
    plan = compile_timeline("LongLive-2.0", [Beat("a", 120, cut=True)], seed=0)
    assert plan.setup[1] == ("set_shot", {"prompt": "a"})


def test_longlive_scene_longer_than_48_chunks_is_rejected():
    with pytest.raises(ValueError, match="add a cut beat"):
        compile_timeline("LongLive-2.0", [Beat("a", 1680)], seed=0)


def test_a_cut_resets_the_longlive_scene_budget():
    plan = compile_timeline("LongLive-2.0", [Beat("a", 1200), Beat("b", 1200, cut=True)], seed=0)
    assert plan.chunks == 75


def test_a_soft_shot_does_not_reset_the_scene_budget():
    with pytest.raises(ValueError, match="add a cut beat"):
        compile_timeline("LongLive-2.0", [Beat("a", 1200), Beat("b", 1200)], seed=0)


def test_longlive_rejects_images():
    with pytest.raises(ValueError, match="reference images"):
        compile_timeline("LongLive-2.0", [Beat("a", 120, image=b"png")], seed=0)


def test_empty_timeline_is_rejected():
    with pytest.raises(ValueError, match="no beats"):
        compile_timeline("LongLive-2.0", [], seed=0)


def test_helios_schedules_prompts_and_sends_image_beats_live():
    # Helios: 33 frames a chunk.
    beats = [Beat("a", 72, image=b"first"), Beat("b", 72), Beat("c", 120, image=b"third")]
    plan = compile_timeline("Helios", beats, seed=3)
    assert plan.chunks == 8
    assert plan.setup == [
        ("set_seed", {"seed": 3}),
        ("set_conditioning", {"prompt": "a", "image": b"first"}),
        ("schedule_prompt", {"prompt": "b", "chunk": 2}),
        ("start", {}),
    ]
    assert plan.timed == [(3, "set_conditioning", {"prompt": "c", "image": b"third"})]


def test_helios_opens_with_set_prompt_without_an_image():
    plan = compile_timeline("Helios", [Beat("a", 120)], seed=0)
    assert plan.setup[1] == ("set_prompt", {"prompt": "a"})


def test_helios_rejects_cuts():
    with pytest.raises(ValueError, match="no hard cuts"):
        compile_timeline("Helios", [Beat("a", 48), Beat("b", 48, cut=True)], seed=0)


def test_helios_ignores_a_cut_on_the_first_beat():
    plan = compile_timeline("Helios", [Beat("a", 48, cut=True), Beat("b", 48)], seed=0)
    assert plan.setup[1] == ("set_prompt", {"prompt": "a"})


def editor(*beats):
    return {"beats": [{"prompt": p, "frames": d, "cut": cut, "image": image} for p, d, cut, image in beats]}


# Visko: 33 frames a chunk: 99 frames -> 3, 198 -> 6.


def test_live_models_open_with_image_then_prompt_then_start():
    plan = compile_timeline("Visko Orbis Dynamic", [Beat("a", 99, image=b"png"), Beat("b", 99)], seed=7)
    assert plan.setup == [
        ("set_seed", {"seed": 7}),
        ("set_image", {"image": b"png"}),
        ("set_prompt", {"prompt": "a"}),
        ("start", {}),
    ]
    assert plan.chunks == 6


def test_live_beats_after_the_first_are_sent_a_chunk_early():
    plan = compile_timeline("Visko Orbis Stable", [Beat("a", 99), Beat("b", 99), Beat("c", 99)], seed=0)
    assert plan.setup[1] == ("set_prompt", {"prompt": "a"})
    assert plan.timed == [(2, "set_prompt", {"prompt": "b"}), (5, "set_prompt", {"prompt": "c"})]


def test_a_follow_up_image_is_dropped_with_a_warning_when_the_model_reads_images_only_at_start(caplog):
    plan = compile_timeline("LingBot", [Beat("a", 48, image=b"one"), Beat("b", 48, image=b"two")], seed=0)
    assert plan.timed == [(1, "set_prompt", {"prompt": "b"})]
    assert "dropping the image on the beat at frame 48" in caplog.text


def test_a_model_that_needs_an_image_refuses_a_timeline_without_one():
    with pytest.raises(ValueError, match="LingBot World 2 needs an image on the first beat"):
        compile_timeline("LingBot World 2", [Beat("a", 144)], seed=0)


def test_a_live_render_longer_than_one_run_raises():
    with pytest.raises(ValueError, match="A LingBot render can last at most 7193 frames"):
        compile_timeline("LingBot", [Beat("a", 7216, image=b"one")], seed=0)


def test_editor_beats_play_in_the_order_they_are_stored():
    beats = editor_beats(editor(("first", 96, False, None), ("middle", 79, False, None), ("late", 79, True, None)), {})
    plan = compile_timeline("LongLive-2.0", beats, seed=0)
    assert plan.setup[1:4] == [
        ("set_shot", {"prompt": "first"}),
        ("schedule_shot", {"prompt": "middle", "at_session_chunk": 3}),
        ("schedule_scene_cut", {"prompt": "late", "at_session_chunk": 6}),
    ]


def test_an_editor_beat_takes_the_image_from_its_named_slot():
    beats = editor_beats(editor(("a", 2, False, "image_1"), ("b", 2, False, None)), {"image_0": b"zero", "image_1": b"one"})
    assert [b.image for b in beats] == [b"one", None]


def test_an_editor_beat_naming_an_unconnected_slot_raises():
    with pytest.raises(ValueError, match="Beat 1 uses image_2, which has no image connected"):
        editor_beats(editor(("a", 72, False, "image_2")), {"image_0": b"zero"})


def test_model_facts_carry_the_chunk_grid_compile_timeline_snaps_to():
    facts = model_facts(MODELS["LongLive-2.0"])
    assert facts == {"fps": 24.0, "frames_per_chunk": 32, "first_chunk_frames": 29,
                     "supports_cuts": True, "max_scene_chunks": 48, "images": "none", "image_required": False,
                     "camera": {}}
    camera = model_facts(MODELS["LingBot"])["camera"]
    assert "rotation_speed_deg" not in camera
    assert camera["look_horizontal"]["speed"] == {"idle": 5.0, "maximum": 30.0}
    assert camera["movement"]["speed"] is None
    assert model_facts(MODELS["Visko Orbis Dynamic"])["first_chunk_frames"] == 33
    assert model_facts(MODELS["Helios"])["supports_cuts"] is False


def test_boundaries_snap_to_whole_chunks_of_video_including_a_short_first_chunk():
    # LingBot: 17 frames, then 24 a chunk. 48 frames is nearest 2 chunks (41); 96 is nearest
    # 4 chunks (89) rather than the 4 whole 24-frame chunks a flat grid gives.
    plan = compile_timeline("LingBot", [Beat("a", 48, image=b"one"), Beat("b", 48), Beat("c", 24)], seed=0)
    assert plan.timed == [(1, "set_prompt", {"prompt": "b"}), (3, "set_prompt", {"prompt": "c"})]
    assert plan.chunks == 5


def test_a_cut_restarts_the_short_first_chunk():
    # The cut at chunk 3 opens a scene at 93 frames; 144 frames is 51 into it: 2 chunks (61), not 1 (29).
    plan = compile_timeline("LongLive-2.0", [Beat("a", 96), Beat("b", 48, cut=True)], seed=0)
    assert plan.setup[2] == ("schedule_scene_cut", {"prompt": "b", "at_session_chunk": 3})
    assert plan.chunks == 5


def test_a_model_setting_goes_out_before_start():
    plan = compile_timeline("Visko Orbis Stable", [Beat("a", 99)], seed=0, settings={"resolution": "4k"})
    assert plan.setup[-2:] == [("set_resolution", {"resolution": "4k"}), ("start", {})]


def test_a_helios_setting_goes_out_before_start():
    plan = compile_timeline("Helios", [Beat("a", 72)], seed=0, settings={"sr_scale": "4x"})
    assert plan.setup[-2:] == [("set_sr_scale", {"sr_scale": "4x"}), ("start", {})]


# LingBot: chunk n of a run ends at 17 + 24(n - 1) frames, so chunk edges fall at frames 17, 41,
# 65, 89 and 113. A 144-frame beat renders 6 chunks.
LINGBOT_BEAT = [Beat("a", 144, image=b"one")]


def camera(plan):
    """The camera commands of a plan: those before `start`, and the timed ones."""
    lanes = {lane.command for spec in MODELS.values() for lane in spec.camera.values()}
    return [c for c in plan.setup if c[0] in lanes], [t for t in plan.timed if t[1] in lanes]


def test_a_move_holds_its_lane_from_its_first_chunk_then_idles():
    plan = compile_timeline("LingBot", LINGBOT_BEAT, seed=0, moves=[Move("movement", "forward", 41, 48)])
    assert camera(plan) == ([], [(1, "set_movement", {"movement": "forward"}), (3, "set_movement", {"movement": "idle"})])


def test_moves_on_different_lanes_overlap_and_go_out_a_chunk_early():
    moves = [Move("movement", "forward", 17, 48), Move("look_horizontal", "left", 41, 48)]
    plan = compile_timeline("LingBot", LINGBOT_BEAT, seed=0, moves=moves)
    assert camera(plan)[1] == [(0, "set_movement", {"movement": "forward"}), (1, "set_look_horizontal", {"look_horizontal": "left"}),
                               (2, "set_movement", {"movement": "idle"}), (3, "set_look_horizontal", {"look_horizontal": "idle"})]


def test_a_move_from_zero_goes_out_before_start():
    plan = compile_timeline("LingBot", LINGBOT_BEAT, seed=0, moves=[Move("look_vertical", "up", 0, 41)])
    assert plan.setup[-2:] == [("set_look_vertical", {"look_vertical": "up"}), ("start", {})]
    assert camera(plan)[1] == [(1, "set_look_vertical", {"look_vertical": "idle"})]


def test_an_unchanged_lane_sends_nothing():
    moves = [Move("movement", "forward", 0, 41), Move("movement", "forward", 41, 24)]
    plan = compile_timeline("LingBot", LINGBOT_BEAT, seed=0, moves=moves)
    assert camera(plan) == ([("set_movement", {"movement": "forward"})], [(2, "set_movement", {"movement": "idle"})])


def test_a_look_move_sends_its_speed_which_holds_until_the_next_look_move():
    moves = [Move("look_horizontal", "right", 17, 24, speed=12), Move("look_vertical", "up", 65, 24)]
    plan = compile_timeline("LingBot", LINGBOT_BEAT, seed=0, moves=moves)
    assert [t for t in camera(plan)[1] if t[1] == "set_rotation_speed_deg"] == [
        (0, "set_rotation_speed_deg", {"rotation_speed_deg": 12.0}), (2, "set_rotation_speed_deg", {"rotation_speed_deg": 5.0})]


def test_overlapping_look_moves_at_different_speeds_raise():
    moves = [Move("look_horizontal", "right", 17, 48, speed=12), Move("look_vertical", "up", 41, 24, speed=20)]
    with pytest.raises(ValueError, match="turn at different speeds"):
        compile_timeline("LingBot", LINGBOT_BEAT, seed=0, moves=moves)


def test_a_move_past_the_end_is_cut_at_the_last_chunk():
    plan = compile_timeline("LingBot", LINGBOT_BEAT, seed=0, moves=[Move("look_horizontal", "right", 89, 1600)])
    assert camera(plan)[1] == [(3, "set_look_horizontal", {"look_horizontal": "right"})]


def test_overlapping_moves_on_one_lane_are_refused():
    moves = [Move("movement", "forward", 0, 65), Move("movement", "back", 41, 48)]
    with pytest.raises(ValueError, match="Two movement moves overlap at frame 41"):
        compile_timeline("LingBot", LINGBOT_BEAT, seed=0, moves=moves)


def test_a_move_on_a_lane_the_model_lacks_is_refused():
    with pytest.raises(ValueError, match="LingBot has no camera_pose camera control"):
        compile_timeline("LingBot", LINGBOT_BEAT, seed=0, moves=[Move("camera_pose", "dolly_in", 0, 48)])
    with pytest.raises(ValueError, match="LongLive-2.0 has no camera controls"):
        compile_timeline("LongLive-2.0", [Beat("a", 120)], seed=0, moves=[Move("movement", "forward", 0, 48)])


def test_a_pose_preset_goes_out_as_its_deltas():
    plan = compile_timeline("LingBot World 2", LINGBOT_BEAT, seed=0, moves=[Move("camera_pose", "dolly_in", 0, 41)])
    assert camera(plan) == ([("set_camera_pose", {"camera_pose": POSES["dolly_in"]})], [(1, "set_camera_pose", {"camera_pose": []})])


def test_a_saved_timeline_without_moves_loads_with_none():
    assert editor_moves({"beats": []}) == []
    assert editor_moves({"beats": [], "moves": [{"lane": "movement", "value": "back", "start_frame": 17, "frames": 24}]}) == [
        Move("movement", "back", 17, 24)]


def test_a_move_continued_across_two_beats_sends_no_idle_between():
    beats = [Beat("a", 65, image=b"one", moves=(Move("movement", "forward", 17, 999),)),
             Beat("b", 79, moves=(Move("movement", "forward", 0, 24),))]
    plan = compile_timeline("LingBot", beats, seed=0)
    assert camera(plan)[1] == [(0, "set_movement", {"movement": "forward"}), (3, "set_movement", {"movement": "idle"})]


def test_a_beat_move_is_placed_from_its_beats_scheduled_start():
    # The first beat's 55 frames snap to 3 chunks, so the second beat starts at frame 65, not 55.
    beats = [Beat("a", 55, image=b"one"), Beat("b", 89, moves=(Move("look_horizontal", "left", 12, 24),))]
    plan = compile_timeline("LingBot", beats, seed=0)
    assert camera(plan)[1] == [(3, "set_look_horizontal", {"look_horizontal": "left"}),
                               (4, "set_look_horizontal", {"look_horizontal": "idle"})]


def test_a_beat_move_is_cut_at_its_beats_end():
    beats = [Beat("a", 65, image=b"one", moves=(Move("look_horizontal", "right", 41, 500),)), Beat("b", 79)]
    plan = compile_timeline("LingBot", beats, seed=0)
    assert camera(plan)[1] == [(1, "set_look_horizontal", {"look_horizontal": "right"}),
                               (2, "set_look_horizontal", {"look_horizontal": "idle"})]


def test_a_beat_move_starting_after_its_beat_is_dropped_with_a_warning(caplog):
    beats = [Beat("a", 65, image=b"one", moves=(Move("movement", "back", 70, 24),)), Beat("b", 79)]
    assert camera(compile_timeline("LingBot", beats, seed=0)) == ([], [])
    assert "after the beat ends" in caplog.text
