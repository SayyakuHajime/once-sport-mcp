import hashlib
import json
import shutil
import subprocess

import pytest

from once_sport_mcp.workflow import Workflow, WorkflowError

TRACKING_MEDIA = {
    "duration": 10.0,
    "width": 64,
    "height": 48,
    "fps": 4.0,
    "sample_aspect_ratio": 1.0,
    "has_audio": False,
}
TRACKING_BBOX = [0.25, 0.25, 0.25, 0.25]


def _ready_tracking_project(workflow):
    created = workflow.create_project(
        "Tracking fixture", "https://youtu.be/abcdefghijk"
    )
    digest = hashlib.sha256(b"synthetic tracking source").hexdigest()
    with workflow.connect() as con:
        project = workflow._get("projects", created["project_id"], con)
        project.update(media=TRACKING_MEDIA, source_digest=digest)
        workflow._put(con, "projects", project)
    return created["project_id"], digest


def test_tracking_queue_requires_explicit_boolean_configuration(tmp_path):
    workflow = Workflow(tmp_path / "default")
    project_id, _ = _ready_tracking_project(workflow)

    with pytest.raises(WorkflowError) as missing:
        workflow.queue_tracking(project_id, 1, 3, TRACKING_BBOX)

    assert missing.value.code == "tracking_not_configured"
    assert not [
        job for job in workflow.list_jobs(project_id) if job["kind"] == "evidence"
    ]

    with pytest.raises(WorkflowError) as invalid:
        Workflow(tmp_path / "invalid", tracking_enabled=1)

    assert invalid.value.code == "invalid_tracking_configuration"


def test_tracking_queue_persists_exact_request_without_source_path(tmp_path):
    workflow = Workflow(tmp_path / "enabled", tracking_enabled=True)
    project_id, digest = _ready_tracking_project(workflow)

    queued = workflow.queue_tracking(project_id, 1.0, 3.0, TRACKING_BBOX)
    job = workflow.get_job(queued["job_id"])

    assert job["kind"] == "evidence"
    assert job["payload"] == {
        "mode": "tracking",
        "source_digest": digest,
        "start_seconds": 1.0,
        "end_seconds": 3.0,
        "initial_bbox": TRACKING_BBOX,
    }
    assert "source_path" not in job["payload"]
    assert "executable" not in job["payload"]


@pytest.mark.parametrize(
    "start,end,bbox",
    [
        (True, 3, TRACKING_BBOX),
        (1, float("nan"), TRACKING_BBOX),
        (-1, 3, TRACKING_BBOX),
        (1, 13, TRACKING_BBOX),
        (1, 3, (0.25, 0.25, 0.25, 0.25)),
        (1, 3, [True, 0.25, 0.25, 0.25]),
        (1, 3, [0.9, 0.25, 0.25, 0.25]),
    ],
)
def test_invalid_tracking_requests_are_rejected_before_queue(
    tmp_path, start, end, bbox
):
    workflow = Workflow(tmp_path / "invalid-request", tracking_enabled=True)
    project_id, _ = _ready_tracking_project(workflow)

    with pytest.raises(WorkflowError) as invalid:
        workflow.queue_tracking(project_id, start, end, bbox)

    assert invalid.value.code == "invalid_tracking_request"
    assert not [
        job for job in workflow.list_jobs(project_id) if job["kind"] == "evidence"
    ]


def test_tracking_retry_retains_source_specific_payload(tmp_path):
    workflow = Workflow(tmp_path / "retry", tracking_enabled=True)
    project_id, _ = _ready_tracking_project(workflow)
    queued = workflow.queue_tracking(project_id, 1, 3, TRACKING_BBOX)
    original = workflow.get_job(queued["job_id"])

    workflow.cancel_job(original["id"])
    retried = workflow.retry_job(original["id"])
    retry = workflow.get_job(retried["job_id"])

    assert retry["kind"] == "evidence"
    assert retry["retry_of"] == original["id"]
    assert retry["payload"] == original["payload"]


def test_completed_tracking_job_can_be_proposed_without_autoapproval(tmp_path):
    workflow = Workflow(tmp_path / "proposal", tracking_enabled=True)
    project_id, _ = _ready_tracking_project(workflow)
    queued = workflow.queue_tracking(project_id, 1, 3, TRACKING_BBOX)
    with workflow.connect() as con:
        tracking_job = workflow._get("jobs", queued["job_id"], con)
        tracking_job["status"] = "completed"
        workflow._put(con, "jobs", tracking_job)

    events = [
        {
            "event_id": "tracking-event",
            "period": 1,
            "start_seconds": 0,
            "end_seconds": 1,
            "team": "Home",
            "phase": "attacking",
            "action": "Pass",
        }
    ]
    proposal = workflow.submit_analysis(
        project_id, events, [], "# Tracking review", [tracking_job["id"]]
    )

    analysis = workflow.get_analysis(proposal["analysis_id"])
    assert proposal["status"] == "awaiting_review"
    assert analysis["proposal"]["evidence_job_ids"] == [tracking_job["id"]]
    assert analysis["reviews"] == []


def _ready_worker_tracking_project(tmp_path):
    from once_sport_mcp.rendering import probe_media

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required for a synthetic H.264 source")
    workflow = Workflow(tmp_path / "worker-workflow", tracking_enabled=True)
    created = workflow.create_project(
        "Synthetic tracking", "https://youtu.be/abcdefghijk"
    )
    ingest_task = workflow.task_dir(created["job_id"])
    output = ingest_task / "output"
    output.mkdir()
    source = output / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x48:r=4:d=10",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    media = probe_media(source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with workflow.connect() as con:
        project = workflow._get("projects", created["project_id"], con)
        project.update(media=media, source_digest=digest, source_path=str(source))
        workflow._put(con, "projects", project)
        ingest = workflow._get("jobs", created["job_id"], con)
        ingest["status"] = "completed"
        workflow._put(con, "jobs", ingest)
    queued = workflow.queue_tracking(created["project_id"], 1.0, 3.0, TRACKING_BBOX)
    return workflow, source, media, queued


def _fake_tracking_result(source, start, end, bbox, stage, media, *, variant=None):
    fps = media["fps"]
    count = round((end - start) * fps)
    radii = [bbox[2] * 0.9, bbox[3] * 0.18]
    anchor = [bbox[0] + bbox[2] / 2, bbox[1] + bbox[3]]
    times = [start + index / fps for index in range(count)]
    samples = [
        {
            "source_frame_index": round(start * fps) + index,
            "source_seconds": moment,
            "bbox": list(bbox),
            "anchor": anchor,
            "fb_iou": 0.8,
        }
        for index, moment in enumerate(times)
    ]
    result = {
        "backend": "opencv-csrt",
        "cv2_version": "synthetic-test-seam",
        "requested_window": {"start_seconds": start, "end_seconds": end},
        "exact_seed": {
            "source_frame_index": round(start * fps),
            "source_seconds": start,
            "bbox_normalized_xywh": list(bbox),
        },
        "actual_first_seconds": times[0],
        "actual_last_seconds": times[-1],
        "valid_end_seconds": end,
        "frame_count": count,
        "status": "complete",
        "loss_reason": None,
        "review_required": True,
        "source_metadata": {
            "duration_seconds": media["duration"],
            "width": media["width"],
            "height": media["height"],
            "fps": media["fps"],
            "sample_aspect_ratio": media["sample_aspect_ratio"],
            "has_audio": media["has_audio"],
        },
        "samples": samples,
        "canonical_keyframes": [
            {"time_seconds": moment, "points": [anchor, radii]} for moment in times
        ],
        "fixed_radii": radii,
        "limitations": "Synthetic deterministic result; review required.",
    }
    preview = stage / "tracking.preview.mp4"
    if variant == "metadata":
        result["source_metadata"]["width"] += 2
    elif variant == "nonfinite":
        result["samples"][0]["fb_iou"] = float("nan")
    if variant == "fakepreview":
        preview.write_bytes(b"not a video")
    else:
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-ss",
                str(start),
                "-i",
                str(source),
                "-t",
                str(end - start),
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-r",
                str(fps),
                str(preview),
            ],
            check=True,
            capture_output=True,
        )
    if variant == "extra":
        (stage / "unexpected.json").write_text("{}", encoding="utf-8")
    elif variant == "symlink":
        preview.unlink()
        preview.symlink_to(source)
    (stage / "tracking.json").write_text(
        json.dumps(result, allow_nan=variant == "nonfinite"), encoding="utf-8"
    )
    return result


def _install_fake_tracker(monkeypatch, media, *, variant=None):
    from once_sport_mcp import tracking

    calls = []

    def track_target(source, start, end, bbox, stage, *, cancelled):
        calls.append((source, start, end, bbox, stage))
        assert cancelled() is False
        return _fake_tracking_result(
            source, start, end, bbox, stage, media, variant=variant
        )

    monkeypatch.setattr(tracking, "track_target", track_target)
    return calls


def test_tracking_worker_publishes_bound_review_only_bundle(tmp_path, monkeypatch):
    from once_sport_mcp.worker import run_worker

    workflow, source, media, queued = _ready_worker_tracking_project(tmp_path)
    calls = _install_fake_tracker(monkeypatch, media)

    run_worker(workflow.root, once=True, tracking_enabled=True)

    job = workflow.get_job(queued["job_id"])
    output = workflow.task_dir(job["id"]) / "output"
    result = json.loads((output / "tracking.json").read_text(encoding="utf-8"))
    artifacts = [
        artifact
        for artifact in workflow.list_artifacts(job["project_id"])
        if artifact["job_id"] == job["id"]
    ]
    assert job["status"] == "completed"
    assert len(calls) == 1
    assert calls[0][:4] == (source, 1.0, 3.0, TRACKING_BBOX)
    assert (
        result["source_digest"]
        == workflow.get_project(job["project_id"])["source_digest"]
    )
    assert result["tracking_job_id"] == job["id"]
    assert result["review_required"] is True
    assert result["status"] == "complete"
    assert {artifact["kind"] for artifact in artifacts} == {
        "tracking_result",
        "tracking_preview",
    }
    assert all(
        workflow.get_artifact(artifact["id"]) == artifact for artifact in artifacts
    )


@pytest.mark.parametrize(
    "variant", ["extra", "symlink", "metadata", "nonfinite", "fakepreview"]
)
def test_tracking_worker_rejects_untrusted_bundle_without_publication(
    tmp_path, monkeypatch, variant
):
    from once_sport_mcp.worker import run_worker

    workflow, _source, media, queued = _ready_worker_tracking_project(tmp_path)
    _install_fake_tracker(monkeypatch, media, variant=variant)

    run_worker(workflow.root, once=True, tracking_enabled=True)

    job = workflow.get_job(queued["job_id"])
    assert job["status"] == "failed"
    assert job["error_code"] == "unsafe_artifact"
    assert not (workflow.task_dir(job["id"]) / "output").exists()
    assert not [
        artifact
        for artifact in workflow.list_artifacts(job["project_id"])
        if artifact["job_id"] == job["id"]
    ]


def test_tracking_worker_without_configuration_has_no_fallback(tmp_path, monkeypatch):
    from once_sport_mcp import tracking
    from once_sport_mcp.worker import run_worker

    workflow, _source, _media, queued = _ready_worker_tracking_project(tmp_path)
    monkeypatch.setattr(
        tracking,
        "track_target",
        lambda *args, **kwargs: pytest.fail("tracking must not fall back"),
    )

    run_worker(workflow.root, once=True)

    job = workflow.get_job(queued["job_id"])
    assert job["status"] == "failed"
    assert job["error_code"] == "tracking_not_configured"
    assert not (workflow.task_dir(job["id"]) / "output").exists()


def test_tracking_worker_rejects_stale_source_payload(tmp_path, monkeypatch):
    from once_sport_mcp import tracking
    from once_sport_mcp.worker import run_worker

    workflow, _source, _media, queued = _ready_worker_tracking_project(tmp_path)
    monkeypatch.setattr(
        tracking,
        "track_target",
        lambda *args, **kwargs: pytest.fail("stale source must be rejected first"),
    )
    with workflow.connect() as con:
        job = workflow._get("jobs", queued["job_id"], con)
        job["payload"]["source_digest"] = "0" * 64
        workflow._put(con, "jobs", job)

    run_worker(workflow.root, once=True, tracking_enabled=True)

    failed = workflow.get_job(queued["job_id"])
    assert failed["status"] == "failed"
    assert failed["error_code"] == "stale_source"
    assert not (workflow.task_dir(failed["id"]) / "output").exists()
