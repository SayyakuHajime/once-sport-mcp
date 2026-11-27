"""Marker, lob, tapered/dotted arrows and the tactical board."""

import itertools

import pytest
from PIL import Image

from once_sport_mcp import native_rendering, rendering

MEDIA = {
    "duration": 4.0,
    "width": 640,
    "height": 360,
    "fps": 10.0,
    "sample_aspect_ratio": 1,
    "has_audio": False,
}
EVENT = {
    "event_id": "one",
    "start_seconds": 0.0,
    "end_seconds": 4.0,
    "team": "Home",
    "phase": "Attack",
    "action": "x",
}


def mark(kind, points, **changes):
    base = {
        "id": "m",
        "event_id": "one",
        "kind": kind,
        "start_seconds": 0.0,
        "end_seconds": 4.0,
        "points": points,
    }
    base.update(changes)
    return base


def paint(tmp_path, drawings, background="#000000"):
    path = tmp_path / "f.png"
    Image.new("RGB", (640, 360), background).save(path)
    rendering._paint(path, EVENT, drawings, 1.0)
    return Image.open(path).convert("RGB")


def count(image, box, predicate):
    pixels = image.crop(box).tobytes()
    return sum(
        1
        for i in range(0, len(pixels), 3)
        if predicate(pixels[i], pixels[i + 1], pixels[i + 2])
    )


BOARD_PLAYERS = [
    {"x_m": 5, "y_m": 34, "number": 1, "name": "Keeper", "position": "GK"},
    {
        "x_m": 30,
        "y_m": 20,
        "number": 4,
        "name": "Back",
        "position": "CB",
        "team": "home",
    },
    {"x_m": 50, "y_m": 40, "number": 9, "team": "away", "color": "#FF8800"},
]


def test_validation_of_new_kinds_and_options():
    good = [
        mark("lob", [[0.2, 0.7], [0.8, 0.4]], arc=0.25),
        mark(
            "player_marker",
            [[0.5, 0.6]],
            position="RWF",
            text="ZERROUKI",
            size=1.2,
            tag_color="#5BD1F0",
        ),
        mark("player_marker", [[0.5, 0.6]]),
        mark("arrow", [[0.1, 0.1], [0.4, 0.3]], dash="dotted"),
        mark("curve", [[0.1, 0.1], [0.2, 0.4], [0.5, 0.3]], taper=True),
        mark(
            "board", [[0.02, 0.05], [0.3, 0.95]], players=BOARD_PLAYERS, links=[[1, 2]]
        ),
    ]
    for item in good:
        assert rendering.validate_plan([EVENT], [item], MEDIA) is None
    bad = [
        mark("lob", [[0.2, 0.7], [0.8, 0.4]], arc=0.9),
        mark("lob", [[0.2, 0.7]]),
        mark("arrow", [[0.1, 0.1], [0.4, 0.3]], arc=0.2),
        mark("player_marker", [[0.5, 0.6]], position="XX"),
        mark("player_marker", [[0.5, 0.6]], size=9),
        mark("player_marker", [[0.5, 0.6]], tag_color="red"),
        mark("arrow", [[0.1, 0.1], [0.4, 0.3]], dash="wavy"),
        mark("arrow", [[0.1, 0.1], [0.4, 0.3]], taper=True, dash=True),
        mark("ellipse", [[0.4, 0.4], [0.1, 0.1]], taper=True),
        mark("board", [[0.02, 0.05], [0.05, 0.95]], players=BOARD_PLAYERS),
        mark("board", [[0.02, 0.05], [0.3, 0.95]], players=[]),
        mark("board", [[0.02, 0.05], [0.3, 0.95]], players=[{"x_m": 200, "y_m": 3}]),
        mark(
            "board", [[0.02, 0.05], [0.3, 0.95]], players=BOARD_PLAYERS, links=[[0, 9]]
        ),
        mark(
            "board",
            [[0.02, 0.05], [0.3, 0.95]],
            players=[{"x_m": 1, "y_m": 1, "name": "x" * 17}],
        ),
        mark("line", [[0.1, 0.1], [0.4, 0.3]], players=BOARD_PLAYERS),
    ]
    for item in bad:
        with pytest.raises(rendering.RenderError):
            rendering.validate_plan([EVENT], [item], MEDIA)


def test_native_export_rejects_every_new_style():
    media = dict(MEDIA, has_audio=True)
    for item in (
        mark("lob", [[0.2, 0.7], [0.8, 0.4]]),
        mark("player_marker", [[0.5, 0.6]]),
        mark("board", [[0.02, 0.05], [0.3, 0.95]], players=BOARD_PLAYERS),
        mark("arrow", [[0.1, 0.1], [0.4, 0.3]], taper=True),
    ):
        with pytest.raises(rendering.RenderError):
            native_rendering.validate_native_plan([EVENT], [item], media)


def test_lob_paints_a_shadow_below_a_raised_arc(tmp_path):
    image = paint(
        tmp_path,
        [mark("lob", [[0.2, 0.7], [0.8, 0.7]], color="#FF2020", arc=0.25)],
        "#30A030",
    )
    red = lambda r, g, b: r > 200 and g < 90 and b < 90
    shadow = lambda r, g, b: r < 40 and g < 110 and b < 50
    apex = count(image, (300, 120, 340, 200), red)
    ground = count(image, (300, 235, 340, 262), shadow)
    assert apex > 20  # the arc rises well above the ground path (y=0.7*360=252)
    assert ground > 20  # a dark ground shadow runs along the straight path
    assert count(image, (300, 235, 340, 262), red) < 10  # the arc is not on the ground


def test_player_marker_draws_ring_column_and_a_single_tag(tmp_path):
    drawing = mark(
        "player_marker", [[0.5, 0.7]], position="CB", text="OTAMENDI", color="#FF2020"
    )
    image = paint(tmp_path, [drawing])
    ring = count(image, (300, 245, 340, 262), lambda r, g, b: r > 200 and g < 90)
    column = count(
        image, (305, 200, 335, 240), lambda r, g, b: r > 30 and g > 30 and b > 30
    )
    tag = count(
        image, (270, 262, 370, 290), lambda r, g, b: r > 230 and g > 230 and b > 230
    )
    assert ring > 40 and column > 40 and tag > 80
    # the text appears once: nothing large is painted to the right of the tag
    assert count(image, (380, 240, 520, 290), lambda r, g, b: r > 200 and g > 200) < 5


def test_dotted_arrows_are_separated_dots_and_tapered_ones_widen(tmp_path):
    dotted = paint(
        tmp_path,
        [mark("arrow", [[0.1, 0.5], [0.8, 0.5]], dash="dotted", color="#FF2020")],
    )
    row = [
        max(dotted.getpixel((x, y))[0] for y in range(176, 184)) > 150
        for x in range(60, 480)
    ]
    transitions = sum(1 for a, b in itertools.pairwise(row) if a != b)
    assert transitions >= 16  # many separate dots
    tapered = paint(
        tmp_path, [mark("arrow", [[0.1, 0.5], [0.8, 0.5]], taper=True, color="#FF2020")]
    )
    thin = count(tapered, (80, 150, 100, 210), lambda r, g, b: r > 90 and g < 60)
    thick = count(tapered, (400, 150, 420, 210), lambda r, g, b: r > 90 and g < 60)
    assert thick > thin > 0


def test_board_draws_pitch_tokens_names_and_links_on_top(tmp_path):
    drawing = mark(
        "board",
        [[0.02, 0.05], [0.3, 0.95]],
        players=BOARD_PLAYERS,
        links=[[1, 2]],
        color="#FFD400",
    )
    image = paint(tmp_path, [drawing])
    assert image.getpixel((600, 20)) == (0, 0, 0)  # nothing outside the panel
    assert (
        count(image, (5, 15, 190, 345), lambda r, g, b: g > r + 20 and g > b) > 500
    )  # green pitch
    assert (
        count(image, (5, 15, 190, 345), lambda r, g, b: r > 220 and g > 170 and b < 80)
        > 30
    )  # link
    assert (
        count(image, (5, 15, 190, 345), lambda r, g, b: b > 200 and r < 80) > 30
    )  # home token
    assert (
        count(
            image,
            (5, 15, 190, 345),
            lambda r, g, b: r > 230 and 100 < g < 160 and b < 40,
        )
        > 15
    )  # custom colour


def test_gaps_keep_arrow_ends_off_the_players(tmp_path):
    tight = paint(tmp_path, [mark("arrow", [[0.1, 0.5], [0.9, 0.5]], color="#FF2020")])
    gapped = paint(
        tmp_path,
        [
            mark(
                "arrow",
                [[0.1, 0.5], [0.9, 0.5]],
                color="#FF2020",
                start_gap=0.06,
                end_gap=0.06,
            )
        ],
    )
    red = lambda r, g, b: r > 200 and g < 90
    start_box, end_box = (50, 165, 80, 195), (555, 165, 590, 195)
    assert count(tight, start_box, red) > 8 and count(tight, end_box, red) > 8
    assert count(gapped, start_box, red) == 0 and count(gapped, end_box, red) == 0
    assert count(gapped, (200, 165, 440, 195), red) > 100  # the shaft is still there
    for bad in (-0.01, 0.2, True, "0.1"):
        with pytest.raises(rendering.RenderError):
            rendering.validate_plan(
                [EVENT], [mark("arrow", [[0.1, 0.5], [0.9, 0.5]], start_gap=bad)], MEDIA
            )
    with pytest.raises(rendering.RenderError):
        rendering.validate_plan(
            [EVENT], [mark("ellipse", [[0.4, 0.4], [0.1, 0.1]], end_gap=0.02)], MEDIA
        )


def test_gaps_apply_to_lobs_and_never_collapse_short_paths(tmp_path):
    lob = mark("lob", [[0.1, 0.7], [0.9, 0.7]], arc=0.2, start_gap=0.05, end_gap=0.05)
    assert rendering.validate_plan([EVENT], [lob], MEDIA) is None
    paint(tmp_path, [lob])
    short = mark("arrow", [[0.5, 0.5], [0.52, 0.5]], start_gap=0.1, end_gap=0.1)
    paint(tmp_path, [short])  # too short for the gaps: drawn whole, no crash


def test_opacity_on_strokes_and_ground_on_arrows_validate():
    line = [[0.1, 0.5], [0.9, 0.5]]
    for good in (
        mark("arrow", line, opacity=0.5),
        mark("curve", [[0.1, 0.5], [0.3, 0.2], [0.8, 0.5]], opacity=1.0, ground=True),
        mark("lob", line, opacity=0.4, ground=True),
        mark("arrow", line, ground=True),
    ):
        assert rendering.validate_plan([EVENT], [good], MEDIA) is None
    for bad in (
        mark("arrow", line, opacity=0.05),
        mark("arrow", line, opacity=1.5),
        mark("arrow", line, opacity=True),
        mark("label", [[0.5, 0.5]], text="x", opacity=0.5),
        mark("nametag", [[0.5, 0.5]], position="CB", opacity=0.5),
    ):
        with pytest.raises(rendering.RenderError):
            rendering.validate_plan([EVENT], [bad], MEDIA)


def test_stroke_opacity_lightens_the_arrow(tmp_path):
    line = [[0.1, 0.5], [0.9, 0.5]]
    full = paint(tmp_path, [mark("arrow", line, color="#FF2020")])
    half = paint(tmp_path, [mark("arrow", line, color="#FF2020", opacity=0.5)])
    assert full.getpixel((320, 180))[0] > 200
    assert 90 < half.getpixel((320, 180))[0] < 160
