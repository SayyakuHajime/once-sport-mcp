"""Characterize pure request-local states without adding persistent machines."""

from copy import deepcopy

import pytest

from once_sport_mcp.tools import (
    PHASES,
    search_events,
    summarize_phases,
    validate_timeline,
)

EVENT = {
    "event_id": "1",
    "period": 1,
    "start_seconds": 0,
    "end_seconds": 0,
    "team": "A",
    "phase": "attacking",
    "action": "Pass",
    "players": ["Ana"],
}


def test_empty_timeline_is_valid_empty_evidence_not_an_error():
    assert validate_timeline([]) == {"valid": True, "events": []}
    assert search_events([]) == {"valid": True, "events": [], "count": 0}
    assert summarize_phases([]) == {
        "valid": True,
        "phases": {phase: {"count": 0, "events": []} for phase in PHASES},
    }


def test_duplicate_and_ordering_state_is_reset_on_each_request():
    first = dict(EVENT, start_seconds=10, end_seconds=11)
    assert validate_timeline([first, EVENT])["valid"] is False
    assert validate_timeline([EVENT, EVENT])["valid"] is False
    assert validate_timeline([EVENT]) == {"valid": True, "events": [EVENT]}
    assert validate_timeline([EVENT]) == {"valid": True, "events": [EVENT]}


@pytest.mark.parametrize(
    "filters,error",
    [
        ({"period": True}, "period must be 1, 2, 3, or 4"),
        ({"period": 0}, "period must be 1, 2, 3, or 4"),
        ({"phase": "unknown"}, "unknown phase"),
        ({"team": ""}, "team must be nonempty safe text"),
        ({"action": "bad\nvalue"}, "action must be nonempty safe text"),
        ({"player": 1}, "player must be nonempty safe text"),
    ],
)
def test_rejected_filters_are_distinct_from_valid_no_matches(filters, error):
    assert search_events([EVENT], **filters) == {"valid": False, "errors": [error]}
    assert search_events([EVENT], team="missing") == {
        "valid": True,
        "events": [],
        "count": 0,
    }


def test_timeline_rejection_precedes_filter_rejection():
    invalid = [dict(EVENT, phase="unknown")]
    assert search_events(invalid, period=0) == validate_timeline(invalid)


def test_queries_do_not_mutate_evidence_or_infer_extra_events():
    events = [deepcopy(EVENT), dict(EVENT, event_id="2", phase="goal")]
    original = deepcopy(events)
    assert validate_timeline(events)["valid"] is True
    assert search_events(events, player="aNA")["events"] == original
    summary = summarize_phases(events)
    assert sum(value["count"] for value in summary["phases"].values()) == len(events)
    assert summary["phases"]["attacking"]["events"] == events[:1]
    assert summary["phases"]["goal"]["events"] == events[1:]
    assert events == original
