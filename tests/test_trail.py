"""Movement trails behind a tracked point."""

import pytest
from PIL import Image

from once_sport_mcp import native_rendering, rendering

MEDIA = {
    "duration": 4.0,
    "width": 320,
    "height": 180,
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
    "action": "Run",
}


def trail(**changes):
    keyframes = [
        {"time_seconds": 0.5 * i, "points": [[0.1 + 0.1 * i, 0.5]]} for i in range(9)
    ]
    mark = {
        "id": "t",
        "event_id": "one",
        "kind": "trail",
        "start_seconds": 0.0,
        "end_seconds": 4.0,
        "points": [[0.1, 0.5]],
        "keyframes": keyframes,
        "color": "#FF3030",
    }
    mark.update(changes)
    return mark


def paint(tmp_path, mark, time):
    path = tmp_path / "f.png"
    Image.new("RGB", (320, 180), "#000000").save(path)
    rendering._paint(path, EVENT, [mark], time)
    return Image.open(path).convert("RGB")


def red(image, box):
    pixels = image.crop(box).tobytes()
    return sum(
        1 for i in range(0, len(pixels), 3) if pixels[i] > 150 and pixels[i + 1] < 90
    )


def test_validation():
    assert rendering.validate_plan([EVENT], [trail()], MEDIA) is None
    assert rendering.validate_plan([EVENT], [trail(trail_seconds=5)], MEDIA) is None
    for bad in (
        trail(trail_seconds=0.1),
        trail(trail_seconds=11),
        trail(trail_seconds=True),
        trail(keyframes=[]),
        trail(points=[[0.1, 0.5], [0.2, 0.5]]),
        {
            **trail(kind="line", points=[[0.1, 0.5], [0.2, 0.5]], keyframes=[]),
            "trail_seconds": 2,
        },
    ):
        with pytest.raises(rendering.RenderError):
            rendering.validate_plan([EVENT], [bad], MEDIA)


def test_trail_covers_only_the_recent_path_and_fades_toward_the_tail(tmp_path):
    image = paint(tmp_path, trail(trail_seconds=1.0), 2.0)  # head at x=0.5*320=160
    assert red(image, (140, 70, 175, 110)) > 40  # the head region is painted
    assert red(image, (10, 70, 60, 110)) == 0  # the old path (t<1) is not
    tail = red(image, (120, 80, 135, 100))
    head = red(image, (145, 80, 160, 100))
    assert head > tail > 0  # thicker and more opaque near the head


def test_longer_trails_reach_further_back(tmp_path):
    short = red(paint(tmp_path, trail(trail_seconds=0.6), 3.0), (30, 70, 160, 110))
    long = red(paint(tmp_path, trail(trail_seconds=3.0), 3.0), (30, 70, 160, 110))
    assert long > short


def test_native_export_rejects_trails():
    with pytest.raises(rendering.RenderError):
        native_rendering.validate_native_plan(
            [EVENT], [trail()], dict(MEDIA, has_audio=True)
        )
