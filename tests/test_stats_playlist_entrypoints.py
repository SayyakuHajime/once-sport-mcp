"""Statistics and playlist workflow entrypoints exercise the persisted job lifecycle."""

import hashlib
import json
import shutil
import subprocess
import zipfile

import pytest

from once_sport_mcp.worker import run_worker
from once_sport_mcp.workflow import Workflow, WorkflowError


def _approved_analysis(workflow, *, events=None, drawings=None, source_path=None):
    created = workflow.create_project("Synthetic match", "https://youtu.be/abcdefghijk")
    source_digest = (
        hashlib.sha256(source_path.read_bytes()).hexdigest()
        if source_path is not None and source_path.is_file()
        else hashlib.sha256(b"synthetic source identity").hexdigest()
    )
    with workflow.connect() as con:
        project = workflow._get("projects", created["project_id"], con)
        project.update(
            source_digest=source_digest,
            source_path=str(source_path) if source_path is not None else None,
            media={
                "duration": 2,
                "width": 64,
                "height": 36,
                "fps": 4,
                "sample_aspect_ratio": 1,
                "has_audio": False,
            },
        )
        workflow._put(con, "projects", project)
        ingest = workflow._get("jobs", created["job_id"], con)
        ingest["status"] = "completed"
        workflow._put(con, "jobs", ingest)
        evidence = workflow._job(
            con, project["id"], "evidence", {"source_digest": source_digest}
        )
        evidence["status"] = "completed"
        workflow._put(con, "jobs", evidence)
    if events is None:
        events = [
            {
                "event_id": "selected",
                "period": 1,
                "start_seconds": 0,
                "end_seconds": 1,
                "team": "Home",
                "phase": "attacking",
                "action": "Pass",
                "playlists": ["review"],
            },
            {
                "event_id": "other",
                "period": 1,
                "start_seconds": 1,
                "end_seconds": 2,
                "team": "Away",
                "phase": "defending",
                "action": "Tackle",
                "playlists": ["full"],
            },
        ]
    proposal = workflow.submit_analysis(
        project["id"], events, drawings or [], "# Synthetic analysis", [evidence["id"]]
    )
    return workflow.get_project(project["id"]), proposal


def test_statistics_requires_approved_exact_digest_and_publishes_verified_files(
    tmp_path,
):
    workflow = Workflow(tmp_path / "workspace")
    deleted_source = tmp_path / "deleted-source.mp4"
    project, proposal = _approved_analysis(workflow, source_path=deleted_source)

    with pytest.raises(WorkflowError) as unapproved:
        workflow.queue_statistics(
            project["id"], proposal["analysis_id"], proposal["digest"]
        )
    assert unapproved.value.code == "approval_required"
    with pytest.raises(WorkflowError) as stale:
        workflow.queue_statistics(project["id"], proposal["analysis_id"], "0" * 64)
    assert stale.value.code == "approval_required"
    assert workflow.list_jobs(project["id"])[-1]["kind"] == "evidence"

    workflow.review_analysis(
        project["id"], proposal["analysis_id"], proposal["digest"], True, "Reviewer"
    )
    queued = workflow.queue_statistics(
        project["id"], proposal["analysis_id"], proposal["digest"]
    )
    job = workflow.get_job(queued["job_id"])
    assert job["kind"] == "export"
    assert job["payload"] == {
        "analysis_id": proposal["analysis_id"],
        "digest": proposal["digest"],
        "backend": "statistics",
    }

    run_worker(workflow.root, once=True)

    assert workflow.get_job(queued["job_id"])["status"] == "completed"
    artifacts = [
        artifact
        for artifact in workflow.list_artifacts(project["id"])
        if artifact["job_id"] == queued["job_id"]
    ]
    assert {artifact["kind"] for artifact in artifacts} == {
        "statistics_xlsx",
        "statistics_html",
        "statistics_json",
    }
    output = workflow.task_dir(queued["job_id"]) / "output"
    assert {path.name for path in output.iterdir()} == {
        "statistics.xlsx",
        "statistics.html",
        "statistics.json",
    }
    with zipfile.ZipFile(output / "statistics.xlsx") as workbook:
        assert workbook.testzip() is None
    statistics = json.loads((output / "statistics.json").read_text())
    assert statistics["event_count"] == 2
    assert statistics["summed_clip_seconds"] == 2.0
    assert statistics["coverage_seconds"] == 2.0
    assert statistics["scoreline"] == "Home 0 - 0 Away"
    html = (output / "statistics.html").read_text()
    assert "By team/phase" in html and "Playlists" in html
    assert "<svg" in html and "<script" not in html.lower()
    with zipfile.ZipFile(output / "statistics.xlsx") as workbook:
        assert workbook.testzip() is None
        workbook_xml = workbook.read("xl/workbook.xml")
        assert b'<sheet name="Summary"' in workbook_xml
        assert b'<sheet name="Team Phase"' in workbook_xml
        assert b'<sheet name="Playlists"' in workbook_xml
    assert {artifact["path"].rsplit("/", 1)[-1] for artifact in artifacts} == {
        "statistics.xlsx",
        "statistics.html",
        "statistics.json",
    }
    assert all(
        workflow.get_artifact(artifact["id"]) == artifact for artifact in artifacts
    )
    assert not deleted_source.exists()


def test_statistics_export_maps_writer_stats_errors_to_workflow_errors(
    tmp_path, monkeypatch
):
    from once_sport_mcp import stats_export

    workflow = Workflow(tmp_path / "stats-error-workspace")
    project, proposal = _approved_analysis(workflow)
    workflow.review_analysis(
        project["id"], proposal["analysis_id"], proposal["digest"], True, "Reviewer"
    )
    queued = workflow.queue_statistics(
        project["id"], proposal["analysis_id"], proposal["digest"]
    )

    def fail_writer(*_args):
        raise stats_export.StatsError("synthetic statistics writer error")

    monkeypatch.setattr(stats_export, "write_xlsx", fail_writer)
    run_worker(workflow.root, once=True)

    job = workflow.get_job(queued["job_id"])
    assert job["status"] == "failed"
    assert job["error_code"] == "invalid_analysis"
    assert job["error"] == "synthetic statistics writer error"


def test_playlist_export_filters_events_and_drawings_in_verified_output(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required")
    source = tmp_path / "synthetic.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=64x36:r=4:d=2",
            "-c:v",
            "mpeg4",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    events = [
        {
            "event_id": "selected",
            "period": 1,
            "start_seconds": 0,
            "end_seconds": 1,
            "team": "Home",
            "phase": "attacking",
            "action": "Pass",
            "playlists": ["review"],
        },
        {
            "event_id": "other",
            "period": 1,
            "start_seconds": 1,
            "end_seconds": 2,
            "team": "Away",
            "phase": "defending",
            "action": "Tackle",
            "playlists": ["full"],
        },
    ]
    drawings = [
        {
            "id": "selected-mark",
            "event_id": "selected",
            "kind": "ellipse",
            "start_seconds": 0,
            "end_seconds": 1,
            "points": [[0.5, 0.5], [0.1, 0.1]],
        },
        {
            "id": "other-mark",
            "event_id": "other",
            "kind": "ellipse",
            "start_seconds": 1,
            "end_seconds": 2,
            "points": [[0.5, 0.5], [0.1, 0.1]],
        },
    ]
    workflow = Workflow(tmp_path / "workspace")
    project, proposal = _approved_analysis(
        workflow, events=events, drawings=drawings, source_path=source
    )
    staged_source = workflow.task_dir(workflow.list_jobs(project["id"])[0]["id"])
    staged_source = staged_source / "output" / "source.mp4"
    staged_source.parent.mkdir()
    shutil.copyfile(source, staged_source)
    with workflow.connect() as con:
        stored = workflow._get("projects", project["id"], con)
        stored["source_path"] = str(staged_source)
        workflow._put(con, "projects", stored)
    workflow.review_analysis(
        project["id"], proposal["analysis_id"], proposal["digest"], True, "Reviewer"
    )

    with pytest.raises(WorkflowError) as unknown:
        workflow.queue_export(
            project["id"],
            proposal["analysis_id"],
            proposal["digest"],
            playlist="missing",
        )
    assert unknown.value.code == "invalid_playlist"
    assert str(unknown.value) == "No event belongs to that playlist."
    for invalid_name in ("", "x" * 61):
        with pytest.raises(WorkflowError) as invalid:
            workflow.queue_export(
                project["id"],
                proposal["analysis_id"],
                proposal["digest"],
                playlist=invalid_name,
            )
        assert invalid.value.code == "invalid_playlist"
    assert not [
        job for job in workflow.list_jobs(project["id"]) if job["kind"] == "export"
    ]

    queued = workflow.queue_export(
        project["id"], proposal["analysis_id"], proposal["digest"], playlist="review"
    )
    assert workflow.get_job(queued["job_id"])["payload"]["playlist"] == "review"
    run_worker(workflow.root, once=True)

    assert workflow.get_job(queued["job_id"])["status"] == "completed"
    output = workflow.task_dir(queued["job_id"]) / "output"
    sidecar = json.loads((output / "reel.drawings.json").read_text())
    assert [mark["id"] for mark in sidecar["drawings"]] == ["selected-mark"]
    assert sidecar["manifest"]["playlist"] == "review"
    assert [clip["event_id"] for clip in sidecar["manifest"]["clips"]] == ["selected"]
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["playlist"] == "review"
    assert manifest["digest"] == proposal["digest"]


def test_mcp_stdio_statistics_and_playlist_tools_have_strict_schemas(tmp_path):
    import asyncio
    import os
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    root = tmp_path / "stdio-workspace"
    env = os.environ.copy()
    for name in (
        "ONCE_SPORT_WORKSPACE",
        "ONCE_SPORT_ALLOWED_SOURCE_DIRS",
        "ONCE_SPORT_NATIVE_EXECUTABLE",
        "ONCE_SPORT_TRACKING_ENABLED",
    ):
        env.pop(name, None)
    env.update(
        ONCE_SPORT_WORKSPACE=str(root),
        ONCE_SPORT_ALLOWED_SOURCE_DIRS=json.dumps([str(tmp_path)]),
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from once_sport_mcp.server import main; main()"],
        env=env,
    )

    async def run():
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            discovered = {tool.name: tool for tool in (await client.list_tools()).tools}
            statistics = discovered["request_analysis_statistics"]
            assert set(statistics.inputSchema["properties"]) == {
                "project_id",
                "analysis_id",
                "expected_digest",
            }
            assert set(statistics.inputSchema["required"]) == {
                "project_id",
                "analysis_id",
                "expected_digest",
            }
            assert statistics.annotations.readOnlyHint is False
            export = discovered["request_analysis_export"]
            assert set(export.inputSchema["properties"]) == {
                "project_id",
                "analysis_id",
                "expected_digest",
                "playlist",
            }
            assert set(export.inputSchema["required"]) == {
                "project_id",
                "analysis_id",
                "expected_digest",
            }
            capabilities = json.loads(
                (await client.call_tool("analysis_capabilities", {})).content[0].text
            )["result"]
            assert capabilities["statistics"] == {
                "tool": "request_analysis_statistics",
                "formats": ["xlsx", "html", "json"],
                "requires_media": False,
            }
            assert capabilities["playlist_export"]["tool"] == "request_analysis_export"

    asyncio.run(run())


def test_cli_statistics_and_playlist_export_emit_json_envelopes(tmp_path, capsys):
    from once_sport_mcp.workflow_cli import main

    root = tmp_path / "cli-workspace"
    workflow = Workflow(root)
    project, proposal = _approved_analysis(workflow)
    workflow.review_analysis(
        project["id"], proposal["analysis_id"], proposal["digest"], True, "Reviewer"
    )
    common = [project["id"], proposal["analysis_id"], "--digest", proposal["digest"]]

    assert main(["--root", str(root), "statistics", *common]) == 0
    statistics = json.loads(capsys.readouterr().out)
    assert statistics["ok"] is True
    assert workflow.get_job(statistics["result"]["job_id"])["payload"]["backend"] == (
        "statistics"
    )

    assert main(["--root", str(root), "export", *common, "--playlist", "review"]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["ok"] is True
    assert workflow.get_job(exported["result"]["job_id"])["payload"]["playlist"] == (
        "review"
    )


def test_native_export_rejects_event_flags_and_accepts_playlists():
    from once_sport_mcp.native_rendering import validate_native_plan
    from once_sport_mcp.rendering import RenderError

    event = {
        "event_id": "native-event",
        "period": 1,
        "start_seconds": 0,
        "end_seconds": 1,
        "team": "Home",
        "phase": "attacking",
        "action": "Pass",
    }
    media = {
        "duration": 2,
        "width": 64,
        "height": 36,
        "fps": 4,
        "sample_aspect_ratio": 1,
        "has_audio": False,
    }
    with pytest.raises(RenderError) as rejected:
        validate_native_plan([dict(event, flag="good")], [], media)
    assert str(rejected.value) == "Native Once export does not support event flags"
    validate_native_plan([dict(event, playlists=["review"])], [], media)
