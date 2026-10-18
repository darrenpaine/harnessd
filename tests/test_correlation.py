"""Request and trace ids on responses and log lines (SPEC-0010 B-0006, B-0007)."""

import logging
import uuid

import httpx
import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent.graph import build_graph
from agent_support import Capture, MakeClient
from api.deps import get_graph
from app import app
from fakes import RaisingChatModel
from logs import ROOT
from tools import TOOLS
from tools.base import ToolContext

RUN = "/v1/agent/run"


def is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


@pytest.mark.spec("SPEC-0010", "B-0006", "B-0007", "AT-0006")
async def test_request_id(
    make_client: MakeClient,
    tool_context: ToolContext,
    capture: Capture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _ = make_client([AIMessage(content="a"), AIMessage(content="b")])
    capture("info")

    echoed = await client.post(
        RUN, json={"message": "hi"}, headers={"X-Request-ID": "req-1.A_b"}
    )
    missing = await client.post(RUN, json={"message": "hi"})
    invalid = await client.post(RUN, json={}, headers={"X-Request-ID": "bad id!"})
    too_long = await client.get("/health", headers={"X-Request-ID": "a" * 129})

    assert echoed.status_code == 200
    assert echoed.headers["X-Request-ID"] == "req-1.A_b"
    assert is_uuid(missing.headers["X-Request-ID"])
    assert invalid.status_code == 422
    assert is_uuid(invalid.headers["X-Request-ID"])
    assert is_uuid(too_long.headers["X-Request-ID"])
    run_lines = [
        r.getMessage()
        for r in caplog.records
        if r.name.startswith(ROOT) and r.getMessage().startswith("run.")
    ]
    assert run_lines and all("request=req-1.A_b" in line for line in run_lines[:2])

    # A 500 carries the id, and so does its request.failed line.
    graph = build_graph(
        RaisingChatModel(error=RuntimeError("boom")),
        tool_context,
        TOOLS,
        InMemorySaver(),
    )
    app.dependency_overrides[get_graph] = lambda: graph
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as raw:
        failed = await raw.post(
            RUN, json={"message": "hi"}, headers={"X-Request-ID": "req-500"}
        )
    assert failed.status_code == 500
    assert failed.headers["X-Request-ID"] == "req-500"
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("request=req-500" in r.getMessage() for r in errors)
