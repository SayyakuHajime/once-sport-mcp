"""Real synthetic-media checks; no match footage or GUI is used."""

import hashlib
import itertools
import json
import subprocess

import pytest

from once_sport_mcp.rendering import RenderError, probe_media


def ffmpeg(*args):
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", *map(str, args)],
        check=True,
        capture_output=True,
    ).stdout


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=0x183828:s=320x180:r=8:d=3",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        path,
    )
    return path


def test_probe_real_video_and_bad_source(source, tmp_path):
    info = probe_media(source)
    assert info["duration"] == pytest.approx(3)
    assert (info["width"], info["height"], info["fps"]) == (320, 180, 8)
    assert info["has_audio"] is False
    with pytest.raises(RenderError):
        probe_media(tmp_path / "missing.mp4")


def event(id="one", start=0, end=1):
    return {
        "event_id": id,
        "start_seconds": start,
        "end_seconds": end,
        "team": "Home",
        "phase": "Attack",
        "action": "Pass",
    }


def test_render_workspace_is_under_destination_parent(source, tmp_path, monkeypatch):
    import tempfile
    from pathlib import Path

    from once_sport_mcp.rendering import render_reel

    output = tmp_path / "new-output-parent" / "reel.mp4"
    output.parent.mkdir()
    original_temporary_directory = tempfile.TemporaryDirectory
    allocations = []

    def record_allocation(*args, **kwargs):
        workspace = original_temporary_directory(*args, **kwargs)
        allocations.append(Path(workspace.name))
        return workspace

    monkeypatch.setattr(tempfile, "TemporaryDirectory", record_allocation)
    render_reel(source, [event()], [], output)

    assert len(allocations) == 1
    assert allocations[0].is_relative_to(output.parent)
    assert output.is_file()
    assert output.with_suffix(".drawings.json").is_file()


def test_real_reel_sorted_independent_clips_manifest_and_sidecar(source, tmp_path):
    from once_sport_mcp.rendering import render_reel

    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / "reel.mp4"
    result = render_reel(
        source, [event("late", 1, 2), event("early", 0, 1.5)], [], output
    )
    assert [c["event_id"] for c in result["clips"]] == ["early", "late"]
    assert [
        (c["source_start_seconds"], c["source_end_seconds"]) for c in result["clips"]
    ] == [(0, 1.5), (1, 2)]
    assert [c["frame_count"] for c in result["clips"]] == [12, 8]
    assert [c["reel_start_seconds"] for c in result["clips"]] == [0, 1.5]
    assert result["output"]["frame_count"] == 20
    assert probe_media(output)["duration"] == pytest.approx(2.5)
    assert probe_media(output)["has_audio"] is False
    raw = ffmpeg("-i", output, "-f", "rawvideo", "-pix_fmt", "rgb24", "-")
    assert len(raw) == 20 * 320 * 180 * 3
    sidecar = json.loads(output.with_suffix(".drawings.json").read_text())
    assert sidecar["drawings"] == []
    assert sidecar["semantics"] == "custom-render-only"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest


def pixels(path, width=320, height=180):
    from PIL import Image

    raw = ffmpeg("-i", path, "-f", "rawvideo", "-pix_fmt", "rgb24", "-")
    size = width * height * 3
    return [
        Image.frombytes("RGB", (width, height), raw[i : i + size])
        for i in range(0, len(raw), size)
    ]


def drawing(**changes):
    value = {
        "id": "mark",
        "event_id": "one",
        "kind": "ellipse",
        "start_seconds": 0,
        "end_seconds": 0.75,
        "points": [[0.25, 0.3], [0.08, 0.08]],
        "color": "#FFD166",
    }
    value.update(changes)
    return value


def bright_count(frame, box):
    return sum(
        r > 160 and g > 100 and b < 170
        for r, g, b in frame.crop(box).get_flattened_data()
    )


def test_overlay_half_open_tracking_and_wrong_owner(source, tmp_path):
    from once_sport_mcp.rendering import render_reel

    output = tmp_path / "tracked.mp4"
    marks = [
        drawing(
            keyframes=[
                {"time_seconds": 0, "points": [[0.25, 0.3], [0.08, 0.08]]},
                {"time_seconds": 0.5, "points": [[0.75, 0.3], [0.08, 0.08]]},
            ]
        )
    ]
    render_reel(source, [event(), event("two")], marks, output)
    frames = pixels(output)
    assert bright_count(frames[0], (45, 20, 115, 90)) > 80
    assert bright_count(frames[2], (125, 20, 195, 90)) > 80
    assert bright_count(frames[4], (205, 20, 275, 90)) > 80
    assert bright_count(frames[6], (0, 0, 320, 100)) == 0
    assert bright_count(frames[8], (0, 0, 320, 100)) == 0


@pytest.mark.parametrize(
    "change",
    [
        {"extra": 1},
        {"event_id": "missing"},
        {"kind": "box"},
        {"points": [[float("nan"), 0], [0.1, 0.1]]},
        {"points": [[1.1, 0], [0.1, 0.1]]},
        {"end_seconds": 2},
        {"start_seconds": 0.75},
        {"color": "red"},
        {"kind": "label"},
        {"keyframes": [{"time_seconds": 0.5, "points": [[0, 0]]}]},
        {
            "keyframes": [
                {"time_seconds": 0.5, "points": [[0, 0], [0.1, 0.1]]},
                {"time_seconds": 0.25, "points": [[0, 0], [0.1, 0.1]]},
            ]
        },
    ],
)
def test_malformed_drawing_rejected_before_publication(source, tmp_path, change):
    from once_sport_mcp.rendering import render_reel

    output = tmp_path / "bad.mp4"
    with pytest.raises(RenderError):
        render_reel(source, [event()], [drawing(**change)], output)
    assert not output.exists()
    assert not output.with_suffix(".drawings.json").exists()


def test_fractional_range_uses_ceil_frame_count(source, tmp_path):
    from once_sport_mcp.rendering import render_reel

    output = tmp_path / "fraction.mp4"
    result = render_reel(source, [event(start=2.82, end=3)], [], output)
    assert result["output"]["frame_count"] == 2
    assert len(pixels(output)) == 2


def test_audio_resized_output_and_edge_labels(tmp_path):
    from once_sport_mcp.rendering import render_reel

    source = tmp_path / "audio.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=0x183828:s=320x180:r=8:d=3",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1.5",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:duration=1.5",
        "-filter_complex",
        "[1:a][2:a]concat=n=2:v=0:a=1[a]",
        "-map",
        "0:v",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        source,
    )
    output = tmp_path / "audio-reel.mp4"
    marks = [
        drawing(kind="label", points=[[1, 0]], text="RIGHT EDGE", color="#FFFFFF"),
        drawing(
            id="left",
            kind="label",
            points=[[0, 0.35]],
            text="LEFT EDGE",
            color="#FFFFFF",
        ),
    ]
    result = render_reel(
        source,
        [event("high", 1.5, 2.5), event("one", 0, 1)],
        marks,
        output,
        width=480,
        fps=8,
    )
    assert result["output"]["has_audio"]
    assert (result["output"]["width"], result["output"]["height"]) == (480, 270)
    frames = pixels(output, 480, 270)
    frame = frames[0]

    # Both labels have bright text, with untouched safe padding at each edge.
    def white(box):
        return sum(min(p) > 190 for p in frame.crop(box).get_flattened_data())

    assert white((350, 0, 480, 40)) > 50
    assert white((0, 80, 130, 130)) > 50
    assert white((0, 0, 3, 160)) == 0
    assert white((478, 0, 480, 160)) == 0
    raw = ffmpeg("-i", output, "-vn", "-ac", 1, "-ar", 8000, "-f", "s16le", "-")
    import array

    samples = array.array("h", raw)

    # Check the retained clip audio by zero crossings away from AAC boundaries.
    def crossings(start, end):
        part = samples[int(start * 8000) : int(end * 8000)]
        return sum(a <= 0 < b for a, b in itertools.pairwise(part)) / (end - start)

    assert crossings(0.2, 0.8) == pytest.approx(440, abs=8)
    assert crossings(1.2, 1.8) == pytest.approx(880, abs=8)


def test_failed_publication_uses_render_error(source, tmp_path):
    from once_sport_mcp.rendering import render_reel

    with pytest.raises(RenderError):
        render_reel(source, [event()], [], tmp_path / "missing" / "reel.mp4")


@pytest.mark.parametrize(
    "events",
    [
        [],
        [event(end=0)],
        [event(start=-1)],
        [event(end=4)],
        [event(), event()],
        [event(start=float("inf"))],
    ],
)
def test_invalid_event_ranges(source, tmp_path, events):
    from once_sport_mcp.rendering import render_reel

    with pytest.raises(RenderError):
        render_reel(source, events, [], tmp_path / "invalid.mp4")


def test_collision_and_cancellation_cleanup(source, tmp_path, monkeypatch):
    import tempfile

    import once_sport_mcp.rendering as renderer

    work = tmp_path / "scratch"
    work.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(work))
    output = tmp_path / "cancelled.mp4"
    painted = []
    original = renderer._paint

    def paint(*args):
        original(*args)
        painted.append(True)

    monkeypatch.setattr(renderer, "_paint", paint)
    with pytest.raises(RenderError, match="cancelled"):
        renderer.render_reel(
            source, [event()], [], output, cancelled=lambda: bool(painted)
        )
    assert not output.exists()
    assert not output.with_suffix(".drawings.json").exists()
    assert list(work.iterdir()) == []
    assert not list(tmp_path.glob(".once-*"))
    output.write_bytes(b"keep existing")
    with pytest.raises(RenderError, match="already exists"):
        renderer.render_reel(source, [event()], [], output)
    assert output.read_bytes() == b"keep existing"
    output.unlink()
    output.with_suffix(".drawings.json").write_text("keep sidecar")
    with pytest.raises(RenderError, match="already exists"):
        renderer.render_reel(source, [event()], [], output)
    assert not output.exists()


def test_encoder_failure_removes_frames_and_never_publishes(
    source, tmp_path, monkeypatch
):
    import tempfile

    import once_sport_mcp.rendering as renderer

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    original = renderer._run

    def fail_encode(args, cancelled=None):
        if "libx264" in args:
            raise RenderError("injected encode failure")
        return original(args, cancelled)

    monkeypatch.setattr(renderer, "_run", fail_encode)
    output = tmp_path / "failure.mp4"
    with pytest.raises(RenderError, match="injected encode failure"):
        renderer.render_reel(source, [event()], [], output)
    assert list(scratch.iterdir()) == []
    assert not output.exists()
    assert not output.with_suffix(".drawings.json").exists()


def test_cancellation_terminates_running_child(monkeypatch):
    import once_sport_mcp.rendering as renderer

    children = []
    original = subprocess.Popen

    def launch(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", launch)
    checks = []

    def cancelled():
        checks.append(True)
        return len(checks) >= 3

    with pytest.raises(RenderError, match="cancelled"):
        renderer._run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-re",
                "-f",
                "lavfi",
                "-i",
                "color=s=32x32:r=8:d=10",
                "-f",
                "null",
                "-",
            ],
            cancelled,
        )
    assert len(children) == 1
    assert children[0].poll() is not None


@pytest.mark.parametrize(
    "kind,points,box",
    [
        ("line", [[0.2, 0.2], [0.8, 0.2]], (60, 28, 260, 45)),
        ("arrow", [[0.2, 0.3], [0.8, 0.3]], (60, 40, 270, 65)),
        ("polygon", [[0.2, 0.2], [0.8, 0.2], [0.5, 0.5]], (60, 28, 260, 95)),
    ],
)
def test_shape_pixels_and_unfilled_regions(source, tmp_path, kind, points, box):
    from once_sport_mcp.rendering import render_reel

    output = tmp_path / "shapes.mp4"
    render_reel(source, [event()], [drawing(kind=kind, points=points)], output)
    frames = pixels(output)
    assert bright_count(frames[0], box) > 150
    assert bright_count(frames[6], box) == 0
    if kind == "polygon":
        assert bright_count(frames[0], (145, 45, 175, 60)) == 0


def test_validation_rejects_wrong_encoded_frame_count(source, tmp_path, monkeypatch):
    import shutil

    import once_sport_mcp.rendering as renderer

    original = renderer._run

    def wrong_encode(args, cancelled=None):
        if "libx264" in args:
            shutil.copyfile(source, args[-1])  # Valid video, but the wrong clip length.
            return b""
        return original(args, cancelled)

    monkeypatch.setattr(renderer, "_run", wrong_encode)
    output = tmp_path / "wrong.mp4"
    with pytest.raises(RenderError, match="frame count"):
        renderer.render_reel(source, [event()], [], output)
    assert not output.exists()
    assert not output.with_suffix(".drawings.json").exists()


def test_long_edge_label_keeps_text_inside_padding(source, tmp_path):
    from once_sport_mcp.rendering import render_reel

    output = tmp_path / "long.mp4"
    render_reel(
        source,
        [event()],
        [drawing(kind="label", points=[[1, 1]], text="W" * 120, color="#FFFFFF")],
        output,
        width=160,
    )
    frame = pixels(output, 160, 90)[0]
    assert (
        sum(min(p) > 180 for p in frame.crop((0, 86, 160, 90)).get_flattened_data())
        == 0
    )


def test_anamorphic_source_keeps_display_aspect_and_native_height(tmp_path):
    from once_sport_mcp.rendering import render_reel

    source = tmp_path / "anamorphic.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=0x183828:s=320x180:r=8:d=1",
        "-vf",
        "setsar=2",
        "-c:v",
        "libx264",
        source,
    )
    output = tmp_path / "square-pixel.mp4"
    result = render_reel(source, [event()], [], output)
    assert (result["output"]["width"], result["output"]["height"]) == (640, 180)
    assert result["output"]["sample_aspect_ratio"] == 1


def test_positive_subframe_range_at_media_end(source, tmp_path):
    from once_sport_mcp.rendering import render_reel

    result = render_reel(source, [event(start=2.99, end=3)], [], tmp_path / "tail.mp4")
    assert result["output"]["frame_count"] == 1


def test_circle_stays_round_at_export_resolution(source, tmp_path):
    from once_sport_mcp.rendering import render_reel

    output = tmp_path / "circle.mp4"
    render_reel(
        source,
        [event()],
        [drawing(points=[[0.5, 0.3], [0.075, 0.075 * 320 / 180]])],
        output,
        width=480,
    )
    frame = pixels(output, 480, 270)[0]
    points = [
        (x, y)
        for y in range(20, 140)
        for x in range(150, 330)
        for r, g, b in [frame.getpixel((x, y))]
        if r > 160 and g > 100 and b < 170
    ]
    extent_x = max(x for x, _ in points) - min(x for x, _ in points)
    extent_y = max(y for _, y in points) - min(y for _, y in points)
    assert extent_x == pytest.approx(extent_y, abs=2)


def test_absolute_nonzero_overlay_interval(source, tmp_path):
    from once_sport_mcp.rendering import render_reel

    output = tmp_path / "absolute.mp4"
    render_reel(
        source,
        [event(start=1, end=2)],
        [drawing(start_seconds=1.25, end_seconds=1.5)],
        output,
    )
    frames = pixels(output)
    box = (45, 20, 115, 90)
    assert bright_count(frames[0], box) == 0
    assert bright_count(frames[2], box) > 80
    assert bright_count(frames[4], box) == 0


def test_bottom_label_stays_above_clip_title(source, tmp_path):
    from once_sport_mcp.rendering import render_reel

    output = tmp_path / "bottom.mp4"
    render_reel(
        source,
        [event()],
        [drawing(kind="label", points=[[0.1, 1]], text="BOTTOM", color="#FF55FF")],
        output,
    )
    frame = pixels(output)[0]
    ys = [
        y
        for y in range(180)
        for x in range(320)
        if (c := frame.getpixel((x, y)))[0] > 150 and c[2] > 150 and c[1] < 140
    ]
    assert ys
    assert max(ys) < 144


def test_public_plan_validation_prevents_approving_unrenderable_geometry(source):
    import once_sport_mcp.rendering as renderer

    assert hasattr(renderer, "validate_plan"), (
        "Workflow needs one shared pre-approval validator"
    )
    info = renderer.probe_media(source)
    assert renderer.validate_plan([event()], [drawing()], info) is None
    with pytest.raises(RenderError, match="Unknown drawing"):
        renderer.validate_plan([event()], [drawing(kind="rectangle")], info)


def test_committed_render_is_success_with_cleanup_warning(
    source, tmp_path, monkeypatch
):
    from pathlib import Path

    from once_sport_mcp.rendering import render_reel

    original = Path.unlink

    def unavailable_cleanup(path, *args, **kwargs):
        if path.parent == tmp_path and path.name.startswith(".once-"):
            raise PermissionError("temporary cleanup denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unavailable_cleanup)
    destination = tmp_path / "complete.mp4"
    result = render_reel(source, [event()], [], destination)
    assert destination.is_file()
    assert result["warnings"], (
        "A committed file must not be reported failed for cleanup only"
    )
    assert probe_media(destination)["duration"] == pytest.approx(1)


def test_failed_pair_publication_preserves_another_writers_sidecar(
    source, tmp_path, monkeypatch
):
    import os

    from once_sport_mcp.rendering import render_reel

    destination = tmp_path / "collision.mp4"
    sidecar = destination.with_suffix(".drawings.json")
    original = os.link

    def competing_write(staged, target, *args, **kwargs):
        if target == destination:
            sidecar.unlink()
            sidecar.write_text("independently replaced sidecar")
            raise FileExistsError("competing final publication")
        return original(staged, target, *args, **kwargs)

    monkeypatch.setattr(os, "link", competing_write)
    with pytest.raises(RenderError):
        render_reel(source, [event()], [], destination)
    assert not destination.exists()
    assert sidecar.read_text() == "independently replaced sidecar"
