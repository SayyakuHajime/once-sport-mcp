import html
import math
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from .tools import PHASES

_REQUIRED = {
    "event_id",
    "period",
    "start_seconds",
    "end_seconds",
    "team",
    "phase",
    "action",
}
_ALLOWED = _REQUIRED | {
    "players",
    "notes",
    "flag",
    "playlists",
    "clock_offset_seconds",
}
_FLAGS = ("good", "bad", "favourite")


class StatsError(ValueError):
    """Invalid statistics input."""


def _validated_events(events):
    if not isinstance(events, list):
        raise StatsError("events must be a list")
    copied = []
    seen_ids = set()
    period_offsets = {}
    for index, event in enumerate(events):
        prefix = f"events[{index}]"
        if not isinstance(event, dict):
            raise StatsError(f"{prefix} must be an object")
        missing = sorted(_REQUIRED - event.keys())
        if missing:
            raise StatsError(f"{prefix} missing required key: {missing[0]}")
        unknown = sorted(event.keys() - _ALLOWED)
        if unknown:
            raise StatsError(f"{prefix} has unknown key: {unknown[0]}")
        for key in ("event_id", "team", "phase", "action"):
            if not isinstance(event[key], str) or not event[key].strip():
                raise StatsError(f"{prefix}.{key} must be nonempty text")
        if event["event_id"] in seen_ids:
            raise StatsError(f"{prefix}.event_id must be unique")
        seen_ids.add(event["event_id"])
        if event["phase"] not in PHASES:
            raise StatsError(f"{prefix}.phase is unknown")
        if type(event["period"]) is not int or event["period"] not in (1, 2, 3, 4):
            raise StatsError(f"{prefix}.period must be 1, 2, 3, or 4")
        start = event["start_seconds"]
        end = event["end_seconds"]
        for key, value in (("start_seconds", start), ("end_seconds", end)):
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or value < 0
                or value > 1e12
            ):
                raise StatsError(f"{prefix}.{key} must be a finite non-negative number")
        if end < start:
            raise StatsError(f"{prefix}.end_seconds must not precede start_seconds")
        if "clock_offset_seconds" in event:
            offset = event["clock_offset_seconds"]
            if (
                type(offset) not in (int, float)
                or not math.isfinite(offset)
                or offset < 0
                or offset > 1e12
            ):
                raise StatsError(
                    f"{prefix}.clock_offset_seconds must be a finite non-negative number"
                )
            if (
                event["period"] in period_offsets
                and period_offsets[event["period"]] != offset
            ):
                raise StatsError(
                    f"{prefix}.clock_offset_seconds must be consistent within a period"
                )
            period_offsets[event["period"]] = offset
        if "players" in event and (
            not isinstance(event["players"], list)
            or not all(isinstance(player, str) for player in event["players"])
        ):
            raise StatsError(f"{prefix}.players must be a list of strings")
        if "notes" in event and not isinstance(event["notes"], str):
            raise StatsError(f"{prefix}.notes must be a string")
        if "flag" in event and event["flag"] not in _FLAGS:
            raise StatsError(f"{prefix}.flag must be good, bad or favourite")
        if "playlists" in event and (
            not isinstance(event["playlists"], list)
            or not all(isinstance(playlist, str) for playlist in event["playlists"])
        ):
            raise StatsError(f"{prefix}.playlists must be a list of strings")
        copied_event = dict(event)
        for key in ("players", "playlists"):
            if key in copied_event:
                copied_event[key] = list(copied_event[key])
        copied.append(copied_event)
    return copied


def _aggregate(values, key_name):
    counts = {}
    seconds = {}
    for event, duration in values:
        key = event[key_name]
        counts[key] = counts.get(key, 0) + 1
        seconds[key] = seconds.get(key, 0.0) + duration
    return [
        {
            key_name: key,
            "count": counts[key],
            "seconds": round(seconds[key], 3),
        }
        for key in sorted(counts)
    ]


def _interval_union_seconds(intervals):
    ordered = sorted((start, end) for start, end in intervals if end > start)
    if not ordered:
        return 0.0
    total = 0.0
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start > current_end:
            total += current_end - current_start
            current_start, current_end = start, end
        else:
            current_end = max(current_end, end)
    return total + current_end - current_start


def _coverage_seconds(events):
    by_period = {}
    for event in events:
        by_period.setdefault(event["period"], []).append(
            (event["start_seconds"], event["end_seconds"])
        )
    return round(
        sum(_interval_union_seconds(intervals) for intervals in by_period.values()), 3
    )


def _format_match_clock(period, start_seconds, offsets):
    if period not in offsets:
        return ""
    total_seconds = math.floor(offsets[period] + start_seconds)
    return f"{total_seconds // 60:02d}:{total_seconds % 60:02d}"


def _scoreline(events, goals):
    teams = sorted({event["team"] for event in events})
    scores = dict.fromkeys(teams, 0)
    for goal in goals:
        if goal["kind"] == "goal":
            scores[goal["team"]] += 1
    home = next((team for team in teams if team.casefold() == "home"), None)
    away = next((team for team in teams if team.casefold() == "away"), None)
    if home is not None and away is not None:
        return f"Home {scores[home]} - {scores[away]} Away"
    return ", ".join(f"{team}: {scores[team]}" for team in teams) or "No goals"


def build_statistics(events, *, title="Analysis statistics"):
    """Aggregate events; clock_offset_seconds sets a consistent per-period clock offset."""
    copied = _validated_events(events)
    ordered = sorted(
        copied,
        key=lambda event: (event["period"], event["start_seconds"], event["event_id"]),
    )
    period_offsets = {
        event["period"]: event["clock_offset_seconds"]
        for event in ordered
        if "clock_offset_seconds" in event
    }
    timed = [
        (event, round(event["end_seconds"] - event["start_seconds"], 3))
        for event in ordered
    ]
    phase_totals = {phase: [0, 0.0] for phase in PHASES}
    team_phase_totals = {}
    for event, duration in timed:
        total = phase_totals[event["phase"]]
        total[0] += 1
        total[1] += duration
        pair = (event["team"], event["phase"])
        pair_total = team_phase_totals.setdefault(pair, [0, 0.0])
        pair_total[0] += 1
        pair_total[1] += duration
    goals = [
        {
            "event_id": event["event_id"],
            "team": event["team"],
            "period": event["period"],
            "start_seconds": event["start_seconds"],
            "kind": event["phase"],
            "match_clock": _format_match_clock(
                event["period"], event["start_seconds"], period_offsets
            ),
        }
        for event in ordered
        if event["phase"] in ("goal", "goal_conceded")
    ]
    player_counts = {}
    playlist_counts = {}
    player_details = {}
    flags = {flag: 0 for flag in _FLAGS}
    for event in ordered:
        if "flag" in event:
            flags[event["flag"]] += 1
        for player in set(event.get("players", [])):
            player_counts[player] = player_counts.get(player, 0) + 1
            details = player_details.setdefault(
                player,
                {
                    "by_phase": dict.fromkeys(PHASES, 0),
                    "by_flag": dict.fromkeys(_FLAGS, 0),
                },
            )
            details["by_phase"][event["phase"]] += 1
            if "flag" in event:
                details["by_flag"][event["flag"]] += 1
        for playlist in set(event.get("playlists", [])):
            playlist_counts[playlist] = playlist_counts.get(playlist, 0) + 1
    summed_clip_seconds = round(sum(duration for _, duration in timed), 3)
    return {
        "title": title,
        "event_count": len(ordered),
        "total_seconds": summed_clip_seconds,
        "summed_clip_seconds": summed_clip_seconds,
        "coverage_seconds": _coverage_seconds(ordered),
        "period_clock_offsets": dict(sorted(period_offsets.items())),
        "scoreline": _scoreline(ordered, goals),
        "by_phase": [
            {
                "phase": phase,
                "count": phase_totals[phase][0],
                "seconds": round(phase_totals[phase][1], 3),
                "coverage_seconds": _coverage_seconds(
                    [event for event in ordered if event["phase"] == phase]
                ),
            }
            for phase in PHASES
        ],
        "by_team": _aggregate(timed, "team"),
        "by_team_phase": [
            {
                "team": team,
                "phase": phase,
                "count": values[0],
                "seconds": round(values[1], 3),
            }
            for (team, phase), values in sorted(
                team_phase_totals.items(),
                key=lambda item: (item[0][0], PHASES.index(item[0][1])),
            )
        ],
        "by_action": _aggregate(timed, "action"),
        "by_period": _aggregate(timed, "period"),
        "goals": goals,
        "players": [
            {
                "player": player,
                "count": player_counts[player],
                "by_phase": player_details[player]["by_phase"],
                "by_flag": player_details[player]["by_flag"],
            }
            for player in sorted(player_counts)
        ],
        "flags": flags,
        "playlists": [
            {"playlist": playlist, "count": playlist_counts[playlist]}
            for playlist in sorted(playlist_counts)
        ],
        "events": ordered,
    }


def _html_text(value):
    return html.escape(str(value), quote=True)


def _html_table(headers, rows):
    header_cells = "".join(
        f'<th scope="col">{_html_text(header)}</th>' for header in headers
    )
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{_html_text(value)}</td>" for value in row)
        body_rows.append(f"<tr>{cells}</tr>")
    return (
        '<div class="table-wrap"><table><thead><tr>'
        + header_cells
        + "</tr></thead><tbody>"
        + "".join(body_rows)
        + "</tbody></table></div>"
    )


def _phase_chart(stats):
    width = 760
    row_height = 26
    plot_x = 210
    plot_width = 500
    height = 32 + row_height * len(stats["by_phase"])
    maximum = max((row["count"] for row in stats["by_phase"]), default=0)
    bars = []
    for index, row in enumerate(stats["by_phase"]):
        y = 24 + index * row_height
        bar_width = row["count"] / maximum * plot_width if maximum else 0
        bars.append(
            f'<text x="8" y="{y + 5}">{_html_text(row["phase"])}</text>'
            f'<rect x="{plot_x}" y="{y - 9}" width="{bar_width:.2f}" '
            'height="16" rx="3" fill="#2878a5"/>'
            f'<text x="{plot_x + bar_width + 6:.2f}" y="{y + 5}">{row["count"]}</text>'
        )
    return (
        f'<svg role="img" aria-label="Events per phase" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}"><title>Events per phase</title>'
        f"{''.join(bars)}</svg>"
    )


def _timeline_chart(stats):
    events = stats["events"]
    periods = sorted({event["period"] for event in events})
    height = max(60, 36 + 34 * len(periods))
    width = 760
    plot_x = 112
    plot_width = 630
    palette = {
        "defending": "#d97706",
        "transition_to_attack": "#7c3aed",
        "attacking": "#16804a",
        "transition_to_defend": "#6d28d9",
        "set_piece_attack": "#0891b2",
        "set_piece_defend": "#0e7490",
        "goal": "#dc2626",
        "goal_conceded": "#be123c",
    }
    lanes = []
    for lane_index, period in enumerate(periods):
        lane_events = [event for event in events if event["period"] == period]
        minimum = min(event["start_seconds"] for event in lane_events)
        maximum = max(event["end_seconds"] for event in lane_events)
        span = max(maximum - minimum, 1.0)
        y = 26 + lane_index * 34
        lanes.append(f'<text x="8" y="{y + 5}">Period {period}</text>')
        lanes.append(
            f'<line x1="{plot_x}" y1="{y}" x2="{plot_x + plot_width}" '
            f'y2="{y}" stroke="#ccd5df" stroke-width="2"/>'
        )
        for event in lane_events:
            x = plot_x + (event["start_seconds"] - minimum) / span * plot_width
            bar_width = max(
                3,
                (event["end_seconds"] - event["start_seconds"]) / span * plot_width,
            )
            label = _html_text(
                f"{event['event_id']} — {event['phase']} "
                f"({event['start_seconds']}–{event['end_seconds']} seconds)"
            )
            fill = palette[event["phase"]]
            lanes.append(
                f'<rect x="{x:.2f}" y="{y - 7}" width="{bar_width:.2f}" '
                f'height="14" rx="3" fill="{fill}"><title>{label}</title></rect>'
            )
    if not periods:
        lanes.append('<text x="8" y="34">No events</text>')
    return (
        f'<svg role="img" aria-label="Event timeline" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}"><title>Event timeline</title>'
        f"{''.join(lanes)}</svg>"
    )


def write_html(stats, path) -> None:
    summary_rows = [
        ("Events", stats["event_count"]),
        ("Summed clip seconds", stats["summed_clip_seconds"]),
        ("Interval-union coverage seconds", stats["coverage_seconds"]),
        ("Scoreline", stats["scoreline"]),
    ]
    phase_rows = [
        (row["phase"], row["count"], row["seconds"], row["coverage_seconds"])
        for row in stats["by_phase"]
    ]
    team_rows = [
        (row["team"], row["count"], row["seconds"]) for row in stats["by_team"]
    ]
    team_phase_rows = [
        (row["team"], row["phase"], row["count"], row["seconds"])
        for row in stats["by_team_phase"]
    ]
    action_rows = [
        (row["action"], row["count"], row["seconds"]) for row in stats["by_action"]
    ]
    period_rows = [
        (row["period"], row["count"], row["seconds"]) for row in stats["by_period"]
    ]
    goal_rows = [
        (
            row["event_id"],
            row["team"],
            row["period"],
            row["start_seconds"],
            row["match_clock"],
            row["kind"],
        )
        for row in stats["goals"]
    ]
    player_headers = ["Player", "Mentions", *PHASES, *_FLAGS]
    player_rows = [
        (
            row["player"],
            row["count"],
            *(row["by_phase"][phase] for phase in PHASES),
            *(row["by_flag"][flag] for flag in _FLAGS),
        )
        for row in stats["players"]
    ]
    flag_rows = [(flag, stats["flags"][flag]) for flag in _FLAGS]
    playlist_rows = [(row["playlist"], row["count"]) for row in stats["playlists"]]
    event_headers = (
        "Event ID",
        "Period",
        "Start Seconds",
        "Match Clock",
        "End Seconds",
        "Team",
        "Phase",
        "Action",
        "Players",
        "Notes",
        "Flag",
        "Playlists",
    )
    event_rows = [
        (
            event["event_id"],
            event["period"],
            event["start_seconds"],
            _format_match_clock(
                event["period"], event["start_seconds"], stats["period_clock_offsets"]
            ),
            event["end_seconds"],
            event["team"],
            event["phase"],
            event["action"],
            "; ".join(event.get("players", [])),
            event.get("notes", ""),
            event.get("flag", ""),
            "; ".join(event.get("playlists", [])),
        )
        for event in stats["events"]
    ]
    title = _html_text(stats["title"])
    document = (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{title}</title><style>"
        "body{font-family:system-ui,sans-serif;line-height:1.5;margin:2rem;color:#172033}"
        "main{max-width:1100px;margin:auto}h1{color:#123b5d}section{margin:2rem 0}"
        ".table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%}"
        "th,td{border:1px solid #ccd5df;padding:.45rem .65rem;text-align:left}"
        "th{background:#eaf1f7}tbody tr:nth-child(even){background:#f7f9fb}"
        "svg{max-width:100%;height:auto;font:12px system-ui,sans-serif}"
        "</style></head><body><main>"
        f"<h1>{title}</h1>"
        f"<section><h2>Summary</h2>{_html_table(('Metric', 'Value'), summary_rows)}</section>"
        f"<section><h2>Events per phase</h2>{_phase_chart(stats)}</section>"
        f"<section><h2>Event timeline</h2>{_timeline_chart(stats)}</section>"
        f"<section><h2>Phase statistics</h2>{_html_table(('Phase', 'Count', 'Clip Seconds', 'Coverage Seconds'), phase_rows)}</section>"
        f"<section><h2>Team statistics</h2>{_html_table(('Team', 'Count', 'Clip Seconds'), team_rows)}</section>"
        f"<section><h2>By team/phase</h2>{_html_table(('Team', 'Phase', 'Count', 'Clip Seconds'), team_phase_rows)}</section>"
        f"<section><h2>By action</h2>{_html_table(('Action', 'Count', 'Clip Seconds'), action_rows)}</section>"
        f"<section><h2>By period</h2>{_html_table(('Period', 'Count', 'Clip Seconds'), period_rows)}</section>"
        f"<section><h2>Goals</h2>{_html_table(('Event ID', 'Team', 'Period', 'Start Seconds', 'Match Clock', 'Kind'), goal_rows)}</section>"
        f"<section><h2>Players</h2>{_html_table(player_headers, player_rows)}</section>"
        f"<section><h2>Flags</h2>{_html_table(('Flag', 'Count'), flag_rows)}</section>"
        f"<section><h2>Playlists</h2>{_html_table(('Playlist', 'Count'), playlist_rows)}</section>"
        f"<section><h2>Events</h2>{_html_table(event_headers, event_rows)}</section>"
        "</main></body></html>\n"
    )
    Path(path).write_text(document, encoding="utf-8")


_XML_DECLARATION = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _clean_xml_text(value):
    return "".join(
        " " if character in "\t\n\r" else character
        for character in str(value)
        if (ord(character) >= 32 or character in "\t\n\r")
        and not 0x7F <= ord(character) <= 0x9F
        and not 0xD800 <= ord(character) <= 0xDFFF
        and ord(character) not in (0xFFFE, 0xFFFF)
    )


def _column_name(number):
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _xlsx_cell(reference, value, *, header=False):
    style = ' s="1"' if header else ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise StatsError("XLSX numeric cells must be finite")
        return f'<c r="{reference}"{style}><v>{value}</v></c>'
    text = escape(_clean_xml_text(value))
    return (
        f'<c r="{reference}" t="inlineStr"{style}>'
        f'<is><t xml:space="preserve">{text}</t></is></c>'
    )


def _xlsx_sheet(rows, *, header_row=0):
    xml_rows = []
    for row_number, row in enumerate(rows, start=1):
        cells = "".join(
            _xlsx_cell(
                f"{_column_name(column_number)}{row_number}",
                value,
                header=row_number - 1 == header_row,
            )
            for column_number, value in enumerate(row, start=1)
        )
        xml_rows.append(f'<row r="{row_number}">{cells}</row>')
    return (
        f'{_XML_DECLARATION}<worksheet xmlns="{_MAIN_NS}">'
        f"<sheetData>{''.join(xml_rows)}</sheetData></worksheet>"
    )


def _xlsx_parts(stats):
    summary_rows = [
        ["Analysis", stats["title"], "", "", ""],
        ["Type", "Name", "Count", "Clip Seconds", "Coverage Seconds"],
        ["Summary", "Event count", stats["event_count"], "", ""],
        ["Summary", "Summed clip seconds", stats["summed_clip_seconds"], "", ""],
        [
            "Summary",
            "Interval-union coverage seconds",
            stats["coverage_seconds"],
            "",
            "",
        ],
        ["Summary", "Scoreline", stats["scoreline"], "", ""],
    ]
    summary_rows.extend(
        ["Phase", row["phase"], row["count"], row["seconds"], row["coverage_seconds"]]
        for row in stats["by_phase"]
    )
    summary_rows.extend(
        ["Team", row["team"], row["count"], row["seconds"], ""]
        for row in stats["by_team"]
    )
    event_headers = [
        "Event ID",
        "Period",
        "Start Seconds",
        "End Seconds",
        "Team",
        "Phase",
        "Action",
        "Players",
        "Notes",
        "Flag",
        "Playlists",
        "Match Clock",
    ]
    event_rows = [event_headers]
    event_rows.extend(
        [
            event["event_id"],
            event["period"],
            event["start_seconds"],
            event["end_seconds"],
            event["team"],
            event["phase"],
            event["action"],
            "; ".join(event.get("players", [])),
            event.get("notes", ""),
            event.get("flag", ""),
            "; ".join(event.get("playlists", [])),
            _format_match_clock(
                event["period"], event["start_seconds"], stats["period_clock_offsets"]
            ),
        ]
        for event in stats["events"]
    )
    goal_rows = [["Event ID", "Team", "Period", "Start Seconds", "Kind", "Match Clock"]]
    goal_rows.extend(
        [
            row["event_id"],
            row["team"],
            row["period"],
            row["start_seconds"],
            row["kind"],
            row["match_clock"],
        ]
        for row in stats["goals"]
    )
    player_headers = ["Player", "Count", *PHASES, *_FLAGS]
    player_rows = [player_headers]
    player_rows.extend(
        [
            row["player"],
            row["count"],
            *(row["by_phase"][phase] for phase in PHASES),
            *(row["by_flag"][flag] for flag in _FLAGS),
        ]
        for row in stats["players"]
    )
    team_rows = [["Team", "Count", "Clip Seconds"]]
    team_rows.extend(
        [row["team"], row["count"], row["seconds"]] for row in stats["by_team"]
    )
    team_phase_rows = [["Team", "Phase", "Count", "Clip Seconds"]]
    team_phase_rows.extend(
        [row["team"], row["phase"], row["count"], row["seconds"]]
        for row in stats["by_team_phase"]
    )
    action_rows = [["Action", "Count", "Clip Seconds"]]
    action_rows.extend(
        [row["action"], row["count"], row["seconds"]] for row in stats["by_action"]
    )
    period_rows = [["Period", "Count", "Clip Seconds"]]
    period_rows.extend(
        [row["period"], row["count"], row["seconds"]] for row in stats["by_period"]
    )
    flag_rows = [["Flag", "Count"]]
    flag_rows.extend([flag, stats["flags"][flag]] for flag in _FLAGS)
    playlist_rows = [["Playlist", "Count"]]
    playlist_rows.extend([row["playlist"], row["count"]] for row in stats["playlists"])
    sheets = [
        ("Summary", _xlsx_sheet(summary_rows, header_row=1)),
        ("Events", _xlsx_sheet(event_rows)),
        ("Goals", _xlsx_sheet(goal_rows)),
        ("Players", _xlsx_sheet(player_rows)),
        ("By Team", _xlsx_sheet(team_rows)),
        ("Team Phase", _xlsx_sheet(team_phase_rows)),
        ("By Action", _xlsx_sheet(action_rows)),
        ("By Period", _xlsx_sheet(period_rows)),
        ("Flags", _xlsx_sheet(flag_rows)),
        ("Playlists", _xlsx_sheet(playlist_rows)),
    ]
    content_types = (
        f'{_XML_DECLARATION}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        + "".join(
            f'<Override PartName="/xl/worksheets/sheet{number}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for number in range(1, len(sheets) + 1)
        )
        + "</Types>"
    )
    root_relationships = (
        f'{_XML_DECLARATION}<Relationships xmlns="{_PKG_REL_NS}">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook = (
        f'{_XML_DECLARATION}<workbook xmlns="{_MAIN_NS}" xmlns:r="{_REL_NS}"><sheets>'
        + "".join(
            f'<sheet name="{escape(name)}" sheetId="{number}" r:id="rId{number}"/>'
            for number, (name, _) in enumerate(sheets, start=1)
        )
        + "</sheets></workbook>"
    )
    styles_id = len(sheets) + 1
    workbook_relationships = (
        f'{_XML_DECLARATION}<Relationships xmlns="{_PKG_REL_NS}">'
        + "".join(
            f'<Relationship Id="rId{number}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{number}.xml"/>'
            for number in range(1, len(sheets) + 1)
        )
        + f'<Relationship Id="rId{styles_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        + "</Relationships>"
    )
    styles = (
        f'{_XML_DECLARATION}<styleSheet xmlns="{_MAIN_NS}">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    )
    return [
        ("[Content_Types].xml", content_types),
        ("_rels/.rels", root_relationships),
        ("xl/workbook.xml", workbook),
        ("xl/_rels/workbook.xml.rels", workbook_relationships),
        ("xl/styles.xml", styles),
        *[
            (f"xl/worksheets/sheet{number}.xml", contents)
            for number, (_, contents) in enumerate(sheets, start=1)
        ],
    ]


def write_xlsx(stats, path) -> None:
    with ZipFile(path, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name, contents in _xlsx_parts(stats):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            info.create_system = 0
            archive.writestr(
                info,
                contents.encode("utf-8"),
                compress_type=ZIP_DEFLATED,
                compresslevel=9,
            )
