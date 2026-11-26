"""Tests for camera-motion estimation and pitch-anchored keyframe generation."""

from __future__ import annotations

import builtins
import itertools
from pathlib import Path

import pytest

from once_sport_mcp.rendering import validate_plan


def _make_pitch_canvas(width: int = 1600, height: int = 900):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    rng = np.random.default_rng(42)
    coarse = cv2.resize(
        rng.integers(-22, 23, (height // 5, width // 5, 3), dtype=np.int16),
        (width, height),
        interpolation=cv2.INTER_LINEAR,
    )
    fine = rng.integers(-14, 15, (height, width, 3), dtype=np.int16)
    base = np.full((height, width, 3), (45, 120, 50), dtype=np.int16) + coarse + fine
    for stripe in range(0, width, 120):
        base[:, stripe : stripe + 60, 1] += 10
    canvas = np.clip(base, 0, 255).astype(np.uint8)
    cv2.rectangle(canvas, (120, 100), (1480, 800), (240, 240, 240), 4)
    cv2.line(canvas, (800, 100), (800, 800), (240, 240, 240), 4)
    cv2.circle(canvas, (800, 450), 110, (240, 240, 240), 4)
    cv2.rectangle(canvas, (120, 260), (340, 640), (240, 240, 240), 4)
    cv2.rectangle(canvas, (1260, 260), (1480, 640), (240, 240, 240), 4)
    return canvas


def _render_pan_zoom_sequence(
    count: int = 40,
    width: int = 640,
    height: int = 360,
    tx_start: float = 200.0,
    tx_delta: float = 180.0,
    ty_start: float = 120.0,
    ty_delta: float = 70.0,
    zoom_delta: float = 0.15,
    moving_blobs: bool = True,
):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    canvas = _make_pitch_canvas()
    frames = []
    gt_norm_homographies = []
    m0 = None
    norm_to_px = np.diag([float(width), float(height), 1.0])
    px_to_norm = np.diag([1.0 / width, 1.0 / height, 1.0])
    for index in range(count):
        t = index / max(1, count - 1)
        scale = 1.0 + zoom_delta * t
        tx = tx_start + tx_delta * t
        ty = ty_start + ty_delta * t
        m_c2f = np.array(
            [
                [scale, 0.0, -tx * scale],
                [0.0, scale, -ty * scale],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        frame = cv2.warpPerspective(
            canvas, m_c2f, (width, height), flags=cv2.INTER_LINEAR
        )
        if moving_blobs:
            for bx0, by0, vx, vy, color in [
                (150, 180, 3.0, 0.5, (30, 30, 220)),
                (400, 220, -2.5, 1.0, (220, 60, 30)),
                (300, 120, 1.5, 1.8, (230, 230, 40)),
            ]:
                cv2.circle(
                    frame,
                    (round(bx0 + vx * index), round(by0 + vy * index)),
                    14,
                    color,
                    -1,
                )
        frames.append(frame)
        if m0 is None:
            m0 = m_c2f
        h_px = m_c2f @ np.linalg.inv(m0)
        h_norm = px_to_norm @ h_px @ norm_to_px
        gt_norm_homographies.append(h_norm / h_norm[2, 2])
    return frames, gt_norm_homographies


def _write_mp4(path: Path, frames, fps: float = 10.0) -> None:
    cv2 = pytest.importorskip("cv2")
    height, width = frames[0].shape[:2]
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height)
    )
    assert writer.isOpened()
    try:
        for frame in frames:
            writer.write(frame)
    finally:
        writer.release()


def test_lazy_import_raises_camera_error_when_missing(monkeypatch):
    from once_sport_mcp import camera

    real_import = builtins.__import__

    def fail_cv2(name, *args, **kwargs):
        if name == "cv2":
            raise ImportError("No module named 'cv2'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_cv2)
    with pytest.raises(camera.CameraError, match="tracking"):
        camera.estimate_motion([])


@pytest.mark.parametrize("use_pitch_mask", [False, True])
def test_estimate_motion_pan_zoom_40_frames_accuracy(use_pitch_mask):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    cv2.setNumThreads(1)
    from once_sport_mcp.camera import estimate_motion, pitch_mask, warp_points

    frames, gt_homographies = _render_pan_zoom_sequence(count=40)
    estimates = estimate_motion(frames, mask_fn=pitch_mask if use_pitch_mask else None)
    assert len(estimates) == len(frames)
    assert np.allclose(estimates[0].matrix, np.eye(3), atol=1e-9)
    assert estimates[0].confidence == pytest.approx(1.0)
    assert not estimates[0].scene_cut
    assert estimates[0].usable

    ref_points = [[0.45, 0.45], [0.55, 0.50], [0.65, 0.62], [0.50, 0.72]]
    max_px_error = 0.0
    for index, estimate in enumerate(estimates):
        assert estimate.usable
        assert not estimate.scene_cut
        assert 0.0 <= estimate.confidence <= 1.0
        if index > 0:
            assert estimate.inliers >= 20
            assert estimate.confidence > 0.0
        est_pts = warp_points(estimate.matrix, ref_points)
        gt_pts = warp_points(gt_homographies[index], ref_points)
        for (eu, ev), (gu, gv) in zip(est_pts, gt_pts, strict=True):
            px_err = float(np.hypot((eu - gu) * 640.0, (ev - gv) * 360.0))
            max_px_error = max(max_px_error, px_err)
    assert max_px_error < 2.0


def test_warp_points_horizon_and_bounds():
    np = pytest.importorskip("numpy")
    from once_sport_mcp.camera import CameraError, warp_points, warp_points_checked

    horizon_matrix = np.array(
        [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 2.0, -1.0]], dtype=np.float64
    )
    with pytest.raises(CameraError, match="horizon"):
        warp_points(horizon_matrix, [[0.5, 0.5]])

    shift = np.array(
        [[1.0, 0.0, 0.3], [0.0, 1.0, -0.2], [0.0, 0.0, 1.0]], dtype=np.float64
    )
    raw = warp_points(shift, [[0.8, 0.1], [0.2, 0.5]])
    assert raw[0][0] == pytest.approx(1.1)
    assert raw[0][1] == pytest.approx(-0.1)

    clamped = warp_points(shift, [[0.8, 0.1], [0.2, 0.5]], clamp=True)
    assert clamped[0] == [pytest.approx(1.0), pytest.approx(0.0)]
    assert clamped[1] == [pytest.approx(0.5), pytest.approx(0.3)]

    checked = warp_points_checked(shift, [[0.8, 0.1], [0.2, 0.5]], clamp=True)
    assert not checked.in_bounds
    assert checked.out_of_bounds_indices == [0]
    assert checked.points[0] == [pytest.approx(1.0), pytest.approx(0.0)]


def test_low_inliers_falls_back_to_previous_matrix():
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    cv2.setNumThreads(1)
    from once_sport_mcp.camera import estimate_motion

    frames, _ = _render_pan_zoom_sequence(count=8, moving_blobs=False)
    call_count = 0

    def drop_frame_three_mask(frame):
        nonlocal call_count
        mask = np.full(frame.shape[:2], 255, dtype=np.uint8)
        if call_count == 3:
            mask[:] = 0
        call_count += 1
        return mask

    estimates = estimate_motion(frames, mask_fn=drop_frame_three_mask)
    assert len(estimates) == len(frames)
    assert estimates[3].confidence == 0.0
    assert not estimates[3].scene_cut
    assert estimates[3].usable
    assert np.allclose(estimates[3].matrix, estimates[2].matrix)
    assert estimates[4].usable
    assert estimates[4].confidence > 0.0


def test_scene_cut_stops_tracking_and_marks_later_unusable():
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    cv2.setNumThreads(1)
    from once_sport_mcp.camera import CameraError, estimate_motion, warp_points

    frames, _ = _render_pan_zoom_sequence(count=18, moving_blobs=False)
    rng = np.random.default_rng(999)
    for idx in range(10, 18):
        unrelated = rng.integers(0, 255, (360, 640, 3), dtype=np.uint8)
        cv2.rectangle(unrelated, (80, 40), (560, 320), (220, 30, 210), -1)
        cv2.circle(unrelated, (320, 180), 90, (20, 210, 240), 6)
        frames[idx] = unrelated

    estimates = estimate_motion(frames)
    assert len(estimates) == len(frames)
    assert all(est.usable and not est.scene_cut for est in estimates[:10])
    assert estimates[10].scene_cut
    assert not estimates[10].usable
    assert estimates[10].matrix is None
    assert estimates[10].confidence == 0.0
    for est in estimates[11:]:
        assert not est.usable
        assert est.matrix is None
        assert est.confidence == 0.0
    with pytest.raises(CameraError):
        warp_points(estimates[10].matrix, [[0.5, 0.5]])


def test_read_frames_downscales_and_samples_window(tmp_path):
    cv2 = pytest.importorskip("cv2")
    pytest.importorskip("numpy")
    cv2.setNumThreads(1)
    from once_sport_mcp.camera import read_frames

    hi_res_frames, _ = _render_pan_zoom_sequence(
        count=20, width=1280, height=720, tx_delta=80.0, ty_delta=20.0, zoom_delta=0.05
    )
    clip = tmp_path / "hd.mp4"
    _write_mp4(clip, hi_res_frames, fps=10.0)

    frames, timestamps = read_frames(clip, 0.2, 1.5, step=2, max_frames=5)
    assert len(frames) == 5
    assert len(timestamps) == 5
    assert frames[0].shape == (360, 640, 3)
    assert all(0.2 - 1e-6 <= t <= 1.5 + 1e-6 for t in timestamps)
    assert all(b > a for a, b in itertools.pairwise(timestamps))


def test_motion_keyframes_stops_when_points_leave_frame_and_validates(tmp_path):
    cv2 = pytest.importorskip("cv2")
    cv2.setNumThreads(1)
    from once_sport_mcp.camera import motion_keyframes

    frames, _ = _render_pan_zoom_sequence(
        count=20,
        width=640,
        height=360,
        tx_start=200.0,
        tx_delta=260.0,
        ty_start=120.0,
        ty_delta=0.0,
        zoom_delta=0.0,
        moving_blobs=True,
    )
    clip = tmp_path / "pan_out.mp4"
    _write_mp4(clip, frames, fps=10.0)

    ref_points = [[0.20, 0.50], [0.45, 0.55]]
    result = motion_keyframes(clip, 0.0, 1.9, ref_points, fps_step=1)
    assert result["stopped_reason"] == "points_left_frame"
    keyframes = result["keyframes"]
    assert 2 <= len(keyframes) < len(frames)
    for keyframe in keyframes:
        assert len(keyframe["points"]) == 2
        for u, v in keyframe["points"]:
            assert 0.0 <= u <= 1.0
            assert 0.0 <= v <= 1.0
    times = [kf["time_seconds"] for kf in keyframes]
    assert all(b > a for a, b in itertools.pairwise(times))

    media = {
        "duration": 2.0,
        "width": 640,
        "height": 360,
        "fps": 10.0,
        "sample_aspect_ratio": 1,
        "has_audio": False,
    }
    event = {
        "event_id": "one",
        "start_seconds": 0.0,
        "end_seconds": 2.0,
        "team": "Home",
        "phase": "Attack",
        "action": "Pass",
    }
    mark = {
        "id": "mark",
        "event_id": "one",
        "kind": "arrow",
        "start_seconds": 0.0,
        "end_seconds": 2.0,
        "points": ref_points,
        "color": "#6DE0D4",
        "keyframes": keyframes,
    }
    assert validate_plan([event], [mark], media) is None


def test_motion_keyframes_full_clip_and_scene_cut(tmp_path):
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    cv2.setNumThreads(1)
    from once_sport_mcp.camera import motion_keyframes

    frames, _ = _render_pan_zoom_sequence(
        count=20,
        width=640,
        height=360,
        tx_start=220.0,
        tx_delta=90.0,
        ty_start=120.0,
        ty_delta=40.0,
        zoom_delta=0.10,
    )
    clip = tmp_path / "smooth.mp4"
    _write_mp4(clip, frames, fps=10.0)

    ref_points = [[0.40, 0.40], [0.55, 0.45], [0.50, 0.65]]
    result = motion_keyframes(clip, 0.0, 1.9, ref_points, fps_step=2)
    assert result["stopped_reason"] is None
    keyframes = result["keyframes"]
    assert len(keyframes) == 10

    media = {
        "duration": 2.0,
        "width": 640,
        "height": 360,
        "fps": 10.0,
        "sample_aspect_ratio": 1,
        "has_audio": False,
    }
    event = {
        "event_id": "one",
        "start_seconds": 0.0,
        "end_seconds": 2.0,
        "team": "Home",
        "phase": "Attack",
        "action": "Pass",
    }
    mark = {
        "id": "zone",
        "event_id": "one",
        "kind": "area",
        "start_seconds": 0.0,
        "end_seconds": 2.0,
        "points": ref_points,
        "color": "#6DE0D4",
        "keyframes": keyframes,
    }
    assert validate_plan([event], [mark], media) is None

    rng = np.random.default_rng(77)
    for idx in range(12, 20):
        frames[idx] = rng.integers(0, 255, (360, 640, 3), dtype=np.uint8)
    cut_clip = tmp_path / "cut.mp4"
    _write_mp4(cut_clip, frames, fps=10.0)
    cut_result = motion_keyframes(cut_clip, 0.0, 1.9, ref_points, fps_step=1)
    assert cut_result["stopped_reason"] == "scene_cut"
    assert len(cut_result["keyframes"]) <= 12
