import csv
import json

from once_sport_mcp import csv_export
from once_sport_mcp.tools import generate_once_import_csv


def test_once_import_csv_remains_three_columns_and_statistics_are_richer(tmp_path):
    event = {
        "event_id": "goal-1",
        "period": 1,
        "start_seconds": 10,
        "end_seconds": 12,
        "team": "Home",
        "phase": "goal",
        "action": "Shot",
        "players": ["Ari"],
        "notes": "Near post",
        "flag": "good",
        "playlists": ["goals"],
    }

    result = generate_once_import_csv([event], "once-import.csv", tmp_path)

    assert result["ok"] is True
    with (tmp_path / "once-import.csv").open(encoding="utf-8", newline="") as file:
        rows = list(csv.reader(file))
    assert rows[0] == ["start time", "end time", "action name"]
    assert len(rows[1]) == 3
    assert rows[1][:2] == ["10", "12"]
    assert json.loads(rows[1][2]) == ["Home", "goal", "Shot"]
    assert "statistics outputs" in (csv_export.__doc__ or "")
