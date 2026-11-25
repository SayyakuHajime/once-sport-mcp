"""Explicit opt-in MCP control plane; media work belongs to the separate worker."""

import shutil
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import StrictBool, StrictFloat, StrictInt

from .workflow import Workflow, WorkflowError
from .workflow_config import (
    native_export_descriptor,
    playlist_export_descriptor,
    statistics_descriptor,
    tracking_descriptor,
)

READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
LOCAL_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
CREATE_PROJECT = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)


def register_workflow_tools(mcp: FastMCP, workflow: Workflow) -> None:
    """Register workspace-only tools without changing legacy names or schemas."""

    def invoke(method, *args, **kwargs):
        try:
            return {"ok": True, "result": method(*args, **kwargs)}
        except WorkflowError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": str(exc)}}

    @mcp.tool(annotations=READ_ONLY)
    def analysis_capabilities() -> dict[str, Any]:
        """Describe real local capabilities and the unverified native boundary."""
        result = {
            "workspace": str(workflow.root),
            "allowed_source_dirs": [str(p) for p in workflow.allowed_source_dirs],
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
            "artifact_delivery": "verified local path and metadata; artifact binary is not transferred through MCP",
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
        return {"ok": True, "result": result}

    @mcp.tool(annotations=CREATE_PROJECT)
    def create_analysis_project(title: str, source: str) -> dict[str, Any]:
        """Queue ingest of one permitted local/YouTube video; this is not completed analysis.

        Local paths must be inside startup-configured source directories. Actual
        media validation/download runs in the worker. Inspect the returned job ID.
        """
        return invoke(workflow.create_project, title, source)

    @mcp.tool(annotations=READ_ONLY)
    def get_analysis_project(project_id: str) -> dict[str, Any]:
        """Read persisted source identity, metadata and latest proposal ID."""
        return invoke(workflow.get_project, project_id)

    @mcp.tool(annotations=READ_ONLY)
    def get_analysis_job(job_id: str) -> dict[str, Any]:
        """Read real job status/progress; queued is not completed or running."""
        return invoke(workflow.get_job, job_id)

    @mcp.tool(annotations=READ_ONLY)
    def list_analysis_jobs(project_id: str) -> dict[str, Any]:
        """Read persisted job history for this project after reconnect or restart."""
        return invoke(workflow.list_jobs, project_id)

    @mcp.tool(annotations=LOCAL_WRITE)
    def cancel_analysis_job(job_id: str) -> dict[str, Any]:
        """Request cancellation; running children stop in the worker, not in this call."""
        return invoke(workflow.cancel_job, job_id)

    @mcp.tool(annotations=CREATE_PROJECT)
    def retry_analysis_job(job_id: str) -> dict[str, Any]:
        """Create one explicit retry ID for an eligible failed/interrupted/cancelled job."""
        return invoke(workflow.retry_job, job_id)

    @mcp.tool(annotations=LOCAL_WRITE)
    def request_analysis_evidence(
        project_id: str,
        start_seconds: float,
        end_seconds: float,
        interval_seconds: float = 2,
    ) -> dict[str, Any]:
        """Queue local evidence-frame extraction for a bounded source interval."""
        return invoke(
            workflow.queue_evidence,
            project_id,
            start_seconds,
            end_seconds,
            interval_seconds,
        )

    if workflow.tracking_enabled:

        @mcp.tool(annotations=LOCAL_WRITE)
        def request_analysis_tracking(
            project_id: str,
            start_seconds: StrictInt | StrictFloat,
            end_seconds: StrictInt | StrictFloat,
            initial_bbox: list[StrictInt | StrictFloat],
        ) -> dict[str, Any]:
            """Queue one manually seeded target as candidate evidence.

            The source interval is bounded to 12 seconds and 600 frames. Results
            require review and do not guarantee identity or re-identification.
            """
            return invoke(
                workflow.queue_tracking,
                project_id,
                start_seconds,
                end_seconds,
                initial_bbox,
            )

        @mcp.tool(annotations=READ_ONLY)
        def build_formation_drawings(
            project_id: str,
            tracking_job_ids: list[str],
            event_id: str,
            links: list[list[StrictInt]] | None = None,
            closed: StrictBool = False,
            rings: StrictBool = True,
            area: StrictBool = False,
            spotlights: StrictBool = False,
            label: str | None = None,
            hold_at_seconds: StrictInt | StrictFloat | None = None,
            hold_seconds: StrictInt | StrictFloat | None = None,
            tags: list[dict[str, str] | None] | None = None,
            id_prefix: str = "formation",
        ) -> dict[str, Any]:
            """Compose review-required tracking evidence into drawings.

            Does not submit or approve a proposal. Stops before possible overlap
            and never re-identifies targets. Use the returned suggested_event and
            include all tracking job IDs in evidence_job_ids.
            """
            return invoke(
                workflow.build_formation,
                project_id,
                tracking_job_ids,
                event_id=event_id,
                links=links,
                closed=closed,
                rings=rings,
                area=area,
                spotlights=spotlights,
                label=label,
                hold_at_seconds=hold_at_seconds,
                hold_seconds=hold_seconds,
                tags=tags,
                id_prefix=id_prefix,
            )

    @mcp.tool(annotations=READ_ONLY)
    def build_pitch_drawings(
        pitch_points: list[list[StrictInt | StrictFloat]],
        image_points: list[list[StrictInt | StrictFloat]],
        template: str,
        event_id: str,
        start_seconds: StrictInt | StrictFloat,
        end_seconds: StrictInt | StrictFloat,
        id_prefix: str = "pitch",
        color: str = "#6DE0D4",
        opacity: StrictInt | StrictFloat | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Project pitch templates (circle, half_spaces, five_channels, zone14,
        zone_grid, rectangle, cover_shadow, offside_line) through a 4-point
        pitch calibration (metres on a 105x68 pitch -> normalized frame
        points). Static calibration: fine for a fixed camera or a short shot,
        it does not follow camera pans. Review-required; does not submit.
        """
        from . import pitch, pitch_tool

        try:
            return pitch_tool.build_pitch_drawings(
                pitch_points,
                image_points,
                template,
                event_id,
                start_seconds,
                end_seconds,
                id_prefix=id_prefix,
                color=color,
                opacity=opacity,
                params=params,
            )
        except pitch.PitchError as exc:
            return {"error": {"code": "invalid_pitch", "message": str(exc)}}

    @mcp.tool(annotations=LOCAL_WRITE)
    def submit_analysis_proposal(
        project_id: str,
        events: list[dict[str, Any]],
        drawings: list[dict[str, Any]],
        report_markdown: str,
        evidence_job_ids: list[str],
    ) -> dict[str, Any]:
        """Persist operator-authored events/drawings unchanged for explicit review."""
        return invoke(
            workflow.submit_analysis,
            project_id,
            events,
            drawings,
            report_markdown,
            evidence_job_ids,
        )

    @mcp.tool(annotations=READ_ONLY)
    def get_analysis_proposal(analysis_id: str) -> dict[str, Any]:
        """Read the persisted proposal, digest, review status and review history."""
        return invoke(workflow.get_analysis, analysis_id)

    @mcp.tool(annotations=LOCAL_WRITE)
    def review_analysis_proposal(
        project_id: str,
        analysis_id: str,
        expected_digest: str,
        approved: StrictBool,
        reviewer: str,
    ) -> dict[str, Any]:
        """Record an explicit operator assertion for the exact proposal digest.

        The reviewer label is not an authenticated identity; approval is never automatic.
        """
        return invoke(
            workflow.review_analysis,
            project_id,
            analysis_id,
            expected_digest,
            approved,
            reviewer,
        )

    @mcp.tool(annotations=LOCAL_WRITE)
    def request_analysis_export(
        project_id: str,
        analysis_id: str,
        expected_digest: str,
        playlist: str | None = None,
    ) -> dict[str, Any]:
        """Queue an export only for an explicitly approved exact proposal digest."""
        return invoke(
            workflow.queue_export,
            project_id,
            analysis_id,
            expected_digest,
            playlist=playlist,
        )

    @mcp.tool(annotations=LOCAL_WRITE)
    def request_analysis_statistics(
        project_id: str, analysis_id: str, expected_digest: str
    ) -> dict[str, Any]:
        """Queue XLSX, HTML, and JSON statistics for an approved exact digest."""
        return invoke(
            workflow.queue_statistics, project_id, analysis_id, expected_digest
        )

    if workflow.native_once_executable is not None:

        @mcp.tool(annotations=LOCAL_WRITE)
        def request_native_analysis_export(
            project_id: str, analysis_id: str, expected_digest: str
        ) -> dict[str, Any]:
            """Queue the configured headless native file/CLI export for an approved digest."""
            return invoke(
                workflow.queue_native_export,
                project_id,
                analysis_id,
                expected_digest,
            )

    @mcp.tool(annotations=READ_ONLY)
    def list_analysis_artifacts(project_id: str) -> dict[str, Any]:
        """List verified local artifact metadata and paths; binaries are not transferred."""
        return invoke(workflow.list_artifacts, project_id)

    @mcp.tool(annotations=READ_ONLY)
    def get_analysis_artifact(artifact_id: str) -> dict[str, Any]:
        """Verify and return one artifact's metadata and local path, not its binary."""
        return invoke(workflow.get_artifact, artifact_id)
