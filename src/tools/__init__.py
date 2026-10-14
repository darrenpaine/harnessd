"""Tool registry (SPEC-0003)."""

from typing import Any

from tools import add_note, fetch_site, web_search
from tools.base import Tool

ALL_TOOLS: tuple[Tool[Any, Any], ...] = (
    web_search.TOOL,
    fetch_site.TOOL,
    add_note.TOOL,
)

TOOLS: dict[str, Tool[Any, Any]] = {tool.name: tool for tool in ALL_TOOLS}
