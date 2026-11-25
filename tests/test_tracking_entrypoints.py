"""Tracking stays an explicit opt-in at each public entrypoint."""

import pytest


def test_tracking_configuration_defaults_and_parses_exact_opt_in_values(tmp_path):
    from once_sport_mcp.workflow_config import load_workspace_config

    root = tmp_path / "workspace"
    omitted = load_workspace_config({"ONCE_SPORT_WORKSPACE": str(root)})
    assert omitted is not None
    assert omitted.tracking_enabled is False
    assert omitted.root == root
    assert omitted.allowed_source_dirs == (root / "inputs",)
    assert omitted.native_once_executable is None

    enabled = load_workspace_config(
        {
            "ONCE_SPORT_WORKSPACE": str(root),
            "ONCE_SPORT_TRACKING_ENABLED": "1",
        }
    )
    disabled = load_workspace_config(
        {
            "ONCE_SPORT_WORKSPACE": str(root),
            "ONCE_SPORT_TRACKING_ENABLED": "0",
        }
    )
    assert enabled.tracking_enabled is True
    assert disabled.tracking_enabled is False
    assert not root.exists(), "Configuration parsing must not create files"


@pytest.mark.parametrize("value", ["", "true", "false", "2", " 1", "1 "])
def test_tracking_configuration_rejects_noncanonical_values(tmp_path, value):
    from once_sport_mcp.workflow_config import load_workspace_config

    with pytest.raises(ValueError, match="ONCE_SPORT_TRACKING_ENABLED"):
        load_workspace_config(
            {
                "ONCE_SPORT_WORKSPACE": str(tmp_path / "workspace"),
                "ONCE_SPORT_TRACKING_ENABLED": value,
            }
        )


def test_tracking_configuration_is_ignored_without_workspace():
    from once_sport_mcp.workflow_config import load_workspace_config

    assert load_workspace_config({"ONCE_SPORT_TRACKING_ENABLED": "invalid"}) is None


def test_server_forwards_tracking_only_when_enabled_and_preserves_native_kwargs(
    tmp_path,
):
    import json
    import os
    import subprocess
    import sys

    script = """
import json
from once_sport_mcp import workflow as workflow_module
calls = []
class FakeWorkflow:
    def __init__(self, *args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
workflow_module.Workflow = FakeWorkflow
import once_sport_mcp.workflow_mcp as workflow_mcp
workflow_mcp.register_workflow_tools = lambda *_args: None
import once_sport_mcp.server
print(json.dumps(calls, default=str))
"""
    root = tmp_path / "workspace"
    executable = tmp_path / "native" / "OnceSport"
    common = {
        "ONCE_SPORT_WORKSPACE": str(root),
        "ONCE_SPORT_ALLOWED_SOURCE_DIRS": json.dumps([str(tmp_path / "media")]),
    }
    cases = [
        (common, {"allowed_source_dirs": [str(tmp_path / "media")]}),
        (
            dict(common, ONCE_SPORT_TRACKING_ENABLED="1"),
            {
                "allowed_source_dirs": [str(tmp_path / "media")],
                "tracking_enabled": True,
            },
        ),
        (
            dict(
                common,
                ONCE_SPORT_TRACKING_ENABLED="1",
                ONCE_SPORT_NATIVE_EXECUTABLE=str(executable),
            ),
            {
                "allowed_source_dirs": [str(tmp_path / "media")],
                "native_once_executable": str(executable),
                "tracking_enabled": True,
            },
        ),
        (
            dict(common, ONCE_SPORT_NATIVE_EXECUTABLE=str(executable)),
            {
                "allowed_source_dirs": [str(tmp_path / "media")],
                "native_once_executable": str(executable),
            },
        ),
    ]
    for overrides, expected_kwargs in cases:
        env = os.environ.copy()
        for name in (
            "ONCE_SPORT_WORKSPACE",
            "ONCE_SPORT_ALLOWED_SOURCE_DIRS",
            "ONCE_SPORT_NATIVE_EXECUTABLE",
            "ONCE_SPORT_TRACKING_ENABLED",
        ):
            env.pop(name, None)
        env.update(overrides)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        calls = json.loads(completed.stdout)
        assert len(calls) == 1
        assert calls[0]["args"] == [str(root)]
        assert calls[0]["kwargs"] == expected_kwargs


def test_tracking_mcp_tool_is_conditional_strict_and_queues_reviewable_evidence(
    tmp_path,
):
    import asyncio
    import json
    import os
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from once_sport_mcp.workflow import Workflow
    from once_sport_mcp.workflow_config import tracking_descriptor

    root = tmp_path / "tracking-workspace"
    source_digest = "a" * 64
    workflow = Workflow(root, [tmp_path], tracking_enabled=True)
    created = workflow.create_project(
        "Synthetic tracking fixture", "https://youtu.be/abcdefghijk"
    )
    with workflow.connect() as connection:
        project = workflow._get("projects", created["project_id"], connection)
        project.update(
            source_digest=source_digest,
            media={
                "duration": 2,
                "width": 100,
                "height": 100,
                "fps": 30,
                "sample_aspect_ratio": 1,
                "has_audio": False,
            },
        )
        workflow._put(connection, "projects", project)

    def parameters(workspace, enabled):
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
                "ONCE_SPORT_WORKSPACE": str(workspace),
                "ONCE_SPORT_ALLOWED_SOURCE_DIRS": json.dumps([str(tmp_path)]),
                "ONCE_SPORT_TRACKING_ENABLED": "1" if enabled else "0",
            }
        )
        return StdioServerParameters(
            command=sys.executable,
            args=["-c", "from once_sport_mcp.server import main; main()"],
            env=env,
        )

    def result(response):
        assert not response.isError
        return json.loads(response.content[0].text)

    async def run():
        async with (
            stdio_client(parameters(root, True)) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            discovered = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert len(discovered) == 21
            assert "request_analysis_tracking" in discovered
            tracking_tool = discovered["request_analysis_tracking"]
            expected_fields = {
                "project_id",
                "start_seconds",
                "end_seconds",
                "initial_bbox",
            }
            assert set(tracking_tool.inputSchema["properties"]) == expected_fields
            assert set(tracking_tool.inputSchema["required"]) == expected_fields
            assert tracking_tool.annotations.readOnlyHint is False

            capabilities = result(await client.call_tool("analysis_capabilities", {}))
            assert capabilities["result"]["tracking"] == tracking_descriptor()

            args = {
                "project_id": created["project_id"],
                "start_seconds": 0.25,
                "end_seconds": 1.25,
                "initial_bbox": [0.1, 0.2, 0.3, 0.4],
            }
            queued = result(await client.call_tool("request_analysis_tracking", args))
            assert queued["ok"] is True
            job = workflow.get_job(queued["result"]["job_id"])
            assert job["status"] == "queued"
            assert job["kind"] == "evidence"
            assert job["payload"] == {
                "mode": "tracking",
                "source_digest": source_digest,
                "start_seconds": args["start_seconds"],
                "end_seconds": args["end_seconds"],
                "initial_bbox": args["initial_bbox"],
            }
            with workflow.connect() as connection:
                assert (
                    connection.execute("SELECT COUNT(*) FROM analyses").fetchone()[0]
                    == 0
                )

            invalid_project = result(
                await client.call_tool(
                    "request_analysis_tracking",
                    dict(args, project_id="../invalid"),
                )
            )
            assert invalid_project["ok"] is False
            assert invalid_project["error"]["code"] == "invalid_id"
            for invalid_args in (
                dict(args, start_seconds=True),
                dict(args, initial_bbox=[0.1, True, 0.3, 0.4]),
            ):
                invalid = await client.call_tool(
                    "request_analysis_tracking", invalid_args
                )
                assert invalid.isError

        disabled_root = tmp_path / "tracking-disabled-workspace"
        async with (
            stdio_client(parameters(disabled_root, False)) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            disabled_tools = {
                tool.name: tool for tool in (await client.list_tools()).tools
            }
            assert len(disabled_tools) == 19
            assert "request_analysis_tracking" not in disabled_tools
            disabled_capabilities = result(
                await client.call_tool("analysis_capabilities", {})
            )["result"]
            assert "tracking" not in disabled_capabilities

    asyncio.run(run())
