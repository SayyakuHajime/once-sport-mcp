"""Arrow geometry helpers and nametag drawings (custom renderer only)."""

import math

import pytest

from once_sport_mcp.rendering import (
    POSITION_CODES,
    RenderError,
    _arrow_head,
    _dashes,
    _path_length,
    _spline,
    _trim_end,
    validate_plan,
)

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
    }
    mark.update(changes)
    return mark


def paint(tmp_path, mark, background="#183828"):
    from PIL import Image

    from once_sport_mcp.rendering import _paint

    path = tmp_path / "frame.png"
    Image.new("RGB", (320, 180), background).save(path)
    _paint(path, EVENT, [mark], 0.5)
    return Image.open(path)


def test_spline_passes_through_every_control_point():
    points = [(0.0, 0.0), (10.0, 5.0), (20.0, 0.0), (30.0, 8.0)]
    curve = _spline(points)
    assert curve[0] == points[0] and curve[-1] == points[-1]
    for point in points:
        assert min(math.dist(point, sample) for sample in curve) < 1e-9


def test_trim_end_shortens_the_path_by_the_requested_length():
    path = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)]
    trimmed = _trim_end(path, 4.0)
    assert trimmed[-1] == pytest.approx((10.0, 6.0))
    assert _path_length(trimmed) == pytest.approx(16.0)
    assert _trim_end(path, 12.0)[-1] == pytest.approx((8.0, 0.0))


def test_arrow_head_points_along_the_path_end_and_has_a_notch():
    head, notch = _arrow_head([(0.0, 0.0), (50.0, 0.0)], 10.0)
    assert head[0] == (50.0, 0.0)
    assert notch == pytest.approx((43.0, 0.0))
    assert head[1][1] == pytest.approx(-head[3][1])  # symmetric about the path


def test_dashes_cover_the_path_with_gaps():
    pieces = _dashes([(0.0, 0.0), (100.0, 0.0)], 10.0, 5.0)
    assert len(pieces) == 7
    assert sum(_path_length(piece) for piece in pieces) == pytest.approx(
        100 * 10 / 15, abs=10
    )
    assert pieces[0][0] == (0.0, 0.0)


def test_smooth_dash_and_flag_validation():
    points = [[0.1, 0.2], [0.4, 0.6], [0.8, 0.3]]
    ok = drawing("polyline", points, smooth=True, dash=True, arrow=True)
    assert validate_plan([EVENT], [ok], MEDIA) is None
    for bad in (
        drawing("polyline", points, smooth="yes"),
        drawing("ellipse", [[0.5, 0.5], [0.1, 0.1]], dash=True),
        drawing("line", points[:2], smooth=True),
    ):
        with pytest.raises(RenderError):
            validate_plan([EVENT], [bad], MEDIA)


def test_nametag_validation_matrix():
    tag = drawing("nametag", [[0.5, 0.6]], position="CF", text="Messi")
    assert validate_plan([EVENT], [tag], MEDIA) is None
    assert (
        validate_plan([EVENT], [drawing("nametag", [[0.5, 0.6]], position="GK")], MEDIA)
        is None
    )
    assert (
        validate_plan([EVENT], [drawing("nametag", [[0.5, 0.6]], text="10")], MEDIA)
        is None
    )
    for bad in (
        drawing("nametag", [[0.5, 0.6]]),
        drawing("nametag", [[0.5, 0.6]], position="XX"),
        drawing("nametag", [[0.5, 0.6]], position="cf"),
        drawing("nametag", [[0.5, 0.6]], text="x" * 25),
        drawing("nametag", [[0.5, 0.6]], text="a\nb"),
        drawing("nametag", [[0.5, 0.6], [0.4, 0.4]], position="CF"),
        drawing("label", [[0.5, 0.6]], text="hi", position="CF"),
    ):
        with pytest.raises(RenderError):
            validate_plan([EVENT], [bad], MEDIA)
    assert {"GK", "CB", "CM", "AMF", "CF", "ST", "RW"} <= set(POSITION_CODES)


def test_nametag_paints_a_colored_badge_above_the_anchor(tmp_path):
    mark = drawing("nametag", [[0.5, 0.8]], position="CF", text="Messi")
    image = paint(tmp_path, mark, "#000000")
    badge = image.crop((120, 60, 200, 110)).tobytes()
    reds = sum(
        1
        for i in range(0, len(badge), 3)
        if badge[i] > 200 and badge[i + 1] < 140 and badge[i + 2] < 140
    )
    assert reds > 40  # forward line badge colour (#FF6B6B)
    assert image.getpixel((160, 5)) == (0, 0, 0)  # nothing drawn far from the tag


def test_nametag_near_the_top_edge_flips_below_and_stays_inside(tmp_path):
    mark = drawing("nametag", [[0.5, 0.05]], position="GK", text="Keeper")
    image = paint(tmp_path, mark, "#000000")
    pixels = image.convert("L")
    bright = [
        (x, y)
        for y in range(180)
        for x in range(320)
        if pixels.getpixel((x, y)) > 150 and y < 150
    ]
    assert bright and min(y for _, y in bright) >= 0
    assert max(y for _, y in bright) > 12  # the tag sits below the anchor


def test_arrow_shaft_does_not_poke_through_the_head(tmp_path):
    mark = drawing("arrow", [[0.1, 0.5], [0.9, 0.5]], color="#FF2020", id="arrow")
    image = paint(tmp_path, mark, "#000000")
    tip_x = round(0.9 * 320)
    column_beyond_tip = [image.getpixel((tip_x + 3, y))[0] for y in range(80, 100)]
    assert max(column_beyond_tip) < 60


def test_dashed_line_has_gaps(tmp_path):
    mark = drawing(
        "line", [[0.05, 0.5], [0.95, 0.5]], color="#FF2020", dash=True, id="dash"
    )
    image = paint(tmp_path, mark, "#000000")
    row = [
        max(image.getpixel((x, y))[0] for y in range(88, 93)) for x in range(10, 310)
    ]
    on = sum(1 for value in row if value > 150)
    assert 0.35 * len(row) < on < 0.8 * len(row)
    assert any(value < 60 for value in row)
