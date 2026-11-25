"""Regressions for the CSV publication lifecycle, not just CSV serialization."""

import csv
import errno
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace

import pytest

from once_sport_mcp import csv_export
from once_sport_mcp.tools import generate_once_import_csv


def event():
    return {
        "event_id": "1",
        "period": 1,
        "start_seconds": 0,
        "end_seconds": 2,
        "team": "A",
        "phase": "goal",
        "action": "Shot",
    }


def test_failed_write_leaves_no_csv_and_same_name_can_be_retried(tmp_path, monkeypatch):
    real_writer = csv.writer

    class FailingWriter:
        def __init__(self, file):
            self.writer = real_writer(file)

        def writerow(self, row):
            self.writer.writerow(row)
            raise OSError("private timeline detail must not escape")

    with monkeypatch.context() as patch:
        patch.setattr(csv, "writer", FailingWriter)
        result = generate_once_import_csv([event()], "retry.csv", tmp_path)

    assert result["ok"] is False
    assert "private timeline" not in str(result)
    assert not (tmp_path / "retry.csv").exists(), (
        "failed writes must not reserve the name"
    )
    assert list(tmp_path.iterdir()) == [], "owned staging files must be cleaned up"
    assert generate_once_import_csv([event()], "retry.csv", tmp_path)["ok"] is True


def test_final_name_is_invisible_until_serialization_finishes(tmp_path, monkeypatch):
    started, proceed = Event(), Event()
    real_writer = csv.writer

    class PausedWriter:
        def __init__(self, file):
            self.writer = real_writer(file)

        def writerow(self, row):
            self.writer.writerow(row)
            started.set()
            assert proceed.wait(5), "test writer was not released"

    monkeypatch.setattr(csv, "writer", PausedWriter)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            generate_once_import_csv, [event()], "complete.csv", tmp_path
        )
        try:
            assert started.wait(5), "serialization never started"
            assert not (tmp_path / "complete.csv").exists()
        finally:
            proceed.set()
        result = future.result(timeout=5)
    assert result["ok"] is True
    assert result["import_verified"] is False
    assert stat.S_IMODE((tmp_path / "complete.csv").stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["complete.csv"]


def test_competing_exports_have_one_winner_and_do_not_overwrite(tmp_path):
    barrier = Barrier(8)

    def attempt(index):
        candidate = dict(event(), action=f"Attempt-{index}")
        barrier.wait(timeout=5)
        return index, generate_once_import_csv([candidate], "shared.csv", tmp_path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(attempt, range(8)))
    winners = [index for index, result in results if result["ok"]]
    assert len(winners) == 1
    assert f"Attempt-{winners[0]}" in (tmp_path / "shared.csv").read_text()
    assert [p.name for p in tmp_path.iterdir()] == ["shared.csv"]


@pytest.mark.parametrize("destination", ["file", "directory", "symlink", "dangling"])
def test_existing_destinations_are_preserved_without_staging_leaks(
    tmp_path, destination
):
    target = tmp_path / "existing.csv"
    if destination == "directory":
        target.mkdir()
    elif destination in ("symlink", "dangling"):
        target.symlink_to(tmp_path / "payload.txt")
        if destination == "symlink":
            (tmp_path / "payload.txt").write_text("preserve me")
    else:
        target.write_text("preserve me")
    before = sorted(p.name for p in tmp_path.iterdir())
    result = generate_once_import_csv([event()], "existing.csv", tmp_path)
    assert result["ok"] is False
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    if destination in ("file", "symlink"):
        assert target.read_text() == "preserve me"
    if destination in ("symlink", "dangling"):
        assert target.is_symlink()


def test_file_sync_failure_aborts_before_publication(tmp_path, monkeypatch):
    def fail_sync(fd):
        raise OSError("private disk detail")

    monkeypatch.setattr(os, "fsync", fail_sync)
    result = generate_once_import_csv([event()], "sync.csv", tmp_path)
    assert result["ok"] is False
    assert list(tmp_path.iterdir()) == []
    assert "private disk detail" not in str(result)


def test_directory_sync_failure_preserves_published_success_with_warning(
    tmp_path, monkeypatch
):
    real_sync = os.fsync

    def fail_directory_sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("private directory detail")
        real_sync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_sync)
    result = generate_once_import_csv([event()], "published.csv", tmp_path)
    assert result["ok"] is True
    assert result["warnings"] == [
        "CSV was published, but directory persistence could not be confirmed."
    ]
    assert "private directory detail" not in str(result)
    assert (tmp_path / "published.csv").is_file()
    assert generate_once_import_csv([event()], "published.csv", tmp_path)["ok"] is False


def test_staging_cleanup_failure_does_not_turn_publication_into_rejection(
    tmp_path, monkeypatch
):
    def fail_unlink(*args, **kwargs):
        raise OSError("private staging detail")

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", fail_unlink)
        result = generate_once_import_csv([event()], "published.csv", tmp_path)
    assert result["ok"] is True
    assert result["warnings"]
    assert "private staging detail" not in str(result)
    assert (tmp_path / "published.csv").is_file()
    staging = list(tmp_path.glob(".once-*.tmp"))
    assert len(staging) == 1
    assert staging[0].read_bytes() == (tmp_path / "published.csv").read_bytes()


def test_stream_open_failure_closes_owned_descriptor(tmp_path, monkeypatch):
    descriptors = []

    def fail_fdopen(fd, *args, **kwargs):
        descriptors.append(fd)
        raise OSError("stream unavailable")

    monkeypatch.setattr(os, "fdopen", fail_fdopen)
    result = generate_once_import_csv([event()], "stream.csv", tmp_path)
    assert result["ok"] is False
    assert list(tmp_path.iterdir()) == []
    assert len(descriptors) == 1
    with pytest.raises(OSError) as error:
        os.fstat(descriptors[0])
    assert error.value.errno == errno.EBADF


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_interruption_aborts_staging_without_suppressing_it(
    tmp_path, monkeypatch, interruption
):
    class InterruptedWriter:
        def __init__(self, file):
            pass

        def writerow(self, row):
            raise interruption()

    monkeypatch.setattr(csv, "writer", InterruptedWriter)
    with pytest.raises(interruption):
        generate_once_import_csv([event()], "interrupted.csv", tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_staging_name_collision_never_deletes_an_unowned_file(tmp_path, monkeypatch):
    collision = tmp_path / ".once-fixture.tmp"
    collision.write_text("not owned by this attempt")
    monkeypatch.setattr(csv_export, "uuid4", lambda: SimpleNamespace(hex="fixture"))
    result = generate_once_import_csv([event()], "collision.csv", tmp_path)
    assert result["ok"] is False
    assert collision.read_text() == "not owned by this attempt"
    assert not (tmp_path / "collision.csv").exists()


@pytest.mark.parametrize("invalid", ["timeline", "filename"])
def test_rejected_input_never_creates_the_output_directory(tmp_path, invalid):
    output = tmp_path / "must-not-exist"
    events = [{}] if invalid == "timeline" else [event()]
    filename = "valid.csv" if invalid == "timeline" else "../escape.csv"
    result = generate_once_import_csv(events, filename, output)
    assert result["ok"] is False
    assert not output.exists()


@pytest.mark.parametrize("published", [False, True])
def test_owned_staging_cleanup_can_be_retried_after_a_transient_failure(
    tmp_path, monkeypatch, published
):
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    publication = csv_export._Publication(directory_fd, "result.csv")
    try:
        publication.write([event()])
        if published:
            publication.publish()
        staging = tmp_path / publication.staging_name

        def fail_unlink(*args, **kwargs):
            raise OSError("temporary cleanup failure")

        with monkeypatch.context() as patch:
            patch.setattr(os, "unlink", fail_unlink)
            assert publication.cleanup()
        assert staging.exists()
        assert publication.cleanup() == []
        assert not staging.exists(), "cleanup ownership must survive an aborted attempt"
        assert (tmp_path / "result.csv").exists() is published
    finally:
        publication.cleanup()
        os.close(directory_fd)


def test_publication_guards_prevent_early_or_duplicate_commit(tmp_path):
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    publication = csv_export._Publication(directory_fd, "guarded.csv")
    try:
        with pytest.raises(RuntimeError, match="fully written"):
            publication.publish()
        assert list(tmp_path.iterdir()) == []
        publication.write([event()])
        with pytest.raises(RuntimeError, match="only be staged once"):
            publication.write([event()])
        publication.publish()
        before = (tmp_path / "guarded.csv").read_bytes()
        with pytest.raises(RuntimeError, match="fully written"):
            publication.publish()
        assert (tmp_path / "guarded.csv").read_bytes() == before
    finally:
        publication.cleanup()
        os.close(directory_fd)


def test_completed_cleanup_does_not_delete_a_replacement_staging_name(tmp_path):
    directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    publication = csv_export._Publication(directory_fd, "unused.csv")
    try:
        publication.write([event()])
        assert publication.cleanup() == []
        replacement = tmp_path / publication.staging_name
        replacement.write_text("belongs to another attempt")
        assert publication.cleanup() == []
        assert replacement.read_text() == "belongs to another attempt"
    finally:
        os.close(directory_fd)


def test_directory_close_failure_does_not_hide_published_artifact(
    tmp_path, monkeypatch
):
    real_close = os.close

    def close_then_report_failure(fd):
        real_close(fd)
        raise OSError("private close detail")

    monkeypatch.setattr(os, "close", close_then_report_failure)
    result = generate_once_import_csv([event()], "closed.csv", tmp_path)
    assert result["ok"] is True
    assert result["warnings"] == [
        "Output directory handle cleanup could not be confirmed."
    ]
    assert "private close detail" not in str(result)
    assert (tmp_path / "closed.csv").is_file()


def test_unsupported_atomic_publication_fails_without_a_partial_csv(
    tmp_path, monkeypatch
):
    def reject_link(*args, **kwargs):
        raise OSError(errno.EOPNOTSUPP, "unsupported hard links")

    monkeypatch.setattr(os, "link", reject_link)
    result = generate_once_import_csv([event()], "unsupported.csv", tmp_path)
    assert result["ok"] is False
    assert list(tmp_path.iterdir()) == []
