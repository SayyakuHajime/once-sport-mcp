"""Focused validation and raster checks for custom-only drawing kinds."""

import pytest

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


def drawing(kind, points, **changes):
    mark = {
        "id": "mark",
        "event_id": "one",
        "kind": kind,
        "start_seconds": 0.0,
        "end_seconds": 2.0,
        "points": points,
        "color": "#6DE0D4",
    }
    mark.update(changes)
    return mark


def test_polyline_validation_bounds_keyframes_and_arrow_flag():
    mark = drawing(
        "polyline",
        [[0.1, 0.2], [0.5, 0.4], [0.8, 0.2]],
        arrow=True,
        keyframes=[
            {"time_seconds": 0.0, "points": [[0.1, 0.2], [0.5, 0.4], [0.8, 0.2]]},
            {"time_seconds": 1.0, "points": [[0.2, 0.2], [0.6, 0.4], [0.9, 0.2]]},
        ],
    )
    assert validate_plan([EVENT], [mark], MEDIA) is None

    for points in ([[0.1, 0.2]], [[0.1, 0.2]] * 33):
        with pytest.raises(RenderError):
            validate_plan([EVENT], [drawing("polyline", points)], MEDIA)
    with pytest.raises(RenderError):
        validate_plan(
            [EVENT], [drawing("polyline", [[0.1, 0.2], [0.8, 0.2]], arrow="yes")], MEDIA
        )


def paint(tmp_path, mark, time=0.5):
    from PIL import Image

    from once_sport_mcp.rendering import _paint

    path = tmp_path / "frame.png"
    Image.new("RGB", (320, 180), "#183828").save(path)
    _paint(path, EVENT, [mark], time)
    return Image.open(path)


def test_polyline_paints_open_path_and_optional_arrow(tmp_path):
    mark = drawing(
        "polyline",
        [[0.1, 0.2], [0.5, 0.7], [0.9, 0.2]],
        color="#FF2020",
        arrow=True,
    )
    image = paint(tmp_path, mark)
    assert image.getpixel((96, 81))[0] > 180
    assert max(image.getpixel((224, y))[0] for y in (80, 81, 82)) > 180
    assert image.getpixel((281, 38))[0] > 180


def test_curve_validation_and_bezier_midpoint(tmp_path):
    mark = drawing(
        "curve",
        [[0.1, 0.4], [0.5, 0.1], [0.9, 0.4]],
        color="#FF2020",
    )
    assert validate_plan([EVENT], [mark], MEDIA) is None
    for points in ([[0.1, 0.2], [0.5, 0.5]], [[0.1, 0.2]] * 4):
        with pytest.raises(RenderError):
            validate_plan([EVENT], [drawing("curve", points)], MEDIA)
    image = paint(tmp_path, mark)
    assert image.getpixel((160, 45))[0] > 180


def test_area_validation_opacity_keyframes_and_fill_blend(tmp_path):
    mark = drawing(
        "area",
        [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]],
        color="#FF0000",
        opacity=0.25,
        keyframes=[
            {
                "time_seconds": 0.0,
                "points": [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]],
            },
            {
                "time_seconds": 1.0,
                "points": [[0.25, 0.2], [0.85, 0.2], [0.85, 0.8], [0.25, 0.8]],
            },
        ],
    )
    assert validate_plan([EVENT], [mark], MEDIA) is None
    for opacity in (0.049, 0.801, True):
        with pytest.raises(RenderError):
            validate_plan(
                [EVENT], [drawing("area", mark["points"], opacity=opacity)], MEDIA
            )
    image = paint(tmp_path, mark)
    inside = image.getpixel((160, 90))
    assert inside[0] > 24 and inside[0] < 255
    assert image.getpixel((20, 20)) == (24, 56, 40)


def test_spotlight_styles_validate_and_paint_differently(tmp_path):
    points = [[0.5, 0.5], [0.25, 0.15]]
    ground = drawing("spotlight", points, style="ground", color="#FF2020")
    halo = drawing("spotlight", points, style="halo", color="#FF2020")
    assert validate_plan([EVENT], [ground], MEDIA) is None
    assert validate_plan([EVENT], [halo], MEDIA) is None
    for invalid in ("blur", "Ground"):
        with pytest.raises(RenderError):
            validate_plan([EVENT], [drawing("spotlight", points, style=invalid)], MEDIA)
    with pytest.raises(RenderError):
        validate_plan([EVENT], [drawing("spotlight", [[0.5, 0.5], [0, 0.1]])], MEDIA)
    ground_pixels = paint(tmp_path, ground).copy()
    halo_pixels = paint(tmp_path, halo)
    assert ground_pixels.getpixel((160, 90))[0] > 24
    assert halo_pixels.getpixel((160, 90)) == (24, 56, 40)


def test_stopwatch_validation_hold_and_elapsed_text_updates(tmp_path):
    mark = drawing(
        "stopwatch",
        [[0.1, 0.4]],
        start_seconds=0.0,
        end_seconds=2.0,
        text="T",
        hold_seconds=0.4,
    )
    assert validate_plan([EVENT], [mark], MEDIA) is None
    with pytest.raises(RenderError):
        validate_plan([EVENT], [drawing("stopwatch", [[0.1, 0.4], [0.2, 0.3]])], MEDIA)
    first = paint(tmp_path, mark, 0.4).copy()
    second = paint(tmp_path, mark, 1.4)
    assert (
        first.crop((20, 60, 160, 115)).tobytes()
        != second.crop((20, 60, 160, 115)).tobytes()
    )


def test_native_renderer_rejects_custom_drawing_kinds():
    from once_sport_mcp.native_rendering import validate_native_plan

    cases = {
        "polyline": [[0.1, 0.2], [0.8, 0.2]],
        "curve": [[0.1, 0.2], [0.5, 0.8], [0.8, 0.2]],
        "area": [[0.1, 0.2], [0.8, 0.2], [0.5, 0.8]],
        "spotlight": [[0.5, 0.5], [0.1, 0.1]],
        "stopwatch": [[0.1, 0.2]],
    }
    for kind, points in cases.items():
        with pytest.raises(RenderError, match="supports arrows and ellipses only"):
            validate_native_plan([EVENT], [drawing(kind, points)], MEDIA)


def test_label_after_area_and_spotlight_is_still_painted(tmp_path):
    from PIL import Image

    from once_sport_mcp.rendering import _paint

    path = tmp_path / "frame.png"
    Image.new("RGB", (320, 180), "#183828").save(path)
    marks = [
        drawing("area", [[0.4, 0.4], [0.8, 0.4], [0.6, 0.8]], id="a"),
        drawing("spotlight", [[0.5, 0.5], [0.05, 0.05]], id="s"),
        drawing("label", [[0.03, 0.05]], id="t", text="HOLD", color="#FF0000"),
    ]
    _paint(path, EVENT, marks, 0.5)
    box = Image.open(path).crop((4, 4, 80, 40))
    raw = box.tobytes()
    assert any(raw[i] > 200 and raw[i + 1] < 80 for i in range(0, len(raw), 3))


def test_caption_sits_fully_inside_the_frame_bottom(tmp_path):
    from PIL import Image

    from once_sport_mcp.rendering import _paint

    path = tmp_path / "frame.png"
    Image.new("RGB", (320, 180), "#000000").save(path)
    _paint(path, EVENT, [], 0.5)
    image = Image.open(path).convert("L")
    bright = [y for y in range(180) for x in range(320) if image.getpixel((x, y)) > 200]
    assert bright and max(bright) < 176
