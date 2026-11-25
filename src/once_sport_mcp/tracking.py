"""Conservative, review-required single-target tracking and diagnostics."""

import json
import math
import os
import stat
import tempfile
from collections.abc import Mapping
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path

from . import rendering as _rendering


class TrackingError(RuntimeError):
    """Invalid tracking input or an unsafe tracking/media-processing failure."""


def _finite_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _frame_window(start_seconds, end_seconds, fps):
    """Return the CFR source-frame indices in the requested half-open window."""
    first = math.ceil(start_seconds * fps - 1e-9)
    stop = math.ceil(end_seconds * fps - 1e-9)
    return first, stop


def validate_tracking_request(start_seconds, end_seconds, initial_bbox, media) -> None:
    """Validate a normalized XYWH seed and bounded square-pixel CFR request."""
    if not (_finite_number(start_seconds) and _finite_number(end_seconds)):
        raise TrackingError("Tracking times must be finite numbers")
    if not isinstance(media, Mapping):
        raise TrackingError("Media metadata must be an object")

    duration = media.get("duration")
    width = media.get("width")
    height = media.get("height")
    fps = media.get("fps")
    sar = media.get("sample_aspect_ratio")
    if not _finite_number(duration) or duration <= 0:
        raise TrackingError("Media duration must be finite and positive")
    if type(width) is not int or width <= 0 or type(height) is not int or height <= 0:
        raise TrackingError("Media dimensions must be positive integers")
    if not _finite_number(fps) or fps <= 0:
        raise TrackingError("Media frame rate must be finite and positive")
    if not _finite_number(sar) or not math.isclose(sar, 1.0, rel_tol=0, abs_tol=1e-9):
        raise TrackingError("Tracking requires square sample aspect ratio")
    if not (0 <= start_seconds < end_seconds <= duration):
        raise TrackingError("Tracking window must be positive and within the media")
    if end_seconds - start_seconds > 12:
        raise TrackingError("Tracking window cannot exceed 12 seconds")
    first, stop = _frame_window(start_seconds, end_seconds, fps)
    frame_count = stop - first
    if frame_count <= 0:
        raise TrackingError("Tracking window must contain a source frame")
    if frame_count > 600:
        raise TrackingError("Tracking window cannot exceed 600 source frames")

    if not isinstance(initial_bbox, list) or len(initial_bbox) != 4:
        raise TrackingError("Initial bbox must contain normalized XYWH values")
    if not all(_finite_number(value) for value in initial_bbox):
        raise TrackingError("Initial bbox values must be finite non-boolean numbers")
    x, y, box_width, box_height = initial_bbox
    if (
        x < 0
        or y < 0
        or box_width <= 0
        or box_height <= 0
        or x + box_width > 1
        or y + box_height > 1
    ):
        raise TrackingError("Initial bbox must be a positive in-frame normalized XYWH")
    if box_width * width < 8 or box_height * height < 8:
        raise TrackingError("Initial bbox must be at least 8 by 8 source pixels")


def _check_cancelled(cancelled):
    if cancelled is None:
        return
    if not callable(cancelled):
        raise TrackingError("Cancellation must be a callable")
    try:
        is_cancelled = cancelled()
    except Exception as exc:
        raise TrackingError(f"Cancellation check failed: {exc}") from exc
    if is_cancelled:
        raise TrackingError("Tracking cancelled")


def _load_cv2():
    try:
        import cv2
    except (ImportError, OSError) as exc:
        raise TrackingError(
            "Tracking requires the optional OpenCV tracking extra"
        ) from exc
    if not hasattr(cv2, "TrackerCSRT_create"):
        raise TrackingError("This OpenCV build does not provide CSRT tracking")
    return cv2


def _tracker_factory():
    """Isolated factory seam for real CSRT and deterministic guard tests."""
    return _load_cv2().TrackerCSRT_create()


@contextmanager
def _open_capture(cv2, source, first_index):
    capture = cv2.VideoCapture(str(source))
    try:
        if not capture.isOpened():
            raise TrackingError("OpenCV could not open the source video")
        if not capture.set(cv2.CAP_PROP_POS_FRAMES, first_index):
            raise TrackingError("Decoder cannot seek to the exact seed frame")
        position = capture.get(cv2.CAP_PROP_POS_FRAMES)
        if not _finite_number(position) or abs(position - first_index) > 0.5:
            raise TrackingError("Decoder did not seek to the exact source frame")
        yield capture
    finally:
        capture.release()


def _iter_capture_frames(
    cv2, capture, first_index, stop_index, fps, width, height, cancelled
):
    previous_pts = None
    for index in range(first_index, stop_index):
        _check_cancelled(cancelled)
        position = capture.get(cv2.CAP_PROP_POS_FRAMES)
        if not _finite_number(position) or abs(position - index) > 0.5:
            raise TrackingError("Decoder source-frame position is unreliable")
        ok, frame = capture.read()
        if not ok or frame is None:
            raise TrackingError("Decoder ended before the requested source window")
        pts_msec = capture.get(cv2.CAP_PROP_POS_MSEC)
        if not _finite_number(pts_msec) or pts_msec < 0:
            raise TrackingError("Decoder returned an invalid source timestamp")
        pts = pts_msec / 1000
        tolerance = max(0.0015, 0.05 / fps)
        if abs(pts - index / fps) > tolerance:
            raise TrackingError(
                "Decoder PTS is unreliable or the source cadence is unsupported"
            )
        if previous_pts is not None:
            delta = pts - previous_pts
            if delta <= 0 or abs(delta - 1 / fps) > 2 * tolerance:
                raise TrackingError(
                    "Nonmonotonic or variable source cadence is unsupported"
                )
        if (
            getattr(frame, "ndim", 0) != 3
            or frame.shape[0] != height
            or frame.shape[1] != width
            or frame.shape[2] != 3
        ):
            raise TrackingError("Decoded source frame dimensions changed")
        previous_pts = pts
        yield index, frame, pts


@contextmanager
def _capture_frames(
    cv2, source, first_index, stop_index, fps, width, height, cancelled
):
    with _open_capture(cv2, source, first_index) as capture:
        yield _iter_capture_frames(
            cv2, capture, first_index, stop_index, fps, width, height, cancelled
        )


def _pixel_box(values, width, height):
    try:
        box = tuple(float(value) for value in values)
    except (TypeError, ValueError, OverflowError):
        return None
    if len(box) != 4 or not all(math.isfinite(value) for value in box):
        return None
    x, y, box_width, box_height = box
    if (
        box_width <= 0
        or box_height <= 0
        or x < 0
        or y < 0
        or x + box_width > width
        or y + box_height > height
    ):
        return None
    return box


def _box_iou(left, right):
    lx, ly, lw, lh = left
    rx, ry, rw, rh = right
    intersection_width = max(0.0, min(lx + lw, rx + rw) - max(lx, rx))
    intersection_height = max(0.0, min(ly + lh, ry + rh) - max(ly, ry))
    intersection = intersection_width * intersection_height
    union = lw * lh + rw * rh - intersection
    return intersection / union if union > 0 else 0.0


def _scene_signature(cv2, frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (64, 36), interpolation=cv2.INTER_AREA)
    histogram = cv2.calcHist([small], [0], None, [32], [0, 256])
    cv2.normalize(histogram, histogram, alpha=1, norm_type=cv2.NORM_L1)
    return small, histogram


def _scene_change(cv2, previous, current):
    difference = cv2.absdiff(previous[0], current[0])
    mean_difference = float(cv2.mean(difference)[0])
    histogram_distance = float(
        cv2.compareHist(previous[1], current[1], cv2.HISTCMP_BHATTACHARYYA)
    )
    return mean_difference >= 45 or histogram_distance >= 0.65


def _reasonable_motion(previous, current, width, height):
    px, py, pw, ph = previous
    x, y, box_width, box_height = current
    old_area = pw * ph
    new_area = box_width * box_height
    area_ratio = new_area / old_area
    if not 0.5 <= area_ratio <= 2.0:
        return False
    if not (0.4 <= box_width / pw <= 2.5 and 0.4 <= box_height / ph <= 2.5):
        return False
    old_center = (px + pw / 2, py + ph / 2)
    new_center = (x + box_width / 2, y + box_height / 2)
    distance = math.hypot(new_center[0] - old_center[0], new_center[1] - old_center[1])
    jump_limit = max(0.04 * math.hypot(width, height), 4 * max(pw, ph))
    return distance <= jump_limit


def _init_tracker(tracker, frame, box):
    rectangle = tuple(round(float(value)) for value in box)
    if len(rectangle) != 4:
        return False
    x, y, width, height = rectangle
    if (
        width <= 0
        or height <= 0
        or x < 0
        or y < 0
        or x + width > frame.shape[1]
        or y + height > frame.shape[0]
    ):
        return False
    initialized = tracker.init(frame, rectangle)
    return initialized is not False


def _validate_block(block):
    """Replay a bounded block backwards; return its per-frame IoUs or reject it."""
    try:
        reverse = _tracker_factory()
        if not _init_tracker(reverse, block[-1]["frame"], block[-1]["bbox_pixels"]):
            return None
        ious = [None] * len(block)
        ious[-1] = 1.0
        for index in range(len(block) - 2, -1, -1):
            ok, reverse_box = reverse.update(block[index]["frame"])
            if not ok:
                return None
            reverse_box = _pixel_box(
                reverse_box, block[index]["width"], block[index]["height"]
            )
            if reverse_box is None:
                return None
            ious[index] = _box_iou(block[index]["bbox_pixels"], reverse_box)
        if min(ious) < 0.35:
            return None
        return ious
    except TrackingError:
        raise
    except (_load_cv2().error, TypeError, ValueError, RuntimeError):
        return None


def _sample_record(record, fb_iou):
    x, y, width, height = record["bbox_pixels"]
    frame_width, frame_height = record["width"], record["height"]
    anchor = [
        (x + width / 2) / frame_width,
        (y + height) / frame_height,
    ]
    return {
        "source_frame_index": record["frame_index"],
        "source_seconds": record["source_seconds"],
        "bbox": [
            x / frame_width,
            y / frame_height,
            width / frame_width,
            height / frame_height,
        ],
        "anchor": anchor,
        "fb_iou": fb_iou,
    }


def _accept_block(block, ious, accepted):
    start = 0
    if accepted and block[0]["frame_index"] == accepted[-1]["frame_index"]:
        accepted[-1]["fb_iou"] = min(accepted[-1]["fb_iou"], ious[0])
        start = 1
    for index in range(start, len(block)):
        record = block[index]
        record["fb_iou"] = ious[index]
        accepted.append({key: value for key, value in record.items() if key != "frame"})


def _normalize_samples(records):
    return [_sample_record(record, record["fb_iou"]) for record in records]


def _draw_label(cv2, frame, text, y, color):
    height = frame.shape[0]
    scale = max(0.4, min(0.8, height / 800))
    thickness = max(1, round(scale * 2))
    origin = (8, min(height - 6, max(18, y)))
    cv2.putText(
        frame,
        text,
        origin,
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        (10, 18, 24),
        thickness + 2,
        cv2.LINE_AA,
    )
    cv2.putText(
        frame,
        text,
        origin,
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        color,
        thickness,
        cv2.LINE_AA,
    )


def _render_preview(
    cv2,
    source,
    raw_path,
    first_index,
    stop_index,
    fps,
    width,
    height,
    accepted,
    valid_end,
    status,
    cancelled,
):
    if width % 2 or height % 2:
        raise TrackingError("H.264 yuv420p preview requires even source dimensions")
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(raw_path), fourcc, float(fps), (width, height))
    if not writer.isOpened():
        writer.release()
        raise TrackingError("OpenCV could not open the private preview writer")
    by_index = {record["frame_index"]: record for record in accepted}
    written = 0
    try:
        with _capture_frames(
            cv2, source, first_index, stop_index, fps, width, height, cancelled
        ) as frames:
            for index, frame, pts in frames:
                _check_cancelled(cancelled)
                record = by_index.get(index)
                if record is not None:
                    x, y, box_width, box_height = record["bbox_pixels"]
                    left, top = round(x), round(y)
                    right, bottom = round(x + box_width - 1), round(y + box_height - 1)
                    cv2.rectangle(frame, (left, top), (right, bottom), (0, 220, 255), 2)
                    anchor_x = round(x + box_width / 2)
                    anchor_y = round(y + box_height)
                    cv2.drawMarker(
                        frame,
                        (anchor_x, anchor_y),
                        (255, 235, 40),
                        cv2.MARKER_CROSS,
                        max(8, round(height / 45)),
                        2,
                    )
                _draw_label(
                    cv2,
                    frame,
                    "UNAPPROVED TRACKING CANDIDATE",
                    22,
                    (245, 245, 245),
                )
                if status != "complete" and pts >= valid_end:
                    _draw_label(
                        cv2,
                        frame,
                        "TRACK LOST - RESEED",
                        46,
                        (70, 100, 255),
                    )
                writer.write(frame)
                written += 1
    finally:
        writer.release()
    if written != stop_index - first_index:
        raise TrackingError("Diagnostic preview did not cover the requested window")


def _encode_and_verify_preview(
    cv2, raw_path, encoded_path, media, expected_frames, cancelled
):
    width, height, fps = media["width"], media["height"], media["fps"]
    _rendering._run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(raw_path),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "passthrough",
            "-movflags",
            "+faststart",
            str(encoded_path),
        ],
        cancelled,
    )
    _rendering._run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(encoded_path),
            "-f",
            "null",
            "-",
        ],
        cancelled,
    )
    data = json.loads(
        _rendering._run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-count_frames",
                "-show_entries",
                "stream=codec_name,pix_fmt,width,height,nb_read_frames,avg_frame_rate",
                "-of",
                "json",
                str(encoded_path),
            ],
            cancelled,
        )
    )
    try:
        stream = data["streams"][0]
        output_fps = float(Fraction(stream["avg_frame_rate"]))
        frame_count = int(stream["nb_read_frames"])
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise TrackingError("Cannot verify the diagnostic preview format") from exc
    if (
        stream.get("codec_name") != "h264"
        or stream.get("pix_fmt") != "yuv420p"
        or int(stream.get("width", -1)) != width
        or int(stream.get("height", -1)) != height
        or frame_count != expected_frames
        or abs(output_fps - fps) > 0.001
    ):
        raise TrackingError(
            "Diagnostic preview failed format or frame-count verification"
        )


def _check_output_family(output_dir):
    directory = Path(output_dir).absolute()
    try:
        mode = directory.lstat().st_mode
    except OSError as exc:
        raise TrackingError(f"Output stage must already exist: {exc}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise TrackingError("Output stage must be an existing non-symlink directory")
    targets = [directory / "tracking.json", directory / "tracking.preview.mp4"]
    for target in targets:
        try:
            target.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise TrackingError(
                f"Cannot inspect tracking output family: {exc}"
            ) from exc
        raise TrackingError(f"Tracking output already exists: {target.name}")
    return directory, targets


def _publish_pair(staged, targets, cancelled):
    published = []
    try:
        for source, target in zip(staged, targets):
            _check_cancelled(cancelled)
            staged_stat = source.lstat()
            if not stat.S_ISREG(staged_stat.st_mode):
                raise TrackingError("Staged tracking output must be a regular file")
            owned = staged_stat
            os.link(source, target)
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


def _track_target(
    source, start_seconds, end_seconds, initial_bbox, output_dir, cancelled
):
    _check_cancelled(cancelled)
    directory, targets = _check_output_family(output_dir)
    try:
        source = Path(source).resolve(strict=True)
        media = _rendering._probe(source, cancelled)
    except _rendering.RenderError as exc:
        raise TrackingError(f"Cannot inspect source media: {exc}") from exc
    validate_tracking_request(start_seconds, end_seconds, initial_bbox, media)
    cv2 = _load_cv2()
    first_index, stop_index = _frame_window(start_seconds, end_seconds, media["fps"])
    width, height, fps = media["width"], media["height"], media["fps"]
    if width % 2 or height % 2:
        raise TrackingError("H.264 yuv420p preview requires even source dimensions")

    seed_bbox = [
        initial_bbox[0] * width,
        initial_bbox[1] * height,
        initial_bbox[2] * width,
        initial_bbox[3] * height,
    ]
    accepted = []
    loss_reason = None
    with _capture_frames(
        cv2, source, first_index, stop_index, fps, width, height, cancelled
    ) as frames:
        iterator = iter(frames)
        try:
            seed_index, seed_frame, seed_pts = next(iterator)
        except StopIteration as exc:
            raise TrackingError(
                "Requested window contains no decodable source frame"
            ) from exc
        forward = _tracker_factory()
        try:
            if not _init_tracker(forward, seed_frame, seed_bbox):
                raise TrackingError("CSRT could not initialize at the exact seed frame")
        except TrackingError:
            raise
        except Exception as exc:
            raise TrackingError(f"CSRT initialization failed: {exc}") from exc
        seed_signature = _scene_signature(cv2, seed_frame)
        block = [
            {
                "frame_index": seed_index,
                "source_seconds": seed_pts,
                "bbox_pixels": tuple(seed_bbox),
                "width": width,
                "height": height,
                "frame": seed_frame,
            }
        ]
        previous_signature = seed_signature
        for index, frame, pts in iterator:
            _check_cancelled(cancelled)
            signature = _scene_signature(cv2, frame)
            if _scene_change(cv2, previous_signature, signature):
                loss_reason = "scene_change"
                break
            try:
                ok, predicted = forward.update(frame)
            except (_load_cv2().error, TypeError, ValueError, RuntimeError):
                ok, predicted = False, None
            if not ok:
                loss_reason = "tracker_update_failed"
                break
            predicted = _pixel_box(predicted, width, height)
            if predicted is None:
                loss_reason = "invalid_tracker_bbox"
                break
            previous_box = block[-1]["bbox_pixels"]
            if not _reasonable_motion(previous_box, predicted, width, height):
                loss_reason = "abrupt_bbox_jump_or_scale_change"
                break
            block.append(
                {
                    "frame_index": index,
                    "source_seconds": pts,
                    "bbox_pixels": predicted,
                    "width": width,
                    "height": height,
                    "frame": frame,
                }
            )
            previous_signature = signature
            if len(block) >= 15:
                ious = _validate_block(block)
                if ious is None:
                    loss_reason = "forward_backward_inconsistent"
                    break
                _accept_block(block, ious, accepted)
                anchor = block[-1]
                block = [{key: value for key, value in anchor.items()}]
        if loss_reason is None and len(block) > 1:
            ious = _validate_block(block)
            if ious is None:
                loss_reason = "forward_backward_inconsistent"
            else:
                _accept_block(block, ious, accepted)

    if len(accepted) < 2:
        raise TrackingError("Fewer than two frames passed forward/backward validation")
    status = (
        "complete"
        if loss_reason is None and accepted[-1]["frame_index"] == stop_index - 1
        else "needs_reseed"
    )
    if status == "needs_reseed" and loss_reason is None:
        loss_reason = "requested_window_not_fully_validated"
    valid_end = min(end_seconds, accepted[-1]["source_seconds"] + 1 / fps)
    samples = _normalize_samples(accepted)
    seed_radii = [initial_bbox[2] * 0.9, initial_bbox[3] * 0.18]
    canonical_keyframes = [
        {
            "time_seconds": sample["source_seconds"],
            "points": [sample["anchor"], seed_radii],
        }
        for sample in samples
    ]
    source_metadata = {
        "duration_seconds": media["duration"],
        "width": width,
        "height": height,
        "fps": fps,
        "sample_aspect_ratio": media["sample_aspect_ratio"],
        "has_audio": media["has_audio"],
    }
    result = {
        "backend": "opencv-csrt",
        "cv2_version": cv2.__version__,
        "requested_window": {
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
        },
        "exact_seed": {
            "source_frame_index": first_index,
            "source_seconds": accepted[0]["source_seconds"],
            "bbox_normalized_xywh": list(initial_bbox),
        },
        "actual_first_seconds": accepted[0]["source_seconds"],
        "actual_last_seconds": accepted[-1]["source_seconds"],
        "valid_end_seconds": valid_end,
        "frame_count": len(samples),
        "status": status,
        "loss_reason": loss_reason,
        "review_required": True,
        "source_metadata": source_metadata,
        "samples": samples,
        "canonical_keyframes": canonical_keyframes,
        "fixed_radii": seed_radii,
        "limitations": (
            "Forward/backward IoU is a consistency heuristic, not calibrated identity confidence. "
            "Crossings, occlusion, and target identity remain unverified; no re-identification "
            "or future extrapolation is performed. Human review is required."
        ),
    }
    json_text = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    expected_preview_frames = stop_index - first_index

    try:
        with tempfile.TemporaryDirectory(
            prefix=".tracking-", dir=directory
        ) as temporary:
            work = Path(temporary)
            raw_preview = work / "preview-raw.mp4"
            staged_preview = work / "tracking.preview.mp4"
            staged_json = work / "tracking.json"
            _render_preview(
                cv2,
                source,
                raw_preview,
                first_index,
                stop_index,
                fps,
                width,
                height,
                accepted,
                valid_end,
                status,
                cancelled,
            )
            _encode_and_verify_preview(
                cv2,
                raw_preview,
                staged_preview,
                media,
                expected_preview_frames,
                cancelled,
            )
            staged_json.write_text(json_text, encoding="utf-8")
            for path in (staged_json, staged_preview):
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
            _publish_pair((staged_json, staged_preview), targets, cancelled)
    except TrackingError:
        raise
    except _rendering.RenderError as exc:
        if "cancel" in str(exc).lower():
            raise TrackingError("Tracking cancelled") from exc
        raise TrackingError(f"Diagnostic preview failed: {exc}") from exc
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise TrackingError(f"Tracking output failed: {exc}") from exc
    return result


def track_target(
    source: Path,
    start_seconds: float,
    end_seconds: float,
    initial_bbox: list,
    output_dir: Path,
    *,
    cancelled=None,
) -> dict:
    """Track one seeded box conservatively and publish a review-only preview."""
    if cancelled is not None and not callable(cancelled):
        raise TrackingError("Cancellation must be a callable")
    try:
        return _track_target(
            source,
            start_seconds,
            end_seconds,
            initial_bbox,
            output_dir,
            cancelled,
        )
    except TrackingError:
        raise
    except _rendering.RenderError as exc:
        if "cancel" in str(exc).lower():
            raise TrackingError("Tracking cancelled") from exc
        raise TrackingError(f"Tracking media operation failed: {exc}") from exc
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise TrackingError(f"Tracking failed: {exc}") from exc
