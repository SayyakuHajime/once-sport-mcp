"""Stage the three-column Once CSV for atomic publication; richer data lives in statistics outputs."""

import csv
import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4


class PublicationState(Enum):
    NEW = "new"
    WRITING = "writing"
    READY = "ready"
    PUBLISHED = "published"
    ABORTED = "aborted"


class StagingState(Enum):
    UNOWNED = "unowned"
    OWNED = "owned"
    REMOVED = "removed"
    CLEANUP_PENDING = "cleanup_pending"


@dataclass(frozen=True)
class PublishedCsv:
    path: Path
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RejectedCsv:
    warnings: tuple[str, ...] = ()


@dataclass
class _Publication:
    directory_fd: int
    filename: str
    state: PublicationState = field(init=False, default=PublicationState.NEW)
    staging_state: StagingState = field(init=False, default=StagingState.UNOWNED)
    staging_name: str = field(
        init=False, default_factory=lambda: f".once-{uuid4().hex}.tmp"
    )

    def write(self, events: list[dict[str, Any]]) -> None:
        if self.state is not PublicationState.NEW:
            raise RuntimeError("CSV can only be staged once")
        fd = os.open(
            self.staging_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.directory_fd,
        )
        self.state = PublicationState.WRITING
        self.staging_state = StagingState.OWNED
        try:
            file = os.fdopen(fd, "w", encoding="utf-8", newline="")
        except BaseException:
            os.close(fd)
            raise
        with file:
            writer = csv.writer(file)
            writer.writerow(["start time", "end time", "action name"])
            for event in events:
                label = json.dumps(
                    [event["team"], event["phase"], event["action"]],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                writer.writerow([event["start_seconds"], event["end_seconds"], label])
            file.flush()
            os.fsync(file.fileno())
        self.state = PublicationState.READY

    def publish(self) -> None:
        if self.state is not PublicationState.READY:
            raise RuntimeError("Only a fully written CSV can be published")
        # Hard-link creation is atomic and refuses any existing destination,
        # including a symlink. Never substitute os.replace: it overwrites.
        os.link(
            self.staging_name,
            self.filename,
            src_dir_fd=self.directory_fd,
            dst_dir_fd=self.directory_fd,
            follow_symlinks=False,
        )
        self.state = PublicationState.PUBLISHED

    def cleanup(self) -> list[str]:
        warnings = []
        if self.staging_state in (StagingState.OWNED, StagingState.CLEANUP_PENDING):
            try:
                os.unlink(self.staging_name, dir_fd=self.directory_fd)
            except FileNotFoundError:
                self.staging_state = StagingState.REMOVED
            except OSError:
                self.staging_state = StagingState.CLEANUP_PENDING
                warnings.append(
                    "A temporary export file could not be removed; check the configured output directory."
                )
            else:
                self.staging_state = StagingState.REMOVED
        if self.state is not PublicationState.PUBLISHED:
            self.state = PublicationState.ABORTED
        return warnings


def publish_csv(events, filename, output_dir) -> PublishedCsv | RejectedCsv:
    """Publish already validated events/name; failures before commit leave no CSV."""
    directory_fd = None
    publication = None
    root = None
    warnings = []
    try:
        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve(strict=True)
        directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        publication = _Publication(directory_fd, filename)
        publication.write(events)
        publication.publish()
    except OSError:
        pass
    finally:
        if publication is not None:
            warnings.extend(publication.cleanup())
        if directory_fd is not None:
            if (
                publication is not None
                and publication.state is PublicationState.PUBLISHED
            ):
                try:
                    os.fsync(directory_fd)
                except OSError:
                    warnings.append(
                        "CSV was published, but directory persistence could not be confirmed."
                    )
            try:
                os.close(directory_fd)
            except OSError:
                warnings.append(
                    "Output directory handle cleanup could not be confirmed."
                )
    if publication is not None and publication.state is PublicationState.PUBLISHED:
        return PublishedCsv(root / filename, tuple(warnings))
    return RejectedCsv(tuple(warnings))
