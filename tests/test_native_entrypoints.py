"""Native entrypoint exposure is opt-in and remains bound to startup config."""

import json
import os
import subprocess
import sys

import pytest


def test_workspace_config_keeps_default_shape_and_parses_native_path(tmp_path):
    from once_sport_mcp.workflow_config import load_workspace_config

    root = tmp_path / "workspace"
    default = load_workspace_config({"ONCE_SPORT_WORKSPACE": str(root)})
    assert default.root == root
    assert default.allowed_source_dirs == (root / "inputs",)
    assert default.native_once_executable is None

    executable = tmp_path / "not-installed" / "OnceSport"
    configured = load_workspace_config(
        {
            "ONCE_SPORT_WORKSPACE": str(root),
            "ONCE_SPORT_NATIVE_EXECUTABLE": str(executable),
        }
    )
    assert configured.native_once_executable == executable
    assert not executable.exists()
    assert not root.exists()
    assert (
        load_workspace_config(
            {"ONCE_SPORT_NATIVE_EXECUTABLE": "ignored-without-workspace"}
        )
        is None
    )


@pytest.mark.parametrize(
    "value", ["", "relative/OnceSport", "/tmp/../OnceSport", "/tmp/Once\x00Sport"]
)
def test_workspace_config_rejects_unsafe_native_executable(tmp_path, value):
    from once_sport_mcp.workflow_config import load_workspace_config

    with pytest.raises(ValueError, match="ONCE_SPORT_NATIVE_EXECUTABLE"):
        load_workspace_config(
            {
                "ONCE_SPORT_WORKSPACE": str(tmp_path / "workspace"),
                "ONCE_SPORT_NATIVE_EXECUTABLE": value,
            }
        )


def _mcp_parameters(tmp_path, *, workspace=None, native_executable=None):
    from mcp import StdioServerParameters

    env = os.environ.copy()
    for name in (
        "ONCE_SPORT_WORKSPACE",
        "ONCE_SPORT_ALLOWED_SOURCE_DIRS",
        "ONCE_SPORT_NATIVE_EXECUTABLE",
    ):
        env.pop(name, None)
    if workspace is not None:
        env["ONCE_SPORT_WORKSPACE"] = str(workspace)
        env["ONCE_SPORT_ALLOWED_SOURCE_DIRS"] = json.dumps([str(tmp_path)])
    if native_executable is not None:
        env["ONCE_SPORT_NATIVE_EXECUTABLE"] = str(native_executable)
    return StdioServerParameters(
        command=sys.executable,
        args=["-c", "from once_sport_mcp.server import main; main()"],
        env=env,
    )


def _mcp_payload(response):
    assert not response.isError
    return json.loads(response.content[0].text)


def test_mcp_discovery_and_capabilities_are_native_opt_in(tmp_path):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async def tool_names(parameters):
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            capabilities = None
            if "analysis_capabilities" in tools:
                capabilities = _mcp_payload(
                    await client.call_tool("analysis_capabilities", {})
                )["result"]
            return tools, capabilities

    async def run():
        legacy, _ = await tool_names(_mcp_parameters(tmp_path))
        workspace, default_caps = await tool_names(
            _mcp_parameters(tmp_path, workspace=tmp_path / "workspace")
        )
        native_path = tmp_path / "not-installed" / "OnceSport"
        native, native_caps = await tool_names(
            _mcp_parameters(
                tmp_path,
                workspace=tmp_path / "native-workspace",
                native_executable=native_path,
            )
        )
        assert len(legacy) == 4
        assert len(workspace) == 19
        assert len(native) == 20
        assert "request_native_analysis_export" not in workspace
        assert "request_native_analysis_export" in native
        assert "native_export" not in default_caps
        descriptor = native_caps["native_export"]
        assert descriptor["configured"] is True
        assert descriptor["headless"] is True
        assert descriptor["cursor_required"] is False
        assert descriptor["display_required"] is False
        assert descriptor["live_database_writes"] is False
        assert descriptor["supported_kinds"] == ["arrow", "ellipse"]
        assert descriptor["tested_build"] == "Once Sport 4.9.6"
        assert descriptor["native_import_verified"] is False
        assert descriptor["native_editable_drawings"] is False
        assert descriptor["gui_project_list_import_verified"] is False
        assert "native_original_reel" in descriptor["outputs"]
        assert "native_analysis_xml" in descriptor["outputs"]
        assert "H.264/AAC" in descriptor["outputs"]["compatible_reel"]["role"]
        assert "official public API" in descriptor["boundary"]
        assert native_caps["native_import_verified"] is False
        assert native_caps["native_editable_drawings"] is False
        tool = native["request_native_analysis_export"]
        assert set(tool.inputSchema["properties"]) == {
            "project_id",
            "analysis_id",
            "expected_digest",
        }
        assert set(tool.inputSchema["required"]) == {
            "project_id",
            "analysis_id",
            "expected_digest",
        }
        assert tool.annotations.readOnlyHint is False

    import asyncio

    asyncio.run(run())


def _seed_unapproved_analysis(root, source_dir, native_executable):
    from once_sport_mcp.workflow import Workflow

    workflow = Workflow(
        root,
        [source_dir],
        native_once_executable=native_executable,
    )
    created = workflow.create_project(
        "MCP native fixture", "https://youtu.be/abcdefghijk"
    )
    with workflow.connect() as con:
        project = workflow._get("projects", created["project_id"], con)
        project.update(
            source_digest="a" * 64,
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
        evidence = workflow._job(
            con, created["project_id"], "evidence", {"source_digest": "a" * 64}
        )
        evidence["status"] = "completed"
        workflow._put(con, "jobs", evidence)
    events = [
        {
            "event_id": "mcp-native-event",
            "period": 1,
            "start_seconds": 0,
            "end_seconds": 1,
            "team": "Home",
            "phase": "attacking",
            "action": "Pass",
        }
    ]
    proposal = workflow.submit_analysis(
        created["project_id"], events, [], "# MCP native report", [evidence["id"]]
    )
    return workflow, created["project_id"], proposal


def test_mcp_native_export_delegates_exact_digest_and_returns_domain_envelope(tmp_path):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    root = tmp_path / "workspace"
    native_path = tmp_path / "fake-once" / "OnceSport"
    workflow, project_id, proposal = _seed_unapproved_analysis(
        root, tmp_path, native_path
    )

    async def run():
        async with (
            stdio_client(
                _mcp_parameters(
                    tmp_path,
                    workspace=root,
                    native_executable=native_path,
                )
            ) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            args = {
                "project_id": project_id,
                "analysis_id": proposal["analysis_id"],
                "expected_digest": proposal["digest"],
            }
            blocked = _mcp_payload(
                await client.call_tool("request_native_analysis_export", args)
            )
            assert blocked["ok"] is False
            assert blocked["error"]["code"] == "approval_required"

            workflow.review_analysis(
                project_id,
                proposal["analysis_id"],
                proposal["digest"],
                True,
                "MCP test operator",
            )
            queued = _mcp_payload(
                await client.call_tool("request_native_analysis_export", args)
            )
            assert queued["ok"] is True
            job = workflow.get_job(queued["result"]["job_id"])
            assert job["kind"] == "export"
            assert job["payload"] == {
                "analysis_id": proposal["analysis_id"],
                "digest": proposal["digest"],
                "backend": "native",
            }
            stale = dict(args, expected_digest="0" * 64)
            rejected = _mcp_payload(
                await client.call_tool("request_native_analysis_export", stale)
            )
            assert rejected["ok"] is False
            assert rejected["error"]["code"] == "approval_required"

    import asyncio

    asyncio.run(run())


def test_unconfigured_workspace_does_not_register_native_mcp_tool(tmp_path):
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async def run():
        async with (
            stdio_client(
                _mcp_parameters(tmp_path, workspace=tmp_path / "workspace")
            ) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert len(names) == 19
            assert "request_native_analysis_export" not in names

    import asyncio

    asyncio.run(run())


def _run_workflow_cli(root, tmp_path, *arguments):
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "once_sport_mcp.workflow_cli",
            "--root",
            str(root),
            *map(str, arguments),
        ],
        capture_output=True,
        text=True,
        env=dict(os.environ, TMPDIR=str(tmp_path)),
        check=False,
    )
    envelope = json.loads(completed.stdout) if completed.stdout else None
    return completed, envelope


def test_cli_native_export_requires_digest_and_queues_json_result(tmp_path):
    root = tmp_path / "workspace"
    native_path = tmp_path / "synthetic" / "OnceSport"
    workflow, project_id, proposal = _seed_unapproved_analysis(
        root, tmp_path, native_path
    )
    workflow.review_analysis(
        project_id,
        proposal["analysis_id"],
        proposal["digest"],
        True,
        "CLI test operator",
    )

    completed, envelope = _run_workflow_cli(
        root,
        tmp_path,
        "--native-once-executable",
        native_path,
        "export-native",
        project_id,
        proposal["analysis_id"],
        "--digest",
        proposal["digest"],
    )
    assert completed.returncode == 0
    assert envelope["ok"] is True
    job = workflow.get_job(envelope["result"]["job_id"])
    assert job["payload"] == {
        "analysis_id": proposal["analysis_id"],
        "digest": proposal["digest"],
        "backend": "native",
    }

    missing, missing_envelope = _run_workflow_cli(
        root,
        tmp_path,
        "--native-once-executable",
        native_path,
        "export-native",
        project_id,
        proposal["analysis_id"],
    )
    assert missing.returncode == 2
    assert missing.stdout == ""
    assert "--digest" in missing.stderr
    assert missing_envelope is None


def test_cli_native_capabilities_are_conditional_and_worker_flag_is_forwarded(
    tmp_path, monkeypatch
):
    root = tmp_path / "capabilities"
    native_path = tmp_path / "missing" / "OnceSport"
    default, default_json = _run_workflow_cli(root, tmp_path, "capabilities")
    configured, configured_json = _run_workflow_cli(
        root,
        tmp_path,
        "--native-once-executable",
        native_path,
        "capabilities",
    )
    assert default.returncode == configured.returncode == 0
    assert "native_export" not in default_json["result"]
    assert configured_json["result"]["native_export"]["configured"] is True
    assert configured_json["result"]["native_import_verified"] is False

    from once_sport_mcp import worker

    calls = []
    monkeypatch.setattr(
        worker,
        "run_worker",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "worker",
            "--root",
            str(root),
            "--once",
            "--native-once-executable",
            str(native_path),
        ],
    )
    worker.main()
    assert calls[-1][1]["native_once_executable"] == native_path
    monkeypatch.setattr(sys, "argv", ["worker", "--root", str(root), "--once"])
    worker.main()
    assert "native_once_executable" not in calls[-1][1]
