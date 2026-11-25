"""Golden protocol contract captured before the state-machine hardening."""

import asyncio
import hashlib
import json
import os
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EVENT = {
    "event_id": "one",
    "period": 1,
    "start_seconds": 0,
    "end_seconds": 2.5,
    "team": 'Équipe, "A"',
    "phase": "goal",
    "action": "シュート",
    "players": ["Ana"],
}


def test_legacy_schemas_results_and_csv_bytes_are_unchanged(tmp_path):
    baseline = json.loads(
        Path(__file__)
        .with_name("fixtures")
        .joinpath("mcp_contract_v1.json")
        .read_text()
    )

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
            listed = await client.list_tools()
            schemas = [
                {
                    "name": t.name,
                    "inputSchema": t.inputSchema,
                    "outputSchema": t.outputSchema,
                }
                for t in listed.tools
            ]
            assert schemas == baseline["schemas"]
            calls = [
                ("validate_timeline", {"events": [EVENT]}),
                ("validate_timeline", {"events": [{}]}),
                ("search_events", {"events": [EVENT], "team": "missing"}),
                ("search_events", {"events": [EVENT], "period": 5}),
                ("summarize_phases", {"events": [EVENT]}),
                (
                    "generate_once_import_csv",
                    {"events": [EVENT], "filename": "contract.csv"},
                ),
                (
                    "generate_once_import_csv",
                    {"events": [EVENT], "filename": "contract.csv"},
                ),
                (
                    "generate_once_import_csv",
                    {"events": [EVENT], "filename": "../escape.csv"},
                ),
            ]
            actual = []
            for name, args in calls:
                response = await client.call_tool(name, args)
                payload = json.loads(response.content[0].text)
                if "path" in payload:
                    payload["path"] = "<OUTPUT>/" + Path(payload["path"]).name
                actual.append(
                    {"tool": name, "isError": response.isError, "payload": payload}
                )
            assert actual == baseline["calls"]
            digest = hashlib.sha256(
                (tmp_path / "contract.csv").read_bytes()
            ).hexdigest()
            assert digest == baseline["csv_sha256"]

    asyncio.run(run())
