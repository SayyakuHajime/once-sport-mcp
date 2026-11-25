from once_sport_mcp.tools import search_events, summarize_phases


def test_search_filters_and_summary_has_evidence():
    events = [
        {
            "event_id": "1",
            "period": 1,
            "start_seconds": 1,
            "end_seconds": 3,
            "team": "A",
            "phase": "goal",
            "action": "Shot",
            "players": ["Ana"],
        },
        {
            "event_id": "2",
            "period": 2,
            "start_seconds": 5,
            "end_seconds": 7,
            "team": "B",
            "phase": "defending",
            "action": "Block",
        },
    ]
    assert search_events(events, team="a", player="ana")["events"] == events[:1]
    summary = summarize_phases(events)
    assert summary["phases"]["goal"]["count"] == 1
    assert summary["phases"]["goal"]["events"] == events[:1]
    assert summary["phases"]["attacking"]["count"] == 0
    assert summary == summarize_phases(events)
