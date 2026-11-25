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
_ALLOWED = _REQUIRED | {"players", "notes", "flag", "playlists"}
_FLAGS = ("good", "bad", "favourite")


class StatsError(ValueError):
    """Invalid statistics input."""


def _validated_events(events):
    if not isinstance(events, list):
        raise StatsError("events must be a list")
    copied = []
    seen_ids = set()
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


def build_statistics(events, *, title="Analysis statistics"):
    copied = _validated_events(events)
    ordered = sorted(
        copied,
        key=lambda event: (event["period"], event["start_seconds"], event["event_id"]),
    )
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
        }
        for event in ordered
        if event["phase"] in ("goal", "goal_conceded")
    ]
    player_counts = {}
    playlist_counts = {}
    flags = {flag: 0 for flag in _FLAGS}
    for event in ordered:
        if "flag" in event:
            flags[event["flag"]] += 1
        for player in set(event.get("players", [])):
            player_counts[player] = player_counts.get(player, 0) + 1
        for playlist in set(event.get("playlists", [])):
            playlist_counts[playlist] = playlist_counts.get(playlist, 0) + 1
    return {
        "title": title,
        "event_count": len(ordered),
        "total_seconds": round(sum(duration for _, duration in timed), 3),
        "by_phase": [
            {
                "phase": phase,
                "count": phase_totals[phase][0],
                "seconds": round(phase_totals[phase][1], 3),
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
            {"player": player, "count": player_counts[player]}
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


def write_html(stats, path) -> None:
    phase_rows = [
        (row["phase"], row["count"], row["seconds"]) for row in stats["by_phase"]
    ]
    team_rows = [
        (row["team"], row["count"], row["seconds"]) for row in stats["by_team"]
    ]
    goal_rows = [
        (row["event_id"], row["team"], row["period"], row["start_seconds"], row["kind"])
        for row in stats["goals"]
    ]
    player_rows = [(row["player"], row["count"]) for row in stats["players"]]
    flag_rows = [(flag, stats["flags"][flag]) for flag in _FLAGS]
    event_headers = (
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
    )
    event_rows = [
        (
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
        "</style></head><body><main>"
        f"<h1>{title}</h1>"
        f"<section><h2>Phase statistics</h2>{_html_table(('Phase', 'Count', 'Seconds'), phase_rows)}</section>"
        f"<section><h2>Team statistics</h2>{_html_table(('Team', 'Count', 'Seconds'), team_rows)}</section>"
        f"<section><h2>Goals</h2>{_html_table(('Event ID', 'Team', 'Period', 'Start Seconds', 'Kind'), goal_rows)}</section>"
        f"<section><h2>Players</h2>{_html_table(('Player', 'Count'), player_rows)}</section>"
        f"<section><h2>Flags</h2>{_html_table(('Flag', 'Count'), flag_rows)}</section>"
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
        character
        for character in str(value)
        if ord(character) >= 32
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
        ["Analysis", stats["title"], "", ""],
        ["Type", "Name", "Count", "Seconds"],
    ]
    summary_rows.extend(
        ["Phase", row["phase"], row["count"], row["seconds"]]
        for row in stats["by_phase"]
    )
    summary_rows.extend(
        ["Team", row["team"], row["count"], row["seconds"]] for row in stats["by_team"]
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
        ]
        for event in stats["events"]
    )
    goal_rows = [["Event ID", "Team", "Period", "Start Seconds", "Kind"]]
    goal_rows.extend(
        [row["event_id"], row["team"], row["period"], row["start_seconds"], row["kind"]]
        for row in stats["goals"]
    )
    player_rows = [["Player", "Count"]]
    player_rows.extend([row["player"], row["count"]] for row in stats["players"])
    content_types = (
        f'{_XML_DECLARATION}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        + "".join(
            f'<Override PartName="/xl/worksheets/sheet{number}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            for number in range(1, 5)
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
        '<sheet name="Summary" sheetId="1" r:id="rId1"/>'
        '<sheet name="Events" sheetId="2" r:id="rId2"/>'
        '<sheet name="Goals" sheetId="3" r:id="rId3"/>'
        '<sheet name="Players" sheetId="4" r:id="rId4"/>'
        "</sheets></workbook>"
    )
    workbook_relationships = (
        f'{_XML_DECLARATION}<Relationships xmlns="{_PKG_REL_NS}">'
        + "".join(
            f'<Relationship Id="rId{number}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{number}.xml"/>'
            for number in range(1, 5)
        )
        + '<Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        + "</Relationships>"
    )
    styles = (
        f'{_XML_DECLARATION}<styleSheet xmlns="{_MAIN_NS}">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
        '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
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
        ("xl/worksheets/sheet1.xml", _xlsx_sheet(summary_rows, header_row=1)),
        ("xl/worksheets/sheet2.xml", _xlsx_sheet(event_rows)),
        ("xl/worksheets/sheet3.xml", _xlsx_sheet(goal_rows)),
        ("xl/worksheets/sheet4.xml", _xlsx_sheet(player_rows)),
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
