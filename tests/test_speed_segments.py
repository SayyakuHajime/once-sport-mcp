"""Synthetic FFmpeg checks for custom-renderer speed segments."""

import pytest

from once_sport_mcp.rendering import RenderError, validate_plan

MEDIA = {
    "duration": 3.0,
    "width": 320,
    "height": 180,
    "fps": 10.0,
    "sample_aspect_ratio": 1,
    "has_audio": True,
}
EVENT = {
    "event_id": "one",
    "start_seconds": 0.0,
    "end_seconds": 2.0,
    "team": "Home",
    "phase": "Attack",
    "action": "Pass",
}


def speed_drawing(**changes):
    mark = {
        "id": "speed-mark",
        "event_id": "one",
        "kind": "speed",
        "start_seconds": 0.5,
        "end_seconds": 1.5,
        "points": [],
        "speed": 0.5,
    }
    mark.update(changes)
    return mark


def test_speed_validation_accepts_supported_rates_and_rejects_invalid_schema():
    for speed in (0.25, 0.5, 2, 4):
        assert validate_plan([EVENT], [speed_drawing(speed=speed)], MEDIA) is None

    for changes in (
        {"speed": True},
        {"speed": 1},
        {"speed": 8},
        {"speed": float("nan")},
        {"speed": "0.5"},
        {"speed": None},
        {"speed": 0.5, "points": [[0.5, 0.5]]},
        {"speed": 0.5, "points": None},
        {"speed": 0.5, "speed_extra": 1},
        {"speed": 0.5, "start_seconds": -0.1},
        {"speed": 0.5, "end_seconds": 2.1},
    ):
        with pytest.raises(RenderError):
            validate_plan([EVENT], [speed_drawing(**changes)], MEDIA)


def test_validation_rejects_a_second_speed_segment_per_event():
    with pytest.raises(RenderError, match="one speed"):
        validate_plan(
            [EVENT],
            [
                speed_drawing(),
                speed_drawing(id="second", start_seconds=1.5, end_seconds=1.8),
            ],
            MEDIA,
        )


# --- real ffmpeg render checks -------------------------------------------------
import array as _array
import math as _math
import subprocess as _subprocess

from once_sport_mcp.rendering import render_reel as _render_reel

_EVENT = {
    "event_id": "e",
    "period": 1,
    "start_seconds": 0,
    "end_seconds": 3,
    "team": "T",
    "phase": "attacking",
    "action": "A",
}


def _source(path):
    _subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=10:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
    )


def _speed(speed, start=1, end=2):
    return {
        "id": "sp",
        "event_id": "e",
        "kind": "speed",
        "start_seconds": start,
        "end_seconds": end,
        "points": [],
        "speed": speed,
    }


def _frames(path):
    raw = _subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(path),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    size = 320 * 180 * 3
    return [raw[i : i + size] for i in range(0, len(raw), size)]


def _pcm(path):
    raw = _subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "s16le",
            "-",
        ],
        check=True,
        capture_output=True,
    ).stdout
    data = _array.array("h")
    data.frombytes(raw)
    return data


def _rms(values):
    return _math.sqrt(sum(v * v for v in values) / max(1, len(values)))


def _render(tmp_path, drawings):
    source = tmp_path / "src.mp4"
    _source(source)
    out = tmp_path / "out.mp4"
    manifest = _render_reel(source, [_EVENT], drawings, out, fps=10)
    return out, manifest


def test_slow_motion_duplicates_frames_and_silences_audio(tmp_path):
    out, manifest = _render(tmp_path, [_speed(0.5)])
    clip = manifest["clips"][0]
    assert clip["frame_count"] == 40
    assert clip["speed"]["output_frames"] == 20
    assert _math.isclose(clip["speed"]["reel_start_seconds"], 1.0)
    frames = _frames(out)
    assert len(frames) == 40

    def diff(a, b):
        return sum(abs(x - y) for x, y in zip(a, b)) / len(a)

    for k in range(10, 30, 2):
        assert diff(frames[k], frames[k + 1]) < 1.0
    assert diff(frames[11], frames[12]) > 2.0
    pcm = _pcm(out)
    assert _rms(pcm[int(1.2 * 48000) : int(2.8 * 48000)]) < 5
    assert _rms(pcm[int(0.2 * 48000) : int(0.8 * 48000)]) > 500
    assert _rms(pcm[int(3.2 * 48000) : int(3.8 * 48000)]) > 500
    assert abs(len(pcm) / 48000 - 4.0) < 0.1


def test_fast_forward_drops_frames(tmp_path):
    out, manifest = _render(tmp_path, [_speed(2)])
    clip = manifest["clips"][0]
    assert clip["frame_count"] == 25
    assert clip["speed"]["output_frames"] == 5
    assert len(_frames(out)) == 25


def test_speed_and_hold_add_up_and_hold_inside_speed_is_rejected(tmp_path):
    hold = {
        "id": "h",
        "event_id": "e",
        "kind": "label",
        "start_seconds": 2.5,
        "end_seconds": 3,
        "points": [[0.1, 0.1]],
        "text": "H",
        "hold_seconds": 1,
    }
    out, manifest = _render(tmp_path, [_speed(0.5), hold])
    clip = manifest["clips"][0]
    assert clip["frame_count"] == 50
    assert clip["hold"]["frame_count"] == 10
    assert _math.isclose(clip["hold"]["reel_start_seconds"], 3.6)
    assert len(_frames(out)) == 50
    bad = dict(hold, start_seconds=1.2, end_seconds=1.8)
    from once_sport_mcp.rendering import validate_plan

    media = {
        "duration": 3,
        "width": 320,
        "height": 180,
        "fps": 10,
        "sample_aspect_ratio": 1,
        "has_audio": True,
    }
    with pytest.raises(RenderError, match="speed segment"):
        validate_plan([_EVENT], [_speed(0.5), bad], media)


def test_native_rejects_speed_segment():
    from once_sport_mcp.native_rendering import validate_native_plan

    media = {
        "duration": 3,
        "width": 320,
        "height": 180,
        "fps": 10,
        "sample_aspect_ratio": 1,
        "has_audio": True,
    }
    with pytest.raises(RenderError):
        validate_native_plan([_EVENT], [_speed(0.5)], media)
