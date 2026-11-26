"""Camera-motion estimation and pitch-anchored keyframe generation."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

# Consecutive frames the tracker may coast on the last matrix before giving up.
_MAX_LOST_RUN = 3


class CameraError(ValueError):
    """Raised for missing OpenCV/NumPy dependencies or invalid camera motion."""


@dataclass(frozen=True)
class MotionEstimate:
    """Per-frame camera homography from reference frame 0 to frame i."""

    frame_index: int
    matrix: Any
    inliers: int
    matches: int
    confidence: float
    scene_cut: bool
    usable: bool


@dataclass(frozen=True)
class WarpResult:
    """Normalized points warped by a homography plus out-of-bounds report."""

    points: list[list[float]]
    in_bounds: bool
    out_of_bounds_indices: list[int]


class FrameSequence(NamedTuple):
    """Downscaled BGR frames and their timestamps in seconds."""

    frames: list[Any]
    timestamps: list[float]


def _load_cv2():
    try:
        import cv2
    except (ImportError, OSError) as exc:
        raise CameraError(
            "Camera motion estimation requires the optional 'tracking' extra (cv2)"
        ) from exc
    return cv2


def _load_numpy():
    try:
        import numpy as np
    except (ImportError, OSError) as exc:
        raise CameraError(
            "Camera motion estimation requires the optional 'tracking' extra (numpy)"
        ) from exc
    return np


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _validate_points(
    points: Sequence[Sequence[float]], *, require_unit_box: bool = False
) -> list[list[float]]:
    if (
        isinstance(points, (str, bytes))
        or not isinstance(points, Sequence)
        or not points
    ):
        raise CameraError("points must be a nonempty sequence of [u, v] pairs")
    cleaned: list[list[float]] = []
    for pair in points:
        if (
            isinstance(pair, (str, bytes))
            or not isinstance(pair, Sequence)
            or len(pair) != 2
            or not (_finite_number(pair[0]) and _finite_number(pair[1]))
        ):
            raise CameraError("Each point must be a finite [u, v] pair")
        u, v = float(pair[0]), float(pair[1])
        if require_unit_box and not (0.0 <= u <= 1.0 and 0.0 <= v <= 1.0):
            raise CameraError("Reference points must lie within [0, 1]")
        cleaned.append([u, v])
    return cleaned


def pitch_mask(frame_bgr: Any) -> Any:
    """Return a uint8 mask keeping grass turf and white lines while excluding players."""
    cv2 = _load_cv2()
    np = _load_numpy()
    hsv = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)
    green = cv2.inRange(hsv, (28, 30, 25), (95, 255, 255))
    white = cv2.inRange(hsv, (0, 0, 170), (180, 65, 255))
    pitch = cv2.bitwise_or(green, white)
    non_pitch = cv2.bitwise_not(pitch)
    kernel = np.ones((5, 5), dtype=np.uint8)
    dilated_non_pitch = cv2.dilate(non_pitch, kernel, iterations=1)
    return cv2.bitwise_not(dilated_non_pitch)


def _create_detector(cv2: Any, detector: str):
    choice = detector.casefold()
    if choice == "auto":
        if hasattr(cv2, "SIFT_create"):
            return cv2.SIFT_create(nfeatures=800, contrastThreshold=0.025), cv2.NORM_L2
        if hasattr(cv2, "xfeatures2d_AKAZE"):
            return cv2.xfeatures2d_AKAZE.create(threshold=0.0005), cv2.NORM_HAMMING
        return cv2.ORB_create(nfeatures=1500, fastThreshold=8), cv2.NORM_HAMMING
    if choice == "sift" and hasattr(cv2, "SIFT_create"):
        return cv2.SIFT_create(nfeatures=800, contrastThreshold=0.025), cv2.NORM_L2
    if choice == "akaze" and hasattr(cv2, "xfeatures2d_AKAZE"):
        return cv2.xfeatures2d_AKAZE.create(threshold=0.0005), cv2.NORM_HAMMING
    if choice == "orb" and hasattr(cv2, "ORB_create"):
        return cv2.ORB_create(nfeatures=1500, fastThreshold=8), cv2.NORM_HAMMING
    raise CameraError(f"Unsupported or unavailable feature detector: {detector}")


def _scene_signature(cv2: Any, gray: Any):
    small = cv2.resize(gray, (64, 36), interpolation=cv2.INTER_AREA)
    histogram = cv2.calcHist([small], [0], None, [32], [0, 256])
    cv2.normalize(histogram, histogram, alpha=1.0, norm_type=cv2.NORM_L1)
    return small, histogram


def _appearance_changed(cv2: Any, previous_sig: Any, current_sig: Any) -> bool:
    mean_diff = float(cv2.mean(cv2.absdiff(previous_sig[0], current_sig[0]))[0])
    hist_dist = float(
        cv2.compareHist(previous_sig[1], current_sig[1], cv2.HISTCMP_BHATTACHARYYA)
    )
    return mean_diff >= 35.0 or hist_dist >= 0.45


def _sane_step_homography(np: Any, matrix: Any, width: int, height: int) -> bool:
    if matrix is None or matrix.shape != (3, 3) or not np.all(np.isfinite(matrix)):
        return False
    if abs(float(matrix[2, 2])) <= 1e-8:
        return False
    normed = matrix / float(matrix[2, 2])
    det2 = float(np.linalg.det(normed[:2, :2]))
    if not 0.25 <= det2 <= 4.0:
        return False
    return not (
        abs(float(normed[2, 0])) * width >= 0.5
        or abs(float(normed[2, 1])) * height >= 0.5
    )


def _validate_frame(np: Any, frame: Any) -> tuple[int, int]:
    arr = np.asarray(frame)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise CameraError("Each frame must be a 3-channel BGR numpy array")
    height, width = int(arr.shape[0]), int(arr.shape[1])
    if width < 16 or height < 16:
        raise CameraError("Frames must be at least 16x16 pixels")
    return width, height


def _apply_mask(
    np: Any, mask_fn: Callable[[Any], Any] | None, frame: Any, width: int, height: int
) -> Any:
    if mask_fn is None:
        return None
    mask = np.asarray(mask_fn(frame))
    if mask.ndim != 2 or mask.shape != (height, width):
        raise CameraError(
            "mask_fn must return a 2D mask matching frame height and width"
        )
    return mask.astype(np.uint8, copy=False)


def estimate_motion(
    frames_bgr: Sequence[Any],
    mask_fn: Callable[[Any], Any] | None = None,
    *,
    detector: str = "auto",
    reproj_threshold: float = 2.5,
    min_inliers: int = 10,
) -> list[MotionEstimate]:
    """Estimate per-frame normalized homographies H_i mapping frame 0 -> frame i."""
    cv2 = _load_cv2()
    np = _load_numpy()
    if not frames_bgr:
        raise CameraError("frames_bgr must contain at least one frame")
    if not _finite_number(reproj_threshold) or reproj_threshold <= 0:
        raise CameraError("reproj_threshold must be a positive finite number")
    if type(min_inliers) is not int or min_inliers < 4:
        raise CameraError("min_inliers must be an integer >= 4")

    feature_det, norm_type = _create_detector(cv2, detector)
    matcher = cv2.BFMatcher(norm_type)

    w0, h0 = _validate_frame(np, frames_bgr[0])
    gray0 = cv2.cvtColor(frames_bgr[0], cv2.COLOR_BGR2GRAY)
    mask0 = _apply_mask(np, mask_fn, frames_bgr[0], w0, h0)
    kps_anchor, des_anchor = feature_det.detectAndCompute(gray0, mask0)
    sig_anchor = _scene_signature(cv2, gray0)

    h_anchor_norm = np.eye(3, dtype=np.float64)
    last_usable_norm = h_anchor_norm.copy()
    anchor_width, anchor_height = w0, h0

    results: list[MotionEstimate] = [
        MotionEstimate(
            frame_index=0,
            matrix=h_anchor_norm.copy(),
            inliers=0,
            matches=0,
            confidence=1.0,
            scene_cut=False,
            usable=True,
        )
    ]

    cut_seen = False
    lost_run = 0
    for index in range(1, len(frames_bgr)):
        if cut_seen:
            results.append(
                MotionEstimate(
                    frame_index=index,
                    matrix=None,
                    inliers=0,
                    matches=0,
                    confidence=0.0,
                    scene_cut=False,
                    usable=False,
                )
            )
            continue

        frame = frames_bgr[index]
        width, height = _validate_frame(np, frame)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mask = _apply_mask(np, mask_fn, frame, width, height)
        sig_cur = _scene_signature(cv2, gray)
        kps_cur, des_cur = feature_det.detectAndCompute(gray, mask)

        good = []
        if (
            des_anchor is not None
            and des_cur is not None
            and len(kps_anchor) >= 4
            and len(kps_cur) >= 4
        ):
            knn = matcher.knnMatch(des_anchor, des_cur, k=2)
            for pair in knn:
                if len(pair) == 2:
                    first, second = pair
                    if first.distance < 0.75 * second.distance:
                        good.append(first)

        h_step_px = None
        inliers = 0
        if len(good) >= 4:
            src_pts = np.array(
                [kps_anchor[m.queryIdx].pt for m in good], dtype=np.float32
            ).reshape(-1, 1, 2)
            dst_pts = np.array(
                [kps_cur[m.trainIdx].pt for m in good], dtype=np.float32
            ).reshape(-1, 1, 2)
            h_step_px, inlier_mask = cv2.findHomography(
                src_pts,
                dst_pts,
                cv2.RANSAC,
                float(reproj_threshold),
                maxIters=2000,
                confidence=0.995,
            )
            if inlier_mask is not None:
                inliers = int(inlier_mask.sum())

        sane = h_step_px is not None and _sane_step_homography(
            np, h_step_px, width, height
        )
        inlier_ratio = inliers / max(1, len(good))
        appearance_jump = _appearance_changed(cv2, sig_anchor, sig_cur)
        is_cut = (
            appearance_jump
            and (not sane or inliers < min_inliers or inlier_ratio < 0.25)
        ) or (len(good) >= 12 and inlier_ratio < 0.15 and inliers < min_inliers)

        if is_cut:
            cut_seen = True
            results.append(
                MotionEstimate(
                    frame_index=index,
                    matrix=None,
                    inliers=inliers,
                    matches=len(good),
                    confidence=0.0,
                    scene_cut=True,
                    usable=False,
                )
            )
            continue

        if h_step_px is None or not sane or inliers < min_inliers:
            lost_run += 1
            if lost_run > _MAX_LOST_RUN:
                # Tracking is lost for good: hold-last-matrix guesses would make
                # drawings drift silently, so retire the whole lost run.
                first_lost = len(results) - (lost_run - 1)
                for position in range(first_lost, len(results)):
                    results[position] = MotionEstimate(
                        frame_index=position,
                        matrix=None,
                        inliers=0,
                        matches=0,
                        confidence=0.0,
                        scene_cut=position == first_lost,
                        usable=False,
                    )
                cut_seen = True
                results.append(
                    MotionEstimate(
                        frame_index=index,
                        matrix=None,
                        inliers=inliers,
                        matches=len(good),
                        confidence=0.0,
                        scene_cut=False,
                        usable=False,
                    )
                )
                continue
            results.append(
                MotionEstimate(
                    frame_index=index,
                    matrix=last_usable_norm.copy(),
                    inliers=inliers,
                    matches=len(good),
                    confidence=0.0,
                    scene_cut=False,
                    usable=True,
                )
            )
            continue

        lost_run = 0
        to_norm = np.diag([1.0 / width, 1.0 / height, 1.0])
        from_norm = np.diag([float(anchor_width), float(anchor_height), 1.0])
        h_step_norm = to_norm @ h_step_px.astype(np.float64) @ from_norm
        h_cur_norm = h_step_norm @ h_anchor_norm
        h_cur_norm = h_cur_norm / float(h_cur_norm[2, 2])

        confidence = min(
            1.0, max(0.05, 0.6 * inlier_ratio + 0.4 * min(inliers, 80) / 80.0)
        )
        h_anchor_norm = h_cur_norm
        last_usable_norm = h_cur_norm
        kps_anchor, des_anchor = kps_cur, des_cur
        sig_anchor = sig_cur
        anchor_width, anchor_height = width, height

        results.append(
            MotionEstimate(
                frame_index=index,
                matrix=h_cur_norm.copy(),
                inliers=inliers,
                matches=len(good),
                confidence=float(confidence),
                scene_cut=False,
                usable=True,
            )
        )
    return results


def warp_points_checked(
    matrix: Any,
    points: Sequence[Sequence[float]],
    *,
    clamp: bool = False,
    eps: float = 1e-6,
) -> WarpResult:
    """Apply normalized homography H to points, checking horizon and [0, 1] bounds."""
    np = _load_numpy()
    if matrix is None:
        raise CameraError("Homography matrix is unusable (None)")
    if not _finite_number(eps) or eps <= 0:
        raise CameraError("eps must be a positive finite number")
    h = np.asarray(matrix, dtype=np.float64)
    if h.shape != (3, 3) or not np.all(np.isfinite(h)):
        raise CameraError("matrix must be a finite 3x3 homography")
    cleaned = _validate_points(points)

    warped: list[list[float]] = []
    out_indices: list[int] = []
    for index, (u, v) in enumerate(cleaned):
        vec = h @ np.array([u, v, 1.0], dtype=np.float64)
        w = float(vec[2])
        if not math.isfinite(w) or w <= eps:
            raise CameraError(
                "Warped point projects behind or onto the horizon (w <= eps)"
            )
        wu = float(vec[0]) / w
        wv = float(vec[1]) / w
        if not (math.isfinite(wu) and math.isfinite(wv)):
            raise CameraError("Warped point coordinates are not finite")
        if not (0.0 <= wu <= 1.0 and 0.0 <= wv <= 1.0):
            out_indices.append(index)
            if clamp:
                wu = min(1.0, max(0.0, wu))
                wv = min(1.0, max(0.0, wv))
        warped.append([wu, wv])
    return WarpResult(
        points=warped,
        in_bounds=not out_indices,
        out_of_bounds_indices=out_indices,
    )


def warp_points(
    matrix: Any,
    points: Sequence[Sequence[float]],
    *,
    clamp: bool = False,
    eps: float = 1e-6,
) -> list[list[float]]:
    """Apply normalized homography H to points, rejecting points behind the horizon."""
    return warp_points_checked(matrix, points, clamp=clamp, eps=eps).points


def read_frames(
    path: str | Path,
    start_seconds: float,
    end_seconds: float,
    step: int = 1,
    max_frames: int = 900,
    *,
    max_width: int = 640,
) -> FrameSequence:
    """Decode sampled frames in [start_seconds, end_seconds], downscaled to <= max_width."""
    cv2 = _load_cv2()
    if not (_finite_number(start_seconds) and _finite_number(end_seconds)):
        raise CameraError("start_seconds and end_seconds must be finite numbers")
    start = float(start_seconds)
    end = float(end_seconds)
    if start < 0.0 or not start < end:
        raise CameraError("Time window must satisfy 0 <= start_seconds < end_seconds")
    if type(step) is not int or step < 1:
        raise CameraError("step must be a positive integer")
    if type(max_frames) is not int or max_frames < 1:
        raise CameraError("max_frames must be a positive integer")
    if type(max_width) is not int or max_width < 16:
        raise CameraError("max_width must be an integer >= 16")

    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise CameraError(f"OpenCV could not open video: {path}")
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        if not math.isfinite(fps) or fps <= 0:
            raise CameraError("Video source does not report a valid FPS")

        first_index = max(0, math.floor(start * fps + 1e-9))
        if first_index > 0:
            capture.set(cv2.CAP_PROP_POS_FRAMES, first_index)
            pos = float(capture.get(cv2.CAP_PROP_POS_FRAMES))
            if not math.isfinite(pos) or pos < 0 or pos > first_index + 0.5:
                capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                pos = 0.0
            while pos < first_index - 0.5:
                ok, _ = capture.read()
                if not ok:
                    break
                pos += 1.0

        frames: list[Any] = []
        timestamps: list[float] = []
        sample_offset = 0
        frame_index = first_index
        tolerance = max(0.002, 0.25 / fps)
        previous_ts: float | None = None
        while len(frames) < max_frames:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            pts_msec = float(capture.get(cv2.CAP_PROP_POS_MSEC))
            ts_est = frame_index / fps
            if math.isfinite(pts_msec) and pts_msec >= 0:
                pts = pts_msec / 1000.0
                if abs(pts - ts_est) <= max(tolerance, 0.5 / fps) or frame_index == 0:
                    ts = pts
                else:
                    ts = ts_est
            else:
                ts = ts_est
            if ts > end + 1e-6:
                break
            if ts >= start - 1e-6 and sample_offset % step == 0:
                if previous_ts is not None and ts <= previous_ts:
                    ts = max(previous_ts + 1e-6, ts_est)
                if frame.shape[1] > max_width:
                    scale = max_width / float(frame.shape[1])
                    new_height = max(1, round(frame.shape[0] * scale))
                    frame = cv2.resize(
                        frame, (max_width, new_height), interpolation=cv2.INTER_AREA
                    )
                frames.append(frame)
                timestamps.append(float(ts))
                previous_ts = float(ts)
            sample_offset += 1
            frame_index += 1
    finally:
        capture.release()

    if not frames:
        raise CameraError("No frames decoded in requested time window")
    return FrameSequence(frames=frames, timestamps=timestamps)


def motion_keyframes(
    source_path: str | Path,
    start_seconds: float,
    end_seconds: float,
    points: Sequence[Sequence[float]],
    fps_step: int = 1,
    *,
    mask_fn: Callable[[Any], Any] | None = None,
    max_frames: int = 900,
) -> dict[str, Any]:
    """Track static reference points across a clip into renderer keyframes."""
    ref_points = _validate_points(points, require_unit_box=True)
    frames, timestamps = read_frames(
        source_path,
        start_seconds,
        end_seconds,
        step=fps_step,
        max_frames=max_frames,
    )
    estimates = estimate_motion(frames, mask_fn=mask_fn)
    start = float(start_seconds)
    end = float(end_seconds)

    keyframes: list[dict[str, Any]] = []
    stopped_reason: str | None = None
    previous_time = -math.inf

    for timestamp, estimate in zip(timestamps, estimates, strict=True):
        if not estimate.usable:
            stopped_reason = "scene_cut" if estimate.scene_cut else "tracking_stopped"
            break
        try:
            warped = warp_points_checked(estimate.matrix, ref_points, clamp=False)
        except CameraError:
            stopped_reason = "horizon_crossed"
            break
        if not warped.in_bounds:
            stopped_reason = "points_left_frame"
            break
        t = round(max(start, min(end, float(timestamp))), 6)
        if t <= previous_time:
            continue
        keyframes.append(
            {
                "time_seconds": t,
                "points": [[round(u, 6), round(v, 6)] for u, v in warped.points],
            }
        )
        previous_time = t

    return {
        "keyframes": keyframes,
        "stopped_reason": stopped_reason,
        "frames_processed": len(frames),
        "usable_frames": len(keyframes),
    }
