"""Synchronous JSON controls for the persistent local workflow."""

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from .workflow import Workflow, WorkflowError
from .workflow_config import (
    native_export_descriptor,
    playlist_export_descriptor,
    statistics_descriptor,
    tracking_descriptor,
)

_PROPOSAL_FIELDS = {
    "events",
    "drawings",
    "report_markdown",
    "evidence_job_ids",
}


def _reject_constant(value: str) -> None:
    raise ValueError(value)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def _read_proposal(path: Path) -> dict[str, Any]:
    message = (
        "Proposal must be readable JSON with exactly events, drawings, "
        "report_markdown, and evidence_job_ids."
    )
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise WorkflowError("invalid_proposal", message) from None
    if type(value) is not dict or set(value) != _PROPOSAL_FIELDS:
        raise WorkflowError("invalid_proposal", message)
    return value


def _capabilities(workflow: Workflow) -> dict[str, Any]:
    result = {
        "workspace": str(workflow.root),
        "allowed_source_dirs": [str(path) for path in workflow.allowed_source_dirs],
        "analysis_backend": "mcp_host",
        "worker": "separate_process",
        "ffmpeg_available": shutil.which("ffmpeg") is not None,
        "ffprobe_available": shutil.which("ffprobe") is not None,
        "yt_dlp_available": shutil.which("yt-dlp") is not None,
        "network_access": "not_probed_at_discovery",
        "native_import_verified": False,
        "native_editable_drawings": False,
        "drawing_semantics": "custom-render-only; burn-in with editable JSON",
        "approval": "explicit operator assertion for an exact proposal digest; not authenticated human identity",
        "artifact_delivery": "verified local path and metadata; artifact binary is not transferred through CLI",
        "max_evidence_window_seconds": 120,
        "max_evidence_frames": 60,
        "native_boundary": "No verified native adapter; these tools never write the Once database or operate the desktop.",
        "statistics": statistics_descriptor(),
        "playlist_export": playlist_export_descriptor(),
    }
    if workflow.tracking_enabled:
        result["tracking"] = tracking_descriptor()
    if workflow.native_once_executable is not None:
        descriptor = native_export_descriptor()
        result["native_export"] = descriptor
        result["drawing_semantics"] = (
            "native Once file/CLI export; GUI project-list import unverified"
        )
        result["native_boundary"] = descriptor["boundary"]
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--native-once-executable", type=Path, default=None)
    parser.add_argument("--enable-tracking", action="store_true", default=False)
    parser.add_argument(
        "--allow-source-dir",
        "--allowed-source-dir",
        dest="allowed_source_dirs",
        type=Path,
        action="append",
        default=None,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("capabilities")

    create = commands.add_parser("create-project")
    create.add_argument("--title", required=True)
    create.add_argument("--source", required=True)

    get_project = commands.add_parser("get-project")
    get_project.add_argument("project_id")

    get_job = commands.add_parser("get-job")
    get_job.add_argument("job_id")

    list_jobs = commands.add_parser("list-jobs")
    list_jobs.add_argument("--project-id")

    cancel_job = commands.add_parser("cancel-job")
    cancel_job.add_argument("job_id")

    retry_job = commands.add_parser("retry-job")
    retry_job.add_argument("job_id")

    evidence = commands.add_parser("evidence")
    evidence.add_argument("project_id")
    evidence.add_argument("--start", type=float, required=True)
    evidence.add_argument("--end", type=float, required=True)
    evidence.add_argument("--interval", type=float, default=2)

    track_target = commands.add_parser("track-target")
    track_target.add_argument("project_id")
    track_target.add_argument("--start", type=float, required=True)
    track_target.add_argument("--end", type=float, required=True)
    track_target.add_argument("--bbox", type=float, nargs=4, required=True)

    formation = commands.add_parser("formation")
    formation.add_argument("project_id")
    formation.add_argument("--event-id", required=True)
    formation.add_argument("--job", dest="job_ids", action="append", required=True)
    formation.add_argument("--link", dest="links", nargs=2, type=int, action="append")
    formation.add_argument("--closed", action="store_true")
    formation.add_argument("--no-rings", action="store_true")
    formation.add_argument("--area", action="store_true")
    formation.add_argument("--spotlights", action="store_true")
    formation.add_argument("--label")
    formation.add_argument("--hold-at", type=float)
    formation.add_argument("--hold-seconds", type=float)
    formation.add_argument("--id-prefix", default="formation")
    formation.add_argument(
        "--tag",
        action="append",
        dest="tags",
        metavar="POSITION[:NAME]",
        help="nametag per tracked job, in --job order (repeat; '-' for none)",
    )

    submit = commands.add_parser("submit-analysis")
    submit.add_argument("project_id")
    submit.add_argument("--proposal", type=Path, required=True)

    get_analysis = commands.add_parser("get-analysis")
    get_analysis.add_argument("analysis_id")

    review = commands.add_parser("review-analysis")
    review.add_argument("project_id")
    review.add_argument("analysis_id")
    review.add_argument("--digest", required=True)
    review.add_argument("--reviewer", required=True)
    decision = review.add_mutually_exclusive_group(required=True)
    decision.add_argument(
        "--approve", dest="approved", action="store_const", const=True
    )
    decision.add_argument(
        "--reject", dest="approved", action="store_const", const=False
    )

    export = commands.add_parser("export")
    export.add_argument("project_id")
    export.add_argument("analysis_id")
    export.add_argument("--digest", required=True)
    export.add_argument("--playlist")

    statistics = commands.add_parser("statistics")
    statistics.add_argument("project_id")
    statistics.add_argument("analysis_id")
    statistics.add_argument("--digest", required=True)

    export_native = commands.add_parser("export-native")
    export_native.add_argument("project_id")
    export_native.add_argument("analysis_id")
    export_native.add_argument("--digest", required=True)

    list_artifacts = commands.add_parser("list-artifacts")
    list_artifacts.add_argument("project_id")

    get_artifact = commands.add_parser("get-artifact")
    get_artifact.add_argument("artifact_id")
    return parser


def _parse_tags(values):
    """Turn repeated `POSITION[:NAME]` options into formation tag dicts."""
    if not values:
        return None
    tags = []
    for value in values:
        if value == "-":
            tags.append(None)
            continue
        position, _, name = value.partition(":")
        tag = {}
        if position:
            tag["position"] = position
        if name:
            tag["name"] = name
        tags.append(tag)
    return tags


def _dispatch(args: argparse.Namespace, workflow: Workflow) -> Any:
    if args.command == "capabilities":
        return _capabilities(workflow)
    if args.command == "create-project":
        return workflow.create_project(args.title, args.source)
    if args.command == "get-project":
        return workflow.get_project(args.project_id)
    if args.command == "get-job":
        return workflow.get_job(args.job_id)
    if args.command == "list-jobs":
        return workflow.list_jobs(args.project_id)
    if args.command == "cancel-job":
        return workflow.cancel_job(args.job_id)
    if args.command == "retry-job":
        return workflow.retry_job(args.job_id)
    if args.command == "evidence":
        return workflow.queue_evidence(
            args.project_id, args.start, args.end, args.interval
        )
    if args.command == "track-target":
        return workflow.queue_tracking(args.project_id, args.start, args.end, args.bbox)
    if args.command == "formation":
        return workflow.build_formation(
            args.project_id,
            args.job_ids,
            event_id=args.event_id,
            id_prefix=args.id_prefix,
            links=args.links if args.links else None,
            closed=args.closed,
            rings=not args.no_rings,
            area=args.area,
            spotlights=args.spotlights,
            label=args.label,
            hold_at_seconds=args.hold_at,
            hold_seconds=args.hold_seconds,
            tags=_parse_tags(args.tags),
        )
    if args.command == "submit-analysis":
        proposal = _read_proposal(args.proposal)
        return workflow.submit_analysis(
            args.project_id,
            proposal["events"],
            proposal["drawings"],
            proposal["report_markdown"],
            proposal["evidence_job_ids"],
        )
    if args.command == "get-analysis":
        return workflow.get_analysis(args.analysis_id)
    if args.command == "review-analysis":
        return workflow.review_analysis(
            args.project_id,
            args.analysis_id,
            args.digest,
            args.approved,
            args.reviewer,
        )
    if args.command == "export":
        return workflow.queue_export(
            args.project_id, args.analysis_id, args.digest, playlist=args.playlist
        )
    if args.command == "statistics":
        return workflow.queue_statistics(args.project_id, args.analysis_id, args.digest)
    if args.command == "export-native":
        return workflow.queue_native_export(
            args.project_id, args.analysis_id, args.digest
        )
    if args.command == "list-artifacts":
        return workflow.list_artifacts(args.project_id)
    if args.command == "get-artifact":
        return workflow.get_artifact(args.artifact_id)
    raise AssertionError("Unreachable CLI command")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        workflow_options = {}
        if args.native_once_executable is not None:
            workflow_options["native_once_executable"] = args.native_once_executable
        if args.enable_tracking:
            workflow_options["tracking_enabled"] = True
        workflow = Workflow(args.root, args.allowed_source_dirs, **workflow_options)
        result = _dispatch(args, workflow)
    except WorkflowError as exc:
        envelope = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        status = 1
    except OSError:
        envelope = {
            "ok": False,
            "error": {"code": "io_error", "message": "Local workflow I/O failed."},
        }
        status = 1
    else:
        envelope = {"ok": True, "result": result}
        status = 0
    print(json.dumps(envelope, ensure_ascii=False, allow_nan=False))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
