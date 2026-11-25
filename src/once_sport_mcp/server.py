"""Local stdio MCP: legacy timeline tools plus an explicit opt-in video workspace."""

import logging
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import tools
from .workflow_config import load_workspace_config

# Resolve once at server startup, not from tool arguments.
OUTPUT_DIR = Path(os.environ.get("ONCE_SPORT_OUTPUT_DIR", ".data/output")).absolute()
mcp = FastMCP("Once Sport timeline bridge", log_level="ERROR")
READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
CREATE_FILE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)


@mcp.tool(annotations=READ_ONLY)
def validate_timeline(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate a human-reviewed timeline. Times are video offsets in seconds."""
    return tools.validate_timeline(events)


@mcp.tool(annotations=CREATE_FILE)
def generate_once_import_csv(
    events: list[dict[str, Any]], filename: str
) -> dict[str, Any]:
    """Create a new CSV in the configured output directory; app import is unverified."""
    return tools.generate_once_import_csv(events, filename, OUTPUT_DIR)


@mcp.tool(annotations=READ_ONLY)
def search_events(
    events: list[dict[str, Any]],
    period: int | None = None,
    phase: str | None = None,
    team: str | None = None,
    action: str | None = None,
    player: str | None = None,
) -> dict[str, Any]:
    """Filter validated source events; team/player match exactly, action is a substring."""
    return tools.search_events(events, period, phase, team, action, player)


@mcp.tool(annotations=READ_ONLY)
def summarize_phases(events: list[dict[str, Any]]) -> dict[str, Any]:
    """Count source events by phase, returning the underlying timestamped evidence."""
    return tools.summarize_phases(events)


WORKSPACE_CONFIG = load_workspace_config(os.environ)
if WORKSPACE_CONFIG is not None:
    from .workflow import Workflow
    from .workflow_mcp import register_workflow_tools

    workflow_options = {}
    if WORKSPACE_CONFIG.tracking_enabled:
        workflow_options["tracking_enabled"] = True
    if WORKSPACE_CONFIG.native_once_executable is None:
        workflow = Workflow(
            WORKSPACE_CONFIG.root,
            allowed_source_dirs=list(WORKSPACE_CONFIG.allowed_source_dirs),
            **workflow_options,
        )
    else:
        workflow = Workflow(
            WORKSPACE_CONFIG.root,
            allowed_source_dirs=list(WORKSPACE_CONFIG.allowed_source_dirs),
            native_once_executable=WORKSPACE_CONFIG.native_once_executable,
            **workflow_options,
        )
    register_workflow_tools(mcp, workflow)


def main():
    previous_log_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        mcp.run(transport="stdio")
    finally:
        logging.disable(previous_log_disable)
