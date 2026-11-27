"""Version-pinned, headless Once native-analysis rendering support."""

import hashlib
import json
import math
import os
import shutil
import stat
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

from . import rendering

APPHOST_SHA256 = "67861e6a601fa985f833ddb783c03b4e0d6b880280296b496560b923d8e13d1a"
DLL_SHA256 = "53325656aae56692e439bf6acee291f8ced0e9bf27329ac0c9240860492110da"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _regular_file(path: Path, *, executable: bool = False) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise rendering.RenderError(
            f"Required native file is unavailable: {path}"
        ) from exc
    if not stat.S_ISREG(mode) or path.is_symlink():
        raise rendering.RenderError(
            f"Native path must be a regular non-symlink file: {path}"
        )
    if executable and not os.access(path, os.X_OK):
        raise rendering.RenderError("OnceSport apphost is not executable")


def _run_native_process(args, cancelled=None, timeout=300):
    """Run only the owned bubblewrap process and reap it on every exit path."""
    deadline = time.monotonic() + timeout
    try:
        with subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as child:
            try:
                while True:
                    rendering._check(cancelled)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise rendering.RenderError("Native Once export timed out")
                    try:
                        stdout, stderr = child.communicate(timeout=min(0.1, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if child.returncode:
                    detail = (stderr or stdout).decode(errors="replace")[-3000:]
                    raise rendering.RenderError(
                        f"Native Once export failed: {detail or child.returncode}"
                    )
                return stdout.decode(errors="replace"), stderr.decode(errors="replace")
            finally:
                if child.poll() is None:
                    try:
                        child.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        child.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.communicate()
    except rendering.RenderError:
        raise
    except OSError as exc:
        raise rendering.RenderError(f"Native process failed: {exc}") from exc


def _inspect_output(path: Path, cancelled=None) -> dict:
    basic = rendering._probe(path, cancelled)
    data = json.loads(
        rendering._run(
            [
                "ffprobe",
                "-v",
                "error",
                "-count_frames",
                "-show_entries",
                "stream=codec_type,codec_name,pix_fmt,nb_read_frames",
                "-of",
                "json",
                str(path),
            ],
            cancelled,
        )
    )
    video = next(
        stream for stream in data["streams"] if stream["codec_type"] == "video"
    )
    audio = [stream for stream in data["streams"] if stream["codec_type"] == "audio"]
    frame_count = int(video["nb_read_frames"])
    if frame_count < 1:
        raise rendering.RenderError("Media contains no decoded video frames")
    return {
        **basic,
        "frame_count": frame_count,
        "video_codec": video["codec_name"],
        "pixel_format": video.get("pix_fmt"),
        "audio_codec": audio[0]["codec_name"] if audio else None,
    }


def _decode_output(path: Path, cancelled=None) -> None:
    rendering._run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-f", "null", "-"],
        cancelled,
    )


def _check_native_media(source: dict, output: dict, stage: str) -> None:
    if (output["width"], output["height"]) != (source["width"], source["height"]):
        raise rendering.RenderError(f"Native {stage} geometry differs from the source")
    if output["sample_aspect_ratio"] != 1:
        raise rendering.RenderError(f"Native {stage} has nonsquare pixels")
    if output["has_audio"] != source["has_audio"]:
        raise rendering.RenderError(f"Native {stage} unexpectedly lost or added audio")


def _publish_native_files(items: list[tuple[Path, Path]], cancelled=None) -> None:
    published = []
    try:
        for staged, target in items:
            rendering._check(cancelled)
            owned = staged.lstat()
            if not stat.S_ISREG(owned.st_mode) or staged.is_symlink():
                raise rendering.RenderError("Native output staging file is not regular")
            os.link(staged, target, follow_symlinks=False)
            published.append((target, owned.st_dev, owned.st_ino))
    except BaseException:
        for target, device, inode in published:
            try:
                current = target.lstat()
                if (current.st_dev, current.st_ino) == (device, inode):
                    target.unlink()
            except OSError:
                pass
        raise


def render_native_reel(
    source: Path,
    events: list[dict],
    drawings: list[dict],
    destination: Path,
    *,
    once_executable: Path,
    cancelled=None,
) -> dict:
    """Render approved arrows/ellipses through the pinned headless Once CLI."""
    try:
        source_path = Path(source).resolve(strict=True)
        destination_path = Path(destination).absolute()
        parent = destination_path.parent
        if not parent.is_dir():
            raise rendering.RenderError("Destination parent must already exist")
        native_target = destination_path.with_suffix(".native.mp4")
        xml_target = destination_path.with_suffix(".once.xml")
        sidecar_target = destination_path.with_suffix(".drawings.json")
        targets = [destination_path, native_target, xml_target, sidecar_target]
        if any(os.path.lexists(path) for path in targets):
            raise rendering.RenderError("Native output family already exists")

        executable_input = Path(once_executable).absolute()
        _regular_file(executable_input, executable=True)
        executable = executable_input.resolve(strict=True)
        appdir = executable.parent.resolve(strict=True)
        dll = appdir / "OnceSport.dll"
        _regular_file(dll)
        if _file_sha256(executable) != APPHOST_SHA256:
            raise rendering.RenderError("Unsupported OnceSport apphost build")
        if _file_sha256(dll) != DLL_SHA256:
            raise rendering.RenderError("Unsupported OnceSport.dll build")

        bwrap = shutil.which("bwrap")
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if not bwrap or not ffmpeg or not ffprobe:
            raise rendering.RenderError(
                "Native rendering requires bwrap, ffmpeg, and ffprobe"
            )

        source_info = rendering._probe(source_path, cancelled)
        validate_native_plan(events, drawings, source_info)
        xml_text = native_analysis_xml(source_path, events, drawings, source_info)
        home = str(Path.home().resolve())
        ordered_events = sorted(events, key=lambda event: event["start_seconds"])
        settings = {
            "framerate": 25,
            "bitrate": "8M",
            "isUploadToCloud": False,
            "drawingPause": 3,
            "includeDrawings": True,
            "copyAudio": True,
        }

        with tempfile.TemporaryDirectory(prefix="once-native-", dir=parent) as folder:
            work = Path(folder)
            temporary = work / "tmp"
            temporary.mkdir()
            if (
                work == appdir
                or work.is_relative_to(appdir)
                or appdir.is_relative_to(work)
            ):
                raise rendering.RenderError("Native work and app directories overlap")
            input_xml = work / "input.xml"
            native_output = work / "native.mp4"
            final_output = work / "final.mp4"
            input_xml.write_text(xml_text, encoding="utf-8")
            args = [
                bwrap,
                "--ro-bind",
                "/",
                "/",
                "--unshare-pid",
                "--unshare-net",
                "--unshare-ipc",
                "--die-with-parent",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                home,
                "--ro-bind",
                str(appdir),
                str(appdir),
                "--ro-bind",
                str(source_path),
                str(source_path),
                "--bind",
                str(work),
                str(work),
                "--bind",
                str(temporary),
                "/tmp",
                "--setenv",
                "HOME",
                home,
                "--setenv",
                "TMPDIR",
                "/tmp",
                "--setenv",
                "XDG_CACHE_HOME",
                f"{home}/.cache",
                "--unsetenv",
                "DISPLAY",
                "--unsetenv",
                "WAYLAND_DISPLAY",
                "--unsetenv",
                "DBUS_SESSION_BUS_ADDRESS",
                "--",
                str(executable),
                "--export-analysis",
                str(input_xml),
                str(native_output),
            ]
            stdout, stderr = _run_native_process(args, cancelled=cancelled, timeout=300)
            warnings = [
                line
                for line in f"{stdout}\n{stderr}".splitlines()
                if "warning" in line.casefold()
                or "chroma key skipped" in line.casefold()
            ]
            if (
                not native_output.is_file()
                or native_output.is_symlink()
                or native_output.stat().st_size == 0
            ):
                raise rendering.RenderError(
                    "Once CLI did not produce a valid native MP4"
                )
            native_info = _inspect_output(native_output, cancelled)
            _decode_output(native_output, cancelled)
            _check_native_media(source_info, native_info, "original")

            encode_args = [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-i",
                str(native_output),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                str(final_output),
            ]
            rendering._run(encode_args, cancelled)
            final_info = _inspect_output(final_output, cancelled)
            _decode_output(final_output, cancelled)
            _check_native_media(source_info, final_info, "final output")
            if (
                final_info["video_codec"] != "h264"
                or final_info["pixel_format"] != "yuv420p"
                or (source_info["has_audio"] and final_info["audio_codec"] != "aac")
            ):
                raise rendering.RenderError("Final native reel codec is incompatible")

            clips = [
                {
                    "event_id": event["event_id"],
                    "source_start_seconds": event["start_seconds"],
                    "source_end_seconds": event["end_seconds"],
                }
                for event in ordered_events
            ]
            manifest = {
                "semantics": "native-once-headless",
                "source": str(source_path),
                "destination": str(destination_path),
                "drawings_sidecar": str(sidecar_target),
                "native_original": str(native_target),
                "native_analysis": str(xml_target),
                "clips": clips,
                "output": final_info,
                "requested_native_settings": settings,
                "native_warnings": warnings,
            }
            sidecar_work = work / "drawings.json"
            sidecar_work.write_text(
                json.dumps(
                    {
                        "semantics": "native-once-headless",
                        "drawings": drawings,
                        "manifest": manifest,
                    },
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                ),
                encoding="utf-8",
            )
            _publish_native_files(
                [
                    (final_output, destination_path),
                    (native_output, native_target),
                    (input_xml, xml_target),
                    (sidecar_work, sidecar_target),
                ],
                cancelled,
            )
            return manifest
    except rendering.RenderError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise rendering.RenderError(f"Native rendering failed: {exc}") from exc


_UUID_NAMESPACE = uuid.UUID("49ef4250-0e93-4fa0-833a-fba69680915b")


def _native_id(kind: str, identifier: str) -> str:
    return str(uuid.uuid5(_UUID_NAMESPACE, f"once-sport-mcp:{kind}:{identifier}"))


def _decimal(value: float) -> str:
    text = format(Decimal(str(value)), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _timespan(seconds: float) -> str:
    ticks = int(
        (Decimal(str(seconds)) * Decimal(10_000_000)).quantize(
            Decimal(1), rounding=ROUND_HALF_EVEN
        )
    )
    ticks_per_second = 10_000_000
    ticks_per_minute = 60 * ticks_per_second
    ticks_per_hour = 60 * ticks_per_minute
    ticks_per_day = 24 * ticks_per_hour
    days, ticks = divmod(ticks, ticks_per_day)
    hours, ticks = divmod(ticks, ticks_per_hour)
    minutes, ticks = divmod(ticks, ticks_per_minute)
    whole_seconds, fraction = divmod(ticks, ticks_per_second)
    prefix = f"{days}." if days else ""
    result = f"{prefix}{hours:02}:{minutes:02}:{whole_seconds:02}"
    if fraction:
        result += f".{fraction:07d}".rstrip("0")
    return result


def _position(pair: list[float]) -> str:
    return " ".join(format(value, ".15g") for value in pair)


def _safe_xml_text(value: str) -> bool:
    return all(
        character in "\t\n\r"
        or "\x20" <= character <= "\ud7ff"
        or "\ue000" <= character <= "\ufffd"
        or "\U00010000" <= character <= "\U0010ffff"
        for character in value
    )


def validate_native_plan(events: list[dict], drawings: list[dict], media: dict) -> None:
    """Validate the canonical plan and fail closed outside Once's native subset."""
    if isinstance(drawings, list) and any(
        isinstance(mark, dict) and "hold_seconds" in mark for mark in drawings
    ):
        raise rendering.RenderError("Native Once export does not support holds")
    rendering.validate_plan(events, drawings, media)
    sar = media.get("sample_aspect_ratio", 1)
    if (
        isinstance(sar, bool)
        or not isinstance(sar, (int, float))
        or not math.isfinite(sar)
        or sar != 1
    ):
        raise rendering.RenderError(
            "Native Once export requires square sample aspect ratio"
        )

    for event in events:
        if "flag" in event:
            raise rendering.RenderError(
                "Native Once export does not support event flags"
            )
        if event.get("players"):
            raise rendering.RenderError(
                "Native Once export does not support event players"
            )
        # Playlist metadata is ignored here and retained in the canonical proposal sidecar.
        notes = event.get("notes", "")
        if not isinstance(notes, str) or not _safe_xml_text(notes):
            raise rendering.RenderError("Native action notes are not valid XML text")
        for key in ("team", "phase", "action"):
            if not _safe_xml_text(event[key]):
                raise rendering.RenderError(
                    "Native action metadata is not valid XML text"
                )

    for mark in drawings:
        if mark["kind"] not in ("arrow", "ellipse"):
            raise rendering.RenderError(
                "Native Once export supports arrows and ellipses only"
            )
        if mark.get("stabilize") or mark.get("ground") or mark.get("taper"):
            raise rendering.RenderError(
                "Native Once export does not support stabilize or ground masking"
            )
        if "dash" in mark and mark.get("dash"):
            raise rendering.RenderError(
                "Native Once export does not support dashed arrows"
            )
        if any(
            key in mark for key in ("fade_in_seconds", "fade_out_seconds", "easing")
        ):
            raise rendering.RenderError(
                "Native Once export does not support fades or keyframe easing"
            )
        if "text" in mark:
            raise rendering.RenderError(
                "Native Once export does not support text annotations"
            )
        keyframes = mark.get("keyframes", [])
        if mark["kind"] == "arrow" and keyframes:
            raise rendering.RenderError(
                "Native Once export does not support arrow keyframes"
            )
        if mark["kind"] == "ellipse" and any(
            keyframe["points"][1] != mark["points"][1] for keyframe in keyframes
        ):
            raise rendering.RenderError(
                "Native tracked ellipses require unchanged radii"
            )


def native_analysis_xml(
    source: str | Path,
    events: list[dict],
    drawings: list[dict],
    media: dict,
) -> str:
    """Project approved events/drawings to a fresh native XML interchange file."""
    validate_native_plan(events, drawings, media)
    source_path = str(Path(source).resolve())
    if not _safe_xml_text(source_path):
        raise rendering.RenderError("Source path is not valid XML text")

    created = datetime.now(UTC)
    root = ET.Element(
        "analysis",
        {
            "dbVersion": "5.2.0",
            "id": str(uuid.uuid4()),
            "title": "MCP reviewed analysis",
            "type": "0",
            "creationDate": (
                created.strftime("%Y-%m-%dT%H:%M:%S.")
                + f"{created.microsecond // 1000:03d}Z"
            ),
            "videoFilepath": source_path,
            "actionConfigurationId": str(uuid.uuid4()),
        },
    )
    ET.SubElement(root, "info")
    actions = ET.SubElement(root, "actions")
    ET.SubElement(root, "playlists")

    ordered_events = sorted(events, key=lambda event: event["start_seconds"])
    drawings_by_owner: dict[str, list[dict]] = {
        event["event_id"]: [] for event in ordered_events
    }
    for mark in drawings:
        drawings_by_owner[mark["event_id"]].append(mark)

    for event in ordered_events:
        phase = event["phase"].casefold()
        action_type = {"attacking": "Attacking", "defending": "Defending"}.get(
            phase, "Other"
        )
        action = ET.SubElement(
            actions,
            "action",
            {
                "id": _native_id("event", event["event_id"]),
                "actionName": json.dumps(
                    [event["team"], event["phase"], event["action"]],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "actionType": action_type,
                "isBad": "False",
                "isFavorite": "False",
                "isGood": "False",
                "remark": event.get("notes", ""),
                "videoFilepath": source_path,
                "startTime": _timespan(event["start_seconds"]),
                "endTime": _timespan(event["end_seconds"]),
                "fieldMapper": "Football ;;fm;; 105 ;;fm;; 68 ;;fm;; []",
            },
        )
        ET.SubElement(action, "players")
        drawing_elements = ET.SubElement(action, "drawingelements")
        for mark in drawings_by_owner[event["event_id"]]:
            color = mark.get("color", "#FFD166")
            rgb = [int(color[index : index + 2], 16) for index in (1, 3, 5)]
            common = {
                "id": _native_id("drawing", mark["id"]),
                "drawingTimeStart": _timespan(mark["start_seconds"]),
                "drawingTimeEnd": _timespan(mark["end_seconds"]),
                "drawingTimeStart_seconds": _decimal(mark["start_seconds"]),
                "drawingTimeEnd_seconds": _decimal(mark["end_seconds"]),
                "isChromaKeyOn": "False",
            }
            if mark["kind"] == "arrow":
                drawing = ET.SubElement(
                    drawing_elements,
                    "drawingelement",
                    {
                        **common,
                        "type": "PlayerArrow",
                        "StartPoint": _position(mark["points"][0]),
                        "EndPoint": _position(mark["points"][1]),
                        "Color": "255 " + " ".join(map(str, rgb)),
                        "ShineColor": "255 255 255 255",
                        "IsDashed": "False",
                    },
                )
            else:
                drawing = ET.SubElement(
                    drawing_elements,
                    "drawingelement",
                    {
                        **common,
                        "type": "PlayerCircle2",
                        "Position": _position(mark["points"][0]),
                        "Size": _position([2 * value for value in mark["points"][1]]),
                        "InnerColor": "255 " + " ".join(map(str, rgb)),
                        "OuterColor": "255 255 255 255",
                        "DotColor": "255 255 255 255",
                        "TrailColor": "0 255 255 255",
                        "HasTrailColor": "False",
                        "IsTrackingStatic": "False",
                    },
                )
                keyframes = mark.get("keyframes", [])
                if keyframes:
                    animations = ET.SubElement(
                        drawing,
                        "animations",
                        {"count": "1", "isTrackingStatic": "False"},
                    )
                    animation = ET.SubElement(animations, "animation")
                    native_keyframes = ET.SubElement(
                        animation, "keyframes", {"count": str(len(keyframes))}
                    )
                    for keyframe in keyframes:
                        ET.SubElement(
                            native_keyframes,
                            "keyframe",
                            {
                                "time": _decimal(keyframe["time_seconds"]),
                                "position": _position(keyframe["points"][0]),
                            },
                        )
            ET.SubElement(
                drawing,
                "camera",
                {"distance": "50", "fov": "50", "height": "20", "tiltAngle": "0"},
            )

    return ET.tostring(root, encoding="unicode")
