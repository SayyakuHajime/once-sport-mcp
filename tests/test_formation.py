import pytest

from once_sport_mcp.formation import FormationError, build_formation

MEDIA = {
    "duration": 2.0,
    "width": 320,
    "height": 180,
    "fps": 10.0,
    "sample_aspect_ratio": 1,
    "has_audio": False,
}


def tracking_result(
    boxes,
    *,
    frames=(0, 1, 2, 3),
    fps=10.0,
    window=(0.0, 0.4),
    status="complete",
    valid_end=None,
):
    first_bbox = list(boxes[0])
    first_time = frames[0] / fps
    if valid_end is None:
        valid_end = frames[-1] / fps + 1 / fps
    samples = []
    for frame, bbox in zip(frames, boxes, strict=True):
        x, y, width, height = bbox
        samples.append(
            {
                "source_frame_index": frame,
                "source_seconds": frame / fps,
                "bbox": list(bbox),
                "anchor": [x + width / 2, y + height],
                "fb_iou": 1.0,
            }
        )
    return {
        "backend": "synthetic",
        "requested_window": {"start_seconds": window[0], "end_seconds": window[1]},
        "exact_seed": {
            "source_frame_index": frames[0],
            "source_seconds": first_time,
            "bbox_normalized_xywh": first_bbox,
        },
        "actual_first_seconds": first_time,
        "actual_last_seconds": frames[-1] / fps,
        "valid_end_seconds": valid_end,
        "frame_count": len(samples),
        "status": status,
        "loss_reason": None if status == "complete" else "synthetic_loss",
        "review_required": True,
        "source_metadata": {
            "duration_seconds": MEDIA["duration"],
            "width": MEDIA["width"],
            "height": MEDIA["height"],
            "fps": fps,
            "sample_aspect_ratio": MEDIA["sample_aspect_ratio"],
            "has_audio": MEDIA["has_audio"],
        },
        "samples": samples,
        "canonical_keyframes": [],
        "fixed_radii": [first_bbox[2] * 0.9, first_bbox[3] * 0.18],
    }


def moving_boxes(start_x, *, step=0.01, y=0.25, width=0.1, height=0.2, count=4):
    return [[start_x + frame * step, y, width, height] for frame in range(count)]


def test_default_links_and_drawing_keyframe_geometry():
    right = tracking_result(moving_boxes(0.7))
    left = tracking_result(moving_boxes(0.1))

    result = build_formation([right, left], media=MEDIA, event_id="attack-1")

    assert result["links"] == [[1, 0]]
    assert result["interval"] == {
        "start_seconds": 0.0,
        "end_seconds": 0.4,
        "frame_count": 4,
    }
    line, *rings = result["drawings"]
    assert line == {
        "id": "formation-line-1-0",
        "event_id": "attack-1",
        "kind": "line",
        "start_seconds": 0.0,
        "end_seconds": 0.4,
        "points": [[0.1 + 0.05, 0.45], [0.7 + 0.05, 0.45]],
        "color": "#6DE0D4",
        "keyframes": [
            {
                "time_seconds": frame / 10,
                "points": [
                    [0.1 + frame * 0.01 + 0.05, 0.45],
                    [0.7 + frame * 0.01 + 0.05, 0.45],
                ],
            }
            for frame in range(4)
        ],
    }
    assert [drawing["id"] for drawing in rings] == [
        "formation-ring-0",
        "formation-ring-1",
    ]
    assert rings[0]["points"] == [[0.7 + 0.05, 0.45], [0.1 * 0.9, 0.2 * 0.18]]
    assert rings[0]["keyframes"][-1] == {
        "time_seconds": 0.3,
        "points": [[0.7 + 3 * 0.01 + 0.05, 0.45], [0.1 * 0.9, 0.2 * 0.18]],
    }
    assert all(drawing["event_id"] == "attack-1" for drawing in result["drawings"])


def test_explicit_and_closed_links_validate_pairs():
    results = [tracking_result(moving_boxes(x)) for x in (0.1, 0.4, 0.7)]

    explicit = build_formation(
        results,
        media=MEDIA,
        event_id="attack-2",
        links=[[2, 0]],
        rings=False,
    )
    assert explicit["links"] == [[2, 0]]
    assert [drawing["id"] for drawing in explicit["drawings"]] == ["formation-line-2-0"]

    closed = build_formation(
        results, media=MEDIA, event_id="attack-3", closed=True, rings=False
    )
    assert closed["links"] == [[0, 1], [1, 2], [2, 0]]

    for invalid in ([[0, 0]], [[0, 1], [1, 0]], [[0, 3]], [[0, 1, 2]]):
        with pytest.raises(FormationError):
            build_formation(
                results, media=MEDIA, event_id="bad", links=invalid, rings=False
            )
    with pytest.raises(FormationError):
        build_formation(
            results,
            media=MEDIA,
            event_id="bad",
            links=[[0, 1]],
            closed=True,
        )


def test_target_count_bounds_and_seed_overlap_are_rejected():
    with pytest.raises(FormationError):
        build_formation(
            [tracking_result(moving_boxes(0.1))], media=MEDIA, event_id="bad"
        )
    too_many = [
        tracking_result(moving_boxes(0.01 + index * 0.09, width=0.04))
        for index in range(12)
    ]
    with pytest.raises(FormationError):
        build_formation(too_many, media=MEDIA, event_id="bad")

    overlapping = [
        tracking_result(moving_boxes(0.1)),
        tracking_result(moving_boxes(0.15)),
    ]
    with pytest.raises(FormationError, match="Seed"):
        build_formation(overlapping, media=MEDIA, event_id="bad")


def test_conflict_guard_truncates_iou_and_center_distance_conflicts():
    stationary = moving_boxes(0.1, step=0)
    approaching = [
        [0.5, 0.25, 0.1, 0.2],
        [0.4, 0.25, 0.1, 0.2],
        [0.17, 0.25, 0.1, 0.2],
        [0.17, 0.25, 0.1, 0.2],
    ]
    iou_result = build_formation(
        [tracking_result(stationary), tracking_result(approaching)],
        media=MEDIA,
        event_id="iou",
        rings=False,
    )
    assert iou_result["interval"] == {
        "start_seconds": 0.0,
        "end_seconds": 0.2,
        "frame_count": 2,
    }
    assert iou_result["conflict"] == {
        "frame_index": 2,
        "time_seconds": 0.2,
        "targets": [0, 1],
    }
    assert iou_result["limited_by"] == "possible_overlap_or_identity_swap"
    assert iou_result["needs_reseed"] is True
    assert iou_result["warnings"]

    shallow_a = [[0.1, 0.2, 0.1, 0.04]] * 4
    shallow_b = [
        [0.1, 0.5, 0.1, 0.04],
        [0.1, 0.5, 0.1, 0.04],
        [0.1, 0.25, 0.1, 0.04],
        [0.1, 0.25, 0.1, 0.04],
    ]
    center_result = build_formation(
        [tracking_result(shallow_a), tracking_result(shallow_b)],
        media=MEDIA,
        event_id="center",
        rings=False,
    )
    assert center_result["conflict"] == {
        "frame_index": 2,
        "time_seconds": 0.2,
        "targets": [0, 1],
    }
    assert center_result["interval"]["frame_count"] == 2

    first_frame_conflict = [
        [0.5, 0.25, 0.1, 0.2],
        [0.17, 0.25, 0.1, 0.2],
        [0.17, 0.25, 0.1, 0.2],
        [0.19, 0.25, 0.1, 0.2],
    ]
    with pytest.raises(FormationError, match="too short"):
        build_formation(
            [tracking_result(stationary), tracking_result(first_frame_conflict)],
            media=MEDIA,
            event_id="short",
            rings=False,
        )


def test_label_hold_uses_common_frame_and_renderer_schema():
    from once_sport_mcp import rendering

    results = [
        tracking_result(moving_boxes(0.1)),
        tracking_result(moving_boxes(0.7)),
    ]
    plan = build_formation(
        results,
        media=MEDIA,
        event_id="held",
        label="Press now",
        hold_at_seconds=0.15,
        hold_seconds=2.0,
    )
    label = next(drawing for drawing in plan["drawings"] if drawing["kind"] == "label")
    assert label == {
        "id": "formation-label",
        "event_id": "held",
        "kind": "label",
        "start_seconds": 0.2,
        "end_seconds": 0.4,
        "points": [[0.03, 0.05]],
        "color": "#FFFFFF",
        "text": "Press now",
        "hold_seconds": 2.0,
    }
    assert plan["interval"]["end_seconds"] == 0.4
    event = {
        "event_id": "held",
        "start_seconds": 0.0,
        "end_seconds": 0.4,
        "team": "Formation",
        "phase": "Tracking",
        "action": "Formation",
    }
    rendering.validate_plan([event], plan["drawings"], MEDIA)


def test_label_hold_requires_label_and_valid_frame_grid():
    results = [
        tracking_result(moving_boxes(0.1)),
        tracking_result(moving_boxes(0.7)),
    ]
    with pytest.raises(FormationError):
        build_formation(results, media=MEDIA, event_id="bad", hold_seconds=1.0)
    with pytest.raises(FormationError):
        build_formation(results, media=MEDIA, event_id="bad", hold_at_seconds=0.1)
    with pytest.raises(FormationError):
        build_formation(
            results, media=MEDIA, event_id="bad", label="Too late", hold_at_seconds=0.31
        )
    for invalid_label in ("", "   ", "two\nlines", "x" * 121):
        with pytest.raises(FormationError):
            build_formation(results, media=MEDIA, event_id="bad", label=invalid_label)
    for invalid_hold in (0, -1, 10.1, float("inf"), True):
        with pytest.raises(FormationError):
            build_formation(
                results,
                media=MEDIA,
                event_id="bad",
                label="Hold",
                hold_seconds=invalid_hold,
            )


def test_area_formation_orders_seed_anchors_and_tracks_keyframes():
    results = [
        tracking_result(moving_boxes(0.2, step=0.002, y=0.2, width=0.05, height=0.05)),
        tracking_result(moving_boxes(0.7, step=0.002, y=0.2, width=0.05, height=0.05)),
        tracking_result(
            moving_boxes(0.45, step=0.002, y=0.65, width=0.05, height=0.05)
        ),
    ]
    plan = build_formation(
        results,
        media=MEDIA,
        event_id="area-event",
        links=[],
        rings=False,
        area=True,
        area_color="#FF2020",
        area_opacity=0.3,
    )
    area = next(mark for mark in plan["drawings"] if mark["kind"] == "area")
    assert area["id"] == "formation-area"
    assert area["color"] == "#FF2020"
    assert area["opacity"] == 0.3
    expected_order = [0, 1, 2]
    anchors = [result["samples"][0]["anchor"] for result in results]
    assert area["points"] == [anchors[index] for index in expected_order]
    assert area["keyframes"][1]["points"] == [
        result["samples"][1]["anchor"] for result in results
    ]


def test_spotlights_replace_rings_and_require_rings_false():
    results = [
        tracking_result(moving_boxes(0.1, y=0.25)),
        tracking_result(moving_boxes(0.7, y=0.25)),
    ]
    with pytest.raises(FormationError):
        build_formation(results, media=MEDIA, event_id="bad", spotlights=True)
    plan = build_formation(
        results,
        media=MEDIA,
        event_id="spots",
        spotlights=True,
        rings=False,
    )
    spots = [mark for mark in plan["drawings"] if mark["kind"] == "spotlight"]
    assert [mark["id"] for mark in spots] == ["formation-spot-0", "formation-spot-1"]
    assert not any(mark["kind"] == "ellipse" for mark in plan["drawings"])
    assert all(mark["keyframes"] for mark in spots)


def test_tags_add_following_nametags_and_validate_shape():
    results = [
        tracking_result(moving_boxes(0.1, y=0.25)),
        tracking_result(moving_boxes(0.7, y=0.25)),
    ]
    plan = build_formation(
        results,
        media=MEDIA,
        event_id="tags",
        tags=[{"position": "CB", "name": "Otamendi"}, None],
    )
    tags = [mark for mark in plan["drawings"] if mark["kind"] == "nametag"]
    assert [mark["id"] for mark in tags] == ["formation-tag-0"]
    assert tags[0]["position"] == "CB" and tags[0]["text"] == "Otamendi"
    assert len(tags[0]["keyframes"]) == plan["interval"]["frame_count"]
    assert len(tags[0]["points"]) == 1
    for bad in (
        [{"position": "CB"}],
        [{"position": "CB"}, {"role": "x"}],
        [{}, {"name": "x"}],
        "CB",
    ):
        with pytest.raises(FormationError):
            build_formation(results, media=MEDIA, event_id="bad", tags=bad)


def test_trails_add_one_following_trail_per_target():
    results = [
        tracking_result(moving_boxes(0.1, y=0.25)),
        tracking_result(moving_boxes(0.7, y=0.25)),
    ]
    plan = build_formation(results, media=MEDIA, event_id="tr", trails=1.5)
    trails = [mark for mark in plan["drawings"] if mark["kind"] == "trail"]
    assert [mark["id"] for mark in trails] == ["formation-trail-0", "formation-trail-1"]
    assert all(mark["trail_seconds"] == 1.5 for mark in trails)
    assert all(
        len(mark["keyframes"]) == plan["interval"]["frame_count"] for mark in trails
    )
    for bad in (0.1, 11, True, "2"):
        with pytest.raises(FormationError):
            build_formation(results, media=MEDIA, event_id="tr", trails=bad)
