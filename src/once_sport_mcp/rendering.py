"""Custom raster telestration and H.264 reels. No native Once drawings are made."""

import itertools
import json
import math
import subprocess
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path


class RenderError(Exception):
    """Invalid input, cancellation, or media processing failure."""


def _check(cancelled):
    if cancelled and cancelled():
        raise RenderError("Rendering cancelled")


def _run(args, cancelled=None):
    _check(cancelled)
    try:
        with subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            try:
                while True:
                    _check(cancelled)
                    try:
                        out, err = process.communicate(timeout=0.1)
                        break
                    except subprocess.TimeoutExpired:
                        pass
                if process.returncode:
                    raise RenderError(err.decode(errors="replace")[-3000:])
                return out
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
    except OSError as exc:
        raise RenderError(f"Media process failed: {exc}") from exc


def _probe(source, cancelled=None):
    try:
        data = json.loads(
            _run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(Path(source).resolve(strict=True)),
                ],
                cancelled,
            )
        )
        video = next(s for s in data["streams"] if s["codec_type"] == "video")
        duration = float(video.get("duration", data["format"].get("duration")))
        rate = float(Fraction(video.get("avg_frame_rate", "0/1")))
        if not math.isfinite(duration) or duration <= 0 or rate <= 0:
            raise ValueError("Invalid duration or frame rate")
        sar = video.get("sample_aspect_ratio", "1:1")
        aspect = float(Fraction(sar.replace(":", "/"))) if sar != "N/A" else 1
        return {
            "duration": duration,
            "width": int(video["width"]),
            "height": int(video["height"]),
            "fps": rate,
            "has_audio": any(s["codec_type"] == "audio" for s in data["streams"]),
            "sample_aspect_ratio": aspect or 1,
        }
    except (OSError, ValueError, KeyError, StopIteration, ZeroDivisionError) as exc:
        raise RenderError(f"Cannot probe media: {exc}") from exc


def probe_media(source: Path) -> dict:
    """Read video dimensions, duration, frame rate and audio presence with ffprobe."""
    return _probe(source)


def validate_plan(
    events: list[dict],
    drawings: list[dict],
    media: dict,
    *,
    width: int | None = None,
    fps: float | None = None,
) -> None:
    """Validate the exact renderer schema before review, without encoding or file writes."""
    _validate(events, drawings, media, width, fps)


def _render_reel(
    source: Path,
    events: list[dict],
    drawings: list[dict],
    destination: Path,
    *,
    width: int | None = None,
    fps: float | None = None,
    cancelled=None,
) -> dict:
    """Encode independent chronological clips and publish an editable drawing sidecar."""
    import os
    import shutil
    import tempfile

    info = _probe(source, cancelled)
    validate_plan(events, drawings, info, width=width, fps=fps)
    source = Path(source).resolve()
    drawings, stabilize_reports = _resolve_stabilize(
        source, events, drawings, cancelled
    )
    destination = Path(destination).absolute()
    sidecar = destination.with_suffix(".drawings.json")
    if destination.exists() or sidecar.exists():
        raise RenderError("Output or drawing sidecar already exists")
    rate = fps if fps is not None else info["fps"]
    out_width = (
        width
        if width is not None
        else info["width"] * max(1, info["sample_aspect_ratio"])
    )
    out_width = max(2, round(out_width / 2) * 2)
    out_height = max(
        2,
        round(
            out_width
            * info["height"]
            / (info["width"] * info["sample_aspect_ratio"])
            / 2
        )
        * 2,
    )
    clips = []
    count = 0
    overlay_cache = {"rest": [None, None], "ground": [None, None]}
    with tempfile.TemporaryDirectory(
        prefix="once-render-", dir=destination.parent
    ) as work:
        work = Path(work)
        for index, event in enumerate(sorted(events, key=lambda e: e["start_seconds"])):
            start, end = event["start_seconds"], event["end_seconds"]
            frames = max(1, math.ceil((end - start) * rate - 1e-9))
            hold_plan = {}  # source frame index -> (drawing, repeated frame count)
            for mark in sorted(
                (
                    m
                    for m in drawings
                    if m["event_id"] == event["event_id"] and "hold_seconds" in m
                ),
                key=lambda m: m["start_seconds"],
            ):
                hold_plan[math.ceil((mark["start_seconds"] - start) * rate - 1e-9)] = (
                    mark,
                    max(1, round(mark["hold_seconds"] * rate)),
                )
            hold_reel_starts = {}
            speed_mark = next(
                (
                    mark
                    for mark in drawings
                    if mark["event_id"] == event["event_id"] and mark["kind"] == "speed"
                ),
                None,
            )
            speed_first = speed_last = 0
            speed_repeat = 1
            speed_step = 1
            if speed_mark is not None:
                speed_first = math.ceil(
                    (speed_mark["start_seconds"] - start) * rate - 1e-9
                )
                speed_last = min(
                    frames,
                    math.ceil((speed_mark["end_seconds"] - start) * rate - 1e-9),
                )
                if speed_mark["speed"] < 1:
                    speed_repeat = round(1 / speed_mark["speed"])
                else:
                    speed_step = round(speed_mark["speed"])
            clip_start_count = count
            speed_output_start = speed_output_frames = 0
            # Include the frame spanning the start, including subframe tail clips.
            seek = max(0, start - 1 / info["fps"])
            lead = start - seek
            folder = work / str(index)
            folder.mkdir()
            _run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-ss",
                    str(seek),
                    "-t",
                    str(end - seek),
                    "-i",
                    str(source),
                    "-vf",
                    (
                        f"tpad=stop_mode=clone:stop_duration={lead + 1 / rate},"
                        f"fps={rate}:start_time={lead},setpts=PTS-STARTPTS,"
                        f"scale={out_width}:{out_height},setsar=1"
                    ),
                    "-frames:v",
                    str(frames),
                    "-compression_level",
                    "1",
                    str(folder / "%08d.png"),
                ],
                cancelled,
            )
            paths = sorted(folder.glob("*.png"))
            if len(paths) != frames:
                raise RenderError("Source did not yield the expected frame count")
            for frame_index, path in enumerate(paths):
                _check(cancelled)
                in_speed = (
                    speed_mark is not None and speed_first <= frame_index < speed_last
                )
                if in_speed and (frame_index - speed_first) % speed_step:
                    continue
                _paint_frame(
                    path, event, drawings, start + frame_index / rate, overlay_cache
                )
                if in_speed and frame_index == speed_first:
                    speed_output_start = count - clip_start_count
                painted = work / f"frame-{count:08d}.png"
                path.rename(painted)
                count += 1
                if in_speed:
                    speed_output_frames += 1
                    for _ in range(speed_repeat - 1):
                        _check(cancelled)
                        shutil.copyfile(painted, work / f"frame-{count:08d}.png")
                        count += 1
                        speed_output_frames += 1
                if frame_index in hold_plan:
                    hold_reel_starts[frame_index] = count
                    for _ in range(hold_plan[frame_index][1]):
                        _check(cancelled)
                        shutil.copyfile(painted, work / f"frame-{count:08d}.png")
                        count += 1
            clip = {
                "event_id": event["event_id"],
                "source_start_seconds": start,
                "source_end_seconds": end,
                "reel_start_seconds": clip_start_count / rate,
                "reel_end_seconds": count / rate,
                "frame_count": count - clip_start_count,
            }
            if hold_plan:
                holds = []
                for hold_frame, (hold_mark, hold_frames) in sorted(hold_plan.items()):
                    hold_reel_start = hold_reel_starts[hold_frame] / rate
                    holds.append(
                        {
                            "drawing_id": hold_mark["id"],
                            "source_seconds": start + hold_frame / rate,
                            "hold_seconds": hold_frames / rate,
                            "reel_start_seconds": hold_reel_start,
                            "reel_end_seconds": hold_reel_start + hold_frames / rate,
                            "frame_count": hold_frames,
                        }
                    )
                clip["hold"] = holds[0]  # first hold, kept for older readers
                if len(holds) > 1:
                    clip["holds"] = holds
            if speed_mark is not None:
                speed_source_start = start + speed_first / rate
                clip["speed"] = {
                    "drawing_id": speed_mark["id"],
                    "source_start_seconds": speed_source_start,
                    "source_end_seconds": min(end, start + speed_last / rate),
                    "speed": speed_mark["speed"],
                    "output_frames": speed_output_frames,
                    "reel_start_seconds": (clip_start_count + speed_output_start)
                    / rate,
                    "reel_end_seconds": (
                        clip_start_count + speed_output_start + speed_output_frames
                    )
                    / rate,
                }
            clips.append(clip)

        encoded = work / "encoded.mp4"
        args = [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-framerate",
            str(rate),
            "-i",
            str(work / "frame-%08d.png"),
        ]
        if info["has_audio"]:
            args += ["-i", str(source)]
            filters = []
            if any("hold" in clip or "speed" in clip for clip in clips):
                audio_format = (
                    "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
                )
                for i, clip in enumerate(clips):
                    cuts = []
                    for hold in clip.get(
                        "holds", [clip["hold"]] if "hold" in clip else []
                    ):
                        cuts.append(
                            (
                                min(
                                    clip["source_end_seconds"],
                                    hold["source_seconds"] + 1 / rate,
                                ),
                                0.0,
                                hold["hold_seconds"],
                                None,
                            )
                        )
                    if "speed" in clip:
                        speed = clip["speed"]
                        cuts.append(
                            (
                                speed["source_start_seconds"],
                                speed["source_end_seconds"]
                                - speed["source_start_seconds"],
                                speed["output_frames"] / rate,
                                speed["speed"],
                            )
                        )
                    cuts.sort()
                    segments = []
                    cursor = clip["source_start_seconds"]
                    after_speed = False
                    for n, (position, length, silence, factor) in enumerate(cuts):
                        if position > cursor:
                            label = f"pre{i}x{n}"
                            filters.append(
                                f"[1:a:0]atrim=start={cursor}:end={position},"
                                f"asetpts=PTS-STARTPTS,apad,"
                                f"atrim=duration={position - cursor},"
                                f"{audio_format}[{label}]"
                            )
                            segments.append(f"[{label}]")
                        label = f"silence{i}x{n}"
                        if factor is None:
                            filters.append(
                                f"anullsrc=r=48000:cl=stereo,atrim=duration={silence},"
                                f"asetpts=PTS-STARTPTS,{audio_format}[{label}]"
                            )
                        else:
                            # Keep the ambience: stretch (pitch preserved), compress
                            # the crowd peaks and fade the edges to avoid jumps.
                            tempo = ",".join(
                                ["atempo=0.5" if factor < 1 else "atempo=2"]
                                * (2 if factor in (0.25, 4) else 1)
                            )
                            fade = min(0.3, silence / 3)
                            out_fade = min(0.15, silence / 3)
                            filters.append(
                                f"[1:a:0]atrim=start={position}:end={position + length},"
                                f"asetpts=PTS-STARTPTS,{tempo},apad,"
                                f"atrim=duration={silence},"
                                f"acompressor=threshold=0.1:ratio=4:attack=20:release=300,"
                                f"afade=t=in:d={fade},afade=t=out:st={max(0.0, silence - out_fade)}:d={out_fade},"
                                f"{audio_format}[{label}]"
                            )
                        after_speed = factor is not None
                        segments.append(f"[{label}]")
                        cursor = position + length
                    if cursor < clip["source_end_seconds"]:
                        label = f"post{i}"
                        filters.append(
                            f"[1:a:0]atrim=start={cursor}:"
                            f"end={clip['source_end_seconds']},asetpts=PTS-STARTPTS,"
                            f"apad,atrim=duration={clip['source_end_seconds'] - cursor},"
                            + (
                                "acompressor=threshold=0.05:ratio=8:attack=10:release=400,"
                                "volume=0.5,afade=t=in:d=0.15,"
                                if after_speed
                                else ""
                            )
                            + f"{audio_format}[{label}]"
                        )
                        segments.append(f"[{label}]")
                    filters.append(
                        "".join(segments)
                        + f"concat=n={len(segments)}:v=0:a=1[joined{i}]"
                    )
                    filters.append(
                        f"[joined{i}]"
                        + (
                            # Hard ceiling so crowd roars never jump out of the mix.
                            "alimiter=limit=0.1:level=disabled:attack=2:release=80,"
                            if "speed" in clip
                            else ""
                        )
                        + f"apad,"
                        f"atrim=duration={clip['frame_count'] / rate},"
                        f"{audio_format}[a{i}]"
                    )
            else:
                for i, clip in enumerate(clips):
                    filters.append(
                        f"[1:a:0]atrim=start={clip['source_start_seconds']}:"
                        f"end={clip['source_end_seconds']},asetpts=PTS-STARTPTS,"
                        f"apad,atrim=duration={clip['frame_count'] / rate}[a{i}]"
                    )
            filters.append(
                "".join(f"[a{i}]" for i in range(len(clips)))
                + f"concat=n={len(clips)}:v=0:a=1[audio]"
            )
            args += [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "0:v:0",
                "-map",
                "[audio]",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
            ]
        args += [
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-frames:v",
            str(count),
            "-movflags",
            "+faststart",
            str(encoded),
        ]
        _run(args, cancelled)
        output = _probe(encoded, cancelled)
        _run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-i",
                str(encoded),
                "-f",
                "null",
                "-",
            ],
            cancelled,
        )
        verified = json.loads(
            _run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-count_frames",
                    "-show_entries",
                    "stream=nb_read_frames,codec_name,pix_fmt",
                    "-of",
                    "json",
                    str(encoded),
                ],
                cancelled,
            )
        )["streams"][0]
        if int(verified.get("nb_read_frames", -1)) != count:
            raise RenderError("Encoded frame count differs from the manifest")
        if (
            verified.get("codec_name") != "h264"
            or verified.get("pix_fmt") != "yuv420p"
            or output["width"] != out_width
            or output["height"] != out_height
            or abs(output["fps"] - rate) > 0.001
            or output["has_audio"] != info["has_audio"]
        ):
            raise RenderError("Encoded format differs from the requested output")
        output["frame_count"] = count
        manifest = {
            "source": str(source),
            "destination": str(destination),
            "semantics": "custom-render-only",
            "clips": clips,
            "output": output,
            "drawings_sidecar": str(sidecar),
        }
        if stabilize_reports:
            manifest["stabilize"] = stabilize_reports
        editable = work / "drawings.json"
        editable.write_text(
            json.dumps(
                {
                    "semantics": "custom-render-only",
                    "drawings": drawings,
                    "manifest": manifest,
                },
                indent=2,
            )
        )
        _check(cancelled)
        # Hard links publish complete files without replacing a concurrent writer.
        # Stage in the destination filesystem to support a separate TMPDIR mount.
        staged = []
        published = []
        warnings = []
        try:
            for target, contents in [(sidecar, editable), (destination, encoded)]:
                with tempfile.NamedTemporaryFile(
                    dir=destination.parent, prefix=".once-", delete=False
                ) as handle:
                    stage = Path(handle.name)
                    staged.append(stage)
                    with contents.open("rb") as reader:
                        import shutil

                        shutil.copyfileobj(reader, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                _check(cancelled)
                owned = stage.stat()
                os.link(stage, target)
                published.append((target, owned.st_dev, owned.st_ino))
        except BaseException:
            for target, device, inode in published:
                try:
                    current = target.lstat()
                    if (current.st_dev, current.st_ino) == (device, inode):
                        target.unlink()
                except OSError:
                    # Preserve the original publication failure; never remove a replacement.
                    pass
            raise
        finally:
            for stage in staged:
                try:
                    stage.unlink(missing_ok=True)
                except OSError:
                    warnings.append("Private publication staging cleanup is pending")
        if warnings:
            manifest["warnings"] = warnings
        return manifest


def _positions(mark, time):
    keys = mark.get("keyframes", [])
    if not keys:
        return mark["points"]
    if time <= keys[0]["time_seconds"]:
        return keys[0]["points"]
    for left, right in itertools.pairwise(keys):
        if time < right["time_seconds"]:
            mix = (time - left["time_seconds"]) / (
                right["time_seconds"] - left["time_seconds"]
            )
            if mark.get("easing", "linear") == "ease_in_out":
                mix = mix * mix * (3 - 2 * mix)
            return [
                [a + (b - a) * mix for a, b in zip(p, q)]
                for p, q in zip(left["points"], right["points"])
            ]
    return keys[-1]["points"]


POSITION_CODES = {
    "GK": "gk",
    "RB": "def", "LB": "def", "RWB": "def", "LWB": "def", "CB": "def",
    "RCB": "def", "LCB": "def", "SW": "def",
    "DM": "mid", "CDM": "mid", "CM": "mid", "RCM": "mid", "LCM": "mid",
    "AM": "mid", "AMF": "mid", "CAM": "mid", "RM": "mid", "LM": "mid",
    "RW": "fwd", "LW": "fwd", "SS": "fwd", "CF": "fwd", "ST": "fwd",
    "RWF": "fwd", "LWF": "fwd", "RF": "fwd", "LF": "fwd",
    "CMF": "mid", "DMF": "mid", "RMF": "mid", "LMF": "mid",
}  # fmt: skip
_POSITION_COLORS = {
    "gk": "#F2C94C",
    "def": "#4F9DFF",
    "mid": "#3DDC97",
    "fwd": "#FF6B6B",
}


def _spline(points, per_segment=16):
    """Catmull-Rom spline through the points (endpoints are interpolated exactly)."""
    if len(points) < 3:
        return list(points)
    padded = [points[0], *points, points[-1]]
    out = []
    for index in range(1, len(padded) - 2):
        p0, p1, p2, p3 = padded[index - 1 : index + 3]
        for step in range(per_segment):
            t = step / per_segment
            out.append(
                tuple(
                    0.5
                    * (
                        2 * p1[axis]
                        + (-p0[axis] + p2[axis]) * t
                        + (2 * p0[axis] - 5 * p1[axis] + 4 * p2[axis] - p3[axis]) * t**2
                        + (-p0[axis] + 3 * p1[axis] - 3 * p2[axis] + p3[axis]) * t**3
                    )
                    for axis in (0, 1)
                )
            )
    out.append(points[-1])
    return out


def _path_length(points):
    return sum(math.dist(a, b) for a, b in itertools.pairwise(points))


def _trim_end(points, distance):
    """Cut `distance` off the end of a path; return (shorter path, tangent at the cut)."""
    remaining = distance
    path = list(points)
    while len(path) > 2 and math.dist(path[-2], path[-1]) <= remaining:
        remaining -= math.dist(path[-2], path[-1])
        path.pop()
    a, b = path[-2], path[-1]
    seg = math.dist(a, b) or 1.0
    keep = max(0.0, seg - remaining) / seg
    path[-1] = (a[0] + (b[0] - a[0]) * keep, a[1] + (b[1] - a[1]) * keep)
    return path


def _dashes(points, dash, gap):
    """Split a path into dash sub-paths by arc length."""
    out, current, draw, left = [], [points[0]], True, dash
    for a, b in itertools.pairwise(points):
        seg = math.dist(a, b)
        pos = 0.0
        while seg - pos > 1e-9:
            step = min(left, seg - pos)
            pos += step
            left -= step
            point = (a[0] + (b[0] - a[0]) * pos / seg, a[1] + (b[1] - a[1]) * pos / seg)
            if draw:
                current.append(point)
            if left <= 1e-9:
                if draw and len(current) > 1:
                    out.append(current)
                draw = not draw
                left = dash if draw else gap
                current = [point] if draw else []
    if draw and len(current) > 1:
        out.append(current)
    return out


def _arrow_head(path, length, spread=0.42):
    """Head polygon with a small notch; direction comes from the last `length` of path."""
    tip = path[-1]
    back = _trim_end(path, length * 0.9)[-1]
    angle = math.atan2(tip[1] - back[1], tip[0] - back[0])
    wings = [
        (
            tip[0] - length * math.cos(angle + sign * spread),
            tip[1] - length * math.sin(angle + sign * spread),
        )
        for sign in (-1, 1)
    ]
    notch = _trim_end(path, length * 0.7)[-1]
    return [tip, wings[0], notch, wings[1]], notch


MAX_HOLDS_PER_EVENT = 5
MAX_HOLD_TOTAL_SECONDS = 30
STABILIZE_KINDS = frozenset(
    {
        "area",
        "line",
        "arrow",
        "polyline",
        "curve",
        "polygon",
        "label",
        "nametag",
        "lob",
        "player_marker",
    }
)
GROUND_KINDS = frozenset(
    {"area", "line", "arrow", "polyline", "curve", "polygon", "ellipse", "spotlight"}
)


def _resolve_stabilize(source, events, drawings, cancelled=None):
    """Turn `stabilize` drawings into camera-compensated keyframes.

    Motion is estimated once per (event, interval) for all drawings that share
    it. When the camera tracker stops early (scene cut, points leaving the
    frame) the drawing ends at the last usable keyframe instead of drifting.
    """
    from . import camera

    marks = [m for m in drawings if m.get("stabilize")]
    if not marks:
        return drawings, []
    groups = {}
    for mark in marks:
        groups.setdefault(
            (mark["event_id"], mark["start_seconds"], mark["end_seconds"]), []
        ).append(mark)
    resolved, reports = {}, []
    for (event_id, start, end), members in groups.items():
        _check(cancelled)
        points = [p for mark in members for p in mark["points"]]
        try:
            result = camera.motion_keyframes(source, start, end, points)
        except camera.CameraError as exc:
            raise RenderError(f"Camera stabilization failed: {exc}") from exc
        keyframes = result["keyframes"]
        if len(keyframes) < 2:
            raise RenderError(
                "Camera stabilization could not follow the pitch "
                f"({result.get('stopped_reason') or 'too few usable frames'})"
            )
        offset = 0
        for mark in members:
            count = len(mark["points"])
            own = [
                {
                    "time_seconds": key["time_seconds"],
                    "points": key["points"][offset : offset + count],
                }
                for key in keyframes
            ]
            offset += count
            end_at = min(end, own[-1]["time_seconds"])
            updated = dict(mark, keyframes=own, end_seconds=end_at)
            resolved[mark["id"]] = updated
            reports.append(
                {
                    "id": mark["id"],
                    "frames": result.get("usable_frames"),
                    "stopped_reason": result.get("stopped_reason"),
                    "ended_at_seconds": end_at,
                }
            )
    return [resolved.get(mark["id"], mark) for mark in drawings], reports


def _tapered_line(pen, points, start_width, end_width, fill):
    """Polyline whose width grows linearly with arc length (round joints)."""
    total = _path_length(points) or 1.0
    # Densify so a two-point line still tapers smoothly.
    points = [*_arc_samples(points, total / 48), points[-1]]
    walked, previous = 0.0, start_width
    for a, b in itertools.pairwise(points):
        walked += math.dist(a, b)
        width = start_width + (end_width - start_width) * walked / total
        pen.line([a, b], fill=fill, width=max(1, round((previous + width) / 2)))
        radius = max(0.5, width / 2)
        pen.ellipse(
            (b[0] - radius, b[1] - radius, b[0] + radius, b[1] + radius), fill=fill
        )
        previous = width
    first = points[0]
    radius = max(0.5, start_width / 2)
    pen.ellipse(
        (first[0] - radius, first[1] - radius, first[0] + radius, first[1] + radius),
        fill=fill,
    )


def _arc_samples(points, spacing):
    """Points every `spacing` of arc length along a path (for dotted lines)."""
    samples, need = [points[0]], spacing
    for a, b in itertools.pairwise(points):
        segment, walked = math.dist(a, b), 0.0
        while segment - walked >= need - 1e-9 and segment > 0:
            walked += need
            need = spacing
            samples.append(
                (
                    a[0] + (b[0] - a[0]) * walked / segment,
                    a[1] + (b[1] - a[1]) * walked / segment,
                )
            )
        need -= segment - walked
    return samples


def _hex_rgb(value):
    return tuple(int(value[index : index + 2], 16) for index in (1, 3, 5))


def _readable_ink(fill_hex):
    red, green, blue = _hex_rgb(fill_hex)
    return "#101B23" if 0.299 * red + 0.587 * green + 0.114 * blue > 140 else "#FFFFFF"


def _draw_board(layer, mark, width, height, scale):
    """Top-down tactical board inset: pitch, player tokens and link lines.

    The panel is the rectangle between the two points. Pitch metres map to the
    panel with the own goal (x_m = 0) at the bottom and y_m running left to
    right, so a back four reads left-to-right like on a coaching board.
    """
    from PIL import Image, ImageDraw

    (x0, y0), (x1, y1) = [
        (x * width * scale, y * height * scale) for x, y in mark["points"]
    ]
    panel_w, panel_h = x1 - x0, y1 - y0
    aspect = 68 / 105
    if panel_w / panel_h > aspect:
        pitch_h = panel_h * 0.94
        pitch_w = pitch_h * aspect
    else:
        pitch_w = panel_w * 0.94
        pitch_h = pitch_w / aspect
    left = x0 + (panel_w - pitch_w) / 2
    top = y0 + (panel_h - pitch_h) / 2
    unit = pitch_h / 105

    def at(x_m, y_m):
        return (left + y_m * unit, top + pitch_h - x_m * unit)

    board = Image.new("RGBA", layer.size)
    pen = ImageDraw.Draw(board)
    pen.rounded_rectangle((x0, y0, x1, y1), radius=8 * scale, fill=(14, 42, 30, 236))
    bands = 14
    for band in range(bands):
        if band % 2:
            pen.rectangle(
                (
                    left,
                    top + pitch_h * band / bands,
                    left + pitch_w,
                    top + pitch_h * (band + 1) / bands,
                ),
                fill=(255, 255, 255, 14),
            )
    line = (240, 240, 240, 235)
    thin = max(1, round(unit * 0.32))

    def rect(xa, ya, xb, yb):
        p, q = at(xa, ya), at(xb, yb)
        pen.rectangle(
            (min(p[0], q[0]), min(p[1], q[1]), max(p[0], q[0]), max(p[1], q[1])),
            outline=line,
            width=thin,
        )

    rect(0, 0, 105, 68)
    pen.line([at(52.5, 0), at(52.5, 68)], fill=line, width=thin)
    centre = at(52.5, 34)
    radius = 9.15 * unit
    pen.ellipse(
        (
            centre[0] - radius,
            centre[1] - radius,
            centre[0] + radius,
            centre[1] + radius,
        ),
        outline=line,
        width=thin,
    )
    for near, sign in ((0.0, 1), (105.0, -1)):
        rect(near, 13.84, near + sign * 16.5, 54.16)
        rect(near, 24.84, near + sign * 5.5, 43.16)
        rect(near, 30.34, near - sign * 2.0, 37.66)
        spot = at(near + sign * 11, 34)
        pen.ellipse(
            (spot[0] - thin, spot[1] - thin, spot[0] + thin, spot[1] + thin), fill=line
        )

    players = mark.get("players", [])
    token = pitch_w * 0.036
    centres = [at(p["x_m"], p["y_m"]) for p in players]
    link_color = mark.get("color", "#FFD400")
    for link in mark.get("links", []):
        route = [centres[index] for index in link]
        pen.line(
            route,
            fill=(16, 27, 35, 255),
            width=round(token * 0.55 + 2 * scale),
            joint="curve",
        )
        pen.line(
            route, fill=link_color, width=max(2, round(token * 0.42)), joint="curve"
        )
    name_font = _font(max(7, round(token * 0.95)), bold=True)
    number_font = _font(max(7, round(token * 1.15)), bold=True)
    code_font = _font(max(6, round(token * 0.72)))
    for player, (cx, cy) in zip(players, centres):
        team_fill = {"home": "#1E3BFF", "away": "#F2F2F2"}[player.get("team", "home")]
        fill = player.get("color", team_fill)
        pen.ellipse(
            (
                cx - token - scale,
                cy - token - scale,
                cx + token + scale,
                cy + token + scale,
            ),
            fill=(16, 27, 35, 255),
        )
        pen.ellipse((cx - token, cy - token, cx + token, cy + token), fill=fill)
        if "number" in player:
            text = str(player["number"])
            box = pen.textbbox((0, 0), text, font=number_font)
            pen.text(
                (cx - (box[0] + box[2]) / 2, cy - (box[1] + box[3]) / 2),
                text,
                font=number_font,
                fill=_readable_ink(fill),
            )
        if player.get("name"):
            box = pen.textbbox((0, 0), player["name"], font=name_font)
            tx, ty = (
                cx - (box[0] + box[2]) / 2,
                cy - token - (box[3] - box[1]) - 3 * scale,
            )
            pen.text(
                (tx + scale, ty + scale),
                player["name"],
                font=name_font,
                fill=(0, 0, 0, 200),
            )
            pen.text((tx, ty), player["name"], font=name_font, fill="#FFFFFF")
        if player.get("position"):
            box = pen.textbbox((0, 0), player["position"], font=code_font)
            pen.text(
                (cx - (box[0] + box[2]) / 2, cy + token + 2 * scale),
                player["position"],
                font=code_font,
                fill=(215, 225, 220, 255),
            )
    return Image.alpha_composite(layer, board)


def _drawing_z_tier(kind):
    """Stable painter tiers: background regions first, annotations last."""
    if kind == "area":
        return 0
    if kind in ("line", "polyline", "arrow", "curve", "polygon", "trail", "lob"):
        return 1
    if kind in ("ellipse", "spotlight", "player_marker"):
        return 2
    if kind == "nametag":
        return 3
    return 4


_FONT_DIRS = (
    "/usr/share/fonts/TTF",
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
    "/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "C:/Windows/Fonts",
)


def _font(size, bold=False):
    """DejaVu Sans (bundled with most Linux installs) with portable fallbacks."""
    from PIL import ImageFont

    names = (
        ("DejaVuSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf")
        if bold
        else ("DejaVuSans.ttf", "Arial.ttf", "arial.ttf")
    )
    candidates = list(names) + [
        str(Path(directory) / name) for directory in _FONT_DIRS for name in names
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _wrap_label(text, measure, max_width):
    """Wrap at spaces; split characters only inside an overlong word."""
    lines = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}" if line else word
        if measure(candidate) <= max_width:
            line = candidate
            continue
        if line:
            lines.append(line)
            line = ""
        if measure(word) <= max_width:
            line = word
            continue
        for character in word:
            if line and measure(line + character) > max_width:
                lines.append(line)
                line = ""
            line += character
    if line:
        lines.append(line)
    return lines


def _place_tag_box(
    left,
    top,
    box_width,
    box_height,
    canvas_width,
    canvas_height,
    footer_height,
    occupied,
    gap,
    margin=4,
):
    """Clamp a nametag to the footer-safe frame and greedily avoid prior tags."""
    minimum = margin
    left = max(minimum, min(left, canvas_width - box_width - minimum))
    minimum_top = minimum
    maximum_top = max(minimum_top, canvas_height - footer_height - box_height - minimum)
    preferred = max(minimum_top, min(top, maximum_top))
    candidates = {preferred, minimum_top, maximum_top}
    for other_left, other_top, other_right, other_bottom in occupied:
        if left < other_right + gap and left + box_width > other_left - gap:
            candidates.add(other_top - box_height - gap)
            candidates.add(other_bottom + gap)
    candidates = {
        candidate
        for candidate in candidates
        if minimum_top <= candidate <= maximum_top
        and all(
            not (
                left < other_right + gap
                and left + box_width > other_left - gap
                and candidate < other_bottom + gap
                and candidate + box_height > other_top - gap
            )
            for other_left, other_top, other_right, other_bottom in occupied
        )
    }
    placed_top = (
        min(candidates, key=lambda candidate: (abs(candidate - preferred), candidate))
        if candidates
        else preferred
    )
    return left, placed_top, left + box_width, placed_top + box_height


def _fade_alpha(mark, time):
    """Compute [0, 1] alpha multiplier from optional fade-in/out ramps."""
    alpha = 1.0
    fade_in = mark.get("fade_in_seconds")
    if fade_in:
        alpha = min(alpha, max(0.0, (time - mark["start_seconds"]) / fade_in))
    fade_out = mark.get("fade_out_seconds")
    if fade_out:
        alpha = min(alpha, max(0.0, (mark["end_seconds"] - time) / fade_out))
    return alpha


def _with_alpha(image, factor):
    """Return an RGBA copy with alpha scaled by `factor`, or None when transparent."""
    if factor >= 1.0 - 1e-6:
        return image
    if factor <= 1e-6:
        return None
    faded = image.copy()
    faded.putalpha(faded.getchannel("A").point(lambda value: round(value * factor)))
    return faded


def _paint_frame(path, event, drawings, time, overlay_cache):
    """Paint ground-masked drawings first, then everything else plus the footer."""
    grounded = [m for m in drawings if m.get("ground")]
    if not any(
        m["event_id"] == event["event_id"]
        and m["start_seconds"] <= time < m["end_seconds"]
        for m in grounded
    ):
        _paint(
            path,
            event,
            [m for m in drawings if not m.get("ground")],
            time,
            overlay_cache["rest"],
        )
        return
    _paint(
        path, event, grounded, time, overlay_cache["ground"], footer=False, ground=True
    )
    _paint(
        path,
        event,
        [m for m in drawings if not m.get("ground")],
        time,
        overlay_cache["rest"],
    )


def _paint(
    path, event, drawings, time, overlay_cache=None, *, footer=True, ground=False
):
    from PIL import Image, ImageDraw

    overlay_cache = overlay_cache if overlay_cache is not None else [None, None]
    with Image.open(path) as source:
        base = source.convert("RGB")
    width, height = base.size
    # Supersample 3x for SD/HD-ready sizes, 2x above 720p to bound memory/time.
    scale = 3 if height <= 720 else 2
    strip = min(height, 120)
    layer = Image.new("RGBA", (width * scale, height * scale))
    pen = ImageDraw.Draw(layer)
    stroke = max(2, height / 200) * scale
    halo = "#101B23"
    font = _font(max(11, round(height / 32)) * scale)
    title = " · ".join(str(event.get(k, "")) for k in ("team", "phase", "action"))
    footer_bounds = pen.multiline_textbbox(
        (0, 0), f"0.00s   |   {title}", font=font, spacing=2 * scale
    )
    footer_height = min(strip * scale, footer_bounds[3] - footer_bounds[1] + 14 * scale)

    def label(text, anchor, title=False, color="#F6F4EC", pen=None, canvas_h=None):
        pen = pen or ImageDraw.Draw(layer)
        # Wrap long labels so both text and padding stay within the frame.
        max_width = width * scale - 16 * scale
        safe_height = (
            (canvas_h or height * scale)
            if title
            else max(
                18 * scale, height * scale - max(40 * scale, font.size + 18 * scale)
            )
        )
        lines = _wrap_label(
            text, lambda value: pen.textlength(value, font=font), max_width - 12 * scale
        )
        max_lines = (
            1
            if title
            else max(1, (safe_height - 18 * scale) // (font.size + 2 * scale))
        )
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            last = lines[-1]
            while (
                last and pen.textlength(last + "…", font=font) > max_width - 12 * scale
            ):
                last = last[:-1]
            lines[-1] = last + "…"
        text = "\n".join(lines)
        bounds = pen.multiline_textbbox((0, 0), text, font=font, spacing=scale * 2)
        bw = bounds[2] - bounds[0] + 12 * scale
        bh = bounds[3] - bounds[1] + 10 * scale
        x = max(4 * scale, min(anchor[0], width * scale - bw - 4 * scale))
        y = max(4 * scale, min(anchor[1], safe_height - bh - 4 * scale))
        pen.rounded_rectangle(
            (x, y, x + bw, y + bh), radius=4 * scale, fill=(12, 22, 30, 225)
        )
        pen.multiline_text(
            (x + 6 * scale - bounds[0], y + 5 * scale - bounds[1]),
            text,
            font=font,
            fill=color,
            spacing=2 * scale,
        )

    active = sorted(
        (
            mark
            for mark in drawings
            if mark["event_id"] == event["event_id"]
            and mark["kind"] != "speed"
            and mark["start_seconds"] <= time < mark["end_seconds"]
        ),
        key=lambda mark: _drawing_z_tier(mark["kind"]),
    )
    alphas = [_fade_alpha(mark, time) for mark in active]
    uniform_fade = len({round(alpha, 6) for alpha in alphas}) == 1
    key = None
    if active:
        key = (
            width,
            height,
            tuple(
                (
                    tuple(
                        sorted(
                            (k, repr(v))
                            for k, v in mark.items()
                            if k not in ("keyframes", "points")
                        )
                    ),
                    tuple(map(tuple, _positions(mark, time))),
                    round(max(0, time - mark["start_seconds"]) * 10)
                    if mark["kind"] == "stopwatch"
                    else (round(time * 1000) if mark["kind"] == "trail" else None),
                    None if uniform_fade else round(alpha, 4),
                )
                for mark, alpha in zip(active, alphas)
            ),
        )
    overlay = None
    if key is not None and overlay_cache[0] == key:
        overlay = overlay_cache[1]
    elif key is not None:
        placed_tags = []
        for mark, alpha in zip(active, alphas):
            if not uniform_fade and alpha <= 1e-6:
                continue
            fade_mark = not uniform_fade and alpha < 1.0 - 1e-6
            target_layer = Image.new("RGBA", layer.size) if fade_mark else layer
            target_pen = ImageDraw.Draw(target_layer) if fade_mark else pen
            pts = [
                (x * width * scale, y * height * scale)
                for x, y in _positions(mark, time)
            ]
            kind = mark["kind"]
            color = mark.get(
                "color",
                {
                    "line": "#6DE0D4",
                    "label": "#FFFFFF",
                    "lob": "#E5484D",
                    "player_marker": "#E5565C",
                }.get(kind, "#FFD166"),
            )
            if kind == "curve":
                start, control, end = pts
                pts = [
                    (
                        (1 - t) ** 2 * start[0]
                        + 2 * (1 - t) * t * control[0]
                        + t**2 * end[0],
                        (1 - t) ** 2 * start[1]
                        + 2 * (1 - t) * t * control[1]
                        + t**2 * end[1],
                    )
                    for index in range(33)
                    for t in [index / 32]
                ]
            if kind == "stopwatch":
                elapsed = max(0, time - mark["start_seconds"])
                minutes, tenths = divmod(round(elapsed * 10), 600)
                text = f"{minutes}:{tenths / 10:04.1f}"
                if mark.get("text"):
                    text = f"{mark['text']} {text}"
                label(text, pts[0], color=color, pen=target_pen)
            elif kind == "trail":
                trail = float(mark.get("trail_seconds", 2.0))
                steps = max(8, min(48, round(trail * 12)))
                begin = max(mark["start_seconds"], time - trail)
                samples = []
                for index in range(steps + 1):
                    when = begin + (time - begin) * index / steps
                    ((px, py),) = _positions(mark, when)
                    samples.append((px * width * scale, py * height * scale))
                rgb = tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))
                count = len(samples) - 1
                for index in range(count):
                    fade = (index + 1) / count  # 0 at the tail, 1 at the head
                    segment = samples[index : index + 2]
                    target_pen.line(
                        segment,
                        fill=(16, 27, 35, round(150 * fade)),
                        width=round(stroke * (0.5 + 1.1 * fade) + 2 * scale),
                    )
                    target_pen.line(
                        segment,
                        fill=(*rgb, round(255 * fade)),
                        width=max(1, round(stroke * (0.4 + 1.0 * fade))),
                    )
                hx, hy = samples[-1]
                dot = stroke * 1.3
                target_pen.ellipse(
                    (hx - dot, hy - dot, hx + dot, hy + dot),
                    fill=color,
                    outline=halo,
                    width=scale,
                )
            elif kind == "lob":
                (lx0, ly0), (lx1, ly1) = pts
                reach = math.dist(pts[0], pts[1])
                lift = float(mark.get("arc", 0.18)) * reach
                control = ((lx0 + lx1) / 2, (ly0 + ly1) / 2 - 2 * lift)
                curve_pts = [
                    (
                        (1 - t) ** 2 * lx0 + 2 * (1 - t) * t * control[0] + t * t * lx1,
                        (1 - t) ** 2 * ly0 + 2 * (1 - t) * t * control[1] + t * t * ly1,
                    )
                    for t in (i / 32 for i in range(33))
                ]
                # Ground shadow: where the ball is on the pitch; the arc is its height.
                target_pen.line(
                    [pts[0], pts[1]],
                    fill=(16, 27, 35, 120),
                    width=max(2, round(stroke * 1.3)),
                )
                head_len = min(
                    max(9, height / 32) * scale, 0.3 * _path_length(curve_pts)
                )
                lob_head, lob_notch = _arrow_head(curve_pts, head_len)
                curve_pts = _trim_end(curve_pts, head_len * 0.7)
                curve_pts[-1] = lob_notch
                _tapered_line(
                    target_pen,
                    curve_pts,
                    stroke * 0.5 + 3 * scale,
                    stroke * 1.7 + 3 * scale,
                    halo,
                )
                _tapered_line(target_pen, curve_pts, stroke * 0.4, stroke * 1.55, color)
                target_pen.polygon(lob_head, fill=halo, outline=halo, width=scale * 2)
                target_pen.polygon(lob_head, fill=color)
            elif kind == "player_marker":
                ax, ay = pts[0]
                size = float(mark.get("size", 1.0))
                rx = 0.0155 * width * scale * size
                ry = rx * 0.34
                column_h = 0.105 * height * scale * size
                column = Image.new("RGBA", target_layer.size)
                column_pen = ImageDraw.Draw(column)
                steps = 24
                for index in range(steps):
                    column_pen.rectangle(
                        (
                            ax - rx * 0.82,
                            ay - column_h * (index + 1) / steps,
                            ax + rx * 0.82,
                            ay - column_h * index / steps,
                        ),
                        fill=(255, 255, 255, round(95 * (1 - index / steps) ** 1.2)),
                    )
                target_layer = Image.alpha_composite(target_layer, column)
                target_pen = ImageDraw.Draw(target_layer)
                target_pen.ellipse(
                    (
                        ax - rx - scale,
                        ay - ry - scale,
                        ax + rx + scale,
                        ay + ry + scale,
                    ),
                    fill=halo,
                )
                target_pen.ellipse((ax - rx, ay - ry, ax + rx, ay + ry), fill=color)
                tag = " | ".join(
                    part for part in (mark.get("position"), mark.get("text")) if part
                )
                if tag:
                    tag_fill = mark.get("tag_color", "#FFFFFF")
                    tag_font = _font(max(10, round(height / 60)) * scale, bold=True)
                    box = target_pen.textbbox((0, 0), tag, font=tag_font)
                    pad = 3 * scale
                    tag_w, tag_h = box[2] - box[0] + 2 * pad, box[3] - box[1] + 2 * pad
                    tag_left = min(
                        max(ax - tag_w / 2, 2 * scale),
                        width * scale - tag_w - 2 * scale,
                    )
                    tag_top = min(
                        ay + ry + 2 * scale,
                        height * scale - footer_height - tag_h - 2 * scale,
                    )
                    target_pen.rounded_rectangle(
                        (
                            tag_left - scale,
                            tag_top - scale,
                            tag_left + tag_w + scale,
                            tag_top + tag_h + scale,
                        ),
                        radius=3 * scale,
                        fill=halo,
                    )
                    target_pen.rounded_rectangle(
                        (tag_left, tag_top, tag_left + tag_w, tag_top + tag_h),
                        radius=2 * scale,
                        fill=tag_fill,
                    )
                    target_pen.text(
                        (tag_left + pad - box[0], tag_top + pad - box[1]),
                        tag,
                        font=tag_font,
                        fill=_readable_ink(tag_fill),
                    )
            elif kind == "board":
                target_layer = _draw_board(target_layer, mark, width, height, scale)
                target_pen = ImageDraw.Draw(target_layer)
            elif kind == "label":
                label(mark["text"], pts[0], color=color, pen=target_pen)
            elif kind == "area":
                fill_layer = Image.new("RGBA", target_layer.size)
                rgb = tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))
                ImageDraw.Draw(fill_layer).polygon(
                    pts, fill=(*rgb, round(255 * mark.get("opacity", 0.25)))
                )
                target_layer = Image.alpha_composite(target_layer, fill_layer)
                target_pen = ImageDraw.Draw(target_layer)
                outline = pts + [pts[0]]
                target_pen.line(
                    outline, fill=halo, width=round(stroke + 3 * scale), joint="curve"
                )
                target_pen.line(outline, fill=color, width=round(stroke), joint="curve")
            elif kind == "spotlight":
                (cx, cy), (rx, ry) = pts
                box = (cx - rx, cy - ry, cx + rx, cy + ry)
                rgb = tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))
                glow = Image.new("RGBA", target_layer.size)
                glow_pen = ImageDraw.Draw(glow)
                style = mark.get("style", "ground")
                if style == "ground":
                    for expansion, alpha_ring in ((6 * scale, 16), (3 * scale, 28)):
                        glow_pen.ellipse(
                            (
                                box[0] - expansion,
                                box[1] - expansion,
                                box[2] + expansion,
                                box[3] + expansion,
                            ),
                            outline=(*rgb, alpha_ring),
                            width=2 * scale,
                        )
                    glow_pen.ellipse(box, fill=(*rgb, 64))
                else:
                    for expansion, alpha_ring in (
                        (6 * scale, 18),
                        (3 * scale, 32),
                        (0, 50),
                    ):
                        glow_pen.ellipse(
                            (
                                box[0] - expansion,
                                box[1] - expansion,
                                box[2] + expansion,
                                box[3] + expansion,
                            ),
                            outline=(*rgb, alpha_ring),
                            width=max(scale, round(stroke + expansion / 3)),
                        )
                target_layer = Image.alpha_composite(target_layer, glow)
                target_pen = ImageDraw.Draw(target_layer)
                target_pen.ellipse(box, outline=halo, width=round(stroke + 3 * scale))
                target_pen.ellipse(box, outline=color, width=round(stroke))
            elif kind == "ellipse":
                (cx, cy), (rx, ry) = pts
                box = (cx - rx, cy - ry, cx + rx, cy + ry)
                target_pen.ellipse(box, outline=halo, width=round(stroke + 3 * scale))
                target_pen.ellipse(box, outline=color, width=round(stroke))
            elif kind == "nametag":
                position = mark.get("position")
                name = mark.get("text")
                group = POSITION_CODES.get(position)
                accent = mark.get("color") or _POSITION_COLORS.get(group, "#FFD166")
                tag_size = max(12, round(height / 46)) * scale
                bold = _font(tag_size, bold=True)
                regular = _font(tag_size)
                ax, ay = pts[0]
                pad = round(tag_size * 0.32)
                parts = []
                if position:
                    parts.append((position, bold, "#101B23", accent))
                if name:
                    parts.append((name, regular, "#FFFFFF", "#101B23"))
                widths = [
                    target_pen.textlength(text, font=part_font) + 2 * pad
                    for text, part_font, _, _ in parts
                ]
                tag_w, tag_h = sum(widths), tag_size + 2 * pad
                lift = height * scale * 0.12
                preferred_top = ay - lift - tag_h
                if preferred_top < 4 * scale:
                    preferred_top = ay + lift * 0.3
                box = _place_tag_box(
                    ax - tag_w / 2 - scale,
                    preferred_top - scale,
                    tag_w + 2 * scale,
                    tag_h + 2 * scale,
                    width * scale,
                    height * scale,
                    footer_height,
                    placed_tags,
                    2 * scale,
                    margin=4 * scale,
                )
                placed_tags.append(box)
                box_left, box_top, _, _ = box
                left, top = box_left + scale, box_top + scale
                pointer_x = min(max(ax, left + tag_h / 2), left + tag_w - tag_h / 2)
                below = top > ay
                stem_y = top if below else top + tag_h
                rgb = tuple(int(accent[index : index + 2], 16) for index in (1, 3, 5))
                # Faint stem keeps the tag attached to the player without hiding them.
                target_pen.line(
                    [(ax, ay), (pointer_x, stem_y)], fill=(*rgb, 120), width=scale * 2
                )
                point = tag_h * 0.28
                tip_y = stem_y + (-point if below else point)
                target_pen.polygon(
                    [
                        (pointer_x - point, stem_y),
                        (pointer_x + point, stem_y),
                        (pointer_x, tip_y),
                    ],
                    fill=halo,
                )
                dot = stroke * 1.1
                target_pen.ellipse(
                    (ax - dot, ay - dot, ax + dot, ay + dot),
                    fill=accent,
                    outline=halo,
                    width=scale,
                )
                cursor = left
                radius = tag_h / 2
                target_pen.rounded_rectangle(
                    (
                        left - scale,
                        top - scale,
                        left + tag_w + scale,
                        top + tag_h + scale,
                    ),
                    radius=radius + scale,
                    fill=halo,
                )
                for (text, part_font, ink, fill), part_w in zip(parts, widths):
                    target_pen.rounded_rectangle(
                        (cursor, top, cursor + part_w, top + tag_h),
                        radius=radius,
                        fill=fill,
                    )
                    bounds = target_pen.textbbox((0, 0), text, font=part_font)
                    target_pen.text(
                        (
                            cursor + pad - bounds[0],
                            top + (tag_h - (bounds[3] - bounds[1])) / 2 - bounds[1],
                        ),
                        text,
                        font=part_font,
                        fill=ink,
                    )
                    cursor += part_w
            else:
                if kind == "polygon":
                    line = pts + [pts[0]]
                elif kind == "polyline" and mark.get("smooth"):
                    line = _spline(pts)
                else:
                    line = pts
                has_head = kind in ("arrow", "curve") or (
                    kind == "polyline" and mark.get("arrow", False)
                )
                head_len = min(max(9, height / 32) * scale, 0.45 * _path_length(line))
                head = None
                if has_head and len(line) >= 2 and head_len > 0:
                    head, notch = _arrow_head(line, head_len)
                    line = _trim_end(line, head_len * 0.7)
                    line[-1] = notch
                if mark.get("dash") == "dotted":
                    dots = _arc_samples(line, 2.6 * stroke)
                    for cx, cy in dots:
                        r = stroke * 0.95
                        target_pen.ellipse(
                            (
                                cx - r - scale,
                                cy - r - scale,
                                cx + r + scale,
                                cy + r + scale,
                            ),
                            fill=halo,
                        )
                    for cx, cy in dots:
                        r = stroke * 0.95
                        target_pen.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
                elif mark.get("taper"):
                    _tapered_line(
                        target_pen,
                        line,
                        stroke * 0.45 + 3 * scale,
                        stroke * 1.6 + 3 * scale,
                        halo,
                    )
                    _tapered_line(target_pen, line, stroke * 0.35, stroke * 1.5, color)
                else:
                    shaft = (
                        _dashes(line, 5 * stroke, 3.2 * stroke)
                        if mark.get("dash")
                        else [line]
                    )
                    for piece in shaft:
                        target_pen.line(
                            piece,
                            fill=halo,
                            width=round(stroke + 3 * scale),
                            joint="curve",
                        )
                    for piece in shaft:
                        target_pen.line(
                            piece, fill=color, width=round(stroke), joint="curve"
                        )
                if head is not None:
                    target_pen.polygon(head, fill=halo, outline=halo, width=scale * 2)
                    target_pen.polygon(head, fill=color)
            if mark.get("text") and kind not in (
                "stopwatch",
                "label",
                "nametag",
                "player_marker",
            ):
                label(mark["text"], pts[0], color=color, pen=target_pen)
            if fade_mark:
                faded_mark = _with_alpha(target_layer, alpha)
                if faded_mark is not None:
                    layer = Image.alpha_composite(layer, faded_mark)
                    pen = ImageDraw.Draw(layer)
            else:
                layer = target_layer
                pen = target_pen
        overlay = layer.resize(base.size, Image.Resampling.LANCZOS)
        overlay_cache[:] = [key, overlay]
    if overlay is not None:
        composite = _with_alpha(overlay, alphas[0]) if uniform_fade else overlay
        if composite is not None:
            if ground:
                from . import grassmask

                mask, reliable = grassmask.ground_mask(base)
                if reliable:
                    base = grassmask.apply_ground_layer(
                        base.convert("RGBA"), composite, mask
                    ).convert("RGB")
                else:
                    base = Image.alpha_composite(
                        base.convert("RGBA"), composite
                    ).convert("RGB")
            else:
                base = Image.alpha_composite(base.convert("RGBA"), composite).convert(
                    "RGB"
                )
    if not footer:
        base.save(path, compress_level=1)
        return
    strip = min(height, 120)
    top = height - strip
    footer = Image.new("RGBA", (width * scale, strip * scale))
    label(
        f"{time:.2f}s   |   {title}",
        (8 * scale, strip * scale),
        True,
        pen=ImageDraw.Draw(footer),
        canvas_h=strip * scale,
    )
    small = footer.resize((width, strip), Image.Resampling.LANCZOS)
    region = base.crop((0, top, width, height)).convert("RGBA")
    base.paste(Image.alpha_composite(region, small).convert("RGB"), (0, top))
    base.save(path, compress_level=1)


def _number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _validate_board(mark):
    import re

    (x0, y0), (x1, y1) = (
        mark["points"] if len(mark.get("points", [])) == 2 else ((0, 0), (0, 0))
    )
    if not (x1 - x0 >= 0.1 and y1 - y0 >= 0.1):
        raise RenderError("A board panel must span at least 10% of the frame both ways")
    players = mark.get("players")
    if not isinstance(players, list) or not 1 <= len(players) <= 30:
        raise RenderError("A board needs 1 to 30 players")
    allowed = {"x_m", "y_m", "number", "name", "position", "team", "color"}
    for player in players:
        if not isinstance(player, dict) or set(player) - allowed:
            raise RenderError("Invalid board player")
        for key, high in (("x_m", 105), ("y_m", 68)):
            if not _number(player.get(key)) or not 0 <= player[key] <= high:
                raise RenderError(
                    f"Board player {key} must be metres on a 105x68 pitch"
                )
        if "number" in player and (
            type(player["number"]) is not int or not 0 <= player["number"] <= 99
        ):
            raise RenderError("Board player number must be an integer 0-99")
        if "name" in player and (
            not isinstance(player["name"], str)
            or not player["name"].strip()
            or len(player["name"]) > 16
            or any(ord(c) < 32 for c in player["name"])
        ):
            raise RenderError("Board player name must be 1-16 printable characters")
        if "position" in player and player["position"] not in POSITION_CODES:
            raise RenderError("Unknown board position code")
        if player.get("team", "home") not in ("home", "away"):
            raise RenderError("Board team must be 'home' or 'away'")
        if "color" in player and (
            not isinstance(player["color"], str)
            or not re.fullmatch(r"#[0-9a-fA-F]{6}", player["color"])
        ):
            raise RenderError("Board player color must be #RRGGBB")
    links = mark.get("links", [])
    if not isinstance(links, list) or len(links) > 12:
        raise RenderError("A board takes at most 12 links")
    for link in links:
        if (
            not isinstance(link, list)
            or not 2 <= len(link) <= 11
            or any(type(i) is not int or not 0 <= i < len(players) for i in link)
        ):
            raise RenderError("Each board link is 2-11 player indexes")


def _validate(events, drawings, info, width, fps):
    import re

    if width is not None and (type(width) is not int or width < 2):
        raise RenderError("Width must be an integer of at least two pixels")
    if fps is not None and (not _number(fps) or fps <= 0 or fps > 240):
        raise RenderError("Frame rate must be finite and in (0, 240]")
    if not isinstance(events, list) or not events:
        raise RenderError("At least one event is required")
    owners = {}
    for event in events:
        if not isinstance(event, dict):
            raise RenderError("Events must be objects")
        identifier = event.get("event_id")
        start, end = event.get("start_seconds"), event.get("end_seconds")
        if not isinstance(identifier, str) or not identifier or identifier in owners:
            raise RenderError("Event IDs must be nonempty and unique")
        if not (
            _number(start) and _number(end) and 0 <= start < end <= info["duration"]
        ):
            raise RenderError(
                "Event range must have positive duration within the media"
            )
        for key in ("team", "phase", "action"):
            if not isinstance(event.get(key), str) or len(event[key]) > 100:
                raise RenderError(f"Event {key} must be text of at most 100 characters")
        owners[identifier] = event
    if not isinstance(drawings, list):
        raise RenderError("Drawings must be a list")
    ids = set()
    allowed = {
        "id",
        "event_id",
        "kind",
        "start_seconds",
        "end_seconds",
        "points",
        "color",
        "text",
        "keyframes",
        "hold_seconds",
        "speed",
        "arrow",
        "opacity",
        "style",
        "smooth",
        "dash",
        "position",
        "fade_in_seconds",
        "fade_out_seconds",
        "easing",
        "stabilize",
        "ground",
        "trail_seconds",
        "taper",
        "arc",
        "size",
        "tag_color",
        "players",
        "links",
    }
    hold_counts = {}
    hold_totals = {}
    hold_frame_keys = set()
    speed_owners = set()
    speed_spans = {}
    hold_starts = {}

    def points(value, kind, count=None):
        if kind == "speed":
            if value != []:
                raise RenderError("Speed drawings must have an empty points list")
            return
        if not isinstance(value, list) or not value:
            raise RenderError("Drawing points must be a nonempty list")
        expected = {
            "ellipse": 2,
            "arrow": 2,
            "line": 2,
            "label": 1,
            "curve": 3,
            "spotlight": 2,
            "stopwatch": 1,
            "nametag": 1,
            "trail": 1,
            "player_marker": 1,
            "lob": 2,
            "board": 2,
        }.get(kind)
        if (
            (expected and len(value) != expected)
            or (kind == "polygon" and len(value) < 3)
            or (kind == "area" and not 3 <= len(value) <= 32)
            or (kind == "polyline" and not 2 <= len(value) <= 32)
        ):
            raise RenderError("Wrong point topology")
        if count is not None and len(value) != count:
            raise RenderError("Tracked points must match the drawing topology")
        for pair in value:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not all(_number(v) and 0 <= v <= 1 for v in pair)
            ):
                raise RenderError("Coordinates must be finite normalized pairs")
        if kind in ("ellipse", "spotlight") and not all(v > 0 for v in value[1]):
            raise RenderError("Ellipse radii must be positive")

    for mark in drawings:
        if not isinstance(mark, dict) or set(mark) - allowed:
            raise RenderError("Unknown drawing keys")
        identifier = mark.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in ids:
            raise RenderError("Drawing IDs must be nonempty and unique")
        ids.add(identifier)
        owner = mark.get("event_id")
        if not isinstance(owner, str) or owner not in owners:
            raise RenderError("Drawing must belong to an existing event")
        kind = mark.get("kind")
        if kind not in (
            "ellipse",
            "arrow",
            "line",
            "polygon",
            "label",
            "polyline",
            "curve",
            "area",
            "spotlight",
            "stopwatch",
            "nametag",
            "trail",
            "lob",
            "player_marker",
            "board",
            "speed",
        ):
            raise RenderError("Unknown drawing kind")
        if "speed" in mark and kind != "speed":
            raise RenderError("speed is only valid for speed drawings")
        if kind == "speed":
            value = mark.get("speed")
            if not _number(value) or value not in (0.25, 0.5, 2, 4):
                raise RenderError("speed must be one of 0.25, 0.5, 2, or 4")
            if owner in speed_owners:
                raise RenderError("At most one speed segment is allowed per event")
            speed_owners.add(owner)
        start, end = mark.get("start_seconds"), mark.get("end_seconds")
        event = owners[owner]
        if (
            not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not math.isfinite(start)
            or not isinstance(end, (int, float))
            or isinstance(end, bool)
            or not math.isfinite(end)
            or not event["start_seconds"] <= start < end <= event["end_seconds"]
        ):
            raise RenderError("Drawing display interval must lie within its event")
        if "hold_seconds" in mark:
            hold_seconds = mark["hold_seconds"]
            if not _number(hold_seconds) or not 0 < hold_seconds <= 10:
                raise RenderError("hold_seconds must be a finite number in (0, 10]")
            if hold_counts.get(owner, 0) >= MAX_HOLDS_PER_EVENT:
                raise RenderError(
                    f"At most {MAX_HOLDS_PER_EVENT} holds are allowed per event"
                )
            if hold_totals.get(owner, 0) + hold_seconds > MAX_HOLD_TOTAL_SECONDS:
                raise RenderError(
                    f"Holds in one event may total at most {MAX_HOLD_TOTAL_SECONDS} s"
                )
            rate = fps if fps is not None else info["fps"]
            frames = max(
                1,
                math.ceil(
                    (event["end_seconds"] - event["start_seconds"]) * rate - 1e-9
                ),
            )
            hold_frame = math.ceil((start - event["start_seconds"]) * rate - 1e-9)
            hold_time = event["start_seconds"] + hold_frame / rate
            if hold_frame >= frames or not hold_time < end:
                raise RenderError(
                    "Hold must land on a displayed frame within its drawing interval"
                )
            hold_counts[owner] = hold_counts.get(owner, 0) + 1
            hold_totals[owner] = hold_totals.get(owner, 0) + hold_seconds
            frame_key = (owner, hold_frame)
            if frame_key in hold_frame_keys:
                raise RenderError("Two holds cannot start on the same frame")
            hold_frame_keys.add(frame_key)
        if kind == "speed":
            speed_spans[owner] = (start, end)
        if "hold_seconds" in mark:
            hold_starts.setdefault(owner, []).append(start)
        if "arrow" in mark and (
            kind != "polyline" or not isinstance(mark["arrow"], bool)
        ):
            raise RenderError("arrow must be a boolean used only with polyline")
        for flag, kinds in (
            ("smooth", ("polyline",)),
            ("taper", ("arrow", "curve", "polyline")),
        ):
            if flag in mark and (kind not in kinds or not isinstance(mark[flag], bool)):
                raise RenderError(f"{flag} must be a boolean used only with {kinds}")
        if "dash" in mark and (
            kind not in ("line", "arrow", "polyline", "curve")
            or not (isinstance(mark["dash"], bool) or mark["dash"] == "dotted")
        ):
            raise RenderError(
                "dash must be true, false or 'dotted' on line-like drawings"
            )
        if mark.get("taper") and mark.get("dash"):
            raise RenderError("taper cannot be combined with dash")
        if "arc" in mark and (
            kind != "lob" or not _number(mark["arc"]) or not 0.05 <= mark["arc"] <= 0.6
        ):
            raise RenderError("arc must be a number in [0.05, 0.6] used only with lob")
        if "size" in mark and (
            kind != "player_marker"
            or not _number(mark["size"])
            or not 0.4 <= mark["size"] <= 3
        ):
            raise RenderError(
                "size must be a number in [0.4, 3] used only with player_marker"
            )
        if "tag_color" in mark and (
            kind != "player_marker"
            or not isinstance(mark["tag_color"], str)
            or not re.fullmatch(r"#[0-9a-fA-F]{6}", mark["tag_color"])
        ):
            raise RenderError("tag_color must be #RRGGBB used only with player_marker")
        if kind == "board":
            _validate_board(mark)
        elif "players" in mark or "links" in mark:
            raise RenderError("players and links are only valid for board drawings")
        if kind in ("nametag", "player_marker"):
            position, name = mark.get("position"), mark.get("text")
            if position is not None and position not in POSITION_CODES:
                raise RenderError("Unknown position code")
            if kind == "nametag" and position is None and name is None:
                raise RenderError("A nametag needs a position code or a name")
            if name is not None and (
                not isinstance(name, str)
                or not name.strip()
                or len(name) > 24
                or any(ord(c) < 32 for c in name)
            ):
                raise RenderError("Nametag text must be 1-24 printable characters")
        elif "position" in mark:
            raise RenderError(
                "position is only valid for nametag and player_marker drawings"
            )
        if "opacity" in mark and (
            kind != "area"
            or not _number(mark["opacity"])
            or not 0.05 <= mark["opacity"] <= 0.8
        ):
            raise RenderError("Area opacity must be a number in [0.05, 0.8]")
        if "style" in mark and (
            kind != "spotlight" or mark["style"] not in ("halo", "ground")
        ):
            raise RenderError("Spotlight style must be 'halo' or 'ground'")
        if kind == "speed" and any(
            key in mark for key in ("fade_in_seconds", "fade_out_seconds", "easing")
        ):
            raise RenderError("Speed drawings do not support fades or keyframe easing")
        fade_in_raw = mark.get("fade_in_seconds")
        fade_out_raw = mark.get("fade_out_seconds")
        fade_total = 0.0
        for fade_key, fade_value in (
            ("fade_in_seconds", fade_in_raw),
            ("fade_out_seconds", fade_out_raw),
        ):
            if fade_key in mark:
                if (
                    not isinstance(fade_value, (int, float))
                    or isinstance(fade_value, bool)
                    or not math.isfinite(fade_value)
                    or not 0 < fade_value <= 5
                ):
                    raise RenderError(f"{fade_key} must be a finite number in (0, 5]")
                fade_total += fade_value
        if fade_total > (end - start) + 1e-9:
            raise RenderError("Total fade duration must not exceed drawing duration")
        if "easing" in mark and mark["easing"] not in ("linear", "ease_in_out"):
            raise RenderError("easing must be 'linear' or 'ease_in_out'")
        points(mark.get("points"), kind)
        if "color" in mark and (
            not isinstance(mark["color"], str)
            or not re.fullmatch(r"#[0-9a-fA-F]{6}", mark["color"])
        ):
            raise RenderError("Color must be #RRGGBB")
        if "text" in mark or kind == "label":
            text = mark.get("text")
            if (
                not isinstance(text, str)
                or not text.strip()
                or len(text) > 120
                or any(ord(c) < 32 for c in text)
            ):
                raise RenderError("Drawing text must be a short nonempty single line")
        if kind == "trail":
            seconds = mark.get("trail_seconds", 2.0)
            if not _number(seconds) or not 0.3 <= seconds <= 10:
                raise RenderError("trail_seconds must be a number in [0.3, 10]")
            if len(mark.get("keyframes", [])) < 2:
                raise RenderError("A trail needs at least two keyframes to follow")
        elif "trail_seconds" in mark:
            raise RenderError("trail_seconds is only valid for trail drawings")
        for flag, kinds in (("stabilize", STABILIZE_KINDS), ("ground", GROUND_KINDS)):
            if flag in mark and (
                type(mark[flag]) is not bool or mark["kind"] not in kinds
            ):
                raise RenderError(
                    f"{flag} must be a boolean used only with {sorted(kinds)}"
                )
        if mark.get("stabilize") and mark.get("keyframes"):
            raise RenderError("A stabilized drawing must not carry keyframes")
        keys = mark.get("keyframes", [])
        if not isinstance(keys, list):
            raise RenderError("Keyframes must be a list")
        previous = -math.inf
        for key in keys:
            if not isinstance(key, dict) or set(key) != {"time_seconds", "points"}:
                raise RenderError("Invalid keyframe keys")
            time = key["time_seconds"]
            if not _number(time) or not start <= time <= end or time <= previous:
                raise RenderError(
                    "Keyframes must be ordered within the display interval"
                )
            points(key["points"], kind, len(mark["points"]))
            previous = time

    rate = fps if fps is not None else (info or {}).get("fps")
    for owner, span in speed_spans.items() if rate else ():
        first = owners[owner]["start_seconds"]
        frames = max(1, math.ceil((owners[owner]["end_seconds"] - first) * rate - 1e-9))
        speed_first = math.ceil((span[0] - first) * rate - 1e-9)
        speed_last = min(frames, math.ceil((span[1] - first) * rate - 1e-9))
        if speed_first >= speed_last:
            raise RenderError("A speed segment must cover at least one output frame")
        for hold_start in hold_starts.get(owner, ()):
            hold_frame = math.ceil((hold_start - first) * rate - 1e-9)
            if speed_first <= hold_frame < speed_last:
                raise RenderError("A hold cannot start inside a speed segment")


def render_reel(
    source: Path,
    events: list[dict],
    drawings: list[dict],
    destination: Path,
    *,
    width: int | None = None,
    fps: float | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict:
    """Render custom telestration; never replace an existing output or sidecar.

    Each clip uses ceil(duration * fps) frames. The last source frame may be
    held for less than one output frame; manifest offsets use encoded time.
    Temporary frames respect the caller's TMPDIR.
    """
    try:
        return _render_reel(
            source,
            events,
            drawings,
            destination,
            width=width,
            fps=fps,
            cancelled=cancelled,
        )
    except (OSError, ValueError) as exc:
        raise RenderError(f"Rendering failed: {exc}") from exc
