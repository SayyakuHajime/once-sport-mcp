"""Durable local jobs and explicit analysis review."""

import hashlib
import json
import math
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from . import tools
from .formation import FormationError, build_formation
from .rendering import RenderError, validate_plan
from .tracking import TrackingError, validate_tracking_request


class WorkflowError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def canonical(value):
    try:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise WorkflowError(
            "invalid_payload", "Payload must contain finite JSON values."
        ) from None
    if len(data.encode()) > 1_000_000:
        raise WorkflowError("invalid_payload", "Payload is too large.")
    return data


def digest_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise WorkflowError("invalid_id", "Invalid identifier.")
    return value


class Workflow:
    def __init__(
        self,
        root: Path,
        allowed_source_dirs=None,
        *,
        native_once_executable=None,
        tracking_enabled=False,
    ):
        if type(tracking_enabled) is not bool:
            raise WorkflowError(
                "invalid_tracking_configuration",
                "Tracking configuration must be an explicit boolean.",
            )
        self.tracking_enabled = tracking_enabled
        self.native_once_executable = None
        if native_once_executable is not None:
            try:
                raw_executable = str(native_once_executable)
                executable = Path(native_once_executable)
            except (TypeError, ValueError):
                raise WorkflowError(
                    "invalid_native_executable",
                    "Native Once executable must be an absolute path.",
                ) from None
            if (
                not raw_executable
                or "\x00" in raw_executable
                or not executable.is_absolute()
                or ".." in executable.parts
            ):
                raise WorkflowError(
                    "invalid_native_executable",
                    "Native Once executable must be an absolute path without parent traversal.",
                )
            self.native_once_executable = executable
        raw = Path(root).absolute()
        if any(p.is_symlink() for p in (raw, *raw.parents)):
            raise WorkflowError("unsafe_path", "Workflow root must not be a symlink.")
        raw.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = raw.resolve()
        os.chmod(self.root, 0o700)
        self.allowed_source_dirs = [
            Path(p).resolve()
            for p in (
                allowed_source_dirs
                if allowed_source_dirs is not None
                else [self.root / "inputs"]
            )
        ]
        for name in ("inputs", "tasks"):
            path = self.root / name
            if path.is_symlink():
                raise WorkflowError(
                    "unsafe_path", "Workflow directories must not be symlinks."
                )
            path.mkdir(mode=0o700, exist_ok=True)
            os.chmod(path, 0o700)
        db = self.root / "workflow.sqlite3"
        if db.is_symlink():
            raise WorkflowError("unsafe_path", "Database must not be a symlink.")
        with self.connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS analyses (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            """)
        os.chmod(db, 0o600)

    def connect(self):
        con = sqlite3.connect(self.root / "workflow.sqlite3", timeout=30)
        con.execute("PRAGMA busy_timeout=30000")
        return con

    def _put(self, con, table, data):
        con.execute(
            f"INSERT OR REPLACE INTO {table} VALUES (?,?)",
            (data["id"], canonical(data)),
        )

    def _get(self, table, key, con=None):
        identifier(key)
        if con is None:
            with self.connect() as connection:
                return self._get(table, key, connection)
        row = con.execute(f"SELECT data FROM {table} WHERE id=?", (key,)).fetchone()
        if row is None:
            raise WorkflowError("not_found", "Record was not found.")
        return json.loads(row[0])

    def _rows(self, table, project_id=None, con=None):
        if project_id is not None:
            identifier(project_id)
        if con is None:
            with self.connect() as connection:
                return self._rows(table, project_id, connection)
        rows = [
            json.loads(r[0])
            for r in con.execute(f"SELECT data FROM {table} ORDER BY rowid")
        ]
        return [
            r for r in rows if project_id is None or r.get("project_id") == project_id
        ]

    def _source(self, source):
        if not isinstance(source, str) or len(source) > 4096:
            raise WorkflowError(
                "invalid_source", "Supply a local video or a supported YouTube URL."
            )
        if "://" in source:
            if not source.startswith("https://") or any(
                ord(c) <= 32 or ord(c) >= 127 for c in source
            ):
                raise WorkflowError(
                    "invalid_source", "Use an exact HTTPS YouTube video URL."
                )
            try:
                url = urlsplit(source)
                if (
                    url.scheme != "https"
                    or url.username
                    or url.password
                    or url.port
                    or url.fragment
                ):
                    raise ValueError
                if (
                    url.netloc in ("youtube.com", "www.youtube.com")
                    and url.path == "/watch"
                ):
                    if not re.fullmatch(r"v=[A-Za-z0-9_-]{11}", url.query):
                        raise ValueError
                    vid = url.query[2:]
                elif url.netloc == "youtu.be" and not url.query:
                    vid = url.path[1:]
                else:
                    raise ValueError
                if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
                    raise ValueError
            except ValueError:
                raise WorkflowError(
                    "invalid_source", "Use a single HTTPS YouTube video URL."
                ) from None
            return {"kind": "youtube", "url": "https://www.youtube.com/watch?v=" + vid}
        try:
            original = Path(source).absolute()
            if ".." in original.parts or any(
                p.is_symlink() for p in (original, *original.parents)
            ):
                raise WorkflowError(
                    "invalid_source", "Source must be a regular allowed video file."
                )
            path = original.resolve()
            if (
                not any(path.is_relative_to(base) for base in self.allowed_source_dirs)
                or not path.is_file()
                or path.suffix.lower() not in (".mp4", ".mov", ".mkv", ".webm", ".avi")
            ):
                raise WorkflowError(
                    "invalid_source",
                    "Source must be a regular video in an allowed directory.",
                )
            return {"kind": "local", "path": str(path)}
        except (OSError, ValueError):
            raise WorkflowError(
                "invalid_source", "Source path is not a readable allowed video."
            ) from None

    def _job(self, con, project_id, kind, payload, retry_of=None):
        job = {
            "id": uuid.uuid4().hex,
            "project_id": project_id,
            "kind": kind,
            "payload": payload,
            "status": "queued",
            "progress": 0,
            "created_at": time.time(),
            "heartbeat": None,
            "cancel_requested": False,
            "retry_of": retry_of,
            "retryable": False,
        }
        self._put(con, "jobs", job)
        return job

    def create_project(self, title, source):
        if not tools._text(title) or len(title) > 200:
            raise WorkflowError("invalid_title", "Supply a short project title.")
        source = self._source(source)
        project = {
            "id": uuid.uuid4().hex,
            "title": title,
            "source": source,
            "source_digest": None,
            "media": None,
            "latest_analysis_id": None,
            "created_at": time.time(),
        }
        with self.connect() as con:
            self._put(con, "projects", project)
            job = self._job(con, project["id"], "ingest", source)
        return {"project_id": project["id"], "job_id": job["id"]}

    def get_project(self, project_id):
        return self._get("projects", project_id)

    def get_job(self, job_id):
        return self._get("jobs", job_id)

    def list_jobs(self, project_id=None):
        if project_id is not None:
            self.get_project(project_id)
        return self._rows("jobs", project_id)

    def cancel_job(self, job_id):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            job = self._get("jobs", job_id, con)
            if job["status"] not in ("queued", "running"):
                raise WorkflowError("stale_job", "Job is no longer active.")
            job["cancel_requested"] = True
            if job["status"] == "queued":
                job.update(status="cancelled", retryable=True)
            else:
                job["status"] = "cancelling"
            self._put(con, "jobs", job)
        return job

    def retry_job(self, job_id):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            job = self._get("jobs", job_id, con)
            if (
                job["status"] not in ("failed", "cancelled", "interrupted")
                or not job["retryable"]
            ):
                raise WorkflowError(
                    "stale_job",
                    "Only failed, cancelled, or interrupted jobs can be retried.",
                )
            if any(j.get("retry_of") == job_id for j in self._rows("jobs", con=con)):
                raise WorkflowError("stale_job", "Job already has a retry.")
            if job["kind"] == "export":
                self._approved(
                    con,
                    job["project_id"],
                    job["payload"]["analysis_id"],
                    job["payload"]["digest"],
                )
            new = self._job(con, job["project_id"], job["kind"], job["payload"], job_id)
        return {"job_id": new["id"], "project_id": job["project_id"]}

    def _media(self, project):
        if not project.get("media") or not project.get("source_digest"):
            raise WorkflowError("source_not_ready", "Wait for ingest to complete.")
        return project["media"]

    def queue_evidence(
        self, project_id, start_seconds, end_seconds, interval_seconds=2
    ):
        project = self.get_project(project_id)
        media = self._media(project)
        values = (start_seconds, end_seconds, interval_seconds)
        if any(
            type(x) not in (int, float) or abs(x) > 1e12 or not math.isfinite(x)
            for x in values
        ):
            raise WorkflowError(
                "invalid_range", "Evidence times must be finite numbers."
            )
        if (
            start_seconds < 0
            or end_seconds <= start_seconds
            or end_seconds > media["duration"]
            or end_seconds - start_seconds > 120
            or interval_seconds <= 0
            or (end_seconds - start_seconds) / interval_seconds > 60
        ):
            raise WorkflowError(
                "invalid_range",
                "Evidence must span at most 120 seconds and 60 frames within the video.",
            )
        with self.connect() as con:
            job = self._job(
                con,
                project_id,
                "evidence",
                {
                    "start_seconds": start_seconds,
                    "end_seconds": end_seconds,
                    "interval_seconds": interval_seconds,
                    "source_digest": project["source_digest"],
                },
            )
        return {"project_id": project_id, "job_id": job["id"]}

    def queue_tracking(self, project_id, start_seconds, end_seconds, initial_bbox):
        if not self.tracking_enabled:
            raise WorkflowError(
                "tracking_not_configured",
                "Tracking requires explicit workflow configuration.",
            )
        project = self.get_project(project_id)
        media = self._media(project)
        try:
            validate_tracking_request(start_seconds, end_seconds, initial_bbox, media)
        except TrackingError as exc:
            raise WorkflowError("invalid_tracking_request", str(exc)) from exc
        with self.connect() as con:
            job = self._job(
                con,
                project_id,
                "evidence",
                {
                    "mode": "tracking",
                    "source_digest": project["source_digest"],
                    "start_seconds": start_seconds,
                    "end_seconds": end_seconds,
                    "initial_bbox": initial_bbox,
                },
            )
        return {"project_id": project_id, "job_id": job["id"]}

    def build_formation(
        self,
        project_id,
        job_ids,
        *,
        event_id,
        id_prefix="formation",
        links=None,
        closed=False,
        rings=True,
        area=False,
        spotlights=False,
        label=None,
        hold_at_seconds=None,
        hold_seconds=None,
        tags=None,
    ):
        if not self.tracking_enabled:
            raise WorkflowError(
                "tracking_not_configured",
                "Tracking requires explicit workflow configuration.",
            )
        project = self.get_project(project_id)
        media = self._media(project)
        if (
            not isinstance(job_ids, list)
            or not 2 <= len(job_ids) <= 11
            or any(not isinstance(job_id, str) or not job_id for job_id in job_ids)
            or len(set(job_ids)) != len(job_ids)
        ):
            raise WorkflowError(
                "invalid_formation", "Supply two to eleven unique tracking job IDs."
            )

        artifacts = self.list_artifacts(project_id)
        results = []
        for job_id in job_ids:
            try:
                job = self.get_job(job_id)
            except WorkflowError as exc:
                if exc.code in ("invalid_id", "not_found"):
                    raise WorkflowError(
                        "invalid_tracking_job",
                        "Tracking jobs must be completed evidence for this project and source.",
                    ) from exc
                raise
            payload = job.get("payload")
            if (
                job.get("project_id") != project_id
                or job.get("kind") != "evidence"
                or not isinstance(payload, dict)
                or payload.get("mode") != "tracking"
                or job.get("status") != "completed"
                or payload.get("source_digest") != project["source_digest"]
            ):
                raise WorkflowError(
                    "invalid_tracking_job",
                    "Tracking jobs must be completed evidence for this project and source.",
                )
            matching = [
                artifact
                for artifact in artifacts
                if artifact.get("job_id") == job_id
                and artifact.get("kind") == "tracking_result"
            ]
            if len(matching) != 1:
                raise WorkflowError(
                    "invalid_tracking_job",
                    "Each tracking job must have one verified tracking result.",
                )
            try:
                artifact = self.get_artifact(matching[0]["id"])
            except WorkflowError as exc:
                if exc.code == "not_found":
                    raise WorkflowError(
                        "invalid_tracking_job",
                        "Each tracking job must have one verified tracking result.",
                    ) from exc
                raise
            try:
                with open(artifact["path"], encoding="utf-8") as stream:
                    result = json.load(stream)
            except (OSError, UnicodeError, TypeError, ValueError, RecursionError):
                raise WorkflowError(
                    "invalid_tracking_job", "Tracking result JSON is invalid."
                ) from None
            if (
                not isinstance(result, dict)
                or result.get("tracking_job_id") != job_id
                or result.get("source_digest") != project["source_digest"]
            ):
                raise WorkflowError(
                    "invalid_tracking_job",
                    "Tracking result does not match its job and project source.",
                )
            results.append(result)

        try:
            result = build_formation(
                results,
                media=media,
                event_id=event_id,
                id_prefix=id_prefix,
                links=links,
                closed=closed,
                rings=rings,
                area=area,
                spotlights=spotlights,
                label=label,
                hold_at_seconds=hold_at_seconds,
                hold_seconds=hold_seconds,
                tags=tags,
            )
        except FormationError as exc:
            raise WorkflowError("invalid_formation", str(exc)) from exc
        return {"project_id": project_id, "evidence_job_ids": list(job_ids), **result}

    def submit_analysis(
        self, project_id, events, drawings, report_markdown, evidence_job_ids
    ):
        canonical(
            {
                "events": events,
                "drawings": drawings,
                "report_markdown": report_markdown,
                "evidence_job_ids": evidence_job_ids,
            }
        )
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            project = self._get("projects", project_id, con)
            duration = self._media(project)["duration"]
            valid = tools.validate_timeline(events)
            if (
                not valid["valid"]
                or not events
                or len(events) > 1000
                or any(
                    e["end_seconds"] <= e["start_seconds"]
                    or e["end_seconds"] > duration
                    for e in events
                )
            ):
                raise WorkflowError(
                    "invalid_analysis",
                    "Supply nonempty valid events within the video duration.",
                )
            if (
                not isinstance(report_markdown, str)
                or not report_markdown.strip()
                or len(report_markdown) > 200_000
                or any(
                    0xD800 <= ord(c) <= 0xDFFF or (ord(c) < 32 and c not in "\n\r\t")
                    for c in report_markdown
                )
            ):
                raise WorkflowError(
                    "invalid_analysis", "Supply a bounded analysis report."
                )
            if (
                not isinstance(evidence_job_ids, list)
                or not evidence_job_ids
                or len(evidence_job_ids) > 100
            ):
                raise WorkflowError(
                    "invalid_evidence", "Supply completed evidence jobs."
                )
            for key in evidence_job_ids:
                evidence = self._get("jobs", key, con)
                if (
                    evidence["project_id"] != project_id
                    or evidence["kind"] != "evidence"
                    or evidence["status"] != "completed"
                    or evidence["payload"]["source_digest"] != project["source_digest"]
                ):
                    raise WorkflowError(
                        "invalid_evidence",
                        "Evidence must be completed for this project and source.",
                    )
            if not isinstance(drawings, list) or len(drawings) > 1000:
                raise WorkflowError(
                    "invalid_drawings", "Supply a bounded drawing list."
                )
            try:
                validate_plan(events, drawings, project["media"])
            except (RenderError, OverflowError) as exc:
                raise WorkflowError(
                    "invalid_render_plan", f"Analysis is not renderable: {exc}"
                ) from exc
            previous = self._rows("analyses", project_id, con)
            proposal = {
                "id": uuid.uuid4().hex,
                "project_id": project_id,
                "revision": len(previous) + 1,
                "source_digest": project["source_digest"],
                "events": events,
                "drawings": drawings,
                "report_markdown": report_markdown,
                "evidence_job_ids": evidence_job_ids,
            }
            encoded = canonical(proposal)
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            analysis = {
                "id": proposal["id"],
                "project_id": project_id,
                "revision": proposal["revision"],
                "digest": digest,
                "proposal": proposal,
                "canonical_json": encoded,
                "status": "awaiting_review",
                "reviews": [],
            }
            for old in previous:
                old["status"] = "superseded"
                self._put(con, "analyses", old)
            self._put(con, "analyses", analysis)
            project["latest_analysis_id"] = analysis["id"]
            self._put(con, "projects", project)
        return {
            "analysis_id": analysis["id"],
            "revision": analysis["revision"],
            "digest": digest,
            "status": analysis["status"],
        }

    def get_analysis(self, analysis_id):
        return self._get("analyses", analysis_id)

    def review_analysis(
        self, project_id, analysis_id, expected_digest, approved, reviewer
    ):
        if (
            type(approved) is not bool
            or not tools._text(reviewer)
            or len(reviewer) > 200
        ):
            raise WorkflowError(
                "invalid_review",
                "Review requires an explicit boolean and reviewer name.",
            )
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            project = self._get("projects", project_id, con)
            analysis = self._get("analyses", analysis_id, con)
            if (
                analysis["project_id"] != project_id
                or project["latest_analysis_id"] != analysis_id
                or analysis["digest"] != expected_digest
            ):
                raise WorkflowError(
                    "stale_analysis",
                    "Review must name the latest proposal and exact digest.",
                )
            analysis["status"] = "approved" if approved else "rejected"
            analysis["reviews"].append(
                {
                    "approved": approved,
                    "reviewer": reviewer,
                    "reviewed_at": time.time(),
                    "digest": expected_digest,
                }
            )
            self._put(con, "analyses", analysis)
        return analysis

    def _approved(self, con, project_id, analysis_id, digest):
        project = self._get("projects", project_id, con)
        analysis = self._get("analyses", analysis_id, con)
        if (
            project["latest_analysis_id"] != analysis_id
            or analysis["project_id"] != project_id
            or analysis["status"] != "approved"
            or analysis["digest"] != digest
            or analysis["proposal"]["source_digest"] != project["source_digest"]
        ):
            raise WorkflowError(
                "approval_required",
                "Export requires the latest approved proposal and exact digest.",
            )
        return analysis

    def queue_export(
        self, project_id, analysis_id, expected_digest=None, *, playlist=None
    ):
        if playlist is not None and (not tools._text(playlist) or len(playlist) > 60):
            raise WorkflowError(
                "invalid_playlist",
                "Playlist must be nonempty text of at most 60 characters.",
            )
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            if expected_digest is None:
                expected_digest = self._get("analyses", analysis_id, con)["digest"]
            analysis = self._approved(con, project_id, analysis_id, expected_digest)
            if playlist is not None and not any(
                playlist in event.get("playlists", [])
                for event in analysis["proposal"]["events"]
            ):
                raise WorkflowError(
                    "invalid_playlist", "No event belongs to that playlist."
                )
            payload = {"analysis_id": analysis_id, "digest": expected_digest}
            if playlist is not None:
                payload["playlist"] = playlist
            job = self._job(con, project_id, "export", payload)
        return {"project_id": project_id, "job_id": job["id"]}

    def queue_statistics(self, project_id, analysis_id, expected_digest):
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            self._approved(con, project_id, analysis_id, expected_digest)
            job = self._job(
                con,
                project_id,
                "export",
                {
                    "analysis_id": analysis_id,
                    "digest": expected_digest,
                    "backend": "statistics",
                },
            )
        return {"project_id": project_id, "job_id": job["id"]}

    def queue_native_export(self, project_id, analysis_id, expected_digest):
        if self.native_once_executable is None:
            raise WorkflowError(
                "native_not_configured",
                "Native Once export requires an explicitly configured executable.",
            )
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            analysis = self._approved(con, project_id, analysis_id, expected_digest)
            project = self._get("projects", project_id, con)
            try:
                from .native_rendering import validate_native_plan

                validate_native_plan(
                    analysis["proposal"]["events"],
                    analysis["proposal"]["drawings"],
                    self._media(project),
                )
            except (RenderError, OverflowError) as exc:
                raise WorkflowError(
                    "invalid_render_plan", f"Analysis is not natively renderable: {exc}"
                ) from exc
            job = self._job(
                con,
                project_id,
                "export",
                {
                    "analysis_id": analysis_id,
                    "digest": expected_digest,
                    "backend": "native",
                },
            )
        return {"project_id": project_id, "job_id": job["id"]}

    def task_dir(self, job_id):
        identifier(job_id)
        path = self.root / "tasks" / job_id
        if path.is_symlink() or not path.resolve().is_relative_to(self.root / "tasks"):
            raise WorkflowError("unsafe_path", "Unsafe task directory.")
        path.mkdir(mode=0o700, exist_ok=True)
        os.chmod(path, 0o700)
        return path

    def _verify_artifact(self, artifact):
        message = "An artifact is missing or has changed."
        try:
            raw_path = artifact.get("path")
            if not isinstance(raw_path, str):
                raise WorkflowError("artifact_tampered", message)
            path = Path(raw_path)
            tasks = self.root / "tasks"
            if tasks.is_symlink() or not tasks.is_dir() or not path.is_absolute():
                raise WorkflowError("artifact_tampered", message)
            try:
                relative = path.relative_to(tasks)
            except ValueError:
                raise WorkflowError("artifact_tampered", message) from None
            if not relative.parts or ".." in relative.parts:
                raise WorkflowError("artifact_tampered", message)
            cursor = tasks
            for part in relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise WorkflowError("artifact_tampered", message)
            resolved_tasks = tasks.resolve(strict=True)
            resolved_path = path.resolve(strict=True)
            if not resolved_path.is_relative_to(resolved_tasks) or not path.is_file():
                raise WorkflowError("artifact_tampered", message)
            metadata = path.stat()
            if (
                type(artifact.get("size_bytes")) is not int
                or artifact["size_bytes"] != metadata.st_size
                or not isinstance(artifact.get("sha256"), str)
                or digest_file(path) != artifact["sha256"]
            ):
                raise WorkflowError("artifact_tampered", message)
        except WorkflowError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError):
            raise WorkflowError("artifact_tampered", message) from None
        return artifact

    def list_artifacts(self, project_id=None):
        if project_id is not None:
            self.get_project(project_id)
        artifacts = self._rows("artifacts", project_id)
        for artifact in artifacts:
            self._verify_artifact(artifact)
        return artifacts

    def get_artifact(self, artifact_id):
        return self._verify_artifact(self._get("artifacts", artifact_id))
