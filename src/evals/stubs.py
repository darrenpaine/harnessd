"""Stubbed tools for eval cases (SPEC-0011 B-0002).

A stub keeps the real tool's name, schema and side-effect flag, so the model
sees the same tools and confirmations still apply; only the result is canned.
"""

import dataclasses
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from schemas import EvalCase, ToolError
from tools import TOOLS
from tools.base import Tool, ToolContext, ToolFailure


def case_tools(case: EvalCase) -> Mapping[str, Tool[Any, Any]]:
    """The tools a case runs with: the real ones for `live`, stubs otherwise."""
    if case.tools == "live":
        return TOOLS
    return {
        name: _stub(tool, getattr(case.tool_outputs, name))
        for name, tool in TOOLS.items()
    }


def _stub(tool: Tool[Any, Any], canned: BaseModel | None) -> Tool[Any, Any]:
    if canned is None:
        if tool.side_effect:
            return tool  # add_note: writes to the case's temporary notes file
        canned = ToolError(
            code="internal", message=f"the eval case has no output for {tool.name}"
        )
    result = canned

    async def run(data: BaseModel, ctx: ToolContext) -> BaseModel:
        if isinstance(result, ToolError):
            raise ToolFailure(result.code, result.message)
        return result

    return dataclasses.replace(tool, run=run)
