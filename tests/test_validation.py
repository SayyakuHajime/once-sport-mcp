from once_sport_mcp.tools import validate_timeline


def test_valid_timeline_is_returned():
    event = {
        "event_id": "e1",
        "period": 1,
        "start_seconds": 0,
        "end_seconds": 2,
        "team": "A",
        "phase": "goal",
        "action": "Shot",
    }
    assert validate_timeline([event]) == {"valid": True, "events": [event]}


def test_invalid_timeline_reports_errors():
    event = {
        "event_id": "x",
        "period": 1,
        "start_seconds": -1,
        "end_seconds": 0,
        "team": "A",
        "phase": "bad",
        "action": "X",
    }
    result = validate_timeline([event, event])
    assert result["valid"] is False
    assert any("start_seconds" in x for x in result["errors"])
    assert any("phase" in x for x in result["errors"])
    assert any("duplicate" in x for x in result["errors"])


def test_invalid_value_types_are_rejected_without_crashing():
    event = {
        "event_id": "x",
        "period": 1,
        "start_seconds": 10**1000,
        "end_seconds": 2,
        "team": "A",
        "phase": [],
        "action": "X",
    }
    assert not validate_timeline([event])["valid"]


def test_boundaries_and_invalid_fields():
    from copy import deepcopy

    base = {
        "event_id": "id",
        "period": 1,
        "start_seconds": 0,
        "end_seconds": 0,
        "team": "A",
        "phase": "attacking",
        "action": "Pass",
    }
    assert validate_timeline([base])["valid"]
    variants = [
        {"start_seconds": True},
        {"start_seconds": float("nan")},
        {"start_seconds": float("inf")},
        {"start_seconds": 2, "end_seconds": 1},
        {"period": True},
        {"period": 5},
        {"event_id": ""},
        {"action": "bad\nlabel"},
        {"team": "\ud800"},
        {"players": ["A", 1]},
        {"notes": None},
        {"unknown": 1},
    ]
    for change in variants:
        event = deepcopy(base)
        event.update(change)
        assert not validate_timeline([event])["valid"], change
    later = dict(base, event_id="other", start_seconds=5, end_seconds=6)
    assert not validate_timeline([later, base])["valid"]
    assert not validate_timeline([None])["valid"]
    assert not validate_timeline(None)["valid"]
