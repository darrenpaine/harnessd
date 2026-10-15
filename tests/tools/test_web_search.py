"""web_search with Tavily mocked at the HTTP layer (SPEC-0003 B-0005)."""

import dataclasses
import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from schemas import ToolError, WebSearchOutput
from tools import TOOLS
from tools.base import ToolContext, call_tool

Handler = Callable[[httpx.Request], httpx.Response]


def _with_tavily(
    ctx: ToolContext, handler: Handler, key: str | None = "tvly-test"
) -> ToolContext:
    settings = ctx.settings.model_copy(
        update={"tavily_api_key": SecretStr(key) if key else None}
    )
    return dataclasses.replace(
        ctx, settings=settings, transport=httpx.MockTransport(handler)
    )


@pytest.mark.spec("SPEC-0003", "B-0005", "AT-0005")
async def test_web_search_maps_results(tool_context: ToolContext) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "LangGraph", "url": "https://x.dev", "content": "Graphs"},
                    "not a result",
                ]
            },
        )

    result = await call_tool(
        TOOLS["web_search"],
        {"query": "langgraph", "max_results": 2},
        _with_tavily(tool_context, handler),
    )

    assert isinstance(result, WebSearchOutput)
    assert [r.model_dump() for r in result.results] == [
        {"title": "LangGraph", "url": "https://x.dev", "snippet": "Graphs"}
    ]
    request = seen[0]
    assert request.url.path == "/search"
    assert request.headers["authorization"] == "Bearer tvly-test"
    body = json.loads(request.content)
    assert body["query"] == "langgraph" and body["max_results"] == 2


@pytest.mark.spec("SPEC-0003", "B-0005", "AT-0005")
async def test_web_search_missing_key(tool_context: ToolContext) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected without a key")

    result = await call_tool(
        TOOLS["web_search"], {"query": "q"}, _with_tavily(tool_context, handler, None)
    )
    assert isinstance(result, ToolError)
    assert result.code == "upstream_error"
    assert "TAVILY_API_KEY" in result.message


@pytest.mark.spec("SPEC-0003", "B-0005")
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"detail": {"error": "bad key"}}),
        httpx.Response(429, json={"detail": {"error": "limit"}}),
        httpx.Response(500, text="oops"),
        httpx.Response(200, json={"results": "not a list"}),
    ],
    ids=["401", "429", "500", "bad-results"],
)
async def test_web_search_upstream_errors(
    tool_context: ToolContext, response: httpx.Response
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    result = await call_tool(
        TOOLS["web_search"], {"query": "q"}, _with_tavily(tool_context, handler)
    )
    assert isinstance(result, ToolError)
    assert result.code == "upstream_error"


@pytest.mark.spec("SPEC-0003", "B-0005")
async def test_web_search_empty_results(tool_context: ToolContext) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"query": "q"})

    result = await call_tool(
        TOOLS["web_search"], {"query": "q"}, _with_tavily(tool_context, handler)
    )
    assert isinstance(result, WebSearchOutput)
    assert result.results == []
