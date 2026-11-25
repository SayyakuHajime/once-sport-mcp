"""Run local media jobs in a single durable worker per workflow root."""

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import tools, tracking
from .rendering import RenderError, validate_plan
from .workflow import Workflow, WorkflowError, canonical

MAX_SOURCE_BYTES = 8 * 1024**3
TRACKING_RESULT_KEYS = {
    "backend",
    "cv2_version",
    "requested_window",
    "exact_seed",
    "actual_first_seconds",
    "actual_last_seconds",
    "valid_end_seconds",
    "frame_count",
    "status",
    "loss_reason",
    "review_required",
    "source_metadata",
    "samples",
    "canonical_keyframes",
    "fixed_radii",
    "limitations",
}


def _tracking_number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _same_tracking_number(left, right):
    return _tracking_number(left) and _tracking_number(right) and left == right


def _validate_tracking_result(result, payload, project):
    message = "Tracking result is invalid or does not match the source request."

    def reject():
        raise WorkflowError("unsafe_artifact", message)

    if not isinstance(result, dict) or set(result) != TRACKING_RESULT_KEYS:
        reject()
    if (
        result.get("backend") != "opencv-csrt"
        or result.get("review_required") is not True
        or result.get("status") not in ("complete", "needs_reseed")
    ):
        reject()
    start, end = payload.get("start_seconds"), payload.get("end_seconds")
    bbox = payload.get("initial_bbox")
    if not (_tracking_number(start) and _tracking_number(end)):
        reject()
    if result.get("requested_window") != {
        "start_seconds": start,
        "end_seconds": end,
    }:
        reject()
    seed = result.get("exact_seed")
    if (
        not isinstance(seed, dict)
        or set(seed) != {"source_frame_index", "source_seconds", "bbox_normalized_xywh"}
        or seed.get("bbox_normalized_xywh") != bbox
    ):
        reject()
    metadata = result.get("source_metadata")
    media = project.get("media")
    if not isinstance(metadata, dict) or set(metadata) != {
        "duration_seconds",
        "width",
        "height",
        "fps",
        "sample_aspect_ratio",
        "has_audio",
    }:
        reject()
    if not isinstance(media, dict):
        reject()
    for result_key, media_key, tolerance in (
        ("duration_seconds", "duration", 1e-6),
        ("fps", "fps", 1e-6),
        ("sample_aspect_ratio", "sample_aspect_ratio", 1e-9),
    ):
        actual, expected = metadata.get(result_key), media.get(media_key)
        if (
            not _tracking_number(actual)
            or not _tracking_number(expected)
            or not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=tolerance)
        ):
            reject()
    for key in ("width", "height"):
        if type(metadata.get(key)) is not int or metadata[key] != media.get(key):
            reject()
    if type(metadata.get("has_audio")) is not bool or metadata[
        "has_audio"
    ] != media.get("has_audio"):
        reject()

    fps = metadata["fps"]
    if not _tracking_number(fps) or fps <= 0:
        reject()
    samples = result.get("samples")
    keyframes = result.get("canonical_keyframes")
    count = result.get("frame_count")
    if (
        type(count) is not int
        or not 2 <= count <= 600
        or not isinstance(samples, list)
        or not isinstance(keyframes, list)
        or len(samples) != count
        or len(keyframes) != count
    ):
        reject()
    actual_first = result.get("actual_first_seconds")
    actual_last = result.get("actual_last_seconds")
    valid_end = result.get("valid_end_seconds")
    if not all(
        _tracking_number(value) for value in (actual_first, actual_last, valid_end)
    ):
        reject()
    if not (
        start <= actual_first <= actual_last < end
        and actual_first < valid_end <= end
        and valid_end <= media["duration"]
    ):
        reject()
    if (
        type(seed.get("source_frame_index")) is not int
        or not _tracking_number(seed.get("source_seconds"))
        or seed["source_frame_index"] < 0
        or not _same_tracking_number(seed["source_seconds"], actual_first)
    ):
        reject()

    radii = result.get("fixed_radii")
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or not all(_tracking_number(value) for value in bbox)
        or not isinstance(radii, list)
        or len(radii) != 2
        or not all(_tracking_number(value) for value in radii)
        or radii != [bbox[2] * 0.9, bbox[3] * 0.18]
    ):
        reject()
    previous_time = None
    previous_index = None
    for index, (sample, keyframe) in enumerate(zip(samples, keyframes)):
        if not isinstance(sample, dict) or set(sample) != {
            "source_frame_index",
            "source_seconds",
            "bbox",
            "anchor",
            "fb_iou",
        }:
            reject()
        if not isinstance(keyframe, dict) or set(keyframe) != {
            "time_seconds",
            "points",
        }:
            reject()
        frame_index = sample.get("source_frame_index")
        moment = sample.get("source_seconds")
        sample_bbox = sample.get("bbox")
        anchor = sample.get("anchor")
        fb_iou = sample.get("fb_iou")
        if (
            type(frame_index) is not int
            or frame_index < 0
            or not _tracking_number(moment)
            or not isinstance(sample_bbox, list)
            or len(sample_bbox) != 4
            or not all(_tracking_number(value) for value in sample_bbox)
            or not isinstance(anchor, list)
            or len(anchor) != 2
            or not all(_tracking_number(value) for value in anchor)
            or not _tracking_number(fb_iou)
            or not 0 <= fb_iou <= 1
        ):
            reject()
        x, y, width, height = sample_bbox
        if (
            x < 0
            or y < 0
            or width <= 0
            or height <= 0
            or x + width > 1
            or y + height > 1
            or not math.isclose(anchor[0], x + width / 2, abs_tol=1e-9)
            or not math.isclose(anchor[1], y + height, abs_tol=1e-9)
        ):
            reject()
        if previous_time is not None and (
            moment <= previous_time or frame_index != previous_index + 1
        ):
            reject()
        if abs(moment - frame_index / fps) > max(0.0015, 0.05 / fps):
            reject()
        points = keyframe.get("points")
        if not _same_tracking_number(
            keyframe.get("time_seconds"), moment
        ) or points != [anchor, radii]:
            reject()
        previous_time, previous_index = moment, frame_index
    if (
        not _same_tracking_number(samples[0]["source_seconds"], actual_first)
        or not _same_tracking_number(samples[-1]["source_seconds"], actual_last)
        or samples[0]["source_frame_index"] != seed["source_frame_index"]
        or samples[-1]["source_seconds"] > valid_end
    ):
        reject()

    event = {
        "event_id": "tracking-candidate",
        "period": 1,
        "start_seconds": actual_first,
        "end_seconds": valid_end,
        "team": "Tracking",
        "phase": "candidate",
        "action": "Review",
    }
    drawing = {
        "id": "tracking-candidate-ellipse",
        "event_id": event["event_id"],
        "kind": "ellipse",
        "start_seconds": actual_first,
        "end_seconds": valid_end,
        "points": [samples[0]["anchor"], radii],
        "keyframes": keyframes,
    }
    try:
        validate_plan([event], [drawing], media)
    except (RenderError, OverflowError, TypeError, ValueError):
        reject()


class Cancelled(Exception):
    pass


class WorkerStopped(Cancelled):
    pass


@contextmanager
def stop_signals(worker):
    previous = {}

    def stop(signum, frame):
        worker.stopping = True

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, stop)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


class Worker:
    def __init__(self, workflow):
        self.workflow = workflow
        self.job = None
        self.stopping = False

    def cancelled(self):
        try:
            self.check()
        except Cancelled:
            return True
        return False

    def check(self, progress=None):
        if self.stopping:
            raise WorkerStopped()
        with self.workflow.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            job = self.workflow._get("jobs", self.job["id"], con)
            if job["cancel_requested"]:
                raise Cancelled()
            job["heartbeat"] = time.time()
            if progress is not None:
                job["progress"] = max(job["progress"], min(0.99, progress))
            self.workflow._put(con, "jobs", job)

    def hash_file(self, path):
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise WorkflowError(
                "unsafe_artifact", "Media must be a regular local file."
            )
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            while chunk := stream.read(1024 * 1024):
                self.check()
                digest.update(chunk)
        return digest.hexdigest()

    def command(self, argv, *, capture_stderr=False):
        self.check()
        # Child output stays in anonymous files; no raw diagnostics enter job records.
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                process = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
            except OSError:
                raise WorkflowError(
                    "tool_unavailable", "A required media tool is unavailable."
                ) from None
            try:
                deadline = time.monotonic() + 600
                while process.poll() is None:
                    self.check()
                    if time.monotonic() > deadline:
                        raise WorkflowError(
                            "process_timeout",
                            "Media operation exceeded its time limit.",
                        )
                    if (
                        os.fstat(stdout.fileno()).st_size
                        + os.fstat(stderr.fileno()).st_size
                        > 2_000_000
                    ):
                        raise WorkflowError(
                            "process_output_limit",
                            "Media operation produced too much output.",
                        )
                    time.sleep(0.05)
                if process.returncode:
                    raise WorkflowError(
                        "process_failed",
                        "Media operation failed; check the source and installed tools.",
                    )
                stdout.seek(0)
                output = stdout.read(2_000_001)
                if capture_stderr:
                    stderr.seek(0)
                    return output, stderr.read(2_000_001)
                return output
            finally:
                # Reap the child and terminate any descendants in its own process group.
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()

    def probe(self, source):
        data = json.loads(
            self.command(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-format_whitelist",
                    "mov,matroska,webm,avi",
                    "-show_format",
                    "-show_streams",
                    "-of",
                    "json",
                    str(source),
                ]
            )
        )
        videos = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
        if not videos:
            raise WorkflowError("invalid_media", "Source has no video stream.")
        stream = videos[0]
        try:
            duration = float(stream.get("duration", data["format"].get("duration")))
            numerator, denominator = stream.get("avg_frame_rate", "0/1").split("/")
            fps = float(numerator) / float(denominator)
            width, height = int(stream["width"]), int(stream["height"])
            sar = stream.get("sample_aspect_ratio", "1:1")
            if sar == "N/A":
                sample_aspect_ratio = 1.0
            else:
                sar_numerator, sar_denominator = sar.split(":")
                sample_aspect_ratio = float(sar_numerator) / float(sar_denominator)
                if sample_aspect_ratio == 0:
                    sample_aspect_ratio = 1.0
            if (
                not math.isfinite(duration)
                or duration <= 0
                or not math.isfinite(fps)
                or fps <= 0
                or min(width, height) <= 0
                or max(width, height) > 16384
                or not math.isfinite(sample_aspect_ratio)
                or sample_aspect_ratio <= 0
            ):
                raise ValueError
        except (ValueError, KeyError, ZeroDivisionError):
            raise WorkflowError(
                "invalid_media", "Source metadata is invalid."
            ) from None
        return {
            "duration": duration,
            "width": width,
            "height": height,
            "fps": fps,
            "sample_aspect_ratio": sample_aspect_ratio,
            "has_audio": any(s.get("codec_type") == "audio" for s in data["streams"]),
        }

    def _source(self, project):
        self.workflow._media(project)
        path = Path(project["source_path"])
        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(self.workflow.root / "tasks")
            or not path.is_file()
            or self.hash_file(path) != project["source_digest"]
        ):
            raise WorkflowError(
                "source_tampered", "Staged source is missing or has changed."
            )
        return path

    def ingest(self, stage):
        source = self.job["payload"]
        destination = stage / "source.mp4"
        if source["kind"] == "youtube":
            if not shutil.which("yt-dlp"):
                raise WorkflowError(
                    "downloader_unavailable", "Install yt-dlp to ingest YouTube videos."
                )
            if shutil.disk_usage(stage).free < 512 * 1024**2:
                raise WorkflowError("insufficient_disk", "Not enough free disk space.")
            download_budget = min(
                MAX_SOURCE_BYTES, max(0, shutil.disk_usage(stage).free - 256 * 1024**2)
            )
            self.command(
                [
                    "yt-dlp",
                    "--limit-rate",
                    "10M",
                    "--ignore-config",
                    "--no-cookies",
                    "--no-playlist",
                    "--no-progress",
                    "--quiet",
                    "--no-warnings",
                    "--retries",
                    "1",
                    "--fragment-retries",
                    "1",
                    "--socket-timeout",
                    "20",
                    "--max-filesize",
                    str(download_budget),
                    "-f",
                    "best[ext=mp4]/best",
                    "-o",
                    str(destination),
                    "--",
                    source["url"],
                ]
            )
        else:
            validated = self.workflow._source(source["path"])
            path = Path(validated["path"])
            with (
                os.fdopen(
                    os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb"
                ) as incoming,
                destination.open("xb") as outgoing,
            ):
                before = os.fstat(incoming.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise WorkflowError(
                        "invalid_source", "Source must be a regular file."
                    )
                if before.st_size > MAX_SOURCE_BYTES or before.st_size <= 0:
                    raise WorkflowError(
                        "source_size", "Source exceeds the supported size limit."
                    )
                if shutil.disk_usage(stage).free < before.st_size + 128 * 1024**2:
                    raise WorkflowError(
                        "insufficient_disk", "Not enough free disk space."
                    )
                copied = 0
                source_hash = hashlib.sha256()
                while chunk := incoming.read(1024 * 1024):
                    self.check(copied / before.st_size * 0.8)
                    copied += len(chunk)
                    if copied > MAX_SOURCE_BYTES:
                        raise WorkflowError(
                            "source_size", "Source exceeds the supported size limit."
                        )
                    source_hash.update(chunk)
                    outgoing.write(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
                after = os.fstat(incoming.fileno())
                current = path.stat()
                if (
                    (
                        before.st_dev,
                        before.st_ino,
                        before.st_size,
                        before.st_mtime_ns,
                        before.st_ctime_ns,
                    )
                    != (
                        after.st_dev,
                        after.st_ino,
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    )
                    or current.st_ino != before.st_ino
                    or copied != before.st_size
                ):
                    raise WorkflowError(
                        "source_changed", "Source changed while it was copied."
                    )
        if (
            destination.is_symlink()
            or not destination.is_file()
            or destination.stat().st_size > MAX_SOURCE_BYTES
        ):
            raise WorkflowError(
                "source_size", "Downloaded source is absent or too large."
            )
        media = self.probe(destination)
        return {
            "media": media,
            "source_digest": source_hash.hexdigest()
            if source["kind"] == "local"
            else self.hash_file(destination),
        }, [("source", destination)]

    def evidence(self, stage, project):
        source = self._source(project)
        payload = self.job["payload"]
        if payload["source_digest"] != project["source_digest"]:
            raise WorkflowError("stale_source", "Evidence source has changed.")
        mode = payload.get("mode")
        if mode is not None:
            if mode != "tracking":
                raise WorkflowError(
                    "unknown_evidence_mode", "Evidence job names an unsupported mode."
                )
            return self._tracking_evidence(stage, project, source, payload)
        artifacts = []
        frames = []
        count = math.ceil(
            (payload["end_seconds"] - payload["start_seconds"])
            / payload["interval_seconds"]
        )
        for index in range(count):
            self.check(index / count)
            moment = payload["start_seconds"] + index * payload["interval_seconds"]
            destination = stage / f"frame-{index:03d}.jpg"
            _, diagnostics = self.command(
                [
                    "ffmpeg",
                    "-v",
                    "info",
                    "-nostdin",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-format_whitelist",
                    "mov,matroska,webm,avi",
                    "-copyts",
                    "-start_at_zero",
                    "-ss",
                    str(moment),
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=min(1280\\,iw):-2,showinfo",
                    "-q:v",
                    "3",
                    str(destination),
                ],
                capture_stderr=True,
            )
            match = re.search(
                rb"\bn:\s*0\s+pts:\s*\S+\s+pts_time:([0-9.eE+\-]+)", diagnostics
            )
            if not match:
                raise WorkflowError(
                    "frame_timestamp_failed",
                    "Unable to measure evidence frame timestamp.",
                )
            timestamp = float(match.group(1))
            if (
                not math.isfinite(timestamp)
                or timestamp < 0
                or timestamp > project["media"]["duration"]
            ):
                raise WorkflowError(
                    "frame_timestamp_failed",
                    "Evidence timestamp lies outside the source duration.",
                )
            frames.append(
                {
                    "filename": destination.name,
                    "requested_seek_seconds": moment,
                    "timestamp_seconds": timestamp,
                }
            )
            if not destination.is_file() or not destination.stat().st_size:
                raise WorkflowError(
                    "frame_failed", "Unable to extract an evidence frame."
                )
            artifacts.append(("frame", destination))
        (stage / "evidence.json").write_text(
            canonical(
                dict(
                    source_digest=project["source_digest"],
                    frames=frames,
                    timestamp_basis="decoded_source_relative_pts",
                    timestamp_precision="Measured from ffmpeg showinfo pts_time after copyts/start_at_zero; values follow the source stream time base and ffmpeg decimal precision.",
                    disclaimer="These sample frames cover only the requested times; review the source video before drawing conclusions.",
                    **{
                        k: payload[k]
                        for k in ("start_seconds", "end_seconds", "interval_seconds")
                    },
                )
            )
        )
        artifacts.append(("evidence_manifest", stage / "evidence.json"))
        return {}, artifacts

    def _tracking_evidence(self, stage, project, source, payload):
        if not self.workflow.tracking_enabled:
            raise WorkflowError(
                "tracking_not_configured",
                "Tracking evidence requires an explicitly configured worker.",
            )
        try:
            tracking.track_target(
                source,
                payload["start_seconds"],
                payload["end_seconds"],
                payload["initial_bbox"],
                stage,
                cancelled=self.cancelled,
            )
        except tracking.TrackingError as exc:
            self.check()
            raise WorkflowError("tracking_failed", "Target tracking failed.") from exc

        result_path = stage / "tracking.json"
        preview_path = stage / "tracking.preview.mp4"
        if {path.name for path in stage.iterdir()} != {
            result_path.name,
            preview_path.name,
        }:
            raise WorkflowError(
                "unsafe_artifact",
                "Tracking renderer produced an unexpected file family.",
            )
        for path in (result_path, preview_path):
            try:
                metadata = path.lstat()
            except OSError as exc:
                raise WorkflowError(
                    "unsafe_artifact", "Tracking outputs must be regular staged files."
                ) from exc
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0:
                raise WorkflowError(
                    "unsafe_artifact",
                    "Tracking outputs must be regular nonempty files.",
                )

        try:
            preview_media = self.probe(preview_path)
        except (WorkflowError, OSError, TypeError, ValueError) as exc:
            raise WorkflowError(
                "unsafe_artifact", "Tracking preview is not a valid local video."
            ) from exc
        source_media = project["media"]
        fps = source_media["fps"]
        expected_frames = math.ceil(payload["end_seconds"] * fps - 1e-9) - math.ceil(
            payload["start_seconds"] * fps - 1e-9
        )
        expected_duration = expected_frames / fps
        if (
            preview_media["width"] != source_media["width"]
            or preview_media["height"] != source_media["height"]
            or not math.isclose(preview_media["fps"], fps, abs_tol=0.001)
            or not math.isclose(
                preview_media["duration"],
                expected_duration,
                rel_tol=0,
                abs_tol=max(0.05, 1 / fps),
            )
            or preview_media["has_audio"]
        ):
            raise WorkflowError(
                "unsafe_artifact",
                "Tracking preview does not match the requested source window.",
            )

        def reject_json_constant(value):
            raise ValueError(f"Invalid JSON constant: {value}")

        try:
            result = json.loads(
                result_path.read_text(encoding="utf-8"),
                parse_constant=reject_json_constant,
            )
            canonical(result)
        except (
            OSError,
            UnicodeError,
            TypeError,
            ValueError,
            RecursionError,
            WorkflowError,
        ) as exc:
            raise WorkflowError(
                "unsafe_artifact", "Tracking result JSON is invalid or non-finite."
            ) from exc
        _validate_tracking_result(result, payload, project)
        result["source_digest"] = project["source_digest"]
        result["tracking_job_id"] = self.job["id"]
        result_path.write_text(canonical(result), encoding="utf-8")
        return {}, [
            ("tracking_result", result_path),
            ("tracking_preview", preview_path),
        ]

    def export(self, stage, project):
        backend = self.job["payload"].get("backend")
        if backend not in (None, "native", "statistics"):
            raise WorkflowError(
                "unknown_export_backend", "Export job names an unsupported backend."
            )
        with self.workflow.connect() as con:
            analysis = self.workflow._approved(
                con,
                project["id"],
                self.job["payload"]["analysis_id"],
                self.job["payload"]["digest"],
            )
        proposal = analysis["proposal"]
        if backend == "statistics":
            from . import stats_export

            xlsx = stage / "statistics.xlsx"
            html = stage / "statistics.html"
            data = stage / "statistics.json"
            try:
                statistics = stats_export.build_statistics(proposal["events"])
                stats_export.write_xlsx(statistics, xlsx)
                stats_export.write_html(statistics, html)
            except stats_export.StatsError as exc:
                raise WorkflowError("invalid_analysis", str(exc)) from exc
            data.write_text(canonical(statistics), encoding="utf-8")
            files = [
                ("statistics_xlsx", xlsx),
                ("statistics_html", html),
                ("statistics_json", data),
            ]
            if {path.name for path in stage.iterdir()} != {
                "statistics.xlsx",
                "statistics.html",
                "statistics.json",
            }:
                raise WorkflowError(
                    "unsafe_artifact", "Statistics exporter produced unexpected output."
                )
            return {}, files

        from .rendering import render_reel

        source = self._source(project)
        native = backend == "native"
        playlist = self.job["payload"].get("playlist")
        render_events = proposal["events"]
        render_drawings = proposal["drawings"]
        if playlist is not None and not native:
            render_events = [
                event
                for event in proposal["events"]
                if playlist in event.get("playlists", [])
            ]
            if not render_events:
                raise WorkflowError(
                    "invalid_playlist", "No event belongs to that playlist."
                )
            selected_ids = {event["event_id"] for event in render_events}
            render_drawings = [
                drawing
                for drawing in proposal["drawings"]
                if drawing["event_id"] in selected_ids
            ]
        # Native rendering ignores playlist tags; proposal.json retains canonical metadata.
        reel = stage / "reel.mp4"
        sidecar = stage / "reel.drawings.json"
        native_original = stage / "reel.native.mp4"
        native_analysis = stage / "reel.once.xml"
        native_manifest = None
        if native:
            if self.workflow.native_once_executable is None:
                raise WorkflowError(
                    "native_not_configured",
                    "Native export worker has no configured Once executable.",
                )
            from .native_rendering import render_native_reel

            try:
                render_native_reel(
                    source,
                    proposal["events"],
                    proposal["drawings"],
                    reel,
                    once_executable=self.workflow.native_once_executable,
                    cancelled=self.cancelled,
                )
            except RenderError:
                self.check()
                raise
            self.check(0.8)
            native_names = {
                reel.name,
                native_original.name,
                native_analysis.name,
                sidecar.name,
            }
            if {path.name for path in stage.iterdir()} != native_names:
                raise WorkflowError(
                    "unsafe_artifact", "Native renderer produced unexpected output."
                )
            for path in (reel, native_original, native_analysis, sidecar):
                if path.is_symlink() or not path.is_file() or not path.stat().st_size:
                    raise WorkflowError(
                        "unsafe_artifact",
                        "Native renderer outputs must be regular nonempty files.",
                    )
            self.probe(reel)
            try:
                sidecar_data = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise WorkflowError(
                    "unsafe_artifact", "Native renderer sidecar is invalid."
                ) from exc
            if (
                not isinstance(sidecar_data, dict)
                or sidecar_data.get("semantics") != "native-once-headless"
                or sidecar_data.get("drawings") != proposal["drawings"]
                or not isinstance(sidecar_data.get("manifest"), dict)
            ):
                raise WorkflowError(
                    "unsafe_artifact",
                    "Native sidecar does not match the approved drawings.",
                )
            native_manifest = sidecar_data["manifest"]
            expected_manifest = {
                "source": str(source.resolve(strict=True)),
                "destination": str(reel),
                "drawings_sidecar": str(sidecar),
                "native_original": str(native_original),
                "native_analysis": str(native_analysis),
            }
            if (
                any(
                    native_manifest.get(key) != value
                    for key, value in expected_manifest.items()
                )
                or not isinstance(native_manifest.get("clips"), list)
                or not isinstance(native_manifest.get("output"), dict)
                or not isinstance(
                    native_manifest.get("requested_native_settings"), dict
                )
            ):
                raise WorkflowError(
                    "unsafe_artifact", "Native sidecar references unexpected outputs."
                )
            native_manifest["destination"] = reel.name
            native_manifest["drawings_sidecar"] = sidecar.name
            native_manifest["native_original"] = native_original.name
            native_manifest["native_analysis"] = native_analysis.name
            sidecar.write_text(
                json.dumps(
                    sidecar_data, sort_keys=True, separators=(",", ":"), allow_nan=False
                ),
                encoding="utf-8",
            )
        else:
            try:
                render_reel(
                    source,
                    render_events,
                    render_drawings,
                    reel,
                    cancelled=self.cancelled,
                )
            except RenderError:
                self.check()
                raise
            self.check(0.8)
            if {p.name for p in stage.iterdir()} != {reel.name, sidecar.name}:
                raise WorkflowError(
                    "unsafe_artifact", "Renderer produced unexpected output."
                )
            for path in (reel, sidecar):
                if path.is_symlink() or not path.is_file() or not path.stat().st_size:
                    raise WorkflowError(
                        "unsafe_artifact",
                        "Renderer outputs must be regular nonempty files.",
                    )
            self.probe(reel)
            try:
                sidecar_data = json.loads(sidecar.read_text())
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise WorkflowError(
                    "unsafe_artifact", "Renderer sidecar is invalid."
                ) from exc
            if (
                not isinstance(sidecar_data, dict)
                or sidecar_data.get("semantics") != "custom-render-only"
                or sidecar_data.get("drawings") != render_drawings
                or not isinstance(sidecar_data.get("manifest"), dict)
            ):
                raise WorkflowError(
                    "unsafe_artifact",
                    "Renderer sidecar does not match the approved drawings.",
                )
            renderer_manifest = sidecar_data["manifest"]
            if renderer_manifest.get("destination") != str(
                reel
            ) or renderer_manifest.get("drawings_sidecar") != str(sidecar):
                raise WorkflowError(
                    "unsafe_artifact", "Renderer sidecar references unexpected outputs."
                )
            renderer_manifest["destination"] = reel.name
            renderer_manifest["drawings_sidecar"] = sidecar.name
            if playlist is not None:
                renderer_manifest["playlist"] = playlist
            sidecar.write_text(
                json.dumps(
                    sidecar_data, sort_keys=True, separators=(",", ":"), allow_nan=False
                )
            )
        (stage / "analysis.md").write_text(proposal["report_markdown"])
        (stage / "proposal.json").write_text(analysis["canonical_json"])
        (stage / "review.json").write_text(canonical(analysis["reviews"][-1]))
        result = tools.generate_once_import_csv(proposal["events"], "events.csv", stage)
        if not result.get("ok"):
            raise WorkflowError("csv_failed", "Unable to write event CSV.")
        names = {
            "reel.mp4",
            "reel.drawings.json",
            "analysis.md",
            "proposal.json",
            "review.json",
            "events.csv",
        }
        if native:
            names.update({"reel.native.mp4", "reel.once.xml"})
        if {p.name for p in stage.iterdir()} != names or any(
            (stage / name).is_symlink() or not (stage / name).is_file()
            for name in names
        ):
            raise WorkflowError(
                "unsafe_artifact", "Export files must be regular staged files."
            )
        manifest = {
            "analysis_id": analysis["id"],
            "digest": analysis["digest"],
            "source_digest": project["source_digest"],
            "native_import_verified": False,
            "drawings": "burn-in-only",
            "files": {name: self.hash_file(stage / name) for name in sorted(names)},
        }
        if playlist is not None and not native:
            manifest["playlist"] = playlist
        if native:
            manifest.update(
                render_engine="once_cli_native_headless",
                native_original={
                    "file": native_original.name,
                    "role": "original native Once CLI output; retained without compatibility transcode",
                },
                compatible_reel={
                    "file": reel.name,
                    "role": "H.264/AAC compatibility transcode of the native original",
                },
                native_import_verified=False,
                gui_project_list_import_verified=False,
                drawings="native-once-headless; GUI project-list import unverified",
                native_manifest=native_manifest,
            )
        (stage / "manifest.json").write_text(canonical(manifest))
        kinds = {
            "reel.mp4": "reel",
            "reel.drawings.json": "drawings_sidecar",
            "analysis.md": "report",
            "events.csv": "once_csv",
            "proposal.json": "proposal",
            "review.json": "review",
            "manifest.json": "manifest",
        }
        if native:
            kinds.update(
                {
                    "reel.native.mp4": "native_original_reel",
                    "reel.once.xml": "native_analysis_xml",
                }
            )
        return {}, [(kinds[p.name], p) for p in stage.iterdir() if p.is_file()]

    @staticmethod
    def sync_path(path, *, directory=False):
        descriptor = os.open(
            path, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else 0)
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def execute(self, job):
        self.job = job
        stage = None
        final = None
        published = False
        try:
            task = self.workflow.task_dir(job["id"])
            stage = task / ("staging-" + uuid.uuid4().hex)
            stage.mkdir(mode=0o700)
            final = task / "output"
            if final.exists() or final.is_symlink():
                raise WorkflowError("output_exists", "Task output already exists.")
            project = self.workflow.get_project(job["project_id"])
            if job["kind"] == "ingest":
                updates, files = self.ingest(stage)
            elif job["kind"] == "evidence":
                updates, files = self.evidence(stage, project)
            else:
                updates, files = self.export(stage, project)
            self.check(0.99)
            for _, path in files:
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or not path.resolve().is_relative_to(stage)
                ):
                    raise WorkflowError(
                        "unsafe_artifact", "Output must be a regular staged file."
                    )
            for _, path in files:
                self.sync_path(path)
            self.sync_path(stage, directory=True)
            artifacts = [
                {
                    "id": uuid.uuid4().hex,
                    "project_id": job["project_id"],
                    "job_id": job["id"],
                    "kind": kind,
                    "path": str(final / path.name),
                    "sha256": self.hash_file(path),
                    "size_bytes": path.stat().st_size,
                }
                for kind, path in files
            ]
            if job["kind"] in ("export", "evidence") and not (
                job["kind"] == "export"
                and job["payload"].get("backend") == "statistics"
            ):
                self._source(project)
            with self.workflow.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                current = self.workflow._get("jobs", job["id"], con)
                if current["cancel_requested"]:
                    raise Cancelled()
                if job["kind"] == "export":
                    self.workflow._approved(
                        con,
                        job["project_id"],
                        job["payload"]["analysis_id"],
                        job["payload"]["digest"],
                    )
                os.rename(stage, final)
                published = True
                self.sync_path(task, directory=True)
                if job["kind"] == "ingest":
                    project.update(updates, source_path=str(final / "source.mp4"))
                    self.workflow._put(con, "projects", project)
                for artifact in artifacts:
                    self.workflow._put(con, "artifacts", artifact)
                current.update(
                    status="completed",
                    progress=1,
                    heartbeat=time.time(),
                    finished_at=time.time(),
                )
                self.workflow._put(con, "jobs", current)
        except Exception as exc:  # noqa: BLE001 - persist a safe failure for each job.
            if self.workflow.get_job(job["id"])["status"] == "completed":
                return
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)
            if published:
                shutil.rmtree(final, ignore_errors=True)
            with self.workflow.connect() as con:
                con.execute("BEGIN IMMEDIATE")
                current = self.workflow._get("jobs", job["id"], con)
                cancelled = isinstance(exc, Cancelled) or current["cancel_requested"]
                current.update(
                    status="interrupted"
                    if isinstance(exc, WorkerStopped)
                    else "cancelled"
                    if cancelled
                    else "failed",
                    retryable=True,
                    error_code="cancelled"
                    if cancelled
                    else getattr(exc, "code", "job_failed"),
                    error="Job was cancelled."
                    if cancelled
                    else getattr(
                        exc,
                        "message",
                        "Job failed; check local tools and source media.",
                    ),
                    finished_at=time.time(),
                    heartbeat=time.time(),
                )
                self.workflow._put(con, "jobs", current)


def _cleanup_interrupted_job(workflow, job, con):
    warning = "Generated task residue cleanup is pending."
    job_id = job.get("id")
    if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{32}", job_id):
        return warning

    tasks = workflow.root / "tasks"
    try:
        if tasks.is_symlink() or not tasks.is_dir():
            return warning
        task = tasks / job_id
        if task.is_symlink():
            return warning
        if not task.exists():
            return None
        if not task.is_dir():
            return warning
        resolved_tasks = tasks.resolve(strict=True)
        resolved_task = task.resolve(strict=True)
        if not resolved_task.is_relative_to(resolved_tasks):
            return warning

        references = [
            (project.get("source_path"),)
            for project in workflow._rows("projects", con=con)
        ]
        references.extend(
            (artifact.get("path"),) for artifact in workflow._rows("artifacts", con=con)
        )

        def is_referenced(directory):
            for (value,) in references:
                if value is None:
                    continue
                if not isinstance(value, str):
                    return True
                try:
                    reference = Path(os.path.abspath(value))
                    resolved_reference = reference.resolve(strict=False)
                except (OSError, RuntimeError, ValueError):
                    return True
                if any(
                    reference == candidate or candidate in reference.parents
                    for candidate in (directory, directory.resolve(strict=True))
                ) or any(
                    resolved_reference == candidate
                    or candidate in resolved_reference.parents
                    for candidate in (directory, directory.resolve(strict=True))
                ):
                    return True
            return False

        pending = False
        for entry in task.iterdir():
            if entry.name != "output" and not re.fullmatch(
                r"staging-[a-f0-9]{32}", entry.name
            ):
                continue
            if entry.is_symlink():
                pending = True
                continue
            try:
                if not entry.is_dir():
                    pending = True
                    continue
                resolved_entry = entry.resolve(strict=True)
                if not resolved_entry.is_relative_to(resolved_task):
                    pending = True
                    continue
                if is_referenced(entry):
                    pending = True
                    continue
                shutil.rmtree(entry)
            except (OSError, RuntimeError, ValueError):
                pending = True
        return warning if pending else None
    except (OSError, RuntimeError, ValueError):
        return warning


def run_worker(
    root,
    *,
    once=False,
    allowed_source_dirs=None,
    native_once_executable=None,
    tracking_enabled=False,
):
    workflow_options = {}
    if native_once_executable is not None:
        workflow_options["native_once_executable"] = native_once_executable
    if tracking_enabled is not False:
        workflow_options["tracking_enabled"] = tracking_enabled
    workflow = Workflow(root, allowed_source_dirs, **workflow_options)
    lock_path = workflow.root / "worker.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WorkflowError(
                "worker_locked", "A worker already owns this workflow root."
            ) from None
        with workflow.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            for job in workflow._rows("jobs", con=con):
                if job["status"] in ("running", "cancelling"):
                    job.update(
                        status="interrupted",
                        retryable=True,
                        error_code="worker_interrupted",
                        error="Worker stopped before the job finished.",
                    )
                elif not (
                    job["status"] == "interrupted" and job.get("cleanup_pending")
                ):
                    continue
                warning = _cleanup_interrupted_job(workflow, job, con)
                if warning:
                    job.update(cleanup_pending=True, cleanup_warning=warning)
                else:
                    job.pop("cleanup_pending", None)
                    job.pop("cleanup_warning", None)
                workflow._put(con, "jobs", job)
        worker = Worker(workflow)
        with stop_signals(worker):
            while True:
                claimed = None
                with workflow.connect() as con:
                    con.execute("BEGIN IMMEDIATE")
                    for job in workflow._rows("jobs", con=con):
                        if job["status"] == "queued":
                            job.update(
                                status="running",
                                heartbeat=time.time(),
                                started_at=time.time(),
                            )
                            workflow._put(con, "jobs", job)
                            claimed = job
                            break
                if claimed:
                    worker.execute(claimed)
                if once or worker.stopping:
                    return
                if claimed is None:
                    time.sleep(0.25)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--native-once-executable", type=Path)
    parser.add_argument("--enable-tracking", action="store_true")
    parser.add_argument(
        "--allow-source-dir",
        "--allowed-source-dir",
        dest="allowed_source_dir",
        type=Path,
        action="append",
    )
    args = parser.parse_args()
    try:
        worker_options = {
            "once": args.once,
            "allowed_source_dirs": args.allowed_source_dir,
        }
        if args.native_once_executable is not None:
            worker_options["native_once_executable"] = args.native_once_executable
        if args.enable_tracking:
            worker_options["tracking_enabled"] = True
        run_worker(args.root, **worker_options)
    except WorkflowError as exc:
        parser.exit(1, exc.message + "\n")


if __name__ == "__main__":
    main()
