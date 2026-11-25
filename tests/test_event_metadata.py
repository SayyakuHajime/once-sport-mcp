import pytest

from once_sport_mcp.tools import validate_timeline

BASE_EVENT = {
    "event_id": "event-1",
    "period": 1,
    "start_seconds": 0,
    "end_seconds": 2,
    "team": "A",
    "phase": "attacking",
    "action": "Pass",
}


@pytest.mark.parametrize(
    "playlists",
    [
        [],
        ["final", "final"],
        "final",
        [f"list-{index}" for index in range(21)],
        [""],
        ["unsafe\nname"],
        ["x" * 61],
    ],
)
def test_invalid_playlists_have_the_contract_error(playlists):
    event = {**BASE_EVENT, "playlists": playlists}

    assert validate_timeline([event]) == {
        "valid": False,
        "errors": ["events[0].playlists: must be a list of unique nonempty names"],
    }


def test_valid_playlists_round_trip_with_flag():
    event = {
        **BASE_EVENT,
        "flag": "favourite",
        "playlists": ["final", "x" * 60],
    }

    assert validate_timeline([event]) == {"valid": True, "events": [event]}


@pytest.mark.parametrize("flag", [None, "Good", 1, "meh"])
def test_invalid_flags_have_the_contract_error(flag):
    event = {**BASE_EVENT, "flag": flag}

    assert validate_timeline([event]) == {
        "valid": False,
        "errors": ["events[0].flag: must be good, bad or favourite"],
    }


@pytest.mark.parametrize("flag", ["good", "bad", "favourite"])
def test_valid_flags_are_preserved(flag):
    event = {**BASE_EVENT, "flag": flag}

    assert validate_timeline([event]) == {"valid": True, "events": [event]}


def test_search_filters_match_flag_and_playlist_exactly_with_and_semantics():
    from once_sport_mcp.tools import search_events

    events = [
        {
            **BASE_EVENT,
            "flag": "good",
            "playlists": ["Final", "league"],
        },
        {
            **BASE_EVENT,
            "event_id": "event-2",
            "start_seconds": 3,
            "end_seconds": 5,
            "flag": "good",
            "playlists": ["final"],
        },
        {
            **BASE_EVENT,
            "event_id": "event-3",
            "start_seconds": 6,
            "end_seconds": 8,
            "flag": "bad",
            "playlists": ["Final"],
        },
        {
            **BASE_EVENT,
            "event_id": "event-4",
            "start_seconds": 9,
            "end_seconds": 11,
        },
    ]

    assert (
        search_events(events, team="a", flag="good", playlist="Final")["events"]
        == events[:1]
    )
    assert search_events(events, playlist="final")["events"] == [events[1]]
    assert search_events([BASE_EVENT]) == {
        "valid": True,
        "events": [BASE_EVENT],
        "count": 1,
    }
    assert search_events([BASE_EVENT], flag="good")["events"] == []


def test_unknown_fields_remain_rejected_and_legacy_events_are_unchanged():
    unknown = {**BASE_EVENT, "new_metadata": "not accepted"}

    assert validate_timeline([unknown]) == {
        "valid": False,
        "errors": ["events[0].new_metadata: unknown field"],
    }
    assert validate_timeline([BASE_EVENT]) == {"valid": True, "events": [BASE_EVENT]}
