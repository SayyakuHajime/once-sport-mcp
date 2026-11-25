"""Strict validation checks for the opt-in tracker core."""

import math

import pytest

from once_sport_mcp.tracking import TrackingError, validate_tracking_request


def media_info(**changes):
    value = {
        "duration": 30.0,
        "width": 320,
        "height": 180,
        "fps": 30.0,
        "sample_aspect_ratio": 1.0,
    }
    value.update(changes)
    return value


def test_request_accepts_valid_normalized_xywh():
    assert (
        validate_tracking_request(1.0, 3.0, [0.25, 0.2, 0.1, 0.2], media_info()) is None
    )


@pytest.mark.parametrize(
    ("start", "end", "bbox", "media"),
    [
        (True, 2.0, [0.1, 0.1, 0.1, 0.1], {}),
        (0.0, math.inf, [0.1, 0.1, 0.1, 0.1], {}),
        (-0.1, 2.0, [0.1, 0.1, 0.1, 0.1], {}),
        (2.0, 2.0, [0.1, 0.1, 0.1, 0.1], {}),
        (0.0, 13.0, [0.1, 0.1, 0.1, 0.1], {}),
        (0.0, 11.0, [0.1, 0.1, 0.1, 0.1], {"fps": 60.0}),
        (0.0, 2.0, [True, 0.1, 0.1, 0.1], {}),
        (0.0, 2.0, [math.nan, 0.1, 0.1, 0.1], {}),
        (0.0, 2.0, [-0.01, 0.1, 0.1, 0.1], {}),
        (0.0, 2.0, [0.9, 0.1, 0.2, 0.1], {}),
        (0.0, 2.0, [0.1, 0.1, 0.02, 0.1], {}),
        (0.0, 2.0, [0.1, 0.1, 0.1, 0.02], {}),
        (0.0, 2.0, [0.1, 0.1, 0.1], {}),
        (0.0, 2.0, [0.1, 0.1, 0.1, 0.1], {"sample_aspect_ratio": 1.2}),
        (0.0, 2.0, [0.1, 0.1, 0.1, 0.1], {"fps": math.nan}),
        (0.0, 31.0, [0.1, 0.1, 0.1, 0.1], {}),
    ],
)
def test_invalid_request_or_media_is_rejected(start, end, bbox, media):
    with pytest.raises(TrackingError):
        validate_tracking_request(start, end, bbox, media_info(**media))


def test_real_csrt_tracks_textured_motion_and_publishes_reviewable_pair(tmp_path):
    import hashlib
    import json

    np = pytest.importorskip("numpy")

    from once_sport_mcp import tracking

    cv2 = pytest.importorskip("cv2")
    assert callable(getattr(tracking, "track_target", None)), (
        "tracking runtime is missing"
    )
    cv2.setNumThreads(1)
    source = tmp_path / "moving.mp4"
    writer = cv2.VideoWriter(
        str(source), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (320, 180)
    )
    assert writer.isOpened()
    texture = np.random.default_rng(17).integers(25, 245, (48, 32, 3), dtype=np.uint8)
    try:
        for index in range(20):
            frame = np.full((180, 320, 3), 60, dtype=np.uint8)
            x, y = 50 + 3 * index, 50 + index
            frame[y : y + 48, x : x + 32] = texture
            writer.write(frame)
    finally:
        writer.release()
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / "output"
    output.mkdir()
    result = tracking.track_target(
        source, 0.0, 2.0, [50 / 320, 50 / 180, 32 / 320, 48 / 180], output
    )
    assert result["status"] == "complete"
    assert result["review_required"] is True
    assert result["frame_count"] == 20
    assert len(result["canonical_keyframes"]) == 20
    last = result["samples"][-1]
    expected_anchor = [(50 + 3 * 19 + 16) / 320, (50 + 19 + 48) / 180]
    assert (
        math.dist(
            [last["anchor"][0] * 320, last["anchor"][1] * 180],
            [expected_anchor[0] * 320, expected_anchor[1] * 180],
        )
        < 12
    )
    assert min(sample["fb_iou"] for sample in result["samples"]) >= 0.35
    assert {path.name for path in output.iterdir()} == {
        "tracking.json",
        "tracking.preview.mp4",
    }
    assert json.loads((output / "tracking.json").read_text()) == result
    assert (output / "tracking.preview.mp4").stat().st_size > 0
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def _synthetic_clip(path, *, count=40, cut_at=None):
    np = pytest.importorskip("numpy")

    cv2 = pytest.importorskip("cv2")
    cv2.setNumThreads(1)
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (320, 180)
    )
    assert writer.isOpened()
    texture = np.random.default_rng(23).integers(25, 245, (48, 32, 3), dtype=np.uint8)
    try:
        for index in range(count):
            if cut_at is not None and index >= cut_at:
                frame = np.full((180, 320, 3), 245, dtype=np.uint8)
            else:
                frame = np.full((180, 320, 3), 60, dtype=np.uint8)
                x, y = 50 + 3 * index, 50 + index
                frame[y : y + 48, x : x + 32] = texture
            writer.write(frame)
    finally:
        writer.release()
    return [50 / 320, 50 / 180, 32 / 320, 48 / 180]


def test_hard_cut_discards_unvalidated_block_and_stops_overlay(tmp_path, monkeypatch):
    from once_sport_mcp import tracking

    cv2 = pytest.importorskip("cv2")
    source = tmp_path / "cut.mp4"
    bbox = _synthetic_clip(source, cut_at=20)
    output = tmp_path / "output"
    output.mkdir()
    marker_positions = []
    original_marker = cv2.drawMarker

    def record_marker(*args, **kwargs):
        marker_positions.append(args[1])
        return original_marker(*args, **kwargs)

    monkeypatch.setattr(cv2, "drawMarker", record_marker)
    result = tracking.track_target(source, 0.0, 4.0, bbox, output)

    assert result["status"] == "needs_reseed"
    assert result["loss_reason"] == "scene_change"
    assert [sample["source_frame_index"] for sample in result["samples"]] == list(
        range(15)
    )
    assert len(marker_positions) == result["frame_count"]
    preview = cv2.VideoCapture(str(output / "tracking.preview.mp4"))
    try:
        assert preview.isOpened()
        assert preview.get(cv2.CAP_PROP_FRAME_COUNT) == 40
    finally:
        preview.release()


def test_reverse_inconsistency_drops_whole_block_and_bounds_memory(
    tmp_path, monkeypatch
):
    from once_sport_mcp import tracking

    source = tmp_path / "moving.mp4"
    bbox = _synthetic_clip(source)
    output = tmp_path / "output"
    output.mkdir()
    factory_calls = 0

    class DeterministicTracker:
        def __init__(self, mode):
            self.mode = mode
            self.box = None

        def init(self, frame, box):
            self.box = list(box)
            return True

        def update(self, frame):
            if self.mode == "forward":
                self.box[0] += 3
                self.box[1] += 1
            elif self.mode == "first_reverse":
                self.box[0] -= 3
                self.box[1] -= 1
            return True, tuple(self.box)

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        mode = {1: "forward", 2: "first_reverse"}.get(factory_calls, "stale_reverse")
        return DeterministicTracker(mode)

    original_normalize = tracking._normalize_samples

    def check_metadata_only(records):
        assert all("frame" not in record for record in records)
        return original_normalize(records)

    monkeypatch.setattr(tracking, "_tracker_factory", factory)
    monkeypatch.setattr(tracking, "_normalize_samples", check_metadata_only)
    result = tracking.track_target(source, 0.0, 4.0, bbox, output)

    assert result["status"] == "needs_reseed"
    assert result["loss_reason"] == "forward_backward_inconsistent"
    assert [sample["source_frame_index"] for sample in result["samples"]] == list(
        range(15)
    )
    assert result["actual_last_seconds"] == pytest.approx(1.4)
    assert result["valid_end_seconds"] == pytest.approx(1.5)


def test_cancellation_during_preview_cleans_private_stage(tmp_path, monkeypatch):
    from once_sport_mcp import tracking

    source = tmp_path / "moving.mp4"
    bbox = _synthetic_clip(source, count=20)
    output = tmp_path / "output"
    output.mkdir()
    cancelled = [False]
    original_preview = tracking._render_preview

    def cancel_in_preview(*args, **kwargs):
        cancelled[0] = True
        return original_preview(*args, **kwargs)

    monkeypatch.setattr(tracking, "_render_preview", cancel_in_preview)
    with pytest.raises(TrackingError, match="cancelled"):
        tracking.track_target(
            source, 0.0, 2.0, bbox, output, cancelled=lambda: cancelled[0]
        )
    assert list(output.iterdir()) == []
    assert list(output.glob(".tracking-*")) == []


@pytest.mark.parametrize("name", ["tracking.json", "tracking.preview.mp4"])
def test_existing_output_family_is_never_overwritten(tmp_path, name):
    from once_sport_mcp import tracking

    output = tmp_path / "output"
    output.mkdir()
    existing = output / name
    existing.write_bytes(b"keep")
    with pytest.raises(TrackingError, match="already exists"):
        tracking.track_target(
            tmp_path / "unused.mp4", 0.0, 1.0, [0.1, 0.1, 0.1, 0.1], output
        )
    assert existing.read_bytes() == b"keep"


def test_output_family_symlink_and_staged_symlink_are_refused(tmp_path):
    from once_sport_mcp import tracking

    output = tmp_path / "output"
    output.mkdir()
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("keep")
    (output / "tracking.json").symlink_to(sentinel)
    with pytest.raises(TrackingError, match="already exists"):
        tracking.track_target(
            tmp_path / "unused.mp4", 0.0, 1.0, [0.1, 0.1, 0.1, 0.1], output
        )
    assert sentinel.read_text() == "keep"

    regular = tmp_path / "regular.stage"
    regular.write_bytes(b"first")
    symlink = tmp_path / "linked.stage"
    symlink.symlink_to(sentinel)
    first_target = tmp_path / "first.out"
    second_target = tmp_path / "second.out"
    with pytest.raises(TrackingError, match="regular file"):
        tracking._publish_pair(
            (regular, symlink), (first_target, second_target), cancelled=None
        )
    assert not first_target.exists()
    assert not second_target.exists()
    assert sentinel.read_text() == "keep"
