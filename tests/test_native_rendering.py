"""Bounded native XML projection checks; no Once application is launched."""

import copy
import hashlib
import json
import re
import shutil
import subprocess
import uuid
import xml.etree.ElementTree as ET

import pytest

from once_sport_mcp.native_rendering import native_analysis_xml, validate_native_plan
from once_sport_mcp.rendering import RenderError

MEDIA = {
    "duration": 10,
    "width": 1280,
    "height": 720,
    "fps": 25,
    "has_audio": True,
    "sample_aspect_ratio": 1,
}


def event(event_id="event-1", start=0, end=2, **changes):
    value = {
        "event_id": event_id,
        "start_seconds": start,
        "end_seconds": end,
        "team": "Home",
        "phase": "attacking",
        "action": "Pass",
    }
    value.update(changes)
    return value


def mark(kind="arrow", **changes):
    value = {
        "id": "mark-1",
        "event_id": "event-1",
        "kind": kind,
        "start_seconds": 0.125,
        "end_seconds": 0.875,
        "points": [[0.1, 0.2], [0.8, 0.9]],
        "color": "#1234AB",
    }
    if kind == "ellipse":
        value["points"] = [[0.4, 0.5], [0.1, 0.2]]
    value.update(changes)
    return value


def test_xml_projects_native_subset_without_mutating_canonical_inputs(tmp_path):
    source = tmp_path / "match <one> & source.mp4"
    events = [
        event(
            "event <α>&",
            0,
            2,
            team="北<&",
            phase="attacking",
            action="進む & pass",
            notes="review <&> ✓",
            players=[],
        ),
        event("defend", 2, 4, phase="defending", action="Tackle"),
    ]
    drawings = [
        mark(
            "ellipse",
            id="tracked circle",
            event_id="event <α>&",
            start_seconds=0.25,
            end_seconds=1.25,
            keyframes=[
                {"time_seconds": 0.5, "points": [[0.3, 0.4], [0.1, 0.2]]},
                {"time_seconds": 1, "points": [[0.6, 0.7], [0.1, 0.2]]},
            ],
        ),
        mark(
            event_id="defend",
            start_seconds=2.25,
            end_seconds=2.75,
            points=[[0.2, 0.3], [0.7, 0.8]],
        ),
    ]
    original = copy.deepcopy((events, drawings))

    validate_native_plan(events, drawings, MEDIA)
    xml = native_analysis_xml(source, events, drawings, MEDIA)
    root = ET.fromstring(xml)

    assert root.tag == "analysis"
    assert root.attrib["dbVersion"] == "5.2.0"
    assert root.attrib["title"] == "MCP reviewed analysis"
    assert root.attrib["type"] == "0"
    assert root.attrib["videoFilepath"] == str(source.resolve())
    assert re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", root.attrib["creationDate"]
    )
    uuid.UUID(root.attrib["id"])
    uuid.UUID(root.attrib["actionConfigurationId"])
    assert root.find("info") is not None
    assert root.find("info").attrib == {}
    assert root.find("playlists") is not None
    assert root.find("playlists").attrib == {}

    actions = root.find("actions")
    assert actions is not None
    action_nodes = actions.findall("action")
    assert [node.attrib["actionType"] for node in action_nodes] == [
        "Attacking",
        "Defending",
    ]
    assert [json.loads(node.attrib["actionName"]) for node in action_nodes] == [
        ["北<&", "attacking", "進む & pass"],
        ["Home", "defending", "Tackle"],
    ]
    assert [node.attrib["remark"] for node in action_nodes] == ["review <&> ✓", ""]
    assert all(
        node.attrib["videoFilepath"] == str(source.resolve()) for node in action_nodes
    )
    assert all(
        node.attrib["fieldMapper"] == "Football ;;fm;; 105 ;;fm;; 68 ;;fm;; []"
        for node in action_nodes
    )
    assert all(
        node.attrib[key] == "False"
        for node in action_nodes
        for key in ("isBad", "isFavorite", "isGood")
    )
    for node in action_nodes:
        uuid.UUID(node.attrib["id"])
        assert node.find("players") is not None
        assert node.find("players").attrib == {}
        assert node.find("drawingelements") is not None

    ellipse = action_nodes[0].find("drawingelements").find("drawingelement")
    arrow = action_nodes[1].find("drawingelements").find("drawingelement")
    assert ellipse.attrib["type"] == "PlayerCircle2"
    assert ellipse.attrib["drawingTimeStart"] == "00:00:00.25"
    assert ellipse.attrib["drawingTimeEnd"] == "00:00:01.25"
    assert ellipse.attrib["drawingTimeStart_seconds"] == "0.25"
    assert ellipse.attrib["drawingTimeEnd_seconds"] == "1.25"
    assert ellipse.attrib["isChromaKeyOn"] == "False"
    assert ellipse.attrib["Position"] == "0.4 0.5"
    assert ellipse.attrib["Size"] == "0.2 0.4"
    assert ellipse.attrib["InnerColor"] == "255 18 52 171"
    assert ellipse.attrib["OuterColor"] == "255 255 255 255"
    assert ellipse.attrib["DotColor"] == "255 255 255 255"
    assert ellipse.attrib["TrailColor"] == "0 255 255 255"
    assert ellipse.attrib["HasTrailColor"] == "False"
    assert ellipse.attrib["IsTrackingStatic"] == "False"
    animations = ellipse.find("animations")
    assert animations.attrib == {"count": "1", "isTrackingStatic": "False"}
    keyframes = animations.find("animation").find("keyframes")
    assert keyframes.attrib == {"count": "2"}
    assert [key.attrib for key in keyframes.findall("keyframe")] == [
        {"time": "0.5", "position": "0.3 0.4"},
        {"time": "1", "position": "0.6 0.7"},
    ]

    assert arrow.attrib["type"] == "PlayerArrow"
    assert arrow.attrib["StartPoint"] == "0.2 0.3"
    assert arrow.attrib["EndPoint"] == "0.7 0.8"
    assert arrow.attrib["Color"] == "255 18 52 171"
    assert arrow.attrib["ShineColor"] == "255 255 255 255"
    assert arrow.attrib["IsDashed"] == "False"
    assert arrow.attrib["drawingTimeStart"] == "00:00:02.25"
    assert arrow.attrib["drawingTimeEnd"] == "00:00:02.75"
    assert arrow.find("camera").attrib == {
        "distance": "50",
        "fov": "50",
        "height": "20",
        "tiltAngle": "0",
    }
    assert "match &lt;one&gt; &amp; source.mp4" in xml
    assert (events, drawings) == original


def test_native_schema_uses_analysisrw_drawingelement_tag(tmp_path):
    root = ET.fromstring(
        native_analysis_xml(tmp_path / "source.mp4", [event()], [mark()], MEDIA)
    )
    drawing_elements = root.find("actions/action/drawingelements")

    assert drawing_elements is not None
    assert [node.tag for node in drawing_elements] == ["drawingelement"]
    assert drawing_elements.findall("drawingelement")[0].attrib["type"] == "PlayerArrow"


def test_native_actions_are_chronological_without_mutating_events(tmp_path):
    events = [event("late", 2, 4), event("early", 0, 1)]
    original = copy.deepcopy(events)
    root = ET.fromstring(
        native_analysis_xml(tmp_path / "source.mp4", events, [], MEDIA)
    )

    assert [node.attrib["startTime"] for node in root.findall("actions/action")] == [
        "00:00:00",
        "00:00:02",
    ]
    assert events == original


def test_native_reel_sandboxes_exports_validates_and_publishes_four_files(
    tmp_path, monkeypatch
):
    from once_sport_mcp import native_rendering as native
    from once_sport_mcp.native_rendering import render_native_reel
    from once_sport_mcp.rendering import probe_media

    source = tmp_path / "source.mp4"
    real_ffmpeg = shutil.which("ffmpeg")
    real_ffprobe = shutil.which("ffprobe")
    subprocess.run(
        [
            real_ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x183828:s=320x180:r=8:d=1.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1.5",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    original_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    appdir = tmp_path / "Once"
    appdir.mkdir()
    executable = appdir / "OnceSport"
    executable.write_bytes(b"test apphost")
    executable.chmod(0o755)
    dll = appdir / "OnceSport.dll"
    dll.write_bytes(b"test dll")
    monkeypatch.setattr(
        native,
        "APPHOST_SHA256",
        hashlib.sha256(executable.read_bytes()).hexdigest(),
        raising=False,
    )
    monkeypatch.setattr(
        native,
        "DLL_SHA256",
        hashlib.sha256(dll.read_bytes()).hexdigest(),
        raising=False,
    )
    real_tools = {"ffmpeg": real_ffmpeg, "ffprobe": real_ffprobe}
    monkeypatch.setattr(
        native.shutil,
        "which",
        lambda name: "/fake/bin/bwrap" if name == "bwrap" else real_tools[name],
    )
    invocations = []

    def fake_native(args, cancelled=None, timeout=300):
        invocations.append(args)
        command = args[args.index("--") + 1 :]
        assert command[0] == str(executable.resolve())
        assert command[1] == "--export-analysis"
        assert Path(command[2]).name == "input.xml"
        shutil.copyfile(source, command[3])
        return "Warning: chroma key skipped", ""

    from pathlib import Path

    monkeypatch.setattr(native, "_run_native_process", fake_native)
    destination = tmp_path / "reel.mp4"
    events = [event("late", 0.75, 1.25), event("early", 0, 0.75)]
    drawings = [mark(event_id="early", end_seconds=0.7)]
    result = render_native_reel(
        source, events, drawings, destination, once_executable=executable
    )

    assert len(invocations) == 1
    command = invocations[0]
    assert ["--ro-bind", "/", "/"] == command[1:4]
    assert "--unshare-net" in command
    assert "--unshare-ipc" in command
    assert "--unshare-pid" in command
    assert "--die-with-parent" in command
    assert "--tmpfs" in command
    assert "--unsetenv" in command
    assert "DISPLAY" in command
    assert "WAYLAND_DISPLAY" in command
    assert "DBUS_SESSION_BUS_ADDRESS" in command
    assert source.resolve().as_posix() in command
    assert appdir.resolve().as_posix() in command
    assert result["semantics"] == "native-once-headless"
    assert [clip["event_id"] for clip in result["clips"]] == ["early", "late"]
    assert all("reel_start_seconds" not in clip for clip in result["clips"])
    assert result["native_warnings"] == ["Warning: chroma key skipped"]
    assert result["output"]["has_audio"] is True
    assert result["output"]["width"] == 320
    assert result["output"]["height"] == 180
    assert result["output"]["frame_count"] > 0
    assert result["output"]["video_codec"] == "h264"
    assert result["output"]["pixel_format"] == "yuv420p"
    assert result["output"]["audio_codec"] == "aac"
    assert probe_media(destination)["has_audio"] is True
    assert destination.with_suffix(".native.mp4").is_file()
    assert destination.with_suffix(".once.xml").is_file()
    sidecar_path = destination.with_suffix(".drawings.json")
    sidecar = json.loads(sidecar_path.read_text())
    assert sidecar["drawings"] == drawings
    assert sidecar["manifest"] == result
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_digest


def test_native_plan_rejects_only_unsupported_or_unrepresentable_cases():
    valid = mark("ellipse")
    keyed = mark(
        "ellipse",
        keyframes=[{"time_seconds": 0.25, "points": [[0.2, 0.3], [0.1, 0.2]]}],
    )
    assert validate_native_plan([event()], [valid], MEDIA) is None
    assert validate_native_plan([event()], [keyed], MEDIA) is None

    unsupported = [
        ([event()], [mark(kind="line")], MEDIA),
        ([event()], [mark(text="label")], MEDIA),
        (
            [event()],
            [
                mark(
                    keyframes=[
                        {"time_seconds": 0.5, "points": [[0.2, 0.3], [0.8, 0.9]]}
                    ]
                )
            ],
            MEDIA,
        ),
        (
            [event()],
            [
                mark(
                    "ellipse",
                    keyframes=[
                        {"time_seconds": 0.5, "points": [[0.2, 0.3], [0.15, 0.2]]}
                    ],
                )
            ],
            MEDIA,
        ),
        ([event(players=["Player One"])], [], MEDIA),
        ([event()], [], {**MEDIA, "sample_aspect_ratio": 4 / 3}),
    ]
    for events, drawings, media in unsupported:
        with pytest.raises(RenderError):
            validate_native_plan(events, drawings, media)
        with pytest.raises(RenderError):
            native_analysis_xml("/fixed/source.mp4", events, drawings, media)


def test_native_validation_delegates_to_canonical_validator(monkeypatch):
    from once_sport_mcp import rendering

    calls = []

    def canonical(events, drawings, media):
        calls.append((events, drawings, media))

    monkeypatch.setattr(rendering, "validate_plan", canonical)
    events, drawings = [event()], [mark()]
    validate_native_plan(events, drawings, MEDIA)
    assert calls == [(events, drawings, MEDIA)]
