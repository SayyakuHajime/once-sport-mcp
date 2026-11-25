import shutil

import pytest

from once_sport_mcp.stats_export import build_statistics, write_html, write_xlsx

_LIBREOFFICE = shutil.which("libreoffice") or shutil.which("soffice")


def test_phase_and_team_counts_seconds_include_zero_phases():
    events = [
        {
            "event_id": "e2",
            "period": 1,
            "start_seconds": 12.25,
            "end_seconds": 14.5,
            "team": "Zebra",
            "phase": "attacking",
            "action": "pass",
        },
        {
            "event_id": "e1",
            "period": 1,
            "start_seconds": 4,
            "end_seconds": 5.125,
            "team": "Alpha",
            "phase": "defending",
            "action": "tackle",
        },
    ]

    stats = build_statistics(events)

    phases = {row["phase"]: row for row in stats["by_phase"]}
    assert list(phases) == [
        "defending",
        "transition_to_attack",
        "attacking",
        "transition_to_defend",
        "set_piece_attack",
        "set_piece_defend",
        "goal",
        "goal_conceded",
    ]
    assert phases["defending"] == {
        "phase": "defending",
        "count": 1,
        "seconds": 1.125,
        "coverage_seconds": 1.125,
    }
    assert phases["transition_to_attack"] == {
        "phase": "transition_to_attack",
        "count": 0,
        "seconds": 0.0,
        "coverage_seconds": 0.0,
    }
    assert stats["by_team"] == [
        {"team": "Alpha", "count": 1, "seconds": 1.125},
        {"team": "Zebra", "count": 1, "seconds": 2.25},
    ]
    assert stats["event_count"] == 2
    assert stats["total_seconds"] == 3.375


def test_secondary_aggregates_and_copied_events_are_ordered():
    events = [
        {
            "event_id": "late",
            "period": 2,
            "start_seconds": 8,
            "end_seconds": 10.4444,
            "team": "Zebra",
            "phase": "attacking",
            "action": "z-pass",
        },
        {
            "event_id": "early",
            "period": 1,
            "start_seconds": 2,
            "end_seconds": 3,
            "team": "Alpha",
            "phase": "defending",
            "action": "a-tackle",
        },
    ]
    original = [dict(event) for event in events]

    stats = build_statistics(events)

    assert stats["by_team_phase"] == [
        {"team": "Alpha", "phase": "defending", "count": 1, "seconds": 1.0},
        {"team": "Zebra", "phase": "attacking", "count": 1, "seconds": 2.444},
    ]
    assert stats["by_action"] == [
        {"action": "a-tackle", "count": 1, "seconds": 1.0},
        {"action": "z-pass", "count": 1, "seconds": 2.444},
    ]
    assert stats["by_period"] == [
        {"period": 1, "count": 1, "seconds": 1.0},
        {"period": 2, "count": 1, "seconds": 2.444},
    ]
    assert [event["event_id"] for event in stats["events"]] == ["early", "late"]
    assert events == original
    stats["events"][0]["event_id"] = "changed"
    assert events == original


def test_goals_are_ordered_by_period_and_start_time():
    events = [
        {
            "event_id": "g-second-period",
            "period": 2,
            "start_seconds": 1.25,
            "end_seconds": 2,
            "team": "Beta",
            "phase": "goal_conceded",
            "action": "goal",
        },
        {
            "event_id": "g-later",
            "period": 1,
            "start_seconds": 90,
            "end_seconds": 91,
            "team": "Alpha",
            "phase": "goal",
            "action": "goal",
        },
        {
            "event_id": "g-earlier",
            "period": 1,
            "start_seconds": 10,
            "end_seconds": 11,
            "team": "Alpha",
            "phase": "goal",
            "action": "goal",
        },
    ]

    stats = build_statistics(events)

    assert stats["goals"] == [
        {
            "event_id": "g-earlier",
            "team": "Alpha",
            "period": 1,
            "start_seconds": 10,
            "kind": "goal",
            "match_clock": "",
        },
        {
            "event_id": "g-later",
            "team": "Alpha",
            "period": 1,
            "start_seconds": 90,
            "kind": "goal",
            "match_clock": "",
        },
        {
            "event_id": "g-second-period",
            "team": "Beta",
            "period": 2,
            "start_seconds": 1.25,
            "kind": "goal_conceded",
            "match_clock": "",
        },
    ]


def test_players_flags_playlists_and_custom_title():
    events = [
        {
            "event_id": "one",
            "period": 1,
            "start_seconds": 0,
            "end_seconds": 1,
            "team": "A",
            "phase": "attacking",
            "action": "pass",
            "players": ["Zoe", "Ari"],
            "flag": "good",
            "playlists": ["review", "attacking"],
        },
        {
            "event_id": "two",
            "period": 1,
            "start_seconds": 2,
            "end_seconds": 2.5,
            "team": "A",
            "phase": "defending",
            "action": "tackle",
            "players": ["Ari"],
            "flag": "bad",
            "playlists": ["review"],
        },
        {
            "event_id": "three",
            "period": 2,
            "start_seconds": 3,
            "end_seconds": 4,
            "team": "B",
            "phase": "goal",
            "action": "shot",
            "flag": "favourite",
        },
    ]

    stats = build_statistics(events, title="Review summary")

    assert stats["title"] == "Review summary"
    assert stats["players"] == [
        {
            "player": "Ari",
            "count": 2,
            "by_phase": {
                "defending": 1,
                "transition_to_attack": 0,
                "attacking": 1,
                "transition_to_defend": 0,
                "set_piece_attack": 0,
                "set_piece_defend": 0,
                "goal": 0,
                "goal_conceded": 0,
            },
            "by_flag": {"good": 1, "bad": 1, "favourite": 0},
        },
        {
            "player": "Zoe",
            "count": 1,
            "by_phase": {
                "defending": 0,
                "transition_to_attack": 0,
                "attacking": 1,
                "transition_to_defend": 0,
                "set_piece_attack": 0,
                "set_piece_defend": 0,
                "goal": 0,
                "goal_conceded": 0,
            },
            "by_flag": {"good": 1, "bad": 0, "favourite": 0},
        },
    ]
    assert stats["flags"] == {"good": 1, "bad": 1, "favourite": 1}
    assert stats["playlists"] == [
        {"playlist": "attacking", "count": 1},
        {"playlist": "review", "count": 2},
    ]


def test_invalid_event_collections_and_optional_values_raise_stats_error():

    from once_sport_mcp.stats_export import StatsError

    valid = {
        "event_id": "event-1",
        "period": 1,
        "start_seconds": 0,
        "end_seconds": 1,
        "team": "A",
        "phase": "attacking",
        "action": "pass",
    }
    invalid_cases = [
        None,
        [{key: value for key, value in valid.items() if key != "action"}],
        [{**valid, "flag": "excellent"}],
        [{**valid, "players": ["Ari", 7]}],
    ]
    for events in invalid_cases:
        with pytest.raises(StatsError):
            build_statistics(events)


def test_html_has_required_standalone_sections(tmp_path):
    from once_sport_mcp.stats_export import write_html

    stats = build_statistics(
        [
            {
                "event_id": "event-1",
                "period": 1,
                "start_seconds": 0,
                "end_seconds": 1,
                "team": "Alpha",
                "phase": "goal",
                "action": "shot",
                "players": ["Ari"],
                "flag": "good",
                "playlists": ["review"],
                "notes": "Header shot",
            }
        ],
        title="Match statistics",
    )
    path = tmp_path / "stats.html"

    write_html(stats, path)
    document = path.read_text(encoding="utf-8")
    lowered = document.lower()
    assert "<!doctype html>" in lowered
    assert "<style>" in lowered and "</style>" in lowered
    for heading in (
        "Phase statistics",
        "Team statistics",
        "Goals",
        "Players",
        "Flags",
        "Events",
    ):
        assert heading in document
    assert "<script" not in lowered
    assert "http://" not in lowered
    assert "https://" not in lowered


def test_xlsx_parts_are_well_formed_and_cells_preserve_values(tmp_path):
    import xml.etree.ElementTree as ET
    from zipfile import ZipFile

    from once_sport_mcp.stats_export import write_xlsx

    stats = build_statistics(
        [
            {
                "event_id": "goal-1",
                "period": 1,
                "start_seconds": 12.5,
                "end_seconds": 13.75,
                "team": "Alpha & Co",
                "phase": "goal",
                "action": "shot",
                "players": ["Ari", "Zoe"],
                "notes": "Saved <& clear",
                "flag": "good",
                "playlists": ["review", "goals"],
            }
        ],
        title="Cup final",
    )
    path = tmp_path / "stats.xlsx"

    write_xlsx(stats, path)

    with ZipFile(path) as archive:
        names = set(archive.namelist())
        required = {
            "[Content_Types].xml",
            "_rels/.rels",
            "xl/workbook.xml",
            "xl/_rels/workbook.xml.rels",
            "xl/worksheets/sheet1.xml",
            "xl/worksheets/sheet2.xml",
            "xl/worksheets/sheet3.xml",
            "xl/worksheets/sheet4.xml",
            "xl/styles.xml",
        }
        assert required <= names
        assert not any("sharedStrings" in name for name in names)
        for name in names:
            ET.fromstring(archive.read(name))
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        namespace = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        assert [
            sheet.attrib["name"]
            for sheet in workbook.findall("m:sheets/m:sheet", namespace)
        ] == [
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
        styles = ET.fromstring(archive.read("xl/styles.xml"))
        assert styles.find(".//m:b", namespace) is not None
        events_sheet = ET.fromstring(archive.read("xl/worksheets/sheet2.xml"))
        cells = {
            cell.attrib["r"]: cell for cell in events_sheet.findall(".//m:c", namespace)
        }

        def value(cell_ref):
            cell = cells[cell_ref]
            inline = cell.find("m:is", namespace)
            if inline is not None:
                return "".join(inline.itertext())
            return cell.findtext("m:v", namespaces=namespace)

        assert value("A2") == "goal-1"
        assert value("E2") == "Alpha & Co"
        assert value("H2") == "Ari; Zoe"
        assert value("I2") == "Saved <& clear"
        assert value("B2") == "1"
        assert cells["B2"].find("m:is", namespace) is None
        assert value("J2") == "good"
        assert value("K2") == "review; goals"
        goals_sheet = ET.fromstring(archive.read("xl/worksheets/sheet3.xml"))
        goal_cells = {
            cell.attrib["r"]: cell for cell in goals_sheet.findall(".//m:c", namespace)
        }
        assert "goal-1" in "".join(goal_cells["A2"].itertext())
        assert "goal" in "".join(goal_cells["E2"].itertext())


def _hostile_events():
    return [
        {
            "event_id": "e1",
            "period": 1,
            "start_seconds": 1,
            "end_seconds": 3,
            "team": "<b>A</b>",
            "phase": "goal",
            "action": "shot",
            "notes": "<script>alert(1)</script> & \x01bell",
            "players": ["P<1>"],
            "playlists": ["Set pieces"],
        }
    ]


def test_html_escapes_hostile_text_and_has_no_script_or_links(tmp_path):
    path = tmp_path / "s.html"
    write_html(build_statistics(_hostile_events()), path)
    text = path.read_text()
    assert "<script" not in text.lower()
    assert "http://" not in text and "https://" not in text
    assert "&lt;script&gt;" in text


def test_exports_are_deterministic(tmp_path):
    stats = build_statistics(_hostile_events())
    for writer, name in ((write_html, "a.html"), (write_xlsx, "a.xlsx")):
        first, second = tmp_path / ("1" + name), tmp_path / ("2" + name)
        writer(stats, first)
        writer(stats, second)
        assert first.read_bytes() == second.read_bytes()
