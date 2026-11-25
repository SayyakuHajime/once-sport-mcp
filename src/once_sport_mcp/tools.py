import math

from .contracts import CsvResult, SearchResult, SummaryResult, TimelineResult

PHASES = (
    "defending",
    "transition_to_attack",
    "attacking",
    "transition_to_defend",
    "set_piece_attack",
    "set_piece_defend",
    "goal",
    "goal_conceded",
)
REQUIRED = {
    "event_id",
    "period",
    "start_seconds",
    "end_seconds",
    "team",
    "phase",
    "action",
}
ALLOWED = REQUIRED | {"players", "notes", "flag", "playlists", "clock_offset_seconds"}


def _text(value):
    return (
        isinstance(value, str)
        and bool(value.strip())
        and all(
            ord(c) >= 32
            and not 0x7F <= ord(c) <= 0x9F
            and not 0xD800 <= ord(c) <= 0xDFFF
            for c in value
        )
    )


def validate_timeline(events) -> TimelineResult:
    errors = []
    if not isinstance(events, list):
        return {"valid": False, "errors": ["events must be a list"]}
    ids = set()
    previous = -1
    for i, event in enumerate(events):
        prefix = f"events[{i}]"
        if not isinstance(event, dict):
            errors.append(f"{prefix}: must be an object")
            continue
        for key in sorted(REQUIRED - event.keys()):
            errors.append(f"{prefix}.{key}: required")
        for key in sorted(event.keys() - ALLOWED):
            errors.append(f"{prefix}.{key}: unknown field")
        for key in ("event_id", "team", "phase", "action"):
            if key in event and not _text(event[key]):
                errors.append(f"{prefix}.{key}: must be nonempty safe text")
        if "notes" in event and not _text(event["notes"]):
            errors.append(f"{prefix}.notes: must be nonempty safe text")
        if "players" in event and (
            not isinstance(event["players"], list)
            or not all(_text(x) for x in event["players"])
        ):
            errors.append(f"{prefix}.players: must be a list of nonempty safe strings")
        if "flag" in event and event["flag"] not in ("good", "bad", "favourite"):
            errors.append(f"{prefix}.flag: must be good, bad or favourite")
        if "playlists" in event:
            playlists = event["playlists"]
            if (
                not isinstance(playlists, list)
                or not playlists
                or len(playlists) > 20
                or not all(_text(name) and len(name) <= 60 for name in playlists)
                or len(set(playlists)) != len(playlists)
            ):
                errors.append(
                    f"{prefix}.playlists: must be a list of unique nonempty names"
                )
        eid = event.get("event_id")
        if _text(eid):
            if eid in ids:
                errors.append(f"{prefix}.event_id: duplicate ID")
            ids.add(eid)
        if event.get("phase") not in PHASES:
            errors.append(f"{prefix}.phase: unknown phase")
        if type(event.get("period")) is not int or event["period"] not in (1, 2, 3, 4):
            errors.append(f"{prefix}.period: must be 1, 2, 3, or 4")
        if "clock_offset_seconds" in event:
            offset = event["clock_offset_seconds"]
            if type(offset) not in (int, float) or not 0 <= offset <= 1e6:
                errors.append(
                    f"{prefix}.clock_offset_seconds: must be a number in [0, 1e6]"
                )
        numbers = []
        for key in ("start_seconds", "end_seconds"):
            value = event.get(key)
            if (
                type(value) not in (int, float)
                or value < 0
                or value > 1e12
                or not math.isfinite(value)
            ):
                errors.append(f"{prefix}.{key}: must be a finite non-negative number")
                numbers.append(None)
            else:
                numbers.append(value)
        start, end = numbers
        if start is not None:
            if start < previous:
                errors.append(
                    f"{prefix}.start_seconds: events must be in chronological order"
                )
            previous = start
        if start is not None and end is not None and end < start:
            errors.append(f"{prefix}.end_seconds: must not precede start_seconds")
    return (
        {"valid": False, "errors": errors}
        if errors
        else {"valid": True, "events": events}
    )


def generate_once_import_csv(events, filename, output_dir) -> CsvResult:
    import re

    from .csv_export import PublishedCsv, publish_csv

    result = validate_timeline(events)
    if not result["valid"]:
        return {"ok": False, "errors": result["errors"]}
    if not isinstance(filename, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}\.csv", filename
    ):
        return {"ok": False, "errors": ["filename must be a simple .csv name"]}
    publication = publish_csv(events, filename, output_dir)
    response: CsvResult
    if isinstance(publication, PublishedCsv):
        response = {
            "ok": True,
            "path": str(publication.path),
            "event_count": len(events),
            "import_verified": False,
        }
    else:
        response = {
            "ok": False,
            "errors": [
                "Unable to create CSV; check output directory permissions and choose a new filename"
            ],
        }
    if publication.warnings:
        response["warnings"] = list(publication.warnings)
    return response


def search_events(
    events,
    period=None,
    phase=None,
    team=None,
    action=None,
    player=None,
    *,
    flag=None,
    playlist=None,
) -> SearchResult:
    result = validate_timeline(events)
    if not result["valid"]:
        return result
    if period is not None and (type(period) is not int or period not in (1, 2, 3, 4)):
        return {"valid": False, "errors": ["period must be 1, 2, 3, or 4"]}
    if phase is not None and phase not in PHASES:
        return {"valid": False, "errors": ["unknown phase"]}
    for name, value in (("team", team), ("action", action), ("player", player)):
        if value is not None and not _text(value):
            return {"valid": False, "errors": [f"{name} must be nonempty safe text"]}
    if flag is not None and flag not in ("good", "bad", "favourite"):
        return {"valid": False, "errors": ["flag must be good, bad or favourite"]}
    if playlist is not None and not _text(playlist):
        return {"valid": False, "errors": ["playlist must be nonempty safe text"]}
    selected = [
        event
        for event in events
        if (period is None or event["period"] == period)
        and (phase is None or event["phase"] == phase)
        and (team is None or team.casefold() == event["team"].casefold())
        and (action is None or action.casefold() in event["action"].casefold())
        and (
            player is None
            or any(player.casefold() == p.casefold() for p in event.get("players", []))
        )
        and (flag is None or event.get("flag") == flag)
        and (playlist is None or playlist in event.get("playlists", []))
    ]
    return {"valid": True, "events": selected, "count": len(selected)}


def summarize_phases(events) -> SummaryResult:
    result = validate_timeline(events)
    if not result["valid"]:
        return result
    phases = {}
    for phase in PHASES:
        selected = [event for event in events if event["phase"] == phase]
        phases[phase] = {"count": len(selected), "events": selected}
    return {"valid": True, "phases": phases}
