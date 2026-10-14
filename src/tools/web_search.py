"""web_search: Tavily web search (SPEC-0003 B-0005)."""

from typing import Any

import httpx
from tavily import (
    AsyncTavilyClient,
    BadRequestError,
    InvalidAPIKeyError,
    UsageLimitExceededError,
)

from schemas import WebSearchInput, WebSearchOutput, WebSearchResult
from tools.base import Tool, ToolContext, ToolFailure

_UPSTREAM_ERRORS = (
    httpx.HTTPError,
    BadRequestError,
    InvalidAPIKeyError,
    UsageLimitExceededError,
)


async def web_search(data: WebSearchInput, ctx: ToolContext) -> WebSearchOutput:
    key = ctx.settings.tavily_api_key
    if key is None:
        raise ToolFailure("upstream_error", "TAVILY_API_KEY is not set")
    async with httpx.AsyncClient(transport=ctx.transport) as http:
        client = AsyncTavilyClient(api_key=key.get_secret_value(), client=http)
        try:
            body = await client.search(data.query, max_results=data.max_results)
        except _UPSTREAM_ERRORS as exc:
            raise ToolFailure(
                "upstream_error", f"Tavily search failed: {type(exc).__name__}"
            ) from exc
    return WebSearchOutput(results=_results(body))


def _results(body: Any) -> list[WebSearchResult]:
    items = body.get("results") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise ToolFailure("upstream_error", "Tavily returned no results list")
    return [
        WebSearchResult(
            title=str(item.get("title", "")),
            url=str(item.get("url", "")),
            snippet=str(item.get("content", "")),
        )
        for item in items
        if isinstance(item, dict)
    ]


TOOL = Tool(
    name="web_search",
    description="Search the web. Returns titles, URLs and snippets.",
    input_model=WebSearchInput,
    output_model=WebSearchOutput,
    side_effect=False,
    idempotent=True,
    timeout_seconds=10,
    run=web_search,
)
