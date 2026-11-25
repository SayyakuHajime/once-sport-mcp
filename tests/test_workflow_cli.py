import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _run_cli(root, tmp_path, *arguments, source_dir=None):
    command = [
        sys.executable,
        "-m",
        "once_sport_mcp.workflow_cli",
        "--root",
        str(root),
    ]
    if source_dir is not None:
        command.extend(["--allow-source-dir", str(source_dir)])
    command.extend(arguments)
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=dict(os.environ, TMPDIR=str(tmp_path)),
        check=False,
    )
    envelope = json.loads(completed.stdout) if completed.stdout else None
    return completed, envelope


def _worker_once(root, tmp_path, source_dir):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "once_sport_mcp.worker",
            "--root",
            str(root),
            "--once",
            "--allow-source-dir",
            str(source_dir),
        ],
        capture_output=True,
        text=True,
        env=dict(os.environ, TMPDIR=str(tmp_path)),
        check=False,
    )


def _make_video(source):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required")
    source.parent.mkdir(parents=True)
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


def _result(completed, envelope):
    assert completed.returncode == 0, completed.stderr
    assert envelope["ok"] is True
    return envelope["result"]


def test_module_cli_capabilities_is_json_and_truthful(tmp_path):
    root = tmp_path / "workspace"
    completed, envelope = _run_cli(root, tmp_path, "capabilities")

    result = _result(completed, envelope)
    assert result["workspace"] == str(root)
    assert result["worker"] == "separate_process"
    assert result["native_import_verified"] is False
    assert "not transferred" in result["artifact_delivery"]


def test_cli_fresh_process_end_to_end_gates_export_and_verifies_artifacts(tmp_path):
    root = tmp_path / "workspace"
    source_dir = tmp_path / "media"
    source = source_dir / "fixture.mp4"
    _make_video(source)

    created = _result(
        *_run_cli(
            root,
            tmp_path,
            "create-project",
            "--title",
            "Synthetic match",
            "--source",
            str(source),
            source_dir=source_dir,
        )
    )
    project_id, ingest_id = created["project_id"], created["job_id"]
    assert (
        _result(
            *_run_cli(root, tmp_path, "get-project", project_id, source_dir=source_dir)
        )["id"]
        == project_id
    )
    assert (
        _result(*_run_cli(root, tmp_path, "get-job", ingest_id, source_dir=source_dir))[
            "status"
        ]
        == "queued"
    )

    worker = _worker_once(root, tmp_path, source_dir)
    assert worker.returncode == 0, worker.stderr
    assert (
        _result(*_run_cli(root, tmp_path, "get-job", ingest_id, source_dir=source_dir))[
            "status"
        ]
        == "completed"
    )

    evidence = _result(
        *_run_cli(
            root,
            tmp_path,
            "evidence",
            project_id,
            "--start",
            "0",
            "--end",
            "1",
            "--interval",
            "0.5",
            source_dir=source_dir,
        )
    )
    worker = _worker_once(root, tmp_path, source_dir)
    assert worker.returncode == 0, worker.stderr
    assert (
        _result(
            *_run_cli(
                root,
                tmp_path,
                "get-job",
                evidence["job_id"],
                source_dir=source_dir,
            )
        )["status"]
        == "completed"
    )

    events = [
        {
            "event_id": "a",
            "period": 1,
            "start_seconds": 0,
            "end_seconds": 1,
            "team": "Home",
            "phase": "attacking",
            "action": "Pass",
        }
    ]
    proposal_body = {
        "events": events,
        "drawings": [],
        "report_markdown": "# Analysis-only report",
        "evidence_job_ids": [evidence["job_id"]],
    }
    proposal_file = tmp_path / "proposal.json"
    proposal_file.write_text(json.dumps(proposal_body), encoding="utf-8")
    proposal = _result(
        *_run_cli(
            root,
            tmp_path,
            "submit-analysis",
            project_id,
            "--proposal",
            str(proposal_file),
            source_dir=source_dir,
        )
    )
    saved = _result(
        *_run_cli(
            root,
            tmp_path,
            "get-analysis",
            proposal["analysis_id"],
            source_dir=source_dir,
        )
    )
    assert saved["status"] == "awaiting_review"
    assert saved["proposal"]["events"] == events
    assert saved["proposal"]["drawings"] == []
    assert saved["proposal"]["evidence_job_ids"] == [evidence["job_id"]]

    blocked, blocked_envelope = _run_cli(
        root,
        tmp_path,
        "export",
        project_id,
        proposal["analysis_id"],
        "--digest",
        proposal["digest"],
        source_dir=source_dir,
    )
    assert blocked.returncode == 1
    assert blocked_envelope["ok"] is False
    assert blocked_envelope["error"]["code"] == "approval_required"

    reviewed = _result(
        *_run_cli(
            root,
            tmp_path,
            "review-analysis",
            project_id,
            proposal["analysis_id"],
            "--digest",
            proposal["digest"],
            "--reviewer",
            "Operator assertion",
            "--approve",
            source_dir=source_dir,
        )
    )
    assert reviewed["status"] == "approved"
    assert reviewed["reviews"][-1]["approved"] is True
    assert reviewed["reviews"][-1]["reviewer"] == "Operator assertion"

    export = _result(
        *_run_cli(
            root,
            tmp_path,
            "export",
            project_id,
            proposal["analysis_id"],
            "--digest",
            proposal["digest"],
            source_dir=source_dir,
        )
    )
    worker = _worker_once(root, tmp_path, source_dir)
    assert worker.returncode == 0, worker.stderr
    assert (
        _result(
            *_run_cli(
                root, tmp_path, "get-job", export["job_id"], source_dir=source_dir
            )
        )["status"]
        == "completed"
    )

    artifacts = _result(
        *_run_cli(
            root,
            tmp_path,
            "list-artifacts",
            project_id,
            source_dir=source_dir,
        )
    )
    reel = next(artifact for artifact in artifacts if artifact["kind"] == "reel")
    retrieved = _result(
        *_run_cli(
            root,
            tmp_path,
            "get-artifact",
            reel["id"],
            source_dir=source_dir,
        )
    )
    artifact_path = Path(retrieved["path"])
    assert retrieved == reel
    assert artifact_path.is_file()
    assert artifact_path.stat().st_size == retrieved["size_bytes"]
    assert hashlib.sha256(artifact_path.read_bytes()).hexdigest() == retrieved["sha256"]

    jobs = _result(
        *_run_cli(
            root,
            tmp_path,
            "list-jobs",
            "--project-id",
            project_id,
            source_dir=source_dir,
        )
    )
    assert {job["id"] for job in jobs} >= {
        ingest_id,
        evidence["job_id"],
        export["job_id"],
    }


def test_cli_queued_cancel_retry_persists_a_distinct_job_id(tmp_path):
    root = tmp_path / "workspace"
    source_dir = tmp_path / "media"
    source_dir.mkdir()
    source = source_dir / "not-yet-probed.mp4"
    source.write_bytes(b"Worker has not been started")
    created = _result(
        *_run_cli(
            root,
            tmp_path,
            "create-project",
            "--title",
            "Queued source",
            "--source",
            str(source),
            source_dir=source_dir,
        )
    )

    cancelled = _result(
        *_run_cli(
            root,
            tmp_path,
            "cancel-job",
            created["job_id"],
            source_dir=source_dir,
        )
    )
    assert cancelled["status"] == "cancelled"
    retried = _result(
        *_run_cli(
            root,
            tmp_path,
            "retry-job",
            created["job_id"],
            source_dir=source_dir,
        )
    )
    assert retried["project_id"] == created["project_id"]
    assert retried["job_id"] != created["job_id"]
    assert (
        _result(
            *_run_cli(
                root,
                tmp_path,
                "get-job",
                retried["job_id"],
                source_dir=source_dir,
            )
        )["status"]
        == "queued"
    )


def test_cli_rejects_bad_proposals_and_domain_errors_without_tracebacks(tmp_path):
    root = tmp_path / "workspace"
    source_dir = tmp_path / "media"
    source_dir.mkdir()
    source = source_dir / "source.mp4"
    source.write_bytes(b"Not processed in this test")
    project = _result(
        *_run_cli(
            root,
            tmp_path,
            "create-project",
            "--title",
            "Proposal validation",
            "--source",
            str(source),
            source_dir=source_dir,
        )
    )
    marker = "PRIVATE_PROPOSAL_CONTENT"
    invalid_documents = [
        f'{{"{marker}":',
        json.dumps(
            {
                "events": [],
                "drawings": [],
                "report_markdown": "report",
            }
        ),
        json.dumps(
            {
                "events": [],
                "drawings": [],
                "report_markdown": "report",
                "evidence_job_ids": [],
                marker: "do not print",
            }
        ),
    ]
    for index, document in enumerate(invalid_documents):
        proposal = tmp_path / f"bad-{index}.json"
        proposal.write_text(document, encoding="utf-8")
        completed, envelope = _run_cli(
            root,
            tmp_path,
            "submit-analysis",
            project["project_id"],
            "--proposal",
            str(proposal),
            source_dir=source_dir,
        )
        assert completed.returncode == 1
        assert envelope["ok"] is False
        assert envelope["error"]["code"] == "invalid_proposal"
        assert marker not in completed.stdout + completed.stderr

    missing, missing_envelope = _run_cli(
        root,
        tmp_path,
        "submit-analysis",
        project["project_id"],
        "--proposal",
        str(tmp_path / "missing.json"),
        source_dir=source_dir,
    )
    assert missing.returncode == 1
    assert missing_envelope["error"]["code"] == "invalid_proposal"

    domain, domain_envelope = _run_cli(
        root, tmp_path, "get-project", "../invalid", source_dir=source_dir
    )
    assert domain.returncode == 1
    assert domain_envelope["error"]["code"] == "invalid_id"
    assert "Traceback" not in domain.stdout + domain.stderr

    invalid_argument = subprocess.run(
        [
            sys.executable,
            "-m",
            "once_sport_mcp.workflow_cli",
            "--root",
            str(root),
            "export",
            project["project_id"],
            "0" * 32,
        ],
        capture_output=True,
        text=True,
        env=dict(os.environ, TMPDIR=str(tmp_path)),
        check=False,
    )
    assert invalid_argument.returncode == 2
    assert invalid_argument.stdout == ""
    assert "--digest" in invalid_argument.stderr


def test_module_and_console_entrypoints_show_help(tmp_path):
    module_help = subprocess.run(
        [sys.executable, "-m", "once_sport_mcp.workflow_cli", "--help"],
        capture_output=True,
        text=True,
        env=dict(os.environ, TMPDIR=str(tmp_path)),
        check=False,
    )
    assert module_help.returncode == 0
    assert "create-project" in module_help.stdout
    console = Path(sys.executable).with_name("once-sport-workflow")
    assert console.is_file()
    console_help = subprocess.run(
        [str(console), "--help"],
        capture_output=True,
        text=True,
        env=dict(os.environ, TMPDIR=str(tmp_path)),
        check=False,
    )
    assert console_help.returncode == 0
    assert "submit-analysis" in console_help.stdout
