"""Validation rules for the API (SPEC-0002) and tool (SPEC-0003) models."""

from typing import Any
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from schemas import (
    AddNoteCall,
    AddNoteInput,
    AgentRequest,
    AgentResponse,
    FetchSiteInput,
    FetchSiteOutput,
    ToolCallRecord,
    WebSearchCall,
    WebSearchInput,
)

record_adapter: TypeAdapter[ToolCallRecord] = TypeAdapter(ToolCallRecord)


# --- AgentRequest -------------------------------------------------------------


@pytest.mark.spec("SPEC-0002", "B-0001")
def test_request_defaults() -> None:
    req = AgentRequest.model_validate({"message": "  hi  "})
    assert req.message == "hi"
    assert req.thread_id is None
    assert req.max_tool_calls == 5


@pytest.mark.spec("SPEC-0002", "B-0001")
@pytest.mark.parametrize(
    "body",
    [
        {"message": ""},
        {"message": "   "},
        {"message": "x" * 4001},
        {},
        {"message": "hi", "confirm": True},
        {"message": "hi", "max_tool_calls": 0},
        {"message": "hi", "max_tool_calls": 21},
        {"message": "hi", "thread_id": "not-a-uuid"},
        {"message": "hi", "unknown": 1},
    ],
)
def test_request_rejects_invalid(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        AgentRequest.model_validate(body)


@pytest.mark.spec("SPEC-0002", "B-0003")
def test_request_confirm_only() -> None:
    req = AgentRequest.model_validate({"thread_id": str(uuid4()), "confirm": False})
    assert req.confirm is False
    assert req.message is None


# --- AgentResponse ------------------------------------------------------------


def _response(**fields: Any) -> AgentResponse:
    return AgentResponse.model_validate({"thread_id": str(uuid4()), **fields})


@pytest.mark.spec("SPEC-0002", "B-0002")
@pytest.mark.parametrize(
    "fields",
    [
        {"status": "ok", "answer": "done"},
        {"status": "truncated"},
        {"status": "truncated", "answer": "partial"},
        {"status": "error", "error": {"code": "internal", "message": "boom"}},
        {
            "status": "awaiting_confirmation",
            "pending_action": {"tool": "add_note", "input": {"text": "buy milk"}},
        },
    ],
)
def test_response_accepts_consistent_status(fields: dict[str, Any]) -> None:
    assert _response(**fields).status == fields["status"]


@pytest.mark.spec("SPEC-0002", "B-0002", "AT-0003")
@pytest.mark.parametrize(
    "fields",
    [
        {"status": "ok"},
        {"status": "ok", "answer": "a", "error": {"code": "internal", "message": "x"}},
        {"status": "error"},
        {
            "status": "error",
            "answer": "a",
            "error": {"code": "internal", "message": "x"},
        },
        {"status": "awaiting_confirmation"},
        {"status": "done", "answer": "a"},
        {"status": "error", "error": {"code": "bad_code", "message": "x"}},
    ],
)
def test_response_rejects_inconsistent_status(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _response(**fields)


# --- Tool call trace ----------------------------------------------------------


@pytest.mark.spec("SPEC-0003", "B-0003")
def test_record_selects_model_by_name() -> None:
    record = record_adapter.validate_python(
        {
            "id": "c1",
            "name": "web_search",
            "input": {"query": "langgraph"},
            "output": {"results": [{"title": "t", "url": "u", "snippet": "s"}]},
        }
    )
    assert isinstance(record, WebSearchCall)
    assert record.input.max_results == 3


@pytest.mark.spec("SPEC-0003", "B-0001")
def test_record_holds_tool_error() -> None:
    record = record_adapter.validate_python(
        {
            "id": "c1",
            "name": "add_note",
            "input": {"text": "x"},
            "error": {"code": "rejected", "message": "user declined"},
        }
    )
    assert isinstance(record, AddNoteCall)
    assert record.error is not None
    assert record.error.code == "rejected"


@pytest.mark.spec("SPEC-0003", "B-0001")
@pytest.mark.parametrize(
    "record",
    [
        {"id": "c1", "name": "add_note", "input": {"text": "x"}},
        {
            "id": "c1",
            "name": "add_note",
            "input": {"text": "x"},
            "output": {"written": True, "line": "x"},
            "error": {"code": "internal", "message": "x"},
        },
        {
            "id": "c1",
            "name": "web_search",
            "input": {"text": "x"},
            "error": {"code": "internal", "message": "x"},
        },
        {
            "id": "c1",
            "name": "delete_all",
            "input": {},
            "error": {"code": "internal", "message": "x"},
        },
    ],
)
def test_record_rejects_invalid(record: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        record_adapter.validate_python(record)


# --- Tool inputs and outputs --------------------------------------------------


@pytest.mark.spec("SPEC-0003", "B-0005")
@pytest.mark.parametrize(
    "data",
    [
        {"query": ""},
        {"query": "x" * 401},
        {"query": "q", "max_results": 0},
        {"query": "q", "max_results": 6},
    ],
)
def test_web_search_input_bounds(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        WebSearchInput.model_validate(data)


@pytest.mark.spec("SPEC-0003", "B-0006")
@pytest.mark.parametrize("url", ["ftp://example.com", "file:///etc/passwd", "nope"])
def test_fetch_site_input_requires_http(url: str) -> None:
    with pytest.raises(ValidationError):
        FetchSiteInput.model_validate({"url": url})


@pytest.mark.spec("SPEC-0003", "B-0006")
def test_fetch_site_output_text_limit() -> None:
    url = "https://example.com"
    FetchSiteOutput.model_validate({"url": url, "text": "x" * 1000, "truncated": True})
    with pytest.raises(ValidationError):
        FetchSiteOutput.model_validate(
            {"url": url, "text": "x" * 1001, "truncated": True}
        )


@pytest.mark.spec("SPEC-0003", "B-0007")
@pytest.mark.parametrize("text", ["", "   ", "x" * 501, "line1\nline2", "a\rb"])
def test_add_note_input_rejects_invalid(text: str) -> None:
    with pytest.raises(ValidationError):
        AddNoteInput.model_validate({"text": text})
