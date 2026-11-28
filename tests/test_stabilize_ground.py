"""render_reel integration for camera stabilization and ground masking."""

import json

import pytest
from test_camera import _render_pan_zoom_sequence, _write_mp4

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
    "end_seconds": 3.0,
    "team": "Home",
    "phase": "Attack",
    "action": "Pass",
}


def mark(kind="line", **changes):
    base = {
        "id": "m",
        "event_id": "one",
        "kind": kind,
        "start_seconds": 0.0,
        "end_seconds": 3.0,
        "points": [[0.3, 0.5], [0.5, 0.5]],
        "color": "#FF3030",
    }
    base.update(changes)
    return base


def test_validation_of_stabilize_and_ground_flags():
    assert rendering.validate_plan([EVENT], [mark(stabilize=True)], MEDIA) is None
    assert rendering.validate_plan([EVENT], [mark(ground=True)], MEDIA) is None
    for bad in (
        mark(stabilize="yes"),
        mark("stopwatch", points=[[0.1, 0.1]], stabilize=True),
        mark("label", points=[[0.1, 0.1]], text="x", ground=True),
        mark(
            stabilize=True,
            keyframes=[{"time_seconds": 0.0, "points": [[0.3, 0.5], [0.5, 0.5]]}],
        ),
    ):
        with pytest.raises(rendering.RenderError):
            rendering.validate_plan([EVENT], [bad], MEDIA)


def test_native_export_rejects_stabilize_and_ground():
    plan_media = dict(MEDIA, has_audio=True)
    for flag in ("stabilize", "ground"):
        with pytest.raises(rendering.RenderError):
            native_rendering.validate_native_plan(
                [EVENT], [mark("arrow", **{flag: True})], plan_media
            )


def test_stabilized_drawing_follows_a_panning_camera(tmp_path):
    frames, truth = _render_pan_zoom_sequence(
        count=30, tx_start=200.0, tx_delta=150.0, ty_delta=40.0, zoom_delta=0.1
    )
    clip = tmp_path / "pan.mp4"
    _write_mp4(clip, frames, fps=10.0)
    points = [[0.45, 0.5], [0.6, 0.6]]
    event = dict(EVENT, end_seconds=2.9)
    drawing = mark("line", points=points, end_seconds=2.9, stabilize=True)
    out = tmp_path / "out.mp4"
    manifest = rendering.render_reel(clip, [event], [drawing], out)
    assert manifest["stabilize"][0]["id"] == "m"
    sidecar = json.loads(out.with_suffix(".drawings.json").read_text())
    keyframes = sidecar["drawings"][0]["keyframes"]
    assert len(keyframes) > 10
    worst = 0.0
    for key in keyframes:
        index = round(key["time_seconds"] * 10)
        matrix = truth[min(index, len(truth) - 1)]
        for (u, v), (cu, cv) in zip(points, key["points"]):
            w = matrix[2][0] * u + matrix[2][1] * v + matrix[2][2]
            tu = (matrix[0][0] * u + matrix[0][1] * v + matrix[0][2]) / w
            tv = (matrix[1][0] * u + matrix[1][1] * v + matrix[1][2]) / w
            worst = max(worst, abs(cu - tu) * 640, abs(cv - tv) * 360)
    assert worst < 4.0  # pixels at 640x360


def test_stabilize_fails_closed_without_a_followable_pitch(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    rng = np.random.default_rng(1)
    frames = [
        np.clip(rng.integers(0, 255, (360, 640, 3)), 0, 255).astype(np.uint8)
        for _ in range(30)
    ]
    assert cv2 is not None
    clip = tmp_path / "noise.mp4"
    _write_mp4(clip, frames, fps=10.0)
    with pytest.raises(rendering.RenderError):
        rendering.render_reel(
            clip,
            [dict(EVENT, end_seconds=2.9)],
            [mark("line", end_seconds=2.9, stabilize=True)],
            tmp_path / "x.mp4",
        )


def test_ground_drawing_does_not_tint_players(tmp_path):
    np = pytest.importorskip("numpy")
    from PIL import Image

    frame = np.zeros((360, 640, 3), np.uint8)
    for column in range(0, 640, 60):
        frame[:, column : column + 60] = (
            (52, 128, 48) if (column // 60) % 2 else (46, 118, 42)
        )
    frame[150:210, 300:318] = (240, 240, 240)  # a white-kit player
    path = tmp_path / "f.png"
    Image.fromarray(frame).save(path)
    area = mark(
        "area",
        points=[[0.1, 0.2], [0.9, 0.2], [0.9, 0.9], [0.1, 0.9]],
        color="#FF3030",
        opacity=0.6,
        ground=True,
    )
    cache = {"rest": [None, None], "ground": [None, None]}
    rendering._paint_frame(path, EVENT, [area], 0.5, cache)
    out = np.asarray(Image.open(path).convert("RGB")).astype(int)
    player = out[160:200, 304:314]
    assert (np.abs(player - 240).sum(axis=2) < 12).mean() > 0.9  # untouched
    tinted = out[100:140, 100:200]
    assert (tinted[:, :, 0] - frame[100:140, 100:200, 0].astype(int)).mean() > 20


def test_layered_player_marker_keeps_the_player_on_top_and_the_tag_above(tmp_path):
    np = pytest.importorskip("numpy")
    from PIL import Image

    frame = np.zeros((360, 640, 3), np.uint8)
    for column in range(0, 640, 60):
        frame[:, column : column + 60] = (
            (52, 128, 48) if (column // 60) % 2 else (46, 118, 42)
        )
    frame[150:210, 300:318] = (240, 240, 240)  # a white-kit player, feet at y=210
    path = tmp_path / "f.png"
    Image.fromarray(frame).save(path)
    marker = mark(
        "player_marker",
        points=[[309 / 640, 208 / 360]],
        text="ENG",
        color="#E5484D",
        tag_color="#FFD166",
        size=2.0,
        ground=True,
    )
    cache = {"rest": [None, None], "ground": [None, None]}
    rendering._paint_frame(path, EVENT, [marker], 0.5, cache)
    out = np.asarray(Image.open(path).convert("RGB")).astype(int)
    body = out[160:200, 304:314]
    assert (np.abs(body - 240).sum(axis=2) < 12).mean() > 0.9  # column/ring not on him
    ring = out[200:216, 260:360]
    assert ((ring[:, :, 0] > 200) & (ring[:, :, 1] < 120)).sum() > 40  # ring on grass
    tag = out[212:240, 270:350]
    assert (
        (tag[:, :, 0] > 230) & (tag[:, :, 1] > 190) & (tag[:, :, 2] < 140)
    ).sum() > 40
