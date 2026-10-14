"""add_note: append a line to the notes file (SPEC-0003 B-0007)."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from schemas import AddNoteInput, AddNoteOutput
from tools.base import Tool, ToolContext


def _append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as notes:
        notes.write(line + "\n")


async def add_note(data: AddNoteInput, ctx: ToolContext) -> AddNoteOutput:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{timestamp}\t{data.text}"
    await asyncio.to_thread(_append, ctx.settings.notes_path, line)
    return AddNoteOutput(written=True, line=line)


TOOL = Tool(
    name="add_note",
    description="Append a one-line note to the user's notes file. Needs confirmation.",
    input_model=AddNoteInput,
    output_model=AddNoteOutput,
    side_effect=True,
    idempotent=False,
    timeout_seconds=2,
    run=add_note,
)
