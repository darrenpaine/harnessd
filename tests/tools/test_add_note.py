"""add_note (SPEC-0003 B-0007)."""

import re

import pytest

from schemas import AddNoteOutput
from tools import TOOLS
from tools.base import ToolContext, call_tool

LINE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\t(.*)$")


@pytest.mark.spec("SPEC-0003", "B-0007", "AT-0007")
async def test_add_note_appends_formatted_line(tool_context: ToolContext) -> None:
    path = tool_context.settings.notes_path
    assert not path.parent.exists()

    first = await call_tool(TOOLS["add_note"], {"text": "  buy milk "}, tool_context)
    second = await call_tool(TOOLS["add_note"], {"text": "call Sam"}, tool_context)

    assert isinstance(first, AddNoteOutput) and first.written
    assert isinstance(second, AddNoteOutput)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines == [first.line, second.line]
    texts = [m.group(1) for line in lines if (m := LINE.match(line))]
    assert texts == ["buy milk", "call Sam"]


@pytest.mark.spec("SPEC-0003", "B-0007")
def test_add_note_requires_confirmation() -> None:
    tool = TOOLS["add_note"]
    assert tool.side_effect and not tool.idempotent
