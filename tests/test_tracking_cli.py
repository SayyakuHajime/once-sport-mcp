"""CLI tracking stays explicitly enabled and queues only a seeded target."""


def test_tracking_target_parser_and_dispatch_forward_exact_arguments(tmp_path):
    from once_sport_mcp.workflow_cli import _build_parser, _dispatch

    root = tmp_path / "workspace"
    project_id = "a" * 32
    args = _build_parser().parse_args(
        [
            "--root",
            str(root),
            "--enable-tracking",
            "track-target",
            project_id,
            "--start",
            "0.5",
            "--end",
            "1.5",
            "--bbox",
            "0.1",
            "0.2",
            "0.3",
            "0.4",
        ]
    )
    calls = []

    class WorkflowSpy:
        def queue_tracking(self, *values):
            calls.append(values)
            return {"job_id": "queued"}

    assert args.enable_tracking is True
    assert args.command == "track-target"
    assert _dispatch(args, WorkflowSpy()) == {"job_id": "queued"}
    assert calls == [(project_id, 0.5, 1.5, [0.1, 0.2, 0.3, 0.4])]


def test_tracking_flag_and_capability_descriptor_are_opt_in(tmp_path):
    from types import SimpleNamespace

    from once_sport_mcp.workflow_cli import _build_parser, _capabilities
    from once_sport_mcp.workflow_config import tracking_descriptor

    root = tmp_path / "workspace"
    parser = _build_parser()
    default_args = parser.parse_args(["--root", str(root), "capabilities"])
    enabled_args = parser.parse_args(
        ["--root", str(root), "--enable-tracking", "capabilities"]
    )
    assert default_args.enable_tracking is False
    assert enabled_args.enable_tracking is True

    workflow = SimpleNamespace(
        root=root,
        allowed_source_dirs=[root / "inputs"],
        native_once_executable=None,
        tracking_enabled=False,
    )
    assert "tracking" not in _capabilities(workflow)
    workflow.tracking_enabled = True
    assert _capabilities(workflow)["tracking"] == tracking_descriptor()


def _seed_ready_project(root, tmp_path, *, tracking_enabled):
    from once_sport_mcp.workflow import Workflow

    workflow = Workflow(root, [tmp_path], tracking_enabled=tracking_enabled)
    created = workflow.create_project(
        "Synthetic tracking target", "https://youtu.be/abcdefghijk"
    )
    with workflow.connect() as connection:
        project = workflow._get("projects", created["project_id"], connection)
        project.update(
            source_digest="b" * 64,
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
    return workflow, created["project_id"]


def _run_cli(tmp_path, root, *arguments):
    import os
    import subprocess
    import sys

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "once_sport_mcp.workflow_cli",
            "--root",
            str(root),
            *arguments,
        ],
        capture_output=True,
        text=True,
        env=dict(os.environ, TMPDIR=str(tmp_path)),
        check=False,
    )


def test_tracking_cli_queues_synthetic_ready_project_and_requires_opt_in(tmp_path):
    import json

    enabled_root = tmp_path / "enabled-workspace"
    enabled_workflow, project_id = _seed_ready_project(
        enabled_root, tmp_path, tracking_enabled=True
    )
    bbox = [0.1, 0.2, 0.3, 0.4]
    completed = _run_cli(
        tmp_path,
        enabled_root,
        "--enable-tracking",
        "track-target",
        project_id,
        "--start",
        "0.25",
        "--end",
        "1.25",
        "--bbox",
        *map(str, bbox),
    )
    assert completed.returncode == 0, completed.stderr
    envelope = json.loads(completed.stdout)
    assert envelope["ok"] is True
    job = enabled_workflow.get_job(envelope["result"]["job_id"])
    assert job["kind"] == "evidence"
    assert job["status"] == "queued"
    assert job["payload"] == {
        "mode": "tracking",
        "source_digest": "b" * 64,
        "start_seconds": 0.25,
        "end_seconds": 1.25,
        "initial_bbox": bbox,
    }

    disabled_root = tmp_path / "disabled-workspace"
    _disabled_workflow, disabled_project_id = _seed_ready_project(
        disabled_root, tmp_path, tracking_enabled=False
    )
    disabled = _run_cli(
        tmp_path,
        disabled_root,
        "track-target",
        disabled_project_id,
        "--start",
        "0.25",
        "--end",
        "1.25",
        "--bbox",
        *map(str, bbox),
    )
    assert disabled.returncode == 1
    disabled_envelope = json.loads(disabled.stdout)
    assert disabled_envelope["ok"] is False
    assert disabled_envelope["error"]["code"] == "tracking_not_configured"


def test_tracking_cli_requires_all_bbox_values(tmp_path):
    root = tmp_path / "missing-bbox-workspace"
    completed = _run_cli(
        tmp_path,
        root,
        "--enable-tracking",
        "track-target",
        "a" * 32,
        "--start",
        "0",
        "--end",
        "1",
    )
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert "--bbox" in completed.stderr


def test_cli_constructor_kwargs_keep_default_native_and_tracking_shapes(
    tmp_path, monkeypatch, capsys
):
    from once_sport_mcp import workflow_cli

    calls = []

    class WorkflowSpy:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(workflow_cli, "Workflow", WorkflowSpy)
    monkeypatch.setattr(workflow_cli, "_dispatch", lambda _args, _workflow: {})
    root = tmp_path / "workspace"
    native = tmp_path / "native" / "OnceSport"
    cases = [
        (["--root", str(root), "capabilities"], {}),
        (
            [
                "--root",
                str(root),
                "--native-once-executable",
                str(native),
                "capabilities",
            ],
            {"native_once_executable": native},
        ),
        (
            ["--root", str(root), "--enable-tracking", "capabilities"],
            {"tracking_enabled": True},
        ),
        (
            [
                "--root",
                str(root),
                "--native-once-executable",
                str(native),
                "--enable-tracking",
                "capabilities",
            ],
            {"native_once_executable": native, "tracking_enabled": True},
        ),
    ]
    for arguments, expected_kwargs in cases:
        assert workflow_cli.main(arguments) == 0
        capsys.readouterr()
        assert calls[-1] == ((root, None), expected_kwargs)
