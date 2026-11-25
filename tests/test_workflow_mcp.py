"""Exercise opt-in workflow tools over an actual stdio MCP connection."""

import asyncio
import json
import os
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def parameters(root, source_directory):
    return StdioServerParameters(
        command=sys.executable,
        args=["-c", "from once_sport_mcp.server import main; main()"],
        env=dict(
            os.environ,
            ONCE_SPORT_WORKSPACE=str(root),
            ONCE_SPORT_ALLOWED_SOURCE_DIRS=json.dumps([str(source_directory)]),
        ),
    )


def payload(response):
    assert not response.isError
    return json.loads(response.content[0].text)


def test_opt_in_stdio_exposes_truthful_capabilities_and_queues_ingest(tmp_path):
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=s=64x36:r=4:d=1",
            "-c:v",
            "libx264",
            str(source),
        ],
        check=True,
        capture_output=True,
    )

    async def run():
        async with (
            stdio_client(parameters(tmp_path / "workspace", tmp_path)) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            discovered = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert "analysis_capabilities" in discovered, (
                "Persistent workspace tools are not registered"
            )
            capabilities = payload(await client.call_tool("analysis_capabilities", {}))
            assert capabilities["ok"]
            assert capabilities["result"]["analysis_backend"] == "mcp_host"
            assert capabilities["result"]["native_import_verified"] is False
            assert capabilities["result"]["native_editable_drawings"] is False
            queued = payload(
                await client.call_tool(
                    "create_analysis_project",
                    {"title": "Synthetic", "source": str(source)},
                )
            )
            assert queued["ok"]
            assert queued["result"]["project_id"]
            assert queued["result"]["job_id"]
            assert (
                discovered["create_analysis_project"].annotations.openWorldHint is True
            )
            assert (
                discovered["create_analysis_project"].annotations.readOnlyHint is False
            )

    asyncio.run(run())


def test_fresh_client_reads_persistent_jobs_and_can_cancel_retry_without_poisoning_stdio(
    tmp_path,
):
    from once_sport_mcp.workflow import Workflow

    root = tmp_path / "workspace"
    source = tmp_path / "queued.mp4"
    source.write_bytes(b"Intentionally unprobed until the ingest worker runs")
    queued = Workflow(root, [tmp_path]).create_project("Queued source", str(source))

    async def run():
        async with (
            stdio_client(parameters(root, tmp_path)) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert {
                "get_analysis_project",
                "get_analysis_job",
                "list_analysis_jobs",
                "cancel_analysis_job",
                "retry_analysis_job",
            } <= names
            project = payload(
                await client.call_tool(
                    "get_analysis_project", {"project_id": queued["project_id"]}
                )
            )
            assert project["result"]["id"] == queued["project_id"]
            job = payload(
                await client.call_tool("get_analysis_job", {"job_id": queued["job_id"]})
            )
            assert job["result"]["status"] == "queued"
            jobs = payload(
                await client.call_tool(
                    "list_analysis_jobs", {"project_id": queued["project_id"]}
                )
            )
            assert len(jobs["result"]) == 1
            invalid = payload(
                await client.call_tool("get_analysis_job", {"job_id": "../other"})
            )
            assert invalid["ok"] is False
            assert invalid["error"]["code"] == "invalid_id"
            cancelled = payload(
                await client.call_tool(
                    "cancel_analysis_job", {"job_id": queued["job_id"]}
                )
            )
            assert cancelled["result"]["status"] == "cancelled"
            retried = payload(
                await client.call_tool(
                    "retry_analysis_job", {"job_id": queued["job_id"]}
                )
            )
            assert retried["result"]["job_id"] != queued["job_id"]
            assert payload(await client.call_tool("analysis_capabilities", {}))["ok"]

    asyncio.run(run())


def test_stdio_analysis_controls_gate_export_and_verify_local_artifacts(tmp_path):
    from once_sport_mcp.worker import run_worker

    source_directory = tmp_path / "synthetic-media"
    source_directory.mkdir()
    source = source_directory / "fixture.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=96x64:r=8:d=2",
            "-c:v",
            "mpeg4",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    root = tmp_path / "workspace"

    async def run():
        async with (
            stdio_client(parameters(root, source_directory)) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            discovered = {tool.name: tool for tool in (await client.list_tools()).tools}
            required = {
                "request_analysis_evidence",
                "submit_analysis_proposal",
                "get_analysis_proposal",
                "review_analysis_proposal",
                "request_analysis_export",
                "request_analysis_statistics",
                "list_analysis_artifacts",
                "get_analysis_artifact",
            }
            assert required <= discovered.keys()
            assert (
                "expected_digest"
                in discovered["request_analysis_export"].inputSchema["required"]
            )
            assert (
                discovered["review_analysis_proposal"].annotations.readOnlyHint is False
            )
            assert (
                discovered["list_analysis_artifacts"].annotations.readOnlyHint is True
            )

            capabilities = payload(await client.call_tool("analysis_capabilities", {}))[
                "result"
            ]
            assert capabilities["analysis_backend"] == "mcp_host"
            assert capabilities["worker"] == "separate_process"
            assert capabilities["native_import_verified"] is False
            assert capabilities["native_editable_drawings"] is False
            assert "operator assertion" in capabilities["approval"]
            assert "not authenticated" in capabilities["approval"]
            assert "verified local path" in capabilities["artifact_delivery"]
            assert "not transferred" in capabilities["artifact_delivery"]

            created = payload(
                await client.call_tool(
                    "create_analysis_project",
                    {"title": "Synthetic fixture", "source": str(source)},
                )
            )["result"]
            run_worker(root, once=True, allowed_source_dirs=[source_directory])
            project = payload(
                await client.call_tool(
                    "get_analysis_project", {"project_id": created["project_id"]}
                )
            )["result"]
            assert project["source_digest"]

            evidence = payload(
                await client.call_tool(
                    "request_analysis_evidence",
                    {
                        "project_id": created["project_id"],
                        "start_seconds": 0,
                        "end_seconds": 1,
                    },
                )
            )["result"]
            run_worker(root, once=True, allowed_source_dirs=[source_directory])
            evidence_job = payload(
                await client.call_tool(
                    "get_analysis_job", {"job_id": evidence["job_id"]}
                )
            )["result"]
            assert evidence_job["status"] == "completed"

            events = [
                {
                    "event_id": "synthetic-fixture-event",
                    "period": 1,
                    "start_seconds": 0,
                    "end_seconds": 1,
                    "team": "synthetic fixture",
                    "phase": "attacking",
                    "action": "test marker",
                }
            ]
            drawings = [
                {
                    "id": "fixture-arrow",
                    "event_id": "synthetic-fixture-event",
                    "kind": "arrow",
                    "start_seconds": 0,
                    "end_seconds": 1,
                    "points": [[0.1, 0.2], [0.8, 0.8]],
                    "keyframes": [
                        {"time_seconds": 0, "points": [[0.1, 0.2], [0.8, 0.8]]},
                        {"time_seconds": 1, "points": [[0.2, 0.3], [0.9, 0.7]]},
                    ],
                }
            ]
            proposal = payload(
                await client.call_tool(
                    "submit_analysis_proposal",
                    {
                        "project_id": created["project_id"],
                        "events": events,
                        "drawings": drawings,
                        "report_markdown": "# Synthetic protocol fixture",
                        "evidence_job_ids": [evidence["job_id"]],
                    },
                )
            )["result"]
            saved = payload(
                await client.call_tool(
                    "get_analysis_proposal",
                    {"analysis_id": proposal["analysis_id"]},
                )
            )["result"]
            assert saved["status"] == "awaiting_review"
            assert saved["proposal"]["events"] == events
            assert saved["proposal"]["drawings"] == drawings

            blocked = payload(
                await client.call_tool(
                    "request_analysis_export",
                    {
                        "project_id": created["project_id"],
                        "analysis_id": proposal["analysis_id"],
                        "expected_digest": proposal["digest"],
                    },
                )
            )
            assert blocked["ok"] is False
            assert blocked["error"]["code"] == "approval_required"

            for invalid_value in ("true", 1):
                invalid_approval = await client.call_tool(
                    "review_analysis_proposal",
                    {
                        "project_id": created["project_id"],
                        "analysis_id": proposal["analysis_id"],
                        "expected_digest": proposal["digest"],
                        "approved": invalid_value,
                        "reviewer": "Synthetic test operator",
                    },
                )
                assert invalid_approval.isError
                assert payload(await client.call_tool("analysis_capabilities", {}))[
                    "ok"
                ]

            stale_review = payload(
                await client.call_tool(
                    "review_analysis_proposal",
                    {
                        "project_id": created["project_id"],
                        "analysis_id": proposal["analysis_id"],
                        "expected_digest": "0" * 64,
                        "approved": True,
                        "reviewer": "Synthetic test operator",
                    },
                )
            )
            assert stale_review["ok"] is False
            assert stale_review["error"]["code"] == "stale_analysis"
            approved = payload(
                await client.call_tool(
                    "review_analysis_proposal",
                    {
                        "project_id": created["project_id"],
                        "analysis_id": proposal["analysis_id"],
                        "expected_digest": proposal["digest"],
                        "approved": True,
                        "reviewer": "Synthetic test operator",
                    },
                )
            )["result"]
            assert approved["status"] == "approved"

            stale_export = payload(
                await client.call_tool(
                    "request_analysis_export",
                    {
                        "project_id": created["project_id"],
                        "analysis_id": proposal["analysis_id"],
                        "expected_digest": "0" * 64,
                    },
                )
            )
            assert stale_export["ok"] is False
            queued_export = payload(
                await client.call_tool(
                    "request_analysis_export",
                    {
                        "project_id": created["project_id"],
                        "analysis_id": proposal["analysis_id"],
                        "expected_digest": proposal["digest"],
                    },
                )
            )["result"]
            run_worker(root, once=True, allowed_source_dirs=[source_directory])
            assert (
                payload(
                    await client.call_tool(
                        "get_analysis_job", {"job_id": queued_export["job_id"]}
                    )
                )["result"]["status"]
                == "completed"
            )

            listed = payload(
                await client.call_tool(
                    "list_analysis_artifacts",
                    {"project_id": created["project_id"]},
                )
            )["result"]
            reel = next(item for item in listed if item["kind"] == "reel")
            retrieved = payload(
                await client.call_tool(
                    "get_analysis_artifact", {"artifact_id": reel["id"]}
                )
            )["result"]
            assert retrieved == reel
            assert os.path.isfile(retrieved["path"])
            assert os.path.getsize(retrieved["path"]) == retrieved["size_bytes"]
            missing = payload(
                await client.call_tool(
                    "get_analysis_artifact", {"artifact_id": "0" * 32}
                )
            )
            assert missing["ok"] is False
            assert payload(await client.call_tool("analysis_capabilities", {}))["ok"]

    asyncio.run(run())
