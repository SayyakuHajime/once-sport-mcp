import asyncio
import json
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_stdio_discovery_and_calls(tmp_path):
    async def run():
        env = dict(os.environ, ONCE_SPORT_OUTPUT_DIR=str(tmp_path))
        params = StdioServerParameters(command="once-sport-mcp", env=env)
        event = {
            "event_id": "1",
            "period": 1,
            "start_seconds": 0,
            "end_seconds": 2,
            "team": "A",
            "phase": "goal",
            "action": "Shot",
        }
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as client,
        ):
            await client.initialize()
            tools = await client.list_tools()
            assert {x.name for x in tools.tools} == {
                "validate_timeline",
                "generate_once_import_csv",
                "search_events",
                "summarize_phases",
            }
            for name, args in [
                ("validate_timeline", {}),
                ("search_events", {}),
                ("summarize_phases", {}),
                ("generate_once_import_csv", {"filename": "smoke.csv"}),
            ]:
                response = await client.call_tool(name, {"events": [event], **args})
                assert not response.isError
                assert json.loads(response.content[0].text)
            assert (tmp_path / "smoke.csv").exists()

    asyncio.run(run())


def test_tool_discovery_describes_side_effects_for_agents(tmp_path):
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
            response = await client.list_tools()
            by_name = {tool.name: tool for tool in response.tools}
            for name, tool in by_name.items():
                assert tool.annotations is not None, (
                    f"{name} needs explicit effect hints"
                )
                read_only = name != "generate_once_import_csv"
                assert tool.annotations.readOnlyHint is read_only
                assert tool.annotations.destructiveHint is False
                assert tool.annotations.idempotentHint is read_only
                assert tool.annotations.openWorldHint is False

    asyncio.run(run())
