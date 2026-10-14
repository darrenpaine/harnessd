"""Pydantic models for the API and tool I/O (SPEC-0001)."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    StringConstraints,
    model_validator,
)


class _Model(BaseModel):
    """Base for every boundary model: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


# --- Tool I/O ---------------------------------------------------------------

ToolName = Literal["web_search", "fetch_site", "add_note"]

ToolErrorCode = Literal[
    "invalid_input", "timeout", "upstream_error", "rejected", "internal"
]


class ToolError(_Model):
    """Structured tool failure returned to the model (B-0003)."""

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
    """A side-effecting tool call waiting for human confirmation (B-0007)."""

    tool: Literal["add_note"] = "add_note"
    input: AddNoteInput


# --- API ----------------------------------------------------------------------

RunStatus = Literal["ok", "truncated", "error", "awaiting_confirmation"]

ErrorCode = Literal["model_output_invalid", "internal"]


class ErrorInfo(_Model):
    code: ErrorCode
    message: str


class AgentRequest(_Model):
    thread_id: UUID | None = None
    message: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=4000),
        ]
        | None
    ) = None
    confirm: bool | None = None
    max_tool_calls: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def _one_of_message_or_confirm(self) -> Self:
        if (self.message is None) == (self.confirm is None):
            raise ValueError("exactly one of message or confirm must be set")
        return self


class AgentResponse(_Model):
    thread_id: UUID
    status: RunStatus
    answer: str | None = None
    tool_calls: list[ToolCallRecord] = []
    pending_action: PendingAction | None = None
    error: ErrorInfo | None = None

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
