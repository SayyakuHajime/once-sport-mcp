"""Formation is a read-only projection wired through workflow, MCP and CLI."""

import hashlib
import json
import os
import subprocess
import sys
import uuid

import pytest

from once_sport_mcp.workflow import Workflow, WorkflowError

MEDIA = {
    "duration": 10.0,
    "width": 64,
    "height": 48,
    "fps": 4.0,
    "sample_aspect_ratio": 1.0,
    "has_audio": False,
}
DIGEST = hashlib.sha256(b"synthetic formation source").hexdigest()


def _ready_project(workflow, title="Formation fixture"):
    created = workflow.create_project(title, "https://youtu.be/abcdefghijk")
    with workflow.connect() as con:
        project = workflow._get("projects", created["project_id"], con)
        project.update(media=MEDIA, source_digest=DIGEST)
        workflow._put(con, "projects", project)
    return created["project_id"]


def _tracking_result(job_id, source_digest, x):
    bbox = [x, 0.25, 0.14, 0.2]
    samples = []
    for offset, frame in enumerate(range(4, 12)):
        current = [x + offset * 0.002, 0.25, 0.14, 0.2]
        samples.append(
            {
                "source_frame_index": frame,
                "source_seconds": frame / MEDIA["fps"],
                "bbox": current,
                "anchor": [current[0] + current[2] / 2, current[1] + current[3]],
                "fb_iou": 1.0,
            }
        )
    return {
        "backend": "synthetic",
        "tracking_job_id": job_id,
        "source_digest": source_digest,
        "requested_window": {"start_seconds": 1.0, "end_seconds": 3.0},
        "exact_seed": {
            "source_frame_index": 4,
            "source_seconds": 1.0,
            "bbox_normalized_xywh": bbox,
        },
        "actual_first_seconds": 1.0,
        "actual_last_seconds": 2.75,
        "valid_end_seconds": 3.0,
        "frame_count": len(samples),
        "status": "complete",
        "loss_reason": None,
        "review_required": True,
        "source_metadata": {
            "duration_seconds": MEDIA["duration"],
            "width": MEDIA["width"],
            "height": MEDIA["height"],
            "fps": MEDIA["fps"],
            "sample_aspect_ratio": MEDIA["sample_aspect_ratio"],
            "has_audio": MEDIA["has_audio"],
        },
        "samples": samples,
        "canonical_keyframes": [],
        "fixed_radii": [bbox[2] * 0.9, bbox[3] * 0.18],
    }


def _publish_tracking_result(workflow, project_id, digest, *, x, result_update=None):
    queued = workflow.queue_tracking(project_id, 1.0, 3.0, [x, 0.25, 0.14, 0.2])
    job_id = queued["job_id"]
    with workflow.connect() as con:
        job = workflow._get("jobs", job_id, con)
        job["status"] = "completed"
        workflow._put(con, "jobs", job)
    output = workflow.task_dir(job_id) / "output"
    output.mkdir()
    result = _tracking_result(job_id, digest, x)
    if result_update:
        result.update(result_update)
    path = output / "tracking.json"
    path.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
    artifact = {
        "id": uuid.uuid4().hex,
        "project_id": project_id,
        "job_id": job_id,
        "kind": "tracking_result",
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size,
    }
    with workflow.connect() as con:
        workflow._put(con, "artifacts", artifact)
    assert workflow.get_artifact(artifact["id"]) == artifact
    assert artifact in workflow.list_artifacts(project_id)
    return job_id, path, artifact


def _tracking_set(workflow, project_id, digest=DIGEST):
    return [
        _publish_tracking_result(workflow, project_id, digest, x=x)[0]
        for x in (0.08, 0.43, 0.77)
    ]


def _table_counts(workflow):
    with workflow.connect() as con:
        return {
            table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("projects", "jobs", "analyses", "artifacts")
        }


def test_workflow_formation_returns_renderable_drawings_without_writes(tmp_path):
    from once_sport_mcp.rendering import validate_plan

    workflow = Workflow(tmp_path / "workspace", tracking_enabled=True)
    project_id = _ready_project(workflow)
    job_ids = _tracking_set(workflow, project_id)
    before = _table_counts(workflow)

    result = workflow.build_formation(
        project_id,
        job_ids,
        event_id="attack-shape",
        label="Pressing line",
        hold_at_seconds=1.5,
        hold_seconds=0.5,
    )

    assert result["project_id"] == project_id
    assert result["evidence_job_ids"] == job_ids
    assert result["interval"] == {
        "start_seconds": 1.0,
        "end_seconds": 3.0,
        "frame_count": 8,
    }
    assert {drawing["kind"] for drawing in result["drawings"]} >= {
        "line",
        "ellipse",
        "label",
    }
    event = {
        "event_id": "attack-shape",
        "period": 1,
        "start_seconds": result["interval"]["start_seconds"],
        "end_seconds": result["interval"]["end_seconds"],
        "team": "Home",
        "phase": "attacking",
        "action": "Press",
    }
    validate_plan([event], result["drawings"], MEDIA)
    reordered = workflow.build_formation(
        project_id,
        [job_ids[2], job_ids[0], job_ids[1]],
        event_id="reordered-shape",
        rings=False,
    )
    assert reordered["links"] == [[1, 2], [2, 0]]
    assert _table_counts(workflow) == before


def test_workflow_formation_requires_tracking_configuration(tmp_path):
    workflow = Workflow(tmp_path / "disabled")
    project_id = _ready_project(workflow)
    with pytest.raises(WorkflowError) as error:
        workflow.build_formation(project_id, [], event_id="event")
    assert error.value.code == "tracking_not_configured"
    assert error.value.message == "Tracking requires explicit workflow configuration."


@pytest.mark.parametrize("mutation", ["project", "mode", "status", "source_digest"])
def test_workflow_formation_rejects_ineligible_tracking_jobs(tmp_path, mutation):
    workflow = Workflow(tmp_path / mutation, tracking_enabled=True)
    project_id = _ready_project(workflow)
    job_ids = _tracking_set(workflow, project_id)
    job_id = job_ids[0]
    with workflow.connect() as con:
        job = workflow._get("jobs", job_id, con)
        if mutation == "project":
            job["project_id"] = "0" * 32
        elif mutation == "mode":
            job["payload"]["mode"] = "evidence"
        elif mutation == "status":
            job["status"] = "queued"
        else:
            job["payload"]["source_digest"] = "0" * 64
        workflow._put(con, "jobs", job)

    with pytest.raises(WorkflowError) as error:
        workflow.build_formation(project_id, job_ids, event_id="event")
    assert error.value.code == "invalid_tracking_job"
    assert str(tmp_path) not in error.value.message


def test_workflow_formation_rejects_tampered_artifact_bytes(tmp_path):
    workflow = Workflow(tmp_path / "tampered", tracking_enabled=True)
    project_id = _ready_project(workflow)
    job_ids = _tracking_set(workflow, project_id)
    artifact = next(
        item
        for item in workflow.list_artifacts(project_id)
        if item["job_id"] == job_ids[0]
    )
    with open(artifact["path"], "ab") as stream:
        stream.write(b"tampered")

    with pytest.raises(WorkflowError) as error:
        workflow.build_formation(project_id, job_ids, event_id="event")
    assert error.value.code == "artifact_tampered"


@pytest.mark.parametrize(
    ("field", "value"),
    [("tracking_job_id", "0" * 32), ("source_digest", "0" * 64)],
)
def test_workflow_formation_rejects_mismatched_result_binding(tmp_path, field, value):
    workflow = Workflow(tmp_path / field, tracking_enabled=True)
    project_id = _ready_project(workflow)
    ids = []
    for index, x in enumerate((0.08, 0.43, 0.77)):
        update = {field: value} if index == 0 else None
        ids.append(
            _publish_tracking_result(
                workflow, project_id, DIGEST, x=x, result_update=update
            )[0]
        )
    with pytest.raises(WorkflowError) as error:
        workflow.build_formation(project_id, ids, event_id="event")
    assert error.value.code == "invalid_tracking_job"


def test_workflow_formation_rejects_duplicate_and_too_few_job_ids(tmp_path):
    workflow = Workflow(tmp_path / "invalid-count", tracking_enabled=True)
    project_id = _ready_project(workflow)
    job_ids = _tracking_set(workflow, project_id)
    for values in ([job_ids[0]], [job_ids[0], job_ids[0]], []):
        with pytest.raises(WorkflowError) as error:
            workflow.build_formation(project_id, values, event_id="event")
        assert error.value.code == "invalid_formation"


def test_formation_mcp_stdio_tool_is_read_only_discovered_and_strict(tmp_path):
    import asyncio

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from once_sport_mcp.workflow_config import tracking_descriptor

    root = tmp_path / "mcp-workspace"
    workflow = Workflow(root, [tmp_path], tracking_enabled=True)
    project_id = _ready_project(workflow)
    job_ids = _tracking_set(workflow, project_id)
    env = os.environ.copy()
    for name in (
        "ONCE_SPORT_WORKSPACE",
        "ONCE_SPORT_ALLOWED_SOURCE_DIRS",
        "ONCE_SPORT_NATIVE_EXECUTABLE",
        "ONCE_SPORT_TRACKING_ENABLED",
    ):
        env.pop(name, None)
    env.update(
        {
            "ONCE_SPORT_WORKSPACE": str(root),
            "ONCE_SPORT_ALLOWED_SOURCE_DIRS": json.dumps([str(tmp_path)]),
            "ONCE_SPORT_TRACKING_ENABLED": "1",
        }
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from once_sport_mcp.server import main; main()"],
        env=env,
    )

    async def run():
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            discovered = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert len(discovered) == 22
            tool = discovered["build_formation_drawings"]
            assert tool.annotations.readOnlyHint is True
            assert set(tool.inputSchema["properties"]) == {
                "project_id",
                "tracking_job_ids",
                "event_id",
                "links",
                "closed",
                "rings",
                "area",
                "spotlights",
                "label",
                "hold_at_seconds",
                "hold_seconds",
                "tags",
                "id_prefix",
            }
            capabilities = json.loads(
                (await client.call_tool("analysis_capabilities", {})).content[0].text
            )
            assert capabilities["result"]["tracking"] == tracking_descriptor()
            response = await client.call_tool(
                "build_formation_drawings",
                {
                    "project_id": project_id,
                    "tracking_job_ids": job_ids,
                    "event_id": "mcp-event",
                },
            )
            envelope = json.loads(response.content[0].text)
            assert envelope["ok"] is True
            assert envelope["result"]["evidence_job_ids"] == job_ids
            for invalid in (
                {
                    "project_id": project_id,
                    "tracking_job_ids": job_ids,
                    "event_id": "mcp-event",
                    "links": [[True, 1]],
                },
                {
                    "project_id": project_id,
                    "tracking_job_ids": job_ids,
                    "event_id": "mcp-event",
                    "hold_seconds": True,
                },
            ):
                rejected = await client.call_tool("build_formation_drawings", invalid)
                assert rejected.isError

        native_env = dict(env)
        native_env["ONCE_SPORT_NATIVE_EXECUTABLE"] = str(
            tmp_path / "not-installed" / "OnceSport"
        )
        native_params = StdioServerParameters(
            command=sys.executable,
            args=["-c", "from once_sport_mcp.server import main; main()"],
            env=native_env,
        )
        async with (
            stdio_client(native_params) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            native_discovered = {
                tool.name: tool for tool in (await client.list_tools()).tools
            }
            assert len(native_discovered) == 23
            assert set(native_discovered) - set(discovered) == {
                "request_native_analysis_export"
            }

    asyncio.run(run())


def test_cli_formation_parser_dispatch_and_subprocess_envelopes(tmp_path):
    from once_sport_mcp.workflow_cli import _build_parser, _dispatch

    project_id = "a" * 32
    job_ids = ["b" * 32, "c" * 32, "d" * 32]
    args = _build_parser().parse_args(
        [
            "--root",
            str(tmp_path / "parser"),
            "--enable-tracking",
            "formation",
            project_id,
            "--event-id",
            "parser-event",
            "--job",
            job_ids[0],
            "--job",
            job_ids[1],
            "--link",
            "0",
            "1",
            "--no-rings",
            "--label",
            "shape",
            "--hold-at",
            "1.5",
            "--hold-seconds",
            "0.5",
            "--id-prefix",
            "shape",
            "--tag",
            "CB:Otamendi",
            "--tag",
            "-",
        ]
    )
    assert args.command == "formation"
    assert args.enable_tracking is True
    calls = []

    class WorkflowSpy:
        def build_formation(self, *values, **options):
            calls.append((values, options))
            return {"ok": True}

    assert _dispatch(args, WorkflowSpy()) == {"ok": True}
    assert calls == [
        (
            (project_id, [job_ids[0], job_ids[1]]),
            {
                "event_id": "parser-event",
                "id_prefix": "shape",
                "links": [[0, 1]],
                "closed": False,
                "rings": False,
                "area": False,
                "spotlights": False,
                "label": "shape",
                "hold_at_seconds": 1.5,
                "hold_seconds": 0.5,
                "tags": [{"position": "CB", "name": "Otamendi"}, None],
            },
        )
    ]

    root = tmp_path / "cli-workspace"
    workflow = Workflow(root, [tmp_path], tracking_enabled=True)
    seeded_project = _ready_project(workflow)
    seeded_jobs = _tracking_set(workflow, seeded_project)
    command = [
        sys.executable,
        "-m",
        "once_sport_mcp.workflow_cli",
        "--root",
        str(root),
        "--enable-tracking",
        "formation",
        seeded_project,
        "--event-id",
        "cli-event",
    ]
    for job_id in seeded_jobs:
        command.extend(["--job", job_id])
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env=dict(os.environ, TMPDIR=str(tmp_path)),
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    envelope = json.loads(completed.stdout)
    assert envelope["ok"] is True
    assert envelope["result"]["evidence_job_ids"] == seeded_jobs

    missing_opt_in = subprocess.run(
        [
            sys.executable,
            "-m",
            "once_sport_mcp.workflow_cli",
            "--root",
            str(root),
            "formation",
            seeded_project,
            "--event-id",
            "cli-event",
            *[value for job_id in seeded_jobs for value in ("--job", job_id)],
        ],
        capture_output=True,
        text=True,
        env=dict(os.environ, TMPDIR=str(tmp_path)),
        check=False,
    )
    assert missing_opt_in.returncode == 1
    missing_envelope = json.loads(missing_opt_in.stdout)
    assert missing_envelope["ok"] is False
    assert missing_envelope["error"]["code"] == "tracking_not_configured"
