"""The tool contract and registry (SPEC-0003 B-0001..B-0003)."""

import asyncio
import dataclasses
from typing import Any, get_args

import pytest
from pydantic import BaseModel

from schemas import AddNoteInput, AddNoteOutput, ToolError, ToolName
from tools import ALL_TOOLS, TOOLS
from tools.base import Tool, ToolContext, call_tool


async def _boom(data: Any, ctx: ToolContext) -> Any:
    raise RuntimeError("secret detail")


async def _slow(data: Any, ctx: ToolContext) -> Any:
    await asyncio.sleep(1)


@pytest.mark.spec("SPEC-0003", "B-0001", "AT-0001")
@pytest.mark.parametrize("tool", ALL_TOOLS, ids=lambda t: t.name)
async def test_tool_errors_are_structured(
    tool: Tool[Any, Any], tool_context: ToolContext
) -> None:
    invalid = await call_tool(tool, {"unexpected": 1}, tool_context)
    assert isinstance(invalid, ToolError)
    assert invalid.code == "invalid_input"

    broken = dataclasses.replace(tool, run=_boom)
    valid_args = {
        "web_search": {"query": "q"},
        "fetch_site": {"url": "https://example.com"},
        "add_note": {"text": "n"},
    }[tool.name]
    failed = await call_tool(broken, valid_args, tool_context)
    assert isinstance(failed, ToolError)
    assert failed.code == "internal"
    assert "secret detail" not in failed.message


@pytest.mark.spec("SPEC-0003", "B-0002", "AT-0002")
async def test_tool_timeout(tool_context: ToolContext) -> None:
    slow = dataclasses.replace(TOOLS["add_note"], run=_slow, timeout_seconds=0.05)
    result = await call_tool(slow, {"text": "n"}, tool_context)
    assert isinstance(result, ToolError)
    assert result.code == "timeout"


@pytest.mark.spec("SPEC-0003", "B-0003", "AT-0003")
def test_registry_declares_metadata() -> None:
    assert len(TOOLS) == len(ALL_TOOLS), "tool names must be unique"
    assert set(TOOLS) == set(get_args(ToolName))
    for tool in ALL_TOOLS:
        assert tool.description
        assert tool.timeout_seconds > 0
        for model in (tool.input_model, tool.output_model):
            assert issubclass(model, BaseModel)
            assert model.model_config.get("extra") == "forbid"
        # Only read-only tools may be idempotent without confirmation.
        assert tool.idempotent != tool.side_effect


@pytest.mark.spec("SPEC-0003", "B-0003")
@pytest.mark.parametrize(("name", "timeout"), [("Add-Note", 1.0), ("add_note", 0.0)])
def test_tool_metadata_validated(name: str, timeout: float) -> None:
    with pytest.raises(ValueError):
        Tool(
            name=name,
            description="d",
            input_model=AddNoteInput,
            output_model=AddNoteOutput,
            side_effect=True,
            idempotent=False,
            timeout_seconds=timeout,
            run=_boom,
        )
