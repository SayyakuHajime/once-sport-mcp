"""Native export queue/worker integration using a fake Once renderer seam."""

import hashlib
import json
import shutil
import subprocess

import pytest

from once_sport_mcp.workflow import Workflow, WorkflowError


def _ready_project(workflow, drawings=None):
    created = workflow.create_project("Native fixture", "https://youtu.be/abcdefghijk")
    task = workflow.task_dir(created["job_id"])
    output = task / "output"
    output.mkdir()
    source = output / "source.mp4"
    source.write_bytes(b"synthetic staged source")
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    events = [
        {
            "event_id": "native-event",
            "period": 1,
            "start_seconds": 0,
            "end_seconds": 1,
            "team": "Home",
            "phase": "attacking",
            "action": "Pass",
        }
    ]
    with workflow.connect() as con:
        project = workflow._get("projects", created["project_id"], con)
        project.update(
            media={
                "duration": 1,
                "width": 64,
                "height": 36,
                "fps": 4,
                "has_audio": False,
                "sample_aspect_ratio": 1,
            },
            source_path=str(source),
            source_digest=source_digest,
        )
        workflow._put(con, "projects", project)
        ingest = workflow._get("jobs", created["job_id"], con)
        ingest["status"] = "completed"
        workflow._put(con, "jobs", ingest)
        evidence = workflow._job(
            con, created["project_id"], "evidence", {"source_digest": source_digest}
        )
        evidence["status"] = "completed"
        workflow._put(con, "jobs", evidence)
    proposal = workflow.submit_analysis(
        created["project_id"],
        events,
        drawings or [],
        "# Native synthetic report",
        [evidence["id"]],
    )
    return (
        workflow.get_project(created["project_id"]),
        proposal,
        events,
        drawings or [],
        source,
    )


def _approve(workflow, project, proposal):
    workflow.review_analysis(
        project["id"],
        proposal["analysis_id"],
        proposal["digest"],
        True,
        "Synthetic reviewer",
    )


def _native_drawings():
    return [
        {
            "id": "native-arrow",
            "event_id": "native-event",
            "kind": "arrow",
            "start_seconds": 0,
            "end_seconds": 1,
            "points": [[0.1, 0.2], [0.8, 0.7]],
        }
    ]


def _valid_fake_render(
    source, events, drawings, destination, *, once_executable, cancelled
):
    assert cancelled() is False
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x36:r=4:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(destination),
        ],
        check=True,
        capture_output=True,
    )
    native_original = destination.with_suffix(".native.mp4")
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x36:r=4:d=1",
            "-c:v",
            "mpeg4",
            str(native_original),
        ],
        check=True,
        capture_output=True,
    )
    native_analysis = destination.with_suffix(".once.xml")
    native_analysis.write_text("<analysis />", encoding="utf-8")
    sidecar = destination.with_suffix(".drawings.json")
    manifest = {
        "semantics": "native-once-headless",
        "source": str(source.resolve()),
        "destination": str(destination),
        "drawings_sidecar": str(sidecar),
        "native_original": str(native_original),
        "native_analysis": str(native_analysis),
        "clips": [
            {
                "event_id": event["event_id"],
                "source_start_seconds": event["start_seconds"],
                "source_end_seconds": event["end_seconds"],
            }
            for event in events
        ],
        "output": {"video_codec": "h264", "width": 64, "height": 36},
        "requested_native_settings": {"framerate": 25, "includeDrawings": True},
    }
    sidecar.write_text(
        json.dumps(
            {
                "semantics": "native-once-headless",
                "drawings": drawings,
                "manifest": manifest,
            }
        ),
        encoding="utf-8",
    )
    assert once_executable.is_absolute()
    return manifest


def _queue_ready_native(tmp_path, drawings=None):
    executable = tmp_path / "trusted" / "OnceSport"
    workflow = Workflow(tmp_path / "workspace", native_once_executable=executable)
    project, proposal, events, submitted_drawings, source = _ready_project(
        workflow, drawings
    )
    _approve(workflow, project, proposal)
    queued = workflow.queue_native_export(
        project["id"], proposal["analysis_id"], proposal["digest"]
    )
    return (
        workflow,
        project,
        proposal,
        events,
        submitted_drawings,
        source,
        executable,
        queued,
    )


def test_native_export_requires_configuration_latest_approval_and_exact_digest(
    tmp_path,
):
    unconfigured = Workflow(tmp_path / "unconfigured")
    project, proposal, _events, _drawings, _source = _ready_project(unconfigured)
    with pytest.raises(WorkflowError) as missing:
        unconfigured.queue_native_export(
            project["id"], proposal["analysis_id"], proposal["digest"]
        )
    assert missing.value.code == "native_not_configured"

    workflow = Workflow(
        tmp_path / "configured", native_once_executable=tmp_path / "OnceSport"
    )
    project, proposal, _events, _drawings, _source = _ready_project(workflow)
    with pytest.raises(WorkflowError) as pending:
        workflow.queue_native_export(
            project["id"], proposal["analysis_id"], proposal["digest"]
        )
    assert pending.value.code == "approval_required"
    with pytest.raises(WorkflowError) as stale:
        workflow.queue_native_export(project["id"], proposal["analysis_id"], "0" * 64)
    assert stale.value.code == "approval_required"
    workflow.review_analysis(
        project["id"],
        proposal["analysis_id"],
        proposal["digest"],
        False,
        "Synthetic reviewer",
    )
    with pytest.raises(WorkflowError) as rejected:
        workflow.queue_native_export(
            project["id"], proposal["analysis_id"], proposal["digest"]
        )
    assert rejected.value.code == "approval_required"
    assert not [
        job for job in workflow.list_jobs(project["id"]) if job["kind"] == "export"
    ]


def test_native_queue_rejects_unsupported_plan_before_persisting_job(tmp_path):
    workflow = Workflow(
        tmp_path / "workspace", native_once_executable=tmp_path / "OnceSport"
    )
    label = {
        "id": "unsupported-label",
        "event_id": "native-event",
        "kind": "label",
        "start_seconds": 0,
        "end_seconds": 1,
        "points": [[0.5, 0.5]],
        "text": "Not a native drawing",
    }
    project, proposal, _events, _drawings, _source = _ready_project(workflow, [label])
    _approve(workflow, project, proposal)

    with pytest.raises(WorkflowError) as unsupported:
        workflow.queue_native_export(
            project["id"], proposal["analysis_id"], proposal["digest"]
        )

    assert unsupported.value.code == "invalid_render_plan"
    assert not [
        job for job in workflow.list_jobs(project["id"]) if job["kind"] == "export"
    ]


def test_native_queue_persists_exact_backend_payload_and_retry_keeps_it(tmp_path):
    workflow, _project, proposal, _events, _drawings, _source, executable, queued = (
        _queue_ready_native(tmp_path)
    )
    original = workflow.get_job(queued["job_id"])
    assert original["kind"] == "export"
    assert original["payload"] == {
        "analysis_id": proposal["analysis_id"],
        "digest": proposal["digest"],
        "backend": "native",
    }
    assert str(executable) not in json.dumps(original["payload"])

    workflow.cancel_job(queued["job_id"])
    retried = workflow.retry_job(queued["job_id"])
    retry = workflow.get_job(retried["job_id"])
    assert retry["payload"] == original["payload"]
    assert retry["retry_of"] == queued["job_id"]
    assert retry["kind"] == "export"


def test_worker_native_bundle_preserves_approved_content_and_verified_artifacts(
    tmp_path, monkeypatch
):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required for a synthetic valid output video")
    from once_sport_mcp import native_rendering, rendering
    from once_sport_mcp.worker import run_worker
    from once_sport_mcp.workflow import digest_file

    drawings = _native_drawings()
    workflow, project, proposal, events, drawings, source, executable, queued = (
        _queue_ready_native(tmp_path, drawings)
    )
    calls = []

    def render(source_arg, events_arg, drawings_arg, destination, **kwargs):
        calls.append(
            (
                source_arg,
                events_arg,
                drawings_arg,
                destination,
                kwargs["once_executable"],
            )
        )
        return _valid_fake_render(
            source_arg,
            events_arg,
            drawings_arg,
            destination,
            once_executable=kwargs["once_executable"],
            cancelled=kwargs["cancelled"],
        )

    monkeypatch.setattr(native_rendering, "render_native_reel", render)
    monkeypatch.setattr(
        rendering,
        "render_reel",
        lambda *args, **kwargs: pytest.fail(
            "native export fell back to custom rendering"
        ),
    )

    run_worker(workflow.root, once=True, native_once_executable=executable)

    job = workflow.get_job(queued["job_id"])
    assert job["status"] == "completed"
    assert len(calls) == 1
    assert calls[0][0] == source
    assert calls[0][1] == events
    assert calls[0][2] == drawings
    assert calls[0][3].name == "reel.mp4"
    assert calls[0][4] == executable

    output = workflow.task_dir(queued["job_id"]) / "output"
    assert (output / "analysis.md").read_text() == "# Native synthetic report"
    assert (
        json.loads((output / "proposal.json").read_text())
        == workflow.get_analysis(proposal["analysis_id"])["proposal"]
    )
    sidecar = json.loads((output / "reel.drawings.json").read_text())
    assert sidecar["semantics"] == "native-once-headless"
    assert sidecar["drawings"] == drawings
    assert sidecar["manifest"]["destination"] == "reel.mp4"
    assert sidecar["manifest"]["drawings_sidecar"] == "reel.drawings.json"
    assert sidecar["manifest"]["native_original"] == "reel.native.mp4"
    assert sidecar["manifest"]["native_analysis"] == "reel.once.xml"
    assert sidecar["manifest"]["source"] == str(source)

    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["source_digest"] == project["source_digest"]
    assert manifest["native_import_verified"] is False
    assert manifest["gui_project_list_import_verified"] is False
    assert manifest["render_engine"] == "once_cli_native_headless"
    assert manifest["native_original"]["file"] == "reel.native.mp4"
    assert manifest["compatible_reel"]["file"] == "reel.mp4"
    assert set(manifest["files"]) == {
        "analysis.md",
        "events.csv",
        "proposal.json",
        "reel.drawings.json",
        "reel.mp4",
        "reel.native.mp4",
        "reel.once.xml",
        "review.json",
    }
    assert all(
        manifest["files"][name] == digest_file(output / name)
        for name in manifest["files"]
    )
    artifacts = [
        artifact
        for artifact in workflow.list_artifacts(project["id"])
        if artifact["job_id"] == queued["job_id"]
    ]
    assert {artifact["kind"] for artifact in artifacts} == {
        "reel",
        "drawings_sidecar",
        "native_original_reel",
        "native_analysis_xml",
        "report",
        "proposal",
        "review",
        "once_csv",
        "manifest",
    }
    for artifact in artifacts:
        assert workflow.get_artifact(artifact["id"]) == artifact
    assert hashlib.sha256(source.read_bytes()).hexdigest() == project["source_digest"]


@pytest.mark.parametrize("bad_output", ["unexpected", "symlink", "missing"])
def test_native_untrusted_stage_family_never_publishes(
    tmp_path, monkeypatch, bad_output
):
    workflow, project, _proposal, _events, _drawings, _source, executable, queued = (
        _queue_ready_native(tmp_path)
    )
    from once_sport_mcp import native_rendering
    from once_sport_mcp.worker import run_worker

    def render(source_arg, events, drawings, destination, **kwargs):
        _valid_fake_render(
            source_arg,
            events,
            drawings,
            destination,
            once_executable=kwargs["once_executable"],
            cancelled=kwargs["cancelled"],
        )
        if bad_output == "unexpected":
            destination.with_name("unexpected.json").write_text("extra")
        elif bad_output == "symlink":
            path = destination.with_suffix(".native.mp4")
            path.unlink()
            path.symlink_to(source_arg)
        else:
            destination.with_suffix(".once.xml").unlink()

    monkeypatch.setattr(native_rendering, "render_native_reel", render)
    run_worker(workflow.root, once=True, native_once_executable=executable)

    assert workflow.get_job(queued["job_id"])["status"] == "failed"
    assert not (workflow.task_dir(queued["job_id"]) / "output").exists()
    assert not [
        a
        for a in workflow.list_artifacts(project["id"])
        if a["job_id"] == queued["job_id"]
    ]


def test_native_worker_without_configuration_fails_closed_and_unknown_backend_is_rejected(
    tmp_path, monkeypatch
):
    workflow, project, _proposal, _events, _drawings, _source, executable, queued = (
        _queue_ready_native(tmp_path)
    )
    from once_sport_mcp import native_rendering, rendering
    from once_sport_mcp.worker import run_worker

    monkeypatch.setattr(
        native_rendering,
        "render_native_reel",
        lambda *args, **kwargs: pytest.fail(
            "native renderer should not run without worker config"
        ),
    )
    monkeypatch.setattr(
        rendering,
        "render_reel",
        lambda *args, **kwargs: pytest.fail(
            "native job must not fall back to custom renderer"
        ),
    )
    run_worker(workflow.root, once=True)
    failed = workflow.get_job(queued["job_id"])
    assert failed["status"] == "failed"
    assert failed["error_code"] == "native_not_configured"
    assert not (workflow.task_dir(queued["job_id"]) / "output").exists()

    project, proposal, _events, _drawings, _source = _ready_project(workflow)
    _approve(workflow, project, proposal)
    unknown = workflow.queue_export(
        project["id"], proposal["analysis_id"], proposal["digest"]
    )
    with workflow.connect() as con:
        job = workflow._get("jobs", unknown["job_id"], con)
        job["payload"]["backend"] = "unrecognized"
        workflow._put(con, "jobs", job)
    run_worker(workflow.root, once=True, native_once_executable=executable)
    assert workflow.get_job(unknown["job_id"])["status"] == "failed"
    assert workflow.get_job(unknown["job_id"])["error_code"] == "unknown_export_backend"
    assert not (workflow.task_dir(unknown["job_id"]) / "output").exists()


def test_native_publication_rechecks_approval_after_renderer_returns(
    tmp_path, monkeypatch
):
    workflow, project, proposal, _events, _drawings, _source, executable, queued = (
        _queue_ready_native(tmp_path)
    )
    from once_sport_mcp import native_rendering
    from once_sport_mcp.worker import run_worker

    def render(source, events, drawings, destination, **kwargs):
        _valid_fake_render(
            source,
            events,
            drawings,
            destination,
            once_executable=kwargs["once_executable"],
            cancelled=kwargs["cancelled"],
        )
        workflow.review_analysis(
            project["id"],
            proposal["analysis_id"],
            proposal["digest"],
            False,
            "Changed reviewer",
        )

    monkeypatch.setattr(native_rendering, "render_native_reel", render)
    run_worker(workflow.root, once=True, native_once_executable=executable)

    assert workflow.get_job(queued["job_id"])["status"] == "failed"
    assert workflow.get_job(queued["job_id"])["error_code"] == "approval_required"
    assert not (workflow.task_dir(queued["job_id"]) / "output").exists()
    assert not [
        a
        for a in workflow.list_artifacts(project["id"])
        if a["job_id"] == queued["job_id"]
    ]
