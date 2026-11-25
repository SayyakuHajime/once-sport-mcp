"""Synthetic FFmpeg checks for custom-renderer freeze/hold behavior."""

import array
import json
import math
import subprocess

import pytest
from PIL import ImageChops

from once_sport_mcp.rendering import RenderError


def ffmpeg(*args):
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", *map(str, args)],
        check=True,
        capture_output=True,
    ).stdout


def make_source(path, *, audio=False):
    args = [
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=10:duration=3",
    ]
    if audio:
        args += [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=3",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
        ]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-shortest"]
    args.append(path)
    ffmpeg(*args)
    return path


@pytest.fixture
def source(tmp_path):
    return make_source(tmp_path / "source.mp4")


def event(event_id="one", start=0, end=2):
    return {
        "event_id": event_id,
        "start_seconds": start,
        "end_seconds": end,
        "team": "Home",
        "phase": "Defending",
        "action": "Hold shape",
    }


def drawing(**changes):
    value = {
        "id": "hold-mark",
        "event_id": "one",
        "kind": "ellipse",
        "start_seconds": 0.4,
        "end_seconds": 1.5,
        "points": [[0.5, 0.35], [0.12, 0.1]],
        "color": "#FFD166",
    }
    value.update(changes)
    return value


def media():
    return {
        "duration": 3,
        "width": 320,
        "height": 180,
        "fps": 10,
        "has_audio": True,
        "sample_aspect_ratio": 1,
    }


@pytest.mark.parametrize(
    "value",
    [True, False, "0.5", float("nan"), float("inf"), -float("inf"), 0, -0.1, 10.01],
)
def test_validation_rejects_invalid_hold_values(value):
    from once_sport_mcp.rendering import validate_plan

    with pytest.raises(RenderError):
        validate_plan([event()], [drawing(hold_seconds=value)], media())


def test_validation_accepts_hold_and_rejects_multiple_or_off_grid_holds():
    from once_sport_mcp.rendering import validate_plan

    assert validate_plan([event()], [drawing(hold_seconds=0.35)], media()) is None
    with pytest.raises(RenderError, match="one hold"):
        validate_plan(
            [event()],
            [
                drawing(hold_seconds=0.2),
                drawing(id="second", hold_seconds=0.3),
            ],
            media(),
        )
    with pytest.raises(RenderError, match="frame"):
        validate_plan(
            [event()],
            [drawing(start_seconds=0.41, end_seconds=0.49, hold_seconds=0.2)],
            media(),
        )


def decoded_frames(path):
    raw = ffmpeg("-i", path, "-f", "rawvideo", "-pix_fmt", "rgb24", "-")
    frame_bytes = 320 * 180 * 3
    assert len(raw) % frame_bytes == 0
    return [
        memoryview(raw)[offset : offset + frame_bytes]
        for offset in range(0, len(raw), frame_bytes)
    ]


def pil_frames(path):
    from PIL import Image

    return [
        Image.frombytes("RGB", (320, 180), bytes(frame))
        for frame in decoded_frames(path)
    ]


def mean_difference(left, right):
    diff = ImageChops.difference(left, right)
    return sum(sum(pixel) for pixel in diff.get_flattened_data()) / (320 * 180 * 3)


def bright_count(frame, box):
    data = frame.crop(box).tobytes()
    return sum(
        data[i] > 160 and data[i + 1] > 100 and data[i + 2] < 170
        for i in range(0, len(data), 3)
    )


def test_render_holds_exact_painted_frame_and_resumes_source(source, tmp_path):
    from once_sport_mcp.rendering import render_reel

    hold_output = tmp_path / "held.mp4"
    reference_output = tmp_path / "reference.mp4"
    render_reel(source, [event()], [drawing(hold_seconds=0.35)], hold_output)
    render_reel(source, [event()], [], reference_output)

    held = pil_frames(hold_output)
    reference = pil_frames(reference_output)
    hold_index = 4
    hold_frames = held[hold_index + 1 : hold_index + 1 + 4]
    assert len(held) == 20 + 4
    assert len(hold_frames) == 4
    assert all(
        mean_difference(hold_frames[0], frame) < 1.0 for frame in hold_frames[1:]
    )
    assert all(bright_count(frame, (110, 35, 210, 95)) > 80 for frame in hold_frames)
    assert mean_difference(held[hold_index - 1], hold_frames[0]) > 1
    assert mean_difference(held[hold_index + 1 + 4], hold_frames[0]) > 1
    assert mean_difference(held[hold_index + 1 + 4], reference[hold_index + 1]) < 4


def test_hold_manifest_shifts_later_clip_and_matches_encoded_frames(source, tmp_path):
    from once_sport_mcp.rendering import render_reel

    output = tmp_path / "manifest.mp4"
    result = render_reel(
        source,
        [event("first", 0, 1.2), event("second", 1.5, 2.5)],
        [drawing(event_id="first", end_seconds=1.1, hold_seconds=0.4)],
        output,
    )
    first, second = result["clips"]
    assert first["frame_count"] == 16
    assert first["reel_end_seconds"] == pytest.approx(1.6)
    assert first["hold"] == {
        "drawing_id": "hold-mark",
        "source_seconds": pytest.approx(0.4),
        "hold_seconds": pytest.approx(0.4),
        "reel_start_seconds": pytest.approx(0.5),
        "reel_end_seconds": pytest.approx(0.9),
        "frame_count": 4,
    }
    assert second["reel_start_seconds"] == pytest.approx(1.6)
    assert second["reel_end_seconds"] == pytest.approx(2.6)
    assert result["output"]["frame_count"] == 26
    probe = json.loads(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-count_frames",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=nb_read_frames",
                "-of",
                "json",
                str(output),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    assert int(probe["streams"][0]["nb_read_frames"]) == result["output"]["frame_count"]


def rms(samples):
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


def test_hold_silences_audio_without_losing_or_repeating_source_audio(tmp_path):
    from once_sport_mcp.rendering import render_reel

    source_path = make_source(tmp_path / "audio-source.mp4", audio=True)
    output = tmp_path / "audio-held.mp4"
    result = render_reel(
        source_path,
        [event()],
        [drawing(start_seconds=0.5, end_seconds=1.5, hold_seconds=0.4)],
        output,
    )
    raw = ffmpeg("-i", output, "-vn", "-ac", 1, "-ar", 48000, "-f", "s16le", "-")
    samples = array.array("h", raw)

    def segment(start, end):
        return samples[round(start * 48000) : round(end * 48000)]

    assert rms(segment(0.1, 0.4)) > 100
    assert rms(segment(0.55, 0.85)) < 25
    assert rms(segment(1.05, 1.35)) > 100
    durations = json.loads(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,duration:format=duration",
                "-of",
                "json",
                str(output),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    video_duration = float(
        next(
            stream["duration"]
            for stream in durations["streams"]
            if stream["codec_type"] == "video"
        )
    )
    audio_duration = float(
        next(
            stream["duration"]
            for stream in durations["streams"]
            if stream["codec_type"] == "audio"
        )
    )
    assert abs(video_duration - audio_duration) <= 0.1
    assert result["output"]["frame_count"] == 24


def test_native_validation_rejects_holds():
    from once_sport_mcp.native_rendering import validate_native_plan

    with pytest.raises(RenderError, match="Native Once export does not support holds"):
        validate_native_plan(
            [event()],
            [drawing(hold_seconds=0.4)],
            {**media(), "has_audio": False},
        )
