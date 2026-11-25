import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from once_sport_mcp.worker import run_worker
from once_sport_mcp.workflow import Workflow, WorkflowError


def write_renderer_sidecar(destination, drawings):
    import json

    sidecar = destination.with_name("reel.drawings.json")
    sidecar.write_text(
        json.dumps(
            {
                "semantics": "custom-render-only",
                "drawings": drawings,
                "manifest": {
                    "destination": str(destination),
                    "drawings_sidecar": str(sidecar),
                    "clips": [],
                },
            }
        )
    )
    return sidecar


def install_fake_yt_dlp(tmp_path, monkeypatch):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    downloader = fake_bin / "yt-dlp"
    downloader.write_text(
        """#!/usr/bin/env python3
import os
import subprocess
import sys
from pathlib import Path

arguments = sys.argv[1:]
destination = Path(arguments[arguments.index("-o") + 1])
subprocess.run(
    [
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "color=c=blue:s=160x120:r=10:d=2", "-c:v", "mpeg4",
        str(destination),
    ],
    check=True,
)
if "--max-downloads" in arguments and arguments[arguments.index("--max-downloads") + 1] == "1":
    raise SystemExit(101)
if os.environ.get("FAKE_YTDLP_EXIT"):
    raise SystemExit(int(os.environ["FAKE_YTDLP_EXIT"]))
"""
    )
    downloader.chmod(0o755)
    monkeypatch.setenv(
        "PATH", os.pathsep.join((str(fake_bin), os.environ.get("PATH", "")))
    )
    monkeypatch.delenv("FAKE_YTDLP_EXIT", raising=False)


def test_youtube_ingest_completes_with_single_video_downloader(tmp_path, monkeypatch):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required")
    from once_sport_mcp.workflow import Workflow

    install_fake_yt_dlp(tmp_path, monkeypatch)
    workflow = Workflow(tmp_path)
    created = workflow.create_project("Match", "https://youtu.be/abcdefghijk")

    run_worker(tmp_path, once=True)

    assert workflow.get_job(created["job_id"])["status"] == "completed"
    project = workflow.get_project(created["project_id"])
    assert project["media"]["width"] == 160
    assert project["media"]["height"] == 120
    assert project["media"]["duration"] == pytest.approx(2, abs=0.1)
    source = Path(project["source_path"])
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert project["source_digest"] == digest
    source_artifact = next(
        artifact
        for artifact in workflow.list_artifacts(project["id"])
        if artifact["kind"] == "source"
    )
    assert source_artifact["sha256"] == digest
    assert Path(source_artifact["path"]) == source


def test_youtube_ingest_nonzero_downloader_does_not_publish(tmp_path, monkeypatch):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required")
    from once_sport_mcp.workflow import Workflow

    install_fake_yt_dlp(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_YTDLP_EXIT", "2")
    workflow = Workflow(tmp_path)
    created = workflow.create_project("Match", "https://youtu.be/abcdefghijk")

    run_worker(tmp_path, once=True)

    assert workflow.get_job(created["job_id"])["status"] == "failed"
    assert not (workflow.task_dir(created["job_id"]) / "output").exists()
    assert workflow.list_artifacts(created["project_id"]) == []


def test_ingest_preserves_sample_aspect_ratio(tmp_path):
    workflow = Workflow(tmp_path)
    source = tmp_path / "inputs" / "anamorphic.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x120:r=10:d=1",
            "-vf",
            "setsar=4/3",
            "-c:v",
            "mpeg4",
            str(source),
        ],
        check=True,
    )
    created = workflow.create_project("Anamorphic", str(source))
    run_worker(tmp_path, once=True)
    media = workflow.get_project(created["project_id"])["media"]
    assert media["sample_aspect_ratio"] == pytest.approx(4 / 3)


def test_ingest_uses_video_duration_when_audio_runs_longer(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required")
    workflow = Workflow(tmp_path)
    source = tmp_path / "inputs" / "video-shorter-than-audio.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x120:r=10:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=1000:duration=4",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    created = workflow.create_project("Unequal stream durations", str(source))
    run_worker(tmp_path, once=True)
    project = workflow.get_project(created["project_id"])

    assert project["media"]["duration"] == pytest.approx(2, abs=0.1)

    evidence = workflow.queue_evidence(project["id"], 0, 1, 0.5)
    run_worker(tmp_path, once=True)
    with pytest.raises(WorkflowError, match="within the video duration"):
        workflow.submit_analysis(
            project["id"],
            [
                {
                    "event_id": "after-video",
                    "period": 1,
                    "start_seconds": 2.5,
                    "end_seconds": 3,
                    "team": "Home",
                    "phase": "attacking",
                    "action": "Pass",
                }
            ],
            [],
            "# Analysis",
            [evidence["job_id"]],
        )


def test_real_ingest_and_evidence_survive_reopen(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required")
    workflow = Workflow(tmp_path)
    source = tmp_path / "inputs" / "tiny.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x120:r=10:d=2",
            "-c:v",
            "mpeg4",
            str(source),
        ],
        check=True,
    )
    created = workflow.create_project("Match", str(source))
    run_worker(tmp_path, once=True)
    workflow = Workflow(tmp_path)
    assert workflow.get_job(created["job_id"])["status"] == "completed"
    project = workflow.get_project(created["project_id"])
    assert project["media"]["width"] == 160
    assert project["source_digest"]
    job = workflow.queue_evidence(project["id"], 0, 1, 0.5)
    run_worker(tmp_path, once=True)
    assert workflow.get_job(job["job_id"])["status"] == "completed"
    artifacts = workflow.list_artifacts(project["id"])
    assert len([a for a in artifacts if a["kind"] == "frame"]) == 2


@pytest.mark.parametrize(
    "drawings",
    [
        pytest.param([], id="empty-drawings"),
        pytest.param(
            [
                {
                    "id": "tracked-arrow",
                    "event_id": "a",
                    "kind": "arrow",
                    "start_seconds": 0.25,
                    "end_seconds": 0.75,
                    "points": [[0.2, 0.5], [0.8, 0.5]],
                    "keyframes": [
                        {
                            "time_seconds": 0.25,
                            "points": [[0.2, 0.5], [0.8, 0.5]],
                        },
                        {
                            "time_seconds": 0.75,
                            "points": [[0.3, 0.4], [0.9, 0.6]],
                        },
                    ],
                }
            ],
            id="timed-keyframed-drawing",
        ),
    ],
)
def test_real_worker_export_publishes_reel_and_sidecar(tmp_path, drawings):
    import json

    from once_sport_mcp.workflow import digest_file

    workflow = Workflow(tmp_path)
    source = tmp_path / "inputs" / "tiny-anamorphic.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x120:r=8:d=2",
            "-vf",
            "setsar=4/3",
            "-c:v",
            "mpeg4",
            str(source),
        ],
        check=True,
    )
    created = workflow.create_project("Synthetic match", str(source))
    run_worker(tmp_path, once=True)
    project = workflow.get_project(created["project_id"])
    assert project["media"]["sample_aspect_ratio"] == pytest.approx(4 / 3)
    evidence = workflow.queue_evidence(project["id"], 0, 1, 0.5)
    run_worker(tmp_path, once=True)
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
    proposal = workflow.submit_analysis(
        project["id"], events, drawings, "# Tactical report", [evidence["job_id"]]
    )
    review = workflow.review_analysis(
        project["id"], proposal["analysis_id"], proposal["digest"], True, "Coach"
    )
    export = workflow.queue_export(project["id"], proposal["analysis_id"])
    run_worker(tmp_path, once=True)

    job = workflow.get_job(export["job_id"])
    assert job["status"] == "completed"
    output = workflow.task_dir(export["job_id"]) / "output"
    reel = output / "reel.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(reel), "-f", "null", "-"],
        check=True,
        capture_output=True,
    )
    sidecar_path = output / "reel.drawings.json"
    sidecar = json.loads(sidecar_path.read_text())
    assert sidecar["semantics"] == "custom-render-only"
    assert sidecar["drawings"] == drawings
    renderer_manifest = sidecar["manifest"]
    assert renderer_manifest["destination"] == "reel.mp4"
    assert renderer_manifest["drawings_sidecar"] == "reel.drawings.json"
    assert (output / renderer_manifest["destination"]).is_file()
    assert (output / renderer_manifest["drawings_sidecar"]).is_file()
    assert renderer_manifest["clips"] == [
        {
            "event_id": "a",
            "source_start_seconds": 0,
            "source_end_seconds": 1,
            "reel_start_seconds": 0,
            "reel_end_seconds": 1,
            "frame_count": 8,
        }
    ]
    assert (output / "analysis.md").read_text() == "# Tactical report"
    assert (
        json.loads((output / "proposal.json").read_text())
        == workflow.get_analysis(proposal["analysis_id"])["proposal"]
    )
    assert json.loads((output / "review.json").read_text()) == review["reviews"][-1]
    assert (output / "events.csv").stat().st_size > 0
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["digest"] == proposal["digest"]
    assert manifest["source_digest"] == project["source_digest"]
    assert set(manifest["files"]) == {
        "analysis.md",
        "events.csv",
        "proposal.json",
        "reel.drawings.json",
        "reel.mp4",
        "review.json",
    }
    assert manifest["files"]["reel.drawings.json"] == digest_file(sidecar_path)
    assert all(
        manifest["files"][name] == digest_file(output / name)
        for name in manifest["files"]
    )
    artifacts = [
        item
        for item in workflow.list_artifacts(project["id"])
        if item["job_id"] == export["job_id"]
    ]
    assert {item["kind"] for item in artifacts} == {
        "reel",
        "drawings_sidecar",
        "report",
        "proposal",
        "review",
        "once_csv",
        "manifest",
    }


def test_export_keeps_exact_review_and_analysis(tmp_path, monkeypatch):
    import sys
    import types

    from once_sport_mcp.workflow import digest_file

    workflow = Workflow(tmp_path)
    source = tmp_path / "inputs" / "tiny.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=s=160x120:r=10:d=2",
            "-c:v",
            "mpeg4",
            str(source),
        ],
        check=True,
    )
    created = workflow.create_project("Match", str(source))
    run_worker(tmp_path, once=True)
    evidence = workflow.queue_evidence(created["project_id"], 0, 1)
    run_worker(tmp_path, once=True)
    events = [
        {
            "event_id": "a",
            "period": 1,
            "start_seconds": 0,
            "end_seconds": 1,
            "team": "A",
            "phase": "goal",
            "action": "Shot",
        }
    ]
    proposal = workflow.submit_analysis(
        created["project_id"], events, [], "# Analysis only", [evidence["job_id"]]
    )
    workflow.review_analysis(
        created["project_id"],
        proposal["analysis_id"],
        proposal["digest"],
        True,
        "Coach",
    )
    called = []

    def render(source, events, drawings, destination, *, cancelled):
        called.append((events, drawings, cancelled()))
        shutil.copyfile(source, destination)
        write_renderer_sidecar(destination, drawings)

    monkeypatch.setitem(
        sys.modules,
        "once_sport_mcp.rendering",
        types.SimpleNamespace(render_reel=render),
    )
    export = workflow.queue_export(created["project_id"], proposal["analysis_id"])
    run_worker(tmp_path, once=True)
    assert workflow.get_job(export["job_id"])["status"] == "completed"
    assert called == [(events, [], False)]
    import json

    output = workflow.task_dir(export["job_id"]) / "output"
    assert (output / "analysis.md").read_text() == "# Analysis only"
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["native_import_verified"] is False
    assert manifest["files"]["reel.mp4"] == digest_file(output / "reel.mp4")
    assert manifest["files"]["reel.drawings.json"] == digest_file(
        output / "reel.drawings.json"
    )
    assert {
        a["kind"]
        for a in workflow.list_artifacts(created["project_id"])
        if a["job_id"] == export["job_id"]
    } == {
        "reel",
        "drawings_sidecar",
        "report",
        "once_csv",
        "proposal",
        "review",
        "manifest",
    }
    assert (
        json.loads((output / "review.json").read_text())["digest"] == proposal["digest"]
    )


def test_evidence_manifest_lists_exact_samples(tmp_path):
    import json

    workflow = Workflow(tmp_path)
    source = tmp_path / "inputs" / "tiny.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=s=160x120:r=10:d=2",
            "-c:v",
            "mpeg4",
            str(source),
        ],
        check=True,
    )
    created = workflow.create_project("Match", str(source))
    run_worker(tmp_path, once=True)
    evidence = workflow.queue_evidence(created["project_id"], 0.25, 1.25, 0.5)
    run_worker(tmp_path, once=True)
    manifest = json.loads(
        (workflow.task_dir(evidence["job_id"]) / "output" / "evidence.json").read_text()
    )
    assert manifest["frames"] == [
        {
            "filename": "frame-000.jpg",
            "requested_seek_seconds": 0.25,
            "timestamp_seconds": 0.3,
        },
        {
            "filename": "frame-001.jpg",
            "requested_seek_seconds": 0.75,
            "timestamp_seconds": 0.8,
        },
    ]
    assert "sample" in manifest["disclaimer"].lower()
    assert manifest["timestamp_basis"] == "decoded_source_relative_pts"
    assert "showinfo" in manifest["timestamp_precision"]


def test_cli_accepts_allow_source_dir(monkeypatch, tmp_path):
    import sys

    from once_sport_mcp import worker

    calls = []
    monkeypatch.setattr(
        worker, "run_worker", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "worker",
            "--root",
            str(tmp_path),
            "--once",
            "--allow-source-dir",
            str(tmp_path / "media"),
        ],
    )
    worker.main()
    assert calls[0][1]["allowed_source_dirs"] == [tmp_path / "media"]


def test_hash_checks_cancel_between_chunks(tmp_path, monkeypatch):
    from once_sport_mcp.worker import Cancelled, Worker

    worker = Worker(Workflow(tmp_path))
    source = tmp_path / "large"
    source.write_bytes(b"x" * (2 * 1024 * 1024))
    checks = []

    def check(*args):
        checks.append(True)
        if len(checks) == 2:
            raise Cancelled()

    monkeypatch.setattr(worker, "check", check)
    with pytest.raises(Cancelled):
        worker.hash_file(source)


def test_cancel_terminates_descendant_which_ignores_term(tmp_path, monkeypatch):
    import os
    import signal
    import sys
    import time

    from once_sport_mcp.worker import Cancelled, Worker

    workflow = Workflow(tmp_path)
    worker = Worker(workflow)
    pidfile = tmp_path / "child.pid"
    child = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    script = f'import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c",{child!r}]); open({str(pidfile)!r},"w").write(str(p.pid)); time.sleep(30)'

    def check(*args):
        if pidfile.exists():
            time.sleep(0.1)
            raise Cancelled()

    monkeypatch.setattr(worker, "check", check)
    child_pid = None
    try:
        with pytest.raises(Cancelled):
            worker.command([sys.executable, "-c", script])
        child_pid = int(pidfile.read_text())
        for _ in range(30):
            state = Path(f"/proc/{child_pid}/stat")
            if not state.exists() or state.read_text().split()[2] == "Z":
                break
            time.sleep(0.05)
        else:
            pytest.fail("descendant kept running after cancellation")
    finally:
        if child_pid:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_probe_restricts_container_demuxers(tmp_path, monkeypatch):
    import json

    from once_sport_mcp.worker import Worker

    worker = Worker(Workflow(tmp_path))
    calls = []

    def command(argv):
        calls.append(argv)
        return json.dumps(
            {
                "format": {"duration": "1"},
                "streams": [
                    {
                        "codec_type": "video",
                        "avg_frame_rate": "10/1",
                        "width": 10,
                        "height": 10,
                    }
                ],
            }
        ).encode()

    monkeypatch.setattr(worker, "command", command)
    worker.probe(tmp_path / "renamed.mp4")
    assert calls[0][calls[0].index("-format_whitelist") + 1] == "mov,matroska,webm,avi"


def test_worker_stop_signal_interrupts_job_and_restores_handlers(tmp_path, monkeypatch):
    import signal

    from once_sport_mcp import worker

    workflow = Workflow(tmp_path)
    created = workflow.create_project("Match", "https://youtu.be/abcdefghijk")
    previous = signal.getsignal(signal.SIGTERM)
    installed = []
    original_signal = signal.signal

    def register(signum, handler):
        if signum == signal.SIGTERM:
            installed.append(handler)
        return original_signal(signum, handler)

    monkeypatch.setattr(signal, "signal", register)

    def ingest(self, stage):
        assert installed
        installed[0](signal.SIGTERM, None)
        self.check()

    monkeypatch.setattr(worker.Worker, "ingest", ingest)
    worker.run_worker(tmp_path, once=True)
    assert workflow.get_job(created["job_id"])["status"] == "interrupted"
    assert signal.getsignal(signal.SIGTERM) == previous


def test_downloader_cannot_publish_symlink(tmp_path, monkeypatch):
    from once_sport_mcp import worker

    workflow = Workflow(tmp_path)
    created = workflow.create_project("Match", "https://youtu.be/abcdefghijk")
    outside = tmp_path / "outside"
    outside.write_bytes(b"video")
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/tool")

    def command(self, argv):
        Path(argv[argv.index("-o") + 1]).symlink_to(outside)

    monkeypatch.setattr(worker.Worker, "command", command)
    monkeypatch.setattr(
        worker.Worker,
        "probe",
        lambda *args: {"duration": 1, "width": 1, "height": 1, "fps": 1},
    )
    run_worker(tmp_path, once=True)
    assert workflow.get_job(created["job_id"])["status"] == "failed"
    assert not (workflow.task_dir(created["job_id"]) / "output").exists()


def test_downloader_has_rate_and_disk_budget(tmp_path, monkeypatch):
    from once_sport_mcp import worker

    workflow = Workflow(tmp_path)
    created = workflow.create_project("Match", "https://youtu.be/abcdefghijk")
    calls = []
    monkeypatch.setattr(worker.shutil, "which", lambda name: "/usr/bin/tool")

    def command(self, argv):
        calls.append(argv)
        raise RuntimeError("no network used")

    monkeypatch.setattr(worker.Worker, "command", command)
    run_worker(tmp_path, once=True)
    args = calls[0]
    assert args[args.index("--limit-rate") + 1] == "10M"
    assert (
        int(args[args.index("--max-filesize") + 1])
        <= worker.shutil.disk_usage(tmp_path).free - 128 * 1024**2
    )
    assert workflow.get_job(created["job_id"])["status"] == "failed"


def test_exception_after_commit_preserves_completed_output(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from once_sport_mcp.worker import Worker

    workflow = Workflow(tmp_path)
    created = workflow.create_project("Match", "https://youtu.be/abcdefghijk")
    worker = Worker(workflow)

    def ingest(stage):
        source = stage / "source.mp4"
        source.write_bytes(b"video")
        return {"media": {"duration": 1}, "source_digest": "digest"}, [
            ("source", source)
        ]

    monkeypatch.setattr(worker, "ingest", ingest)
    original = workflow.connect
    raised = []

    @contextmanager
    def connection():
        with original() as con:
            yield con
        with original() as con:
            current = workflow._get("jobs", created["job_id"], con)
        if current["status"] == "completed" and not raised:
            raised.append(True)
            raise OSError("after committed publication")

    monkeypatch.setattr(workflow, "connect", connection)
    worker.execute(workflow.get_job(created["job_id"]))
    assert workflow.get_job(created["job_id"])["status"] == "completed"
    assert (
        workflow.task_dir(created["job_id"]) / "output" / "source.mp4"
    ).read_bytes() == b"video"


def prepared_export(workflow):
    from once_sport_mcp.workflow import digest_file

    created = workflow.create_project("Match", "https://youtu.be/abcdefghijk")
    directory = workflow.task_dir(created["job_id"]) / "output"
    directory.mkdir()
    source = directory / "source.mp4"
    source.write_bytes(b"immutable source")
    with workflow.connect() as con:
        project = workflow._get("projects", created["project_id"], con)
        project.update(
            media={"duration": 10},
            source_path=str(source),
            source_digest=digest_file(source),
        )
        workflow._put(con, "projects", project)
        ingest = workflow._get("jobs", created["job_id"], con)
        ingest["status"] = "completed"
        workflow._put(con, "jobs", ingest)
        evidence = workflow._job(
            con, project["id"], "evidence", {"source_digest": project["source_digest"]}
        )
        evidence["status"] = "completed"
        workflow._put(con, "jobs", evidence)
    events = [
        {
            "event_id": "a",
            "period": 1,
            "start_seconds": 0,
            "end_seconds": 1,
            "team": "A",
            "phase": "goal",
            "action": "Shot",
        }
    ]
    proposal = workflow.submit_analysis(
        project["id"], events, [], "# Report", [evidence["id"]]
    )
    workflow.review_analysis(
        project["id"], proposal["analysis_id"], proposal["digest"], True, "Coach"
    )
    export = workflow.queue_export(project["id"], proposal["analysis_id"])
    return project, proposal, export, source


def test_source_mutation_during_render_blocks_publication(tmp_path, monkeypatch):
    import sys
    import types

    from once_sport_mcp.worker import Worker

    workflow = Workflow(tmp_path)
    _, _, export, _ = prepared_export(workflow)

    def render(source, events, drawings, destination, *, cancelled):
        destination.write_bytes(b"video")
        write_renderer_sidecar(destination, drawings)
        source.write_bytes(b"changed source")

    monkeypatch.setitem(
        sys.modules,
        "once_sport_mcp.rendering",
        types.SimpleNamespace(render_reel=render),
    )
    monkeypatch.setattr(Worker, "probe", lambda *args: {"duration": 1})
    run_worker(tmp_path, once=True)
    assert workflow.get_job(export["job_id"])["status"] == "failed"
    assert not (workflow.task_dir(export["job_id"]) / "output").exists()


def test_renderer_symlink_is_rejected_before_probe(tmp_path, monkeypatch):
    import sys
    import types

    from once_sport_mcp.worker import Worker

    workflow = Workflow(tmp_path)
    _, _, export, source = prepared_export(workflow)

    def render(source, events, drawings, destination, *, cancelled):
        destination.symlink_to(source)
        write_renderer_sidecar(destination, drawings)

    calls = []
    monkeypatch.setitem(
        sys.modules,
        "once_sport_mcp.rendering",
        types.SimpleNamespace(render_reel=render),
    )
    monkeypatch.setattr(
        Worker, "probe", lambda *args: calls.append(True) or {"duration": 1}
    )
    run_worker(tmp_path, once=True)
    assert not calls
    assert workflow.get_job(export["job_id"])["status"] == "failed"
    assert source.is_file()


def test_unsafe_task_directory_marks_job_failed_without_deleting_target(tmp_path):
    workflow = Workflow(tmp_path / "root")
    created = workflow.create_project("Match", "https://youtu.be/abcdefghijk")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    (workflow.root / "tasks" / created["job_id"]).symlink_to(
        outside, target_is_directory=True
    )
    run_worker(workflow.root, once=True)
    assert workflow.get_job(created["job_id"])["status"] == "failed"
    assert (outside / "keep").read_text() == "keep"


def test_restart_marks_running_job_interrupted_and_requires_new_retry(tmp_path):
    workflow = Workflow(tmp_path)
    created = workflow.create_project("Match", "https://youtu.be/abcdefghijk")
    with workflow.connect() as con:
        job = workflow._get("jobs", created["job_id"], con)
        job["status"] = "running"
        workflow._put(con, "jobs", job)
    run_worker(tmp_path, once=True)
    assert workflow.get_job(job["id"])["status"] == "interrupted"
    retry = workflow.retry_job(job["id"])
    assert retry["job_id"] != job["id"]
    assert workflow.get_job(retry["job_id"])["status"] == "queued"


def test_second_worker_cannot_claim_held_root(tmp_path):
    import fcntl

    from once_sport_mcp.workflow import WorkflowError

    workflow = Workflow(tmp_path)
    created = workflow.create_project("Match", "https://youtu.be/abcdefghijk")
    with (tmp_path / "worker.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(WorkflowError, match="already owns"):
            run_worker(tmp_path, once=True)
    assert workflow.get_job(created["job_id"])["status"] == "queued"


def test_rejected_queued_export_never_calls_renderer(tmp_path, monkeypatch):
    import sys
    import types

    workflow = Workflow(tmp_path)
    project, proposal, export, _ = prepared_export(workflow)
    workflow.review_analysis(
        project["id"], proposal["analysis_id"], proposal["digest"], False, "Coach"
    )
    calls = []
    monkeypatch.setitem(
        sys.modules,
        "once_sport_mcp.rendering",
        types.SimpleNamespace(render_reel=lambda *args, **kwargs: calls.append(True)),
    )
    run_worker(tmp_path, once=True)
    assert not calls
    assert workflow.get_job(export["job_id"])["status"] == "failed"
    assert not (workflow.task_dir(export["job_id"]) / "output").exists()


def test_tampered_staged_source_blocks_evidence(tmp_path):
    workflow = Workflow(tmp_path)
    project, _, _, source = prepared_export(workflow)
    # Skip the queued export so this worker claims the evidence request.
    queued_export = workflow.list_jobs(project["id"])[-1]
    workflow.cancel_job(queued_export["id"])
    evidence = workflow.queue_evidence(project["id"], 0, 1)
    source.write_bytes(b"tampered")
    run_worker(tmp_path, once=True)
    job = workflow.get_job(evidence["job_id"])
    assert job["status"] == "failed"
    assert job["error_code"] == "source_tampered"


def test_process_failure_is_retryable_and_hides_diagnostics(tmp_path, monkeypatch):
    import sys

    from once_sport_mcp import worker

    workflow = Workflow(tmp_path)
    created = workflow.create_project("Match", "https://youtu.be/abcdefghijk")
    children = []
    original = worker.subprocess.Popen

    def popen(*args, **kwargs):
        process = original(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(worker.subprocess, "Popen", popen)

    def ingest(self, stage):
        self.command(
            [
                sys.executable,
                "-c",
                'import sys;sys.stderr.write("PRIVATE_DIAGNOSTIC");sys.exit(3)',
            ]
        )

    monkeypatch.setattr(worker.Worker, "ingest", ingest)
    run_worker(tmp_path, once=True)
    job = workflow.get_job(created["job_id"])
    assert job["status"] == "failed"
    assert job["retryable"] is True
    assert "PRIVATE_DIAGNOSTIC" not in str(job)
    assert children[0].returncode == 3
    assert workflow.list_artifacts(created["project_id"]) == []


def test_running_cancel_reaps_child_and_publishes_nothing(tmp_path, monkeypatch):
    import sys

    from once_sport_mcp import worker

    workflow = Workflow(tmp_path)
    created = workflow.create_project("Match", "https://youtu.be/abcdefghijk")
    children = []
    original = worker.subprocess.Popen

    def popen(*args, **kwargs):
        process = original(*args, **kwargs)
        children.append(process)
        workflow.cancel_job(created["job_id"])
        return process

    monkeypatch.setattr(worker.subprocess, "Popen", popen)

    def ingest(self, stage):
        self.command([sys.executable, "-c", "import time;time.sleep(30)"])

    monkeypatch.setattr(worker.Worker, "ingest", ingest)
    run_worker(tmp_path, once=True)
    assert workflow.get_job(created["job_id"])["status"] == "cancelled"
    assert children[0].returncode is not None
    assert workflow.list_artifacts(created["project_id"]) == []
    assert not (workflow.task_dir(created["job_id"]) / "output").exists()


def test_source_change_during_copy_fails_without_publication(tmp_path, monkeypatch):
    from once_sport_mcp.worker import Worker

    workflow = Workflow(tmp_path)
    source = tmp_path / "inputs" / "match.mp4"
    source.write_bytes(b"a" * (2 * 1024 * 1024))
    created = workflow.create_project("Match", str(source))
    original = Worker.check
    changed = []

    def check(self, progress=None):
        if progress is not None and not changed:
            changed.append(True)
            source.write_bytes(b"b" * (2 * 1024 * 1024))
        return original(self, progress)

    monkeypatch.setattr(Worker, "check", check)
    run_worker(tmp_path, once=True)
    job = workflow.get_job(created["job_id"])
    assert job["status"] == "failed"
    assert job["error_code"] == "source_changed"
    assert workflow.list_artifacts(created["project_id"]) == []


def test_unexpected_renderer_output_is_rejected_before_probe(tmp_path, monkeypatch):
    import sys
    import types

    from once_sport_mcp.worker import Worker

    workflow = Workflow(tmp_path)
    _, _, export, _source = prepared_export(workflow)

    def render(source, events, drawings, destination, *, cancelled):
        destination.write_bytes(b"video")
        write_renderer_sidecar(destination, drawings)
        destination.with_name("extra.json").symlink_to(source)

    calls = []
    monkeypatch.setitem(
        sys.modules,
        "once_sport_mcp.rendering",
        types.SimpleNamespace(render_reel=render),
    )
    monkeypatch.setattr(
        Worker, "probe", lambda *args: calls.append(True) or {"duration": 1}
    )
    run_worker(tmp_path, once=True)
    assert not calls
    assert workflow.get_job(export["job_id"])["status"] == "failed"


def test_publication_syncs_files_and_directories(tmp_path, monkeypatch):
    import os

    from once_sport_mcp.worker import Worker

    workflow = Workflow(tmp_path)
    created = workflow.create_project("Match", "https://youtu.be/abcdefghijk")
    worker = Worker(workflow)

    def ingest(stage):
        source = stage / "source.mp4"
        source.write_bytes(b"video")
        return {"media": {"duration": 1}, "source_digest": "digest"}, [
            ("source", source)
        ]

    monkeypatch.setattr(worker, "ingest", ingest)
    synced = []
    original = os.fsync

    def fsync(fd):
        synced.append(os.readlink(f"/proc/self/fd/{fd}"))
        original(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    worker.execute(workflow.get_job(created["job_id"]))
    assert any(name.endswith("/source.mp4") for name in synced)
    assert any("staging-" in Path(name).name for name in synced)
    assert str(workflow.task_dir(created["job_id"])) in synced


def test_source_swap_to_symlink_after_validation_is_rejected(tmp_path, monkeypatch):
    from once_sport_mcp.worker import Worker

    workflow = Workflow(tmp_path)
    source = tmp_path / "inputs" / "match.mp4"
    source.write_bytes(b"allowed")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside synthetic source")
    created = workflow.create_project("Match", str(source))
    original = Workflow._source

    def validate(self, value):
        result = original(self, value)
        source.unlink()
        source.symlink_to(outside)
        return result

    monkeypatch.setattr(Workflow, "_source", validate)
    monkeypatch.setattr(Worker, "probe", lambda *args: {"duration": 1})
    run_worker(tmp_path, once=True)
    assert workflow.get_job(created["job_id"])["status"] == "failed"
    assert not (workflow.task_dir(created["job_id"]) / "output").exists()


def real_export_for_render_cancellation(tmp_path):
    import shutil

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required")
    workflow = Workflow(tmp_path)
    source = tmp_path / "inputs" / "tiny-render-cancel.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x120:r=8:d=2",
            "-c:v",
            "mpeg4",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    created = workflow.create_project("Synthetic render cancellation", str(source))
    run_worker(tmp_path, once=True)
    project = workflow.get_project(created["project_id"])
    evidence = workflow.queue_evidence(project["id"], 0, 1, 0.5)
    run_worker(tmp_path, once=True)
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
    proposal = workflow.submit_analysis(
        project["id"], events, [], "# Synthetic report", [evidence["job_id"]]
    )
    workflow.review_analysis(
        project["id"], proposal["analysis_id"], proposal["digest"], True, "Coach"
    )
    export = workflow.queue_export(project["id"], proposal["analysis_id"])
    return workflow, project, export


def run_export_worker_paused_in_renderer(workflow, export, *, operator_cancel):
    import os
    import signal
    import sys
    import time

    root = workflow.root
    ready = root / "renderer-child.ready"
    release = root / "renderer-child.release"
    script = """
import os
import subprocess
import sys
import time
from pathlib import Path

root, ready, release = map(Path, sys.argv[1:])
sys.path.insert(0, str(root / "src"))
original_popen = subprocess.Popen

def synchronized_popen(args, *popen_args, **popen_kwargs):
    process = original_popen(args, *popen_args, **popen_kwargs)
    if (
        not ready.exists()
        and isinstance(args, (list, tuple))
        and args
        and Path(args[0]).name in {"ffprobe", "ffmpeg"}
    ):
        ready.write_text(f"{os.getpid()}:{process.pid}")
        deadline = time.monotonic() + 20
        while not release.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("renderer synchronization timed out")
            time.sleep(0.01)
    return process

subprocess.Popen = synchronized_popen
from once_sport_mcp.worker import run_worker

run_worker(root, once=True)
"""
    env = os.environ.copy()
    env["TMPDIR"] = os.environ["TMPDIR"]
    source_root = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_root, env.get("PYTHONPATH", "")) if part
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(root),
            str(ready),
            str(release),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                stdout, stderr = process.communicate(timeout=5)
                pytest.fail(
                    "worker did not reach the renderer subprocess; "
                    f"stdout={stdout!r}, stderr={stderr!r}"
                )
            time.sleep(0.01)
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=5)
            pytest.fail(
                f"worker exited before renderer synchronization: {stdout!r} {stderr!r}"
            )
        worker_pid, renderer_pid = map(int, ready.read_text().split(":"))
        assert worker_pid == process.pid
        assert renderer_pid > 0
        if operator_cancel:
            workflow.cancel_job(export["job_id"])
        else:
            os.kill(process.pid, signal.SIGTERM)
        release.touch()
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, f"stdout={stdout!r}, stderr={stderr!r}"
        return renderer_pid
    finally:
        release.touch(exist_ok=True)
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_worker_stop_during_real_export_is_interrupted_without_publication(tmp_path):
    workflow, project, export = real_export_for_render_cancellation(tmp_path)
    renderer_pid = run_export_worker_paused_in_renderer(
        workflow, export, operator_cancel=False
    )
    job = workflow.get_job(export["job_id"])
    assert job["status"] == "interrupted"
    assert not (workflow.task_dir(export["job_id"]) / "output").exists()
    assert not [
        artifact
        for artifact in workflow.list_artifacts(project["id"])
        if artifact["job_id"] == export["job_id"]
    ]
    assert renderer_pid > 0


def test_operator_cancel_during_real_export_is_cancelled_without_publication(tmp_path):
    workflow, project, export = real_export_for_render_cancellation(tmp_path)
    renderer_pid = run_export_worker_paused_in_renderer(
        workflow, export, operator_cancel=True
    )
    job = workflow.get_job(export["job_id"])
    assert job["status"] == "cancelled"
    assert not (workflow.task_dir(export["job_id"]) / "output").exists()
    assert not [
        artifact
        for artifact in workflow.list_artifacts(project["id"])
        if artifact["job_id"] == export["job_id"]
    ]
    assert renderer_pid > 0


def test_genuine_renderer_error_during_export_remains_failed(tmp_path, monkeypatch):
    import sys
    import types

    from once_sport_mcp.rendering import RenderError

    workflow = Workflow(tmp_path)
    project, _, export, _ = prepared_export(workflow)

    def render(*args, **kwargs):
        raise RenderError("synthetic encoder failure")

    monkeypatch.setitem(
        sys.modules,
        "once_sport_mcp.rendering",
        types.SimpleNamespace(render_reel=render, RenderError=RenderError),
    )
    run_worker(tmp_path, once=True)
    job = workflow.get_job(export["job_id"])
    assert job["status"] == "failed"
    assert job["error_code"] == "job_failed"
    assert not (workflow.task_dir(export["job_id"]) / "output").exists()
    assert not [
        artifact
        for artifact in workflow.list_artifacts(project["id"])
        if artifact["job_id"] == export["job_id"]
    ]


def seed_running_job(workflow):
    created = workflow.create_project("Restart cleanup", "https://youtu.be/abcdefghijk")
    with workflow.connect() as con:
        job = workflow._get("jobs", created["job_id"], con)
        job["status"] = "running"
        workflow._put(con, "jobs", job)
    return created


def seed_restart_residue(workflow):
    created = seed_running_job(workflow)
    task = workflow.root / "tasks" / created["job_id"]
    task.mkdir(mode=0o700, exist_ok=True)
    staging = task / ("staging-" + "a" * 32)
    staging.mkdir()
    (staging / "partial").write_text("synthetic staging residue")
    output = task / "output"
    output.mkdir()
    (output / "partial").write_text("synthetic uncommitted output")
    return created, task, staging, output


def test_startup_removes_interrupted_job_residue_and_preserves_neighbors(tmp_path):
    workflow = Workflow(tmp_path)
    created, task, staging, output = seed_restart_residue(workflow)
    unknown_stage = task / "staging-not-a-generated-uuid"
    unknown_stage.mkdir()
    (unknown_stage / "keep").write_text("unknown entry")
    unrelated = task / "unrelated"
    unrelated.write_text("keep")

    completed = workflow.create_project("Completed job", "https://youtu.be/abcdefghijk")
    completed_task = workflow.root / "tasks" / completed["job_id"]
    completed_task.mkdir(mode=0o700, exist_ok=True)
    completed_output = completed_task / "output"
    completed_output.mkdir()
    (completed_output / "keep").write_text("committed output")
    with workflow.connect() as con:
        job = workflow._get("jobs", completed["job_id"], con)
        job["status"] = "completed"
        workflow._put(con, "jobs", job)

    run_worker(tmp_path, once=True)

    job = workflow.get_job(created["job_id"])
    assert job["status"] == "interrupted"
    assert job["retryable"] is True
    assert not staging.exists()
    assert not output.exists()
    assert (unknown_stage / "keep").read_text() == "unknown entry"
    assert unrelated.read_text() == "keep"
    assert (completed_output / "keep").read_text() == "committed output"


def test_startup_leaves_external_target_behind_task_symlink(tmp_path):
    workflow = Workflow(tmp_path)
    created = seed_running_job(workflow)
    task = workflow.root / "tasks" / created["job_id"]
    task.mkdir(mode=0o700, exist_ok=True)
    task.rmdir()
    outside = tmp_path / "external"
    outside.mkdir()
    output = outside / "output"
    output.mkdir()
    (output / "keep").write_text("outside data")
    task.symlink_to(outside, target_is_directory=True)

    run_worker(tmp_path, once=True)

    job = workflow.get_job(created["job_id"])
    assert job["status"] == "interrupted"
    assert job["cleanup_pending"] is True
    assert task.is_symlink()
    assert (output / "keep").read_text() == "outside data"


def test_startup_preserves_output_with_registered_source_or_artifact_reference(
    tmp_path,
):
    workflow = Workflow(tmp_path)
    created, _task, staging, output = seed_restart_residue(workflow)
    source = output / "source.mp4"
    source.write_bytes(b"registered synthetic source")
    artifact_path = output / "registered.bin"
    artifact_path.write_bytes(b"registered synthetic artifact")
    with workflow.connect() as con:
        project = workflow._get("projects", created["project_id"], con)
        project["source_path"] = str(source)
        workflow._put(con, "projects", project)
        workflow._put(
            con,
            "artifacts",
            {
                "id": "b" * 32,
                "project_id": created["project_id"],
                "job_id": created["job_id"],
                "kind": "reel",
                "path": str(artifact_path),
                "sha256": "synthetic",
                "size_bytes": artifact_path.stat().st_size,
            },
        )

    run_worker(tmp_path, once=True)

    job = workflow.get_job(created["job_id"])
    assert job["status"] == "interrupted"
    assert job["cleanup_pending"] is True
    assert job["cleanup_warning"]
    assert not staging.exists()
    assert source.read_bytes() == b"registered synthetic source"
    assert artifact_path.read_bytes() == b"registered synthetic artifact"


def test_startup_cleanup_failure_is_reported_and_retried(tmp_path, monkeypatch):
    from once_sport_mcp import worker as worker_module

    workflow = Workflow(tmp_path)
    created, _task, _staging, output = seed_restart_residue(workflow)
    original_rmtree = worker_module.shutil.rmtree
    failed = []

    def fail_output_once(path, *args, **kwargs):
        if Path(path) == output and not failed:
            failed.append(True)
            raise PermissionError("synthetic cleanup denial")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(worker_module.shutil, "rmtree", fail_output_once)
    run_worker(tmp_path, once=True)
    job = workflow.get_job(created["job_id"])
    assert job["status"] == "interrupted"
    assert job["retryable"] is True
    assert job["cleanup_pending"] is True
    assert job["cleanup_warning"]
    assert output.exists()

    monkeypatch.setattr(worker_module.shutil, "rmtree", original_rmtree)
    run_worker(tmp_path, once=True)
    job = workflow.get_job(created["job_id"])
    assert not output.exists()
    assert not job.get("cleanup_pending", False)
    assert not job.get("cleanup_warning")
