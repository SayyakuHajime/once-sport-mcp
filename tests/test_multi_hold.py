"""Several holds in one event (step-by-step freezes)."""

import array
import json
import subprocess

import pytest
from test_freeze import (  # noqa: F401
    drawing,
    event,
    ffmpeg,
    mean_difference,
    media,
    pil_frames,
    rms,
    source,
)

from once_sport_mcp.rendering import RenderError, render_reel, validate_plan


def test_validation_allows_five_holds_and_rejects_the_sixth_or_long_totals():
    marks = [
        drawing(
            id=f"h{i}",
            start_seconds=0.2 * i,
            end_seconds=0.2 * i + 0.1,
            hold_seconds=0.3,
        )
        for i in range(5)
    ]
    assert validate_plan([event()], marks, media()) is None
    sixth = drawing(id="h5", start_seconds=1.2, end_seconds=1.3, hold_seconds=0.3)
    with pytest.raises(RenderError, match="At most 5"):
        validate_plan([event()], [*marks, sixth], media())
    long = [
        drawing(
            id=f"l{i}",
            start_seconds=0.2 * i,
            end_seconds=0.2 * i + 0.1,
            hold_seconds=10,
        )
        for i in range(4)
    ]
    with pytest.raises(RenderError, match="total"):
        validate_plan([event()], long, media())


def test_two_holds_repeat_two_frames_in_order_and_fill_the_manifest(source, tmp_path):  # noqa: F811
    output = tmp_path / "two.mp4"
    marks = [
        drawing(id="first", start_seconds=0.4, end_seconds=0.5, hold_seconds=0.3),
        drawing(id="second", start_seconds=1.0, end_seconds=1.1, hold_seconds=0.2),
    ]
    result = render_reel(source, [event()], marks, output)
    clip = result["clips"][0]
    assert [h["drawing_id"] for h in clip["holds"]] == ["first", "second"]
    assert clip["hold"] == clip["holds"][0]
    assert [h["frame_count"] for h in clip["holds"]] == [3, 2]
    assert clip["frame_count"] == 20 + 3 + 2
    frames = pil_frames(output)
    assert len(frames) == 25
    first = clip["holds"][0]
    start = round(first["reel_start_seconds"] * 10)
    assert mean_difference(frames[start], frames[start + 2]) < 1.0
    second = clip["holds"][1]
    start = round(second["reel_start_seconds"] * 10)
    assert mean_difference(frames[start], frames[start + 1]) < 1.0
    assert second["reel_start_seconds"] > first["reel_end_seconds"]
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
    assert int(probe["streams"][0]["nb_read_frames"]) == 25


def test_two_holds_silence_audio_only_inside_each_hold(tmp_path):
    clip = tmp_path / "tones.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=10:duration=3",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=3",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        clip,
    )
    output = tmp_path / "held.mp4"
    result = render_reel(
        clip,
        [event()],
        [
            drawing(id="a", start_seconds=0.5, end_seconds=0.6, hold_seconds=0.4),
            drawing(id="b", start_seconds=1.5, end_seconds=1.6, hold_seconds=0.4),
        ],
        output,
    )
    holds = result["clips"][0]["holds"]
    raw = ffmpeg("-i", output, "-vn", "-ac", 1, "-ar", 48000, "-f", "s16le", "-")
    samples = array.array("h", raw)

    def level(start, end):
        return rms(samples[round(start * 48000) : round(end * 48000)])

    for hold in holds:
        inside = (hold["reel_start_seconds"] + 0.05, hold["reel_end_seconds"] - 0.05)
        assert level(*inside) < 40
        assert (
            level(hold["reel_start_seconds"] - 0.25, hold["reel_start_seconds"] - 0.1)
            > 500
        )
        assert (
            level(hold["reel_end_seconds"] + 0.1, hold["reel_end_seconds"] + 0.25) > 500
        )
