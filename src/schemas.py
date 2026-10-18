"""Pydantic models for the API (SPEC-0002, SPEC-0009) and tool I/O (SPEC-0003)."""

from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    model_validator,
)
from pydantic.config import JsonDict


class _Model(BaseModel):
    """Base for every boundary model: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


# --- Tool I/O ---------------------------------------------------------------

ToolName = Literal["web_search", "fetch_site", "add_note"]

ToolErrorCode = Literal[
    "invalid_input", "timeout", "upstream_error", "rejected", "internal"
]


class ToolError(_Model):
    """Structured tool failure returned to the model (SPEC-0003 B-0001)."""

    code: ToolErrorCode
    message: str


class WebSearchInput(_Model):
    query: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)
    ]
    max_results: int = Field(default=3, ge=1, le=5)


class WebSearchResult(_Model):
    title: str
    url: str
    snippet: str


class WebSearchOutput(_Model):
    results: list[WebSearchResult]


class FetchSiteInput(_Model):
    url: HttpUrl


class FetchSiteOutput(_Model):
    url: HttpUrl
    text: str = Field(max_length=1000)
    truncated: bool


class AddNoteInput(_Model):
    text: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True, min_length=1, max_length=500, pattern=r"^[^\r\n]*$"
        ),
    ]


class AddNoteOutput(_Model):
    written: bool
    line: str


class _ToolCallBase[InputT: BaseModel, OutputT: BaseModel](_Model):
    """One entry in the tool-call trace: exactly one of output/error is set."""

    id: str
    input: InputT
    output: OutputT | None = None
    error: ToolError | None = None

    @model_validator(mode="after")
    def _one_of_output_or_error(self) -> Self:
        if (self.output is None) == (self.error is None):
            raise ValueError("exactly one of output or error must be set")
        return self


class WebSearchCall(_ToolCallBase[WebSearchInput, WebSearchOutput]):
    name: Literal["web_search"] = "web_search"


class FetchSiteCall(_ToolCallBase[FetchSiteInput, FetchSiteOutput]):
    name: Literal["fetch_site"] = "fetch_site"


class AddNoteCall(_ToolCallBase[AddNoteInput, AddNoteOutput]):
    name: Literal["add_note"] = "add_note"


ToolCallRecord = Annotated[
    WebSearchCall | FetchSiteCall | AddNoteCall, Field(discriminator="name")
]


class PendingAction(_Model):
    """A side-effecting tool call waiting for human confirmation (SPEC-0001 B-0005)."""

    tool: Literal["add_note"] = "add_note"
    input: AddNoteInput


# --- API ----------------------------------------------------------------------

RunStatus = Literal["ok", "truncated", "error", "awaiting_confirmation"]

ErrorCode = Literal["model_output_invalid", "internal"]


class ErrorInfo(_Model):
    code: ErrorCode
    message: str


_THREAD = "6f1c2a9e-4b7d-4e2a-9c1f-3d5e8a7b2c10"
_NOTE_CALL: JsonDict = {
    "id": "call-1",
    "name": "add_note",
    "input": {"text": "buy milk"},
    "output": {"written": True, "line": "2026-10-15T09:30:00Z\tbuy milk"},
}


# Named request examples, shown as a list in Swagger UI (SPEC-0007 B-0005).
REQUEST_EXAMPLES: dict[str, JsonDict] = {
    "new": {
        "summary": "New conversation",
        "description": "An unused thread_id starts a new thread; the other "
        "examples continue it. Omit thread_id to have one generated.",
        "value": {
            "thread_id": _THREAD,
            "message": "What is new in Python 3.14?",
            "max_tool_calls": 5,
        },
    },
    "continue": {
        "summary": "Continue a thread",
        "description": "Send the thread_id from an earlier response.",
        "value": {"thread_id": _THREAD, "message": "And what changed in 3.13?"},
    },
    "confirm": {
        "summary": "Confirm a pending action",
        "description": "Answer an awaiting_confirmation response: true runs the "
        "action, false rejects it.",
        "value": {"thread_id": _THREAD, "confirm": True},
    },
}


class AgentRequest(_Model):
    """One turn on a thread: a new user message, or a confirmation decision."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [example["value"] for example in REQUEST_EXAMPLES.values()]
        }
    )

    thread_id: UUID | None = Field(
        default=None,
        description="Thread to continue. Omit to start a new thread.",
    )
    message: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=4000),
        ]
        | None
    ) = Field(
        default=None,
        description="The user's message, 1-4000 characters. "
        "Exactly one of message and confirm must be set.",
    )
    confirm: bool | None = Field(
        default=None,
        description="Approve (true) or reject (false) the pending action. "
        "Only valid while the thread is awaiting confirmation.",
    )
    max_tool_calls: int = Field(
        default=5, ge=1, le=20, description="Most tool calls this request may make."
    )

    @model_validator(mode="after")
    def _one_of_message_or_confirm(self) -> Self:
        if (self.message is None) == (self.confirm is None):
            raise ValueError("exactly one of message or confirm must be set")
        return self


class AgentResponse(_Model):
    """The result of one run. Which fields are set depends on `status`."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "thread_id": _THREAD,
                    "status": "ok",
                    "answer": "Noted: buy milk.",
                    "tool_calls": [_NOTE_CALL],
                    "pending_action": None,
                    "error": None,
                },
                {
                    "thread_id": _THREAD,
                    "status": "truncated",
                    "answer": None,
                    "tool_calls": [],
                    "pending_action": None,
                    "error": None,
                },
                {
                    "thread_id": _THREAD,
                    "status": "error",
                    "answer": None,
                    "tool_calls": [],
                    "pending_action": None,
                    "error": {
                        "code": "model_output_invalid",
                        "message": "the model gave no answer",
                    },
                },
                {
                    "thread_id": _THREAD,
                    "status": "awaiting_confirmation",
                    "answer": None,
                    "tool_calls": [],
                    "pending_action": {
                        "tool": "add_note",
                        "input": {"text": "buy milk"},
                    },
                    "error": None,
                },
            ]
        }
    )

    thread_id: UUID = Field(description="The thread, new or continued.")
    status: RunStatus = Field(description="How the run ended.")
    answer: str | None = Field(
        default=None,
        description="The answer when status is ok; may hold a partial answer "
        "when truncated; otherwise null.",
    )
    tool_calls: list[ToolCallRecord] = Field(
        default=[], description="Tool calls made in this request, in order."
    )
    pending_action: PendingAction | None = Field(
        default=None,
        description="The action waiting for confirmation; set only when status "
        "is awaiting_confirmation.",
    )
    error: ErrorInfo | None = Field(
        default=None, description="What went wrong; set only when status is error."
    )

    @model_validator(mode="after")
    def _fields_match_status(self) -> Self:
        if self.status == "ok" and self.answer is None:
            raise ValueError("answer is required when status is 'ok'")
        if (
            self.status in ("error", "awaiting_confirmation")
            and self.answer is not None
        ):
            raise ValueError(f"answer must be null when status is {self.status!r}")
        if (self.status == "awaiting_confirmation") != (
            self.pending_action is not None
        ):
            raise ValueError(
                "pending_action is set only when status is 'awaiting_confirmation'"
            )
        if (self.status == "error") != (self.error is not None):
            raise ValueError("error is set only when status is 'error'")
        return self


# --- Threads (SPEC-0009) -------------------------------------------------------

ThreadStatus = Literal["idle", "awaiting_confirmation"]


class ThreadToolCall(_Model):
    """A tool call as the model sent it; invalid calls keep their raw args."""

    id: str | None = Field(description="The call's id.")
    name: str | None = Field(description="The tool the model asked for.")
    args: dict[str, Any] | str | None = Field(
        description="The arguments; a string when they were not valid JSON."
    )


class ThreadMessage(_Model):
    role: Literal["user", "assistant", "tool"] = Field(description="Who sent it.")
    content: str = Field(description="The message text.")
    tool_calls: list[ThreadToolCall] = Field(
        default=[], description="Calls an assistant message requested."
    )
    tool_call_id: str | None = Field(
        default=None, description="The call a tool message answers."
    )


class ThreadState(_Model):
    """A thread's latest saved state (SPEC-0009 B-0001)."""

    thread_id: UUID = Field(description="The thread.")
    status: ThreadStatus = Field(description="Whether an action awaits confirmation.")
    pending_action: PendingAction | None = Field(
        default=None,
        description="The action waiting for confirmation; set only when status "
        "is awaiting_confirmation.",
    )
    summary: str | None = Field(
        default=None, description="The summary written by the last compaction."
    )
    messages: list[ThreadMessage] = Field(
        description="The messages after the summary, oldest first."
    )
    expires_in_seconds: int | None = Field(
        default=None,
        description="Seconds until the thread expires; null if it does not.",
    )

    @model_validator(mode="after")
    def _pending_matches_status(self) -> Self:
        if (self.status == "awaiting_confirmation") != (
            self.pending_action is not None
        ):
            raise ValueError(
                "pending_action is set only when status is 'awaiting_confirmation'"
            )
        return self


class CompactRequest(_Model):
    keep_turns: int = Field(
        default=2,
        ge=0,
        le=20,
        description="The most recent turns to keep word for word. A turn is a "
        "user message and everything after it up to the next user message.",
    )


class CompactResponse(_Model):
    thread_id: UUID = Field(description="The thread.")
    compacted: bool = Field(description="False when there was nothing to compact.")
    messages_removed: int = Field(description="Messages replaced by the summary.")
    summary: str | None = Field(description="The thread's summary after the call.")


ApiErrorCode = Literal[
    "thread_busy",
    "thread_not_found",
    "thread_awaiting_confirmation",
    "thread_run_unfinished",
    "dependency_unavailable",
    "internal",
]


class ApiErrorDetail(_Model):
    code: ApiErrorCode
    message: str


class ApiError(_Model):
    """Body of a 404, 409, 500 or 503 response (SPEC-0002, SPEC-0007 B-0004)."""

    error: ApiErrorDetail


class HealthResponse(_Model):
    status: Literal["ok"] = "ok"
