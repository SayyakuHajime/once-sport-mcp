import csv
import json

from once_sport_mcp.tools import generate_once_import_csv


def test_csv_roundtrip_and_no_overwrite(tmp_path):
    events = [
        {
            "event_id": "1",
            "period": 1,
            "start_seconds": 0,
            "end_seconds": 2.5,
            "team": 'Équipe, "A"',
            "phase": "goal",
            "action": "シュート",
        }
    ]
    result = generate_once_import_csv(events, "safe.csv", tmp_path)
    assert result["ok"]
    with (tmp_path / "safe.csv").open(encoding="utf-8", newline="") as file:
        rows = list(csv.reader(file))
    assert rows[0] == ["start time", "end time", "action name"]
    assert len(rows[1]) == 3
    assert rows[1][:2] == ["0", "2.5"]
    assert json.loads(rows[1][2]) == [events[0]["team"], "goal", "シュート"]
    assert not generate_once_import_csv(events, "safe.csv", tmp_path)["ok"]
    assert not generate_once_import_csv(events, "../escape.csv", tmp_path)["ok"]


def test_symlink_and_invalid_input_do_not_modify_files(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("unchanged")
    (tmp_path / "link.csv").symlink_to(target)
    event = {
        "event_id": "1",
        "period": 1,
        "start_seconds": 0,
        "end_seconds": 1,
        "team": "A",
        "phase": "goal",
        "action": "X",
    }
    assert not generate_once_import_csv([event], "link.csv", tmp_path)["ok"]
    assert target.read_text() == "unchanged"
    event["phase"] = "unknown"
    assert not generate_once_import_csv([event], "invalid.csv", tmp_path)["ok"]
    assert not (tmp_path / "invalid.csv").exists()


def test_csv_preserves_multiple_event_order(tmp_path):
    first = {
        "event_id": "1",
        "period": 1,
        "start_seconds": 0,
        "end_seconds": 2,
        "team": "A",
        "phase": "goal",
        "action": "First",
    }
    second = dict(first, event_id="2", start_seconds=3, end_seconds=4, action="Second")
    assert generate_once_import_csv([first, second], "ordered.csv", tmp_path)["ok"]
    with (tmp_path / "ordered.csv").open(encoding="utf-8", newline="") as file:
        rows = list(csv.reader(file))
    assert [row[:2] for row in rows[1:]] == [["0", "2"], ["3", "4"]]
    assert [json.loads(row[2])[2] for row in rows[1:]] == ["First", "Second"]
