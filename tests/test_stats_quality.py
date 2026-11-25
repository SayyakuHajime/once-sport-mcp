import xml.etree.ElementTree as ET
from zipfile import ZipFile

import pytest

from once_sport_mcp.stats_export import (
    StatsError,
    build_statistics,
    write_html,
    write_xlsx,
)
from once_sport_mcp.tools import validate_timeline

_PHASES = (
    "defending",
    "transition_to_attack",
    "attacking",
    "transition_to_defend",
    "set_piece_attack",
    "set_piece_defend",
    "goal",
    "goal_conceded",
)


def _event(event_id, period, start, end, team, phase, action, **optional):
    return {
        "event_id": event_id,
        "period": period,
        "start_seconds": start,
        "end_seconds": end,
        "team": team,
        "phase": phase,
        "action": action,
        **optional,
    }


def _sheet_rows(archive, sheet_number):
    namespace = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    root = ET.fromstring(archive.read(f"xl/worksheets/sheet{sheet_number}.xml"))
    rows = []
    for row in root.findall("m:sheetData/m:row", namespace):
        values = []
        for cell in row.findall("m:c", namespace):
            inline = cell.find("m:is", namespace)
            values.append(
                "".join(inline.itertext())
                if inline is not None
                else cell.findtext("m:v", namespaces=namespace)
            )
        rows.append(values)
    return rows


def test_summed_clip_seconds_and_union_coverage_do_not_conflate_overlaps():
    stats = build_statistics(
        [
            _event("a", 1, 10, 20, "Home", "attacking", "pass"),
            _event("b", 1, 15, 25, "Home", "attacking", "carry"),
            _event("c", 1, 20, 30, "Away", "defending", "press"),
        ]
    )

    assert stats["total_seconds"] == stats["summed_clip_seconds"] == 30
    assert stats["coverage_seconds"] == 20
    phases = {row["phase"]: row for row in stats["by_phase"]}
    assert phases["attacking"]["seconds"] == 20
    assert phases["attacking"]["coverage_seconds"] == 15
    assert phases["defending"]["seconds"] == 10
    assert phases["defending"]["coverage_seconds"] == 10


def test_player_mentions_are_split_by_phase_and_flag():
    stats = build_statistics(
        [
            _event(
                "a",
                1,
                0,
                1,
                "Home",
                "attacking",
                "pass",
                players=["Ari", "Ari"],
                flag="good",
            ),
            _event(
                "b",
                1,
                2,
                3,
                "Home",
                "defending",
                "tackle",
                players=["Ari", "Bo"],
                flag="bad",
            ),
            _event(
                "c",
                1,
                4,
                5,
                "Away",
                "attacking",
                "run",
                players=["Ari", "Bo"],
                flag="favourite",
            ),
            _event("d", 1, 6, 7, "Away", "goal", "shot", players=["Bo"]),
        ]
    )
    players = {row["player"]: row for row in stats["players"]}

    assert players["Ari"]["count"] == 3
    assert players["Ari"]["by_phase"] == {
        phase: count
        for phase, count in (
            (phase, 2 if phase == "attacking" else 1 if phase == "defending" else 0)
            for phase in _PHASES
        )
    }
    assert players["Ari"]["by_flag"] == {"good": 1, "bad": 1, "favourite": 1}
    assert players["Bo"]["by_flag"] == {"good": 0, "bad": 1, "favourite": 1}
    assert players["Bo"]["by_phase"]["goal"] == 1


def test_period_clock_offsets_and_team_scoreline_are_statistics_only():
    events = [
        _event(
            "first-half-goal",
            1,
            90,
            91,
            "Home",
            "goal",
            "shot",
            clock_offset_seconds=0,
        ),
        _event(
            "second-half-goal",
            2,
            60,
            61,
            "Away",
            "goal",
            "header",
            clock_offset_seconds=2700,
        ),
    ]

    stats = build_statistics(events)

    assert stats["period_clock_offsets"] == {1: 0, 2: 2700}
    assert [row["match_clock"] for row in stats["goals"]] == ["01:30", "46:00"]
    assert stats["scoreline"] == "Home 1 - 1 Away"
    assert not validate_timeline(events)["valid"]
    fallback = build_statistics(
        [
            _event("alpha-goal", 1, 3, 4, "Alpha", "goal", "shot"),
            _event("beta-run", 1, 5, 6, "Beta", "attacking", "run"),
        ]
    )
    assert fallback["scoreline"] == "Alpha: 1, Beta: 0"


def test_goal_conceded_does_not_credit_conceding_team_in_scoreline():
    stats = build_statistics(
        [
            _event("h-goal", 1, 10, 11, "Home", "goal", "shot"),
            _event("h-conceded", 1, 20, 21, "Home", "goal_conceded", "goal"),
            _event("a-pass", 1, 30, 31, "Away", "attacking", "pass"),
        ]
    )
    assert stats["scoreline"] == "Home 1 - 0 Away"


def test_clock_offsets_are_validated_as_consistent_period_metadata():
    with pytest.raises(StatsError, match="clock_offset_seconds"):
        build_statistics(
            [
                _event(
                    "a", 2, 0, 1, "Home", "attacking", "pass", clock_offset_seconds=2700
                ),
                _event(
                    "b", 2, 1, 2, "Home", "attacking", "pass", clock_offset_seconds=2800
                ),
            ]
        )
    with pytest.raises(StatsError, match="clock_offset_seconds"):
        build_statistics(
            [_event("a", 2, 0, 1, "Home", "attacking", "pass", clock_offset_seconds=-1)]
        )


def test_html_and_xlsx_export_complete_statistics_and_safe_inline_charts(tmp_path):
    events = [
        _event(
            '<img src=x onerror="alert(1)">',
            1,
            90,
            92,
            "Home",
            "goal",
            "shot",
            players=["Ada"],
            notes="Safe <text>",
            flag="good",
            playlists=["Final third"],
            clock_offset_seconds=0,
        ),
        _event(
            "away-goal",
            2,
            60,
            61,
            "Away",
            "goal",
            "header",
            players=["Ada", "Bo"],
            flag="bad",
            playlists=["Review"],
            clock_offset_seconds=2700,
        ),
        _event(
            "press",
            1,
            15,
            16,
            "Home",
            "defending",
            "press",
            players=["Bo"],
            flag="favourite",
            playlists=["Review"],
        ),
    ]
    stats = build_statistics(events, title="Cup <Final>")
    html_path = tmp_path / "statistics.html"
    xlsx_path = tmp_path / "statistics.xlsx"

    write_html(stats, html_path)
    write_xlsx(stats, xlsx_path)
    document = html_path.read_text(encoding="utf-8")
    lowered = document.lower()

    for section in (
        "Summary",
        "Team statistics",
        "By team/phase",
        "By action",
        "By period",
        "Flags",
        "Playlists",
        "Players",
        "Goals",
        "Scoreline",
    ):
        assert section in document
    assert "Summed clip seconds" in document
    assert "Interval-union coverage seconds" in document
    assert "01:30" in document and "46:00" in document
    assert document.count("<svg") == 2
    assert "Events per phase" in document and "Event timeline" in document
    assert "<script" not in lowered
    assert "http://" not in lowered and "https://" not in lowered
    assert "<img" not in document
    assert "&lt;img" in document and "&lt;text&gt;" in document

    namespace = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(xlsx_path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        names = [
            sheet.attrib["name"]
            for sheet in workbook.findall("m:sheets/m:sheet", namespace)
        ]
        assert names == [
            "Summary",
            "Events",
            "Goals",
            "Players",
            "By Team",
            "Team Phase",
            "By Action",
            "By Period",
            "Flags",
            "Playlists",
        ]
        assert archive.testzip() is None
        styles = ET.fromstring(archive.read("xl/styles.xml"))
        assert (
            styles.find(".//m:patternFill[@patternType='gray125']", namespace)
            is not None
        )
        summary = _sheet_rows(archive, 1)
        assert any("Summed clip seconds" in row for row in summary)
        assert any("Interval-union coverage seconds" in row for row in summary)
        assert any("Home 1 - 1 Away" in row for row in summary)
        event_rows = _sheet_rows(archive, 2)
        assert "Match Clock" in event_rows[0]
        clock_column = event_rows[0].index("Match Clock")
        assert event_rows[1][clock_column] == "00:15"
        assert event_rows[2][clock_column] == "01:30"
        assert event_rows[3][clock_column] == "46:00"
        goal_rows = _sheet_rows(archive, 3)
        assert "Match Clock" in goal_rows[0]
        assert goal_rows[1][-1] == "01:30"
        player_headers = _sheet_rows(archive, 4)[0]
        assert "attacking" in player_headers and "defending" in player_headers
        assert (
            "good" in player_headers
            and "bad" in player_headers
            and "favourite" in player_headers
        )
        assert _sheet_rows(archive, 6)[0][:2] == ["Team", "Phase"]
        assert "Action" in _sheet_rows(archive, 7)[0]
        assert "Period" in _sheet_rows(archive, 8)[0]
        assert "Flag" in _sheet_rows(archive, 9)[0]
        assert "Playlist" in _sheet_rows(archive, 10)[0]


def test_statistics_clock_offset_does_not_change_legacy_event_times():
    stats = build_statistics([_event("plain", 1, 62, 63, "Home", "attacking", "run")])

    assert stats["period_clock_offsets"] == {}
    assert stats["events"][0]["start_seconds"] == 62
    assert stats["events"][0].get("clock_offset_seconds") is None
    assert stats["scoreline"] == "Home: 0"
