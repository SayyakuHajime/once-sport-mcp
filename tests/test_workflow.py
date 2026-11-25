from once_sport_mcp.workflow import Workflow


def test_project_queue_survives_reopen(tmp_path):
    root = tmp_path / "work"
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    source = inputs / "match.mp4"
    source.write_bytes(b"video")
    created = Workflow(root).create_project("Match", str(source))
    reopened = Workflow(root)
    assert reopened.get_job(created["job_id"])["status"] == "queued"
    assert reopened.get_project(created["project_id"])["title"] == "Match"


def test_cancel_retry_retains_history(tmp_path):
    import pytest

    from once_sport_mcp.workflow import WorkflowError

    workflow = Workflow(tmp_path)
    created = workflow.create_project("Match", "https://youtu.be/abcdefghijk")
    workflow.cancel_job(created["job_id"])
    retry = workflow.retry_job(created["job_id"])
    assert retry["job_id"] != created["job_id"]
    assert workflow.get_job(created["job_id"])["status"] == "cancelled"
    assert workflow.get_job(retry["job_id"])["retry_of"] == created["job_id"]
    with pytest.raises(WorkflowError, match="Only failed"):
        workflow.retry_job(retry["job_id"])


def test_review_is_bound_to_latest_proposal(tmp_path):
    import pytest

    from once_sport_mcp.workflow import WorkflowError

    workflow = Workflow(tmp_path)
    project_id = workflow.create_project("Match", "https://youtu.be/abcdefghijk")[
        "project_id"
    ]
    with workflow.connect() as con:
        project = workflow._get("projects", project_id, con)
        project.update(source_digest="abc", media={"duration": 10})
        workflow._put(con, "projects", project)
        evidence = workflow._job(con, project_id, "evidence", {"source_digest": "abc"})
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
        project_id, events, [], "# Report", [evidence["id"]]
    )
    workflow.review_analysis(
        project_id, proposal["analysis_id"], proposal["digest"], True, "Coach"
    )
    workflow.queue_export(project_id, proposal["analysis_id"], proposal["digest"])
    new = workflow.submit_analysis(
        project_id, events, [], "# Changed", [evidence["id"]]
    )
    assert new["revision"] == 2
    with pytest.raises(WorkflowError):
        workflow.queue_export(project_id, proposal["analysis_id"], proposal["digest"])


def test_url_id_must_be_literal_and_root_cannot_cross_symlink(tmp_path):
    import pytest

    from once_sport_mcp.workflow import WorkflowError

    workflow = Workflow(tmp_path / "root")
    with pytest.raises(WorkflowError):
        workflow.create_project("Match", "https://youtube.com/watch?v=%61bcdefghijk")
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "link"
    link.symlink_to(actual, target_is_directory=True)
    with pytest.raises(WorkflowError):
        Workflow(link / "child")


def test_missing_project_lists_and_running_cancel_state(tmp_path):
    import pytest

    from once_sport_mcp.workflow import WorkflowError

    workflow = Workflow(tmp_path)
    with pytest.raises(WorkflowError):
        workflow.list_jobs("0" * 32)
    with pytest.raises(WorkflowError):
        workflow.list_artifacts("0" * 32)
    created = workflow.create_project("Match", "https://youtu.be/abcdefghijk")
    with workflow.connect() as con:
        job = workflow._get("jobs", created["job_id"], con)
        job["status"] = "running"
        workflow._put(con, "jobs", job)
    assert workflow.cancel_job(job["id"])["status"] == "cancelling"


def ready_project(workflow):
    project_id = workflow.create_project("Match", "https://youtu.be/abcdefghijk")[
        "project_id"
    ]
    with workflow.connect() as con:
        project = workflow._get("projects", project_id, con)
        project.update(source_digest="abc", media={"duration": 10})
        workflow._put(con, "projects", project)
        evidence = workflow._job(con, project_id, "evidence", {"source_digest": "abc"})
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
    return project_id, evidence["id"], events


def test_submit_analysis_accepts_timed_keyframed_renderer_drawing(tmp_path):
    workflow = Workflow(tmp_path)
    project_id, evidence, events = ready_project(workflow)
    drawing = {
        "id": "arrow-1",
        "event_id": "a",
        "kind": "arrow",
        "start_seconds": 0,
        "end_seconds": 1,
        "points": [[0.1, 0.2], [0.8, 0.8]],
        "keyframes": [
            {"time_seconds": 0, "points": [[0.1, 0.2], [0.8, 0.8]]},
            {"time_seconds": 1, "points": [[0.2, 0.3], [0.9, 0.7]]},
        ],
    }
    proposal = workflow.submit_analysis(
        project_id, events, [drawing], "# Report", [evidence]
    )
    saved = workflow.get_analysis(proposal["analysis_id"])
    assert saved["status"] == "awaiting_review"
    assert saved["proposal"]["drawings"] == [drawing]


def test_drawings_require_shape_geometry_and_reject_unknown_keys(tmp_path):
    import pytest

    from once_sport_mcp.workflow import WorkflowError

    workflow = Workflow(tmp_path)
    project_id, evidence, events = ready_project(workflow)
    for drawing in [
        {
            "id": "missing-points",
            "event_id": "a",
            "kind": "arrow",
            "start_seconds": 0,
            "end_seconds": 1,
        },
        {
            "id": "unknown-field",
            "event_id": "a",
            "kind": "ellipse",
            "start_seconds": 0,
            "end_seconds": 1,
            "points": [[0.1, 0.2], [0.1, 0.1]],
            "secret": "hidden",
        },
    ]:
        with pytest.raises(WorkflowError):
            workflow.submit_analysis(
                project_id, events, [drawing], "# Report", [evidence]
            )


def test_renderer_rejects_invalid_drawing_plans_before_persistence(tmp_path):
    import pytest

    from once_sport_mcp.workflow import WorkflowError

    workflow = Workflow(tmp_path)
    project_id, evidence, events = ready_project(workflow)
    drawing = {
        "id": "tracked-arrow",
        "event_id": "a",
        "kind": "arrow",
        "start_seconds": 0,
        "end_seconds": 1,
        "points": [[0.1, 0.2], [0.8, 0.8]],
        "keyframes": [
            {"time_seconds": 0, "points": [[0.1, 0.2], [0.8, 0.8]]},
            {"time_seconds": 1, "points": [[0.2, 0.3], [0.9, 0.7]]},
        ],
    }
    invalid = []
    wrong_topology = dict(drawing, points=[[0.1, 0.2], [0.5, 0.5], [0.8, 0.8]])
    invalid.append(wrong_topology)
    outside_interval = dict(drawing, end_seconds=1.1)
    invalid.append(outside_interval)
    mismatched_keyframe = dict(
        drawing,
        keyframes=[
            {"time_seconds": 0, "points": [[0.1, 0.2], [0.8, 0.8]]},
            {"time_seconds": 1, "points": [[0.9, 0.7]]},
        ],
    )
    invalid.append(mismatched_keyframe)

    for candidate in invalid:
        with pytest.raises(WorkflowError) as error:
            workflow.submit_analysis(
                project_id, events, [candidate], "# Report", [evidence]
            )
        assert error.value.code == "invalid_render_plan"
        assert workflow._rows("analyses", project_id) == []
        assert workflow.get_project(project_id)["latest_analysis_id"] is None


def test_tiny_interval_is_typed_error(tmp_path):
    import pytest

    from once_sport_mcp.workflow import WorkflowError

    workflow = Workflow(tmp_path)
    project_id, _, _ = ready_project(workflow)
    with pytest.raises(WorkflowError):
        workflow.queue_evidence(project_id, 0, 1, 1e-320)


def test_extreme_integer_evidence_time_is_typed_error(tmp_path):
    import pytest

    from once_sport_mcp.workflow import WorkflowError

    workflow = Workflow(tmp_path)
    project_id, _, _ = ready_project(workflow)
    with pytest.raises(WorkflowError):
        workflow.queue_evidence(project_id, 0, 10**1000, 2)


import pytest

from once_sport_mcp.workflow import WorkflowError


@pytest.mark.parametrize(
    "start,end,interval",
    [
        (True, 1, 2),
        (0, float("nan"), 2),
        (0, float("inf"), 2),
        (0, 1, False),
        (-1, 1, 2),
        (0, 11, 2),
        (0, 1, 0),
    ],
)
def test_invalid_evidence_ranges(tmp_path, start, end, interval):
    workflow = Workflow(tmp_path)
    project_id, _, _ = ready_project(workflow)
    with pytest.raises(WorkflowError):
        workflow.queue_evidence(project_id, start, end, interval)


@pytest.mark.parametrize("key", ["../escape", "", "0" * 32])
def test_bad_or_missing_ids(tmp_path, key):
    workflow = Workflow(tmp_path)
    for operation in (
        workflow.get_project,
        workflow.get_job,
        workflow.cancel_job,
        workflow.retry_job,
        workflow.task_dir,
    ):
        if operation == workflow.task_dir and key == "0" * 32:
            continue
        with pytest.raises(WorkflowError):
            operation(key)


@pytest.mark.parametrize(
    "url",
    [
        "http://youtu.be/abcdefghijk",
        "https://user@youtu.be/abcdefghijk",
        "https://youtu.be:443/abcdefghijk",
        "https://youtube.com/watch?v=abcdefghijk&list=aaa",
        "https://youtu.be/abcdefghijk/",
        "https://evil.test/watch?v=abcdefghijk",
        "https://youtube.com/watch?v=abcdefghijk&v=abcdefghijk",
    ],
)
def test_rejects_unsafe_urls(tmp_path, url):
    with pytest.raises(WorkflowError):
        Workflow(tmp_path).create_project("Match", url)


def test_source_symlink_escape(tmp_path):
    workflow = Workflow(tmp_path / "root")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"fake")
    link = workflow.root / "inputs" / "link.mp4"
    link.symlink_to(outside)
    with pytest.raises(WorkflowError):
        workflow.create_project("Match", str(link))


def test_caller_mutation_stale_digest_and_rejection(tmp_path):
    workflow = Workflow(tmp_path)
    project_id, evidence, events = ready_project(workflow)
    proposal = workflow.submit_analysis(
        project_id, events, [], "# Original", [evidence]
    )
    events[0]["action"] = "Changed"
    assert (
        workflow.get_analysis(proposal["analysis_id"])["proposal"]["events"][0][
            "action"
        ]
        == "Shot"
    )
    with pytest.raises(WorkflowError):
        workflow.review_analysis(
            project_id, proposal["analysis_id"], "wrong", True, "Coach"
        )
    with pytest.raises(WorkflowError):
        workflow.review_analysis(
            project_id, proposal["analysis_id"], proposal["digest"], 1, "Coach"
        )
    workflow.review_analysis(
        project_id, proposal["analysis_id"], proposal["digest"], True, "Coach"
    )
    workflow.review_analysis(
        project_id, proposal["analysis_id"], proposal["digest"], False, "Coach"
    )
    with pytest.raises(WorkflowError):
        workflow.queue_export(project_id, proposal["analysis_id"], proposal["digest"])
    assert len(Workflow(tmp_path).get_analysis(proposal["analysis_id"])["reviews"]) == 2


@pytest.mark.parametrize(
    "events",
    [
        [],
        [
            {
                "event_id": "a",
                "period": 1,
                "start_seconds": 0,
                "end_seconds": float("nan"),
                "team": "A",
                "phase": "goal",
                "action": "Shot",
            }
        ],
        [
            {
                "event_id": "a",
                "period": 1,
                "start_seconds": False,
                "end_seconds": 1,
                "team": "A",
                "phase": "goal",
                "action": "Shot",
            }
        ],
        [{1: "mixed", "event_id": "a"}],
    ],
)
def test_invalid_analysis_is_domain_error(tmp_path, events):
    workflow = Workflow(tmp_path)
    project_id, evidence, _ = ready_project(workflow)
    with pytest.raises(WorkflowError):
        workflow.submit_analysis(project_id, events, [], "# Report", [evidence])


@pytest.mark.parametrize("change", ["missing", "tamper", "symlink"])
def test_artifacts_detect_missing_tampered_and_symlink_files(tmp_path, change):
    from once_sport_mcp.workflow import digest_file

    workflow = Workflow(tmp_path)
    project_id, evidence, _ = ready_project(workflow)
    path = workflow.task_dir(evidence) / "frame.jpg"
    path.write_bytes(b"frame")
    with workflow.connect() as con:
        workflow._put(
            con,
            "artifacts",
            {
                "id": "1" * 32,
                "project_id": project_id,
                "job_id": evidence,
                "path": str(path),
                "sha256": digest_file(path),
                "size_bytes": path.stat().st_size,
            },
        )
    if change == "missing":
        path.unlink()
    elif change == "tamper":
        path.write_bytes(b"changed")
    else:
        other = path.with_name("other")
        other.write_bytes(b"frame")
        path.unlink()
        path.symlink_to(other)
    with pytest.raises(WorkflowError):
        Workflow(tmp_path).list_artifacts(project_id)


def test_payload_surrogate_text_is_domain_error(tmp_path):
    workflow = Workflow(tmp_path)
    project_id, evidence, events = ready_project(workflow)
    with pytest.raises(WorkflowError):
        workflow.submit_analysis(project_id, events, [], "\ud800", [evidence])


def test_url_cannot_normalize_hidden_characters(tmp_path):
    workflow = Workflow(tmp_path)
    for source in (
        "https://youtu.be/abcdefghijk\n",
        " https://youtu.be/abcdefghijk",
        "HTTPS://youtu.be/abcdefghijk",
        "https://youtu.be/abcde\tfghijk",
    ):
        with pytest.raises(WorkflowError):
            workflow.create_project("Match", source)


def test_json_drawing_owner_and_large_geometry_are_domain_errors(tmp_path):
    workflow = Workflow(tmp_path)
    project_id, evidence, events = ready_project(workflow)
    for drawing in [
        {
            "id": "bad-owner",
            "event_id": [],
            "kind": "ellipse",
            "start_seconds": 0,
            "end_seconds": 1,
            "points": [[0.1, 0.2], [0.1, 0.1]],
        },
        {
            "id": "large-geometry",
            "event_id": "a",
            "kind": "ellipse",
            "start_seconds": 0,
            "end_seconds": 1,
            "points": [[10**400, 0.2], [0.1, 0.1]],
        },
    ]:
        with pytest.raises(WorkflowError):
            workflow.submit_analysis(
                project_id, events, [drawing], "# Report", [evidence]
            )


def test_deep_json_and_long_source_are_domain_errors(tmp_path):
    workflow = Workflow(tmp_path)
    project_id, evidence, events = ready_project(workflow)
    nested = []
    for _ in range(1100):
        nested = [nested]
    with pytest.raises(WorkflowError):
        workflow.submit_analysis(project_id, events, nested, "# Report", [evidence])
    with pytest.raises(WorkflowError):
        workflow.create_project(
            "Match", str(tmp_path / "inputs" / ("x" * 300 + ".mp4"))
        )


def test_existing_workflow_directories_are_private(tmp_path):
    import stat

    root = tmp_path / "work"
    (root / "tasks").mkdir(parents=True, mode=0o777)
    (root / "inputs").mkdir(mode=0o777)
    workflow = Workflow(root)
    for path in (root, root / "tasks", root / "inputs"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    task = root / "tasks" / ("f" * 32)
    task.mkdir(mode=0o777)
    workflow.task_dir("f" * 32)
    assert stat.S_IMODE(task.stat().st_mode) == 0o700


@pytest.mark.parametrize("change", ["missing", "tamper", "symlink", "size"])
def test_get_artifact_verifies_one_record_and_rejects_bad_content(tmp_path, change):
    import pytest

    from once_sport_mcp.workflow import WorkflowError, digest_file

    workflow = Workflow(tmp_path)
    project_id, evidence, _ = ready_project(workflow)
    directory = workflow.task_dir(evidence)
    good_path = directory / "good.bin"
    good_path.write_bytes(b"verified synthetic artifact")
    broken_path = directory / "broken.bin"
    broken_path.write_bytes(b"original bytes")
    good_id, broken_id, unrelated_id = "1" * 32, "2" * 32, "3" * 32
    with workflow.connect() as con:
        workflow._put(
            con,
            "artifacts",
            {
                "id": good_id,
                "project_id": project_id,
                "job_id": evidence,
                "path": str(good_path),
                "sha256": digest_file(good_path),
                "size_bytes": good_path.stat().st_size,
            },
        )
        workflow._put(
            con,
            "artifacts",
            {
                "id": unrelated_id,
                "project_id": project_id,
                "job_id": evidence,
                "path": str(directory / "unrelated-missing.bin"),
                "sha256": "0" * 64,
                "size_bytes": 1,
            },
        )
        workflow._put(
            con,
            "artifacts",
            {
                "id": broken_id,
                "project_id": project_id,
                "job_id": evidence,
                "path": str(broken_path),
                "sha256": digest_file(broken_path),
                "size_bytes": broken_path.stat().st_size,
            },
        )

    assert workflow.get_artifact(good_id)["path"] == str(good_path)
    if change == "missing":
        broken_path.unlink()
    elif change == "tamper":
        broken_path.write_bytes(b"tampered bytes")
    elif change == "symlink":
        target = directory / "target.bin"
        target.write_bytes(b"original bytes")
        broken_path.unlink()
        broken_path.symlink_to(target)
    else:
        with workflow.connect() as con:
            broken = workflow._get("artifacts", broken_id, con)
            broken["path"] = str(broken_path)
            broken["sha256"] = digest_file(broken_path)
            broken["size_bytes"] = broken_path.stat().st_size + 1
            workflow._put(con, "artifacts", broken)

    with pytest.raises(WorkflowError, match="artifact"):
        workflow.get_artifact(broken_id)
    assert workflow.get_artifact(good_id)["size_bytes"] == good_path.stat().st_size
