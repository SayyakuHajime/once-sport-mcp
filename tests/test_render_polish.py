"""Regression tests for renderer ordering, layout, fades, and keyframe easing."""

import pytest
from PIL import Image

from once_sport_mcp import rendering
from once_sport_mcp.native_rendering import validate_native_plan
from once_sport_mcp.rendering import RenderError, validate_plan

MEDIA = {
    "duration": 3.0,
    "width": 320,
    "height": 180,
    "fps": 10.0,
    "sample_aspect_ratio": 1,
    "has_audio": False,
}
EVENT = {
    "event_id": "one",
    "start_seconds": 0.0,
    "end_seconds": 2.0,
    "team": "Home",
    "phase": "Attack",
    "action": "Pass",
}


def mark(kind, points, **changes):
    value = {
        "id": f"{kind}-{len(points)}-{len(changes)}",
        "event_id": "one",
        "kind": kind,
        "start_seconds": 0.0,
        "end_seconds": 2.0,
        "points": points,
    }
    value.update(changes)
    return value


def paint(tmp_path, drawings, time=0.5, cache=None):
    path = tmp_path / "frame.png"
    Image.new("RGB", (320, 180), "#183828").save(path)
    if cache is None:
        rendering._paint(path, EVENT, drawings, time)
    else:
        rendering._paint(path, EVENT, drawings, time, cache)
    return Image.open(path).convert("RGB")


def test_drawings_use_required_z_tiers_and_are_stable_within_tiers(tmp_path):
    tiers = [
        ["area"],
        ["line", "polyline", "arrow", "curve", "polygon"],
        ["ellipse", "spotlight"],
        ["nametag"],
        ["label", "stopwatch"],
    ]
    assert [rendering._drawing_z_tier(kind) for tier in tiers for kind in tier] == [
        index for index, tier in enumerate(tiers) for _ in tier
    ]

    area = mark(
        "area",
        [[0.05, 0.05], [0.95, 0.05], [0.95, 0.95], [0.05, 0.95]],
        color="#FF0000",
    )
    label = mark("label", [[0.3, 0.2]], text="TOP", color="#FFFFFF")
    assert (
        paint(tmp_path, [label, area]).tobytes()
        == paint(tmp_path, [area, label]).tobytes()
    )

    first = mark("label", [[0.1, 0.2]], text="FIRST", color="#FF0000", id="first")
    second = mark("label", [[0.1, 0.2]], text="SECOND", color="#00FF00", id="second")
    assert (
        paint(tmp_path, [first, second]).tobytes()
        != paint(tmp_path, [second, first]).tobytes()
    )


def test_overlay_cache_is_not_module_global():
    assert "_OVERLAY_CACHE" not in rendering.__dict__


def test_short_arrow_keeps_a_proportionate_tangent_aligned_head(tmp_path):
    image = paint(
        tmp_path,
        [
            mark(
                "arrow",
                [[0.4, 0.25], [0.45, 0.25]],
                color="#FF2020",
            )
        ],
    )
    # At 320x180 this is a 16px arrow: the former fixed 27px head was omitted.
    pixel_wing = image.getpixel((138, 43))
    pixel_tip = image.getpixel((140, 45))
    assert isinstance(pixel_wing, tuple) and pixel_wing[0] > 150
    assert isinstance(pixel_tip, tuple) and pixel_tip[0] > 150

    # Degenerate zero-length arrow does not crash.
    paint(tmp_path, [mark("arrow", [[0.5, 0.5], [0.5, 0.5]], color="#FF2020")])


def test_label_wraps_at_word_boundaries_before_splitting_long_words():
    measure = lambda value: len(value)
    assert rendering._wrap_label("one two", measure, 7) == ["one two"]
    assert rendering._wrap_label("one two", measure, 6) == ["one", "two"]
    assert rendering._wrap_label("abcdefgh", measure, 5) == ["abcde", "fgh"]


def test_nametag_boxes_are_footer_clamped_and_greedily_nudged(tmp_path, monkeypatch):
    placements = []
    place = rendering._place_tag_box

    def record(*args, **kwargs):
        box = place(*args, **kwargs)
        placements.append(box)
        return box

    monkeypatch.setattr(rendering, "_place_tag_box", record)
    tags = [
        mark("nametag", [[0.5, 0.95]], position="CF", text="One", id="one"),
        mark("nametag", [[0.5, 0.95]], position="CF", text="Two", id="two"),
    ]
    original_points = [drawing["points"][:] for drawing in tags]
    paint(tmp_path, tags)

    assert len(placements) == 2
    left_a, top_a, right_a, bottom_a = placements[0]
    left_b, top_b, right_b, bottom_b = placements[1]
    # Footer pill at 180p occupies the bottom 25px (4px margin + 21px box).
    assert bottom_a <= (180 - 25) * 3 and bottom_b <= (180 - 25) * 3
    assert (
        right_a <= left_b or right_b <= left_a or bottom_a <= top_b or bottom_b <= top_a
    )
    assert [drawing["points"] for drawing in tags] == original_points

    placements.clear()
    hd = tmp_path / "hd.png"
    Image.new("RGB", (1920, 1080), "#183828").save(hd)
    rendering._paint(
        hd, EVENT, [mark("nametag", [[0.5, 0.99]], position="CF", text="Bottom")], 0.5
    )
    assert placements[0][3] <= (1080 - 48) * 2


def test_fade_alpha_is_applied_after_reusing_the_opaque_overlay_cache(tmp_path):
    drawing = mark(
        "line",
        [[0.1, 0.25], [0.9, 0.25]],
        color="#FF0000",
        fade_in_seconds=0.4,
        fade_out_seconds=0.4,
    )
    cache = [None, None]
    at_start = paint(tmp_path, [drawing], time=0.0, cache=cache).getpixel((160, 45))
    assert cache[1] is not None
    cached_alpha = cache[1].getpixel((160, 45))
    halfway_in = paint(tmp_path, [drawing], time=0.2, cache=cache).getpixel((160, 45))
    cached_overlay = cache[1]
    fully_visible = paint(tmp_path, [drawing], time=0.4, cache=cache).getpixel(
        (160, 45)
    )
    assert cache[1] is cached_overlay
    halfway_out = paint(tmp_path, [drawing], time=1.8, cache=cache).getpixel((160, 45))

    assert isinstance(cached_alpha, tuple) and cached_alpha[3] >= 250
    assert at_start == (24, 56, 40)
    assert (
        isinstance(at_start, tuple)
        and isinstance(halfway_in, tuple)
        and isinstance(fully_visible, tuple)
    )
    assert isinstance(halfway_out, tuple)
    assert at_start[0] < halfway_in[0] < fully_visible[0]
    assert halfway_out[0] == pytest.approx(halfway_in[0], abs=5)

    # Mixed fades on the same frame still fade each drawing independently.
    steady = mark("line", [[0.1, 0.6], [0.9, 0.6]], color="#0000FF", id="steady")
    mixed = paint(tmp_path, [drawing, steady], time=0.2)
    mixed_fade = mixed.getpixel((160, 45))
    mixed_steady = mixed.getpixel((160, 108))
    assert isinstance(mixed_fade, tuple) and isinstance(mixed_steady, tuple)
    assert mixed_fade[0] == pytest.approx(halfway_in[0], abs=5)
    assert mixed_steady[2] > 200


def test_fades_and_easing_validate_and_ease_keyframe_interpolation():
    drawing = mark(
        "line",
        [[0.0, 0.5], [1.0, 0.5]],
        fade_in_seconds=0.3,
        fade_out_seconds=0.4,
        easing="ease_in_out",
        keyframes=[
            {"time_seconds": 0.0, "points": [[0.0, 0.5], [1.0, 0.5]]},
            {"time_seconds": 1.0, "points": [[1.0, 0.5], [0.0, 0.5]]},
        ],
    )
    assert validate_plan([EVENT], [drawing], MEDIA) is None
    assert rendering._positions(drawing, 0.25)[0][0] == pytest.approx(0.15625)
    assert (
        validate_plan(
            [EVENT],
            [
                {
                    "id": "sp",
                    "event_id": "one",
                    "kind": "speed",
                    "start_seconds": 0.0,
                    "end_seconds": 1.0,
                    "points": [],
                    "speed": 0.5,
                }
            ],
            MEDIA,
        )
        is None
    )

    for changes in (
        {"fade_in_seconds": 0},
        {"fade_in_seconds": 5.01},
        {"fade_in_seconds": True},
        {"fade_in_seconds": 1.1, "fade_out_seconds": 1.0},
        {"easing": "smooth"},
    ):
        with pytest.raises(RenderError):
            validate_plan(
                [EVENT], [mark("line", [[0.1, 0.5], [0.9, 0.5]], **changes)], MEDIA
            )
    with pytest.raises(RenderError):
        validate_plan(
            [EVENT],
            [
                {
                    "id": "sp",
                    "event_id": "one",
                    "kind": "speed",
                    "start_seconds": 0.0,
                    "end_seconds": 1.0,
                    "points": [],
                    "speed": 0.5,
                    "fade_in_seconds": 0.2,
                }
            ],
            MEDIA,
        )


def test_native_renderer_rejects_dash_smooth_and_position_kinds():
    arrow = mark("arrow", [[0.1, 0.2], [0.8, 0.8]], dash=True)
    with pytest.raises(RenderError, match="dashed"):
        validate_native_plan([EVENT], [arrow], MEDIA)

    unsupported = (
        mark("polyline", [[0.1, 0.2], [0.8, 0.8]], smooth=True),
        mark("nametag", [[0.5, 0.5]], position="CF"),
    )
    for drawing in unsupported:
        with pytest.raises(RenderError):
            validate_native_plan([EVENT], [drawing], MEDIA)


def test_native_renderer_rejects_fades_and_easing():
    unsupported = (
        mark("arrow", [[0.1, 0.2], [0.8, 0.8]], fade_in_seconds=0.2),
        mark("arrow", [[0.1, 0.2], [0.8, 0.8]], fade_out_seconds=0.2),
        mark("ellipse", [[0.5, 0.5], [0.1, 0.1]], easing="linear"),
    )
    for drawing in unsupported:
        with pytest.raises(RenderError):
            validate_native_plan([EVENT], [drawing], MEDIA)
