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
    overlay_cache = [None, None]
    with tempfile.TemporaryDirectory(
        prefix="once-render-", dir=destination.parent
    ) as work:
        work = Path(work)
        for index, event in enumerate(sorted(events, key=lambda e: e["start_seconds"])):
            start, end = event["start_seconds"], event["end_seconds"]
            frames = max(1, math.ceil((end - start) * rate - 1e-9))
            hold_mark = next(
                (
                    mark
                    for mark in drawings
                    if mark["event_id"] == event["event_id"] and "hold_seconds" in mark
                ),
                None,
            )
            hold_frame = None
            hold_frames = 0
            if hold_mark is not None:
                hold_frame = math.ceil(
                    (hold_mark["start_seconds"] - start) * rate - 1e-9
                )
                hold_frames = max(1, round(hold_mark["hold_seconds"] * rate))
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
            hold_reel_start_count = None
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
                _paint(path, event, drawings, start + frame_index / rate, overlay_cache)
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
                if frame_index == hold_frame:
                    hold_reel_start_count = count
                    for _ in range(hold_frames):
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
            if hold_mark is not None:
                source_seconds = start + hold_frame / rate
                hold_reel_start = hold_reel_start_count / rate
                hold_reel_end = hold_reel_start + hold_frames / rate
                clip["hold"] = {
                    "drawing_id": hold_mark["id"],
                    "source_seconds": source_seconds,
                    "hold_seconds": hold_frames / rate,
                    "reel_start_seconds": hold_reel_start,
                    "reel_end_seconds": hold_reel_end,
                    "frame_count": hold_frames,
                }
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
                    if "hold" in clip:
                        hold = clip["hold"]
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


def _drawing_z_tier(kind):
    """Stable painter tiers: background regions first, annotations last."""
    if kind == "area":
        return 0
    if kind in ("line", "polyline", "arrow", "curve", "polygon"):
        return 1
    if kind in ("ellipse", "spotlight"):
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


def _paint(path, event, drawings, time, overlay_cache=None):
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
                    else None,
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
                "color", {"line": "#6DE0D4", "label": "#FFFFFF"}.get(kind, "#FFD166")
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
                shaft = (
                    _dashes(line, 5 * stroke, 3.2 * stroke)
                    if mark.get("dash")
                    else [line]
                )
                for piece in shaft:
                    target_pen.line(
                        piece, fill=halo, width=round(stroke + 3 * scale), joint="curve"
                    )
                for piece in shaft:
                    target_pen.line(
                        piece, fill=color, width=round(stroke), joint="curve"
                    )
                if head is not None:
                    target_pen.polygon(head, fill=halo, outline=halo, width=scale * 2)
                    target_pen.polygon(head, fill=color)
            if mark.get("text") and kind not in ("stopwatch", "label", "nametag"):
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
            base = Image.alpha_composite(base.convert("RGBA"), composite).convert("RGB")
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
    }
    hold_owners = set()
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
            if owner in hold_owners:
                raise RenderError("At most one hold is allowed per event")
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
            hold_owners.add(owner)
        if kind == "speed":
            speed_spans[owner] = (start, end)
        if "hold_seconds" in mark:
            hold_starts[owner] = start
        if "arrow" in mark and (
            kind != "polyline" or not isinstance(mark["arrow"], bool)
        ):
            raise RenderError("arrow must be a boolean used only with polyline")
        for flag, kinds in (
            ("smooth", ("polyline",)),
            ("dash", ("line", "arrow", "polyline", "curve")),
        ):
            if flag in mark and (kind not in kinds or not isinstance(mark[flag], bool)):
                raise RenderError(f"{flag} must be a boolean used only with {kinds}")
        if kind == "nametag":
            position, name = mark.get("position"), mark.get("text")
            if position is not None and position not in POSITION_CODES:
                raise RenderError("Unknown nametag position code")
            if position is None and name is None:
                raise RenderError("A nametag needs a position code or a name")
            if name is not None and (
                not isinstance(name, str)
                or not name.strip()
                or len(name) > 24
                or any(ord(c) < 32 for c in name)
            ):
                raise RenderError("Nametag text must be 1-24 printable characters")
        elif "position" in mark:
            raise RenderError("position is only valid for nametag drawings")
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
        hold_start = hold_starts.get(owner)
        if hold_start is not None:
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
