"""Exercise transport errors and configuration changes at process boundaries."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EVENT = {
    "event_id": "one",
    "period": 1,
    "start_seconds": 0,
    "end_seconds": 2,
    "team": "A",
    "phase": "attacking",
    "action": "Pass",
}


def test_protocol_and_domain_errors_do_not_poison_the_next_request(tmp_path):
    async def run():
        params = StdioServerParameters(
            command="once-sport-mcp",
            env=dict(os.environ, ONCE_SPORT_OUTPUT_DIR=str(tmp_path)),
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            missing_arguments = await client.call_tool("validate_timeline", {})
            assert missing_arguments.isError is True
            unknown_tool = await client.call_tool("not_a_registered_tool", {})
            assert unknown_tool.isError is True
            rejected_timeline = await client.call_tool(
                "validate_timeline", {"events": [{}]}
            )
            assert rejected_timeline.isError is False
            assert json.loads(rejected_timeline.content[0].text)["valid"] is False
            rejected_export = await client.call_tool(
                "generate_once_import_csv",
                {"events": [EVENT], "filename": "../escape.csv"},
            )
            assert rejected_export.isError is False
            assert json.loads(rejected_export.content[0].text)["ok"] is False
            empty_search = await client.call_tool(
                "search_events", {"events": [EVENT], "team": "missing"}
            )
            assert empty_search.isError is False
            assert json.loads(empty_search.content[0].text) == {
                "valid": True,
                "events": [],
                "count": 0,
            }
            accepted = await client.call_tool("validate_timeline", {"events": [EVENT]})
            assert accepted.isError is False
            assert json.loads(accepted.content[0].text) == {
                "valid": True,
                "events": [EVENT],
            }
        assert list(tmp_path.iterdir()) == []

    asyncio.run(run())


def test_output_directory_is_pinned_before_later_cwd_and_environment_changes(tmp_path):
    initial = tmp_path / "initial"
    later = tmp_path / "later"
    initial.mkdir()
    later.mkdir()
    code = """
import json, os, sys
from once_sport_mcp import server
os.chdir(sys.argv[1])
os.environ['ONCE_SPORT_OUTPUT_DIR'] = sys.argv[2]
print(json.dumps(server.generate_once_import_csv([], 'empty.csv')))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(later), str(later / "ignored")],
        cwd=initial,
        env=dict(os.environ, ONCE_SPORT_OUTPUT_DIR="exports"),
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert Path(payload["path"]) == initial / "exports" / "empty.csv"
    assert not (later / "exports").exists()
    assert not (later / "ignored").exists()
