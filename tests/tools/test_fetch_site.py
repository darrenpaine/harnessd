"""fetch_site with HTTP and DNS mocked (SPEC-0003 B-0006)."""

import dataclasses
import ipaddress
from collections.abc import Callable

import httpx
import pytest

from schemas import FetchSiteOutput, ToolError
from tools import TOOLS
from tools.base import ToolContext, call_tool, resolve_host
from tools.fetch_site import visible_text

Handler = Callable[[httpx.Request], httpx.Response]

DNS = {
    "example.com": ["93.184.215.14"],
    "next.example.com": ["93.184.215.15"],
    "intranet.local": ["10.1.2.3"],
    "7-zip.org": ["93.184.215.16"],
}


async def fake_resolve(host: str) -> list[str]:
    if host in DNS:
        return DNS[host]
    try:
        return [str(ipaddress.ip_address(host))]  # IP literal
    except ValueError:
        pass
    raise OSError(f"unknown host {host}")


def _ctx(base: ToolContext, handler: Handler) -> ToolContext:
    return dataclasses.replace(
        base, transport=httpx.MockTransport(handler), resolve=fake_resolve
    )


async def _fetch(ctx: ToolContext, url: str) -> FetchSiteOutput | ToolError:
    return await call_tool(TOOLS["fetch_site"], {"url": url}, ctx)


def _page(body: str, content_type: str = "text/html; charset=utf-8") -> Handler:
    return lambda request: httpx.Response(
        200, headers={"content-type": content_type}, text=body
    )


def _no_request(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected request to {request.url}")


@pytest.mark.spec("SPEC-0003", "B-0006", "AT-0006")
async def test_extracts_visible_text(tool_context: ToolContext) -> None:
    html = (
        "<html><head><title>Docs</title><style>p{color:red}</style></head>"
        "<body><script>track()</script><p>Hello &amp;\n  welcome</p>"
        "<noscript>enable js</noscript></body></html>"
    )
    result = await _fetch(_ctx(tool_context, _page(html)), "https://example.com/")
    assert isinstance(result, FetchSiteOutput)
    assert result.text == "Docs Hello & welcome"
    assert not result.truncated


@pytest.mark.spec("SPEC-0003", "B-0006")
async def test_host_starting_with_digit(tool_context: ToolContext) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "7-zip.org"
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="7z")

    result = await _fetch(_ctx(tool_context, handler), "https://7-zip.org/")
    assert isinstance(result, FetchSiteOutput)
    assert result.text == "7z"


@pytest.mark.spec("SPEC-0003", "B-0006", "AT-0006")
async def test_truncates_to_1000_chars(tool_context: ToolContext) -> None:
    body = "word " * 400  # 2000 chars of text
    result = await _fetch(
        _ctx(tool_context, _page(body, "text/plain")), "https://example.com/a.txt"
    )
    assert isinstance(result, FetchSiteOutput)
    assert len(result.text) == 1000
    assert result.truncated


@pytest.mark.spec("SPEC-0003", "B-0006", "AT-0006")
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://intranet.local/",
    ],
)
async def test_rejects_non_public_addresses(
    tool_context: ToolContext, url: str
) -> None:
    result = await _fetch(_ctx(tool_context, _no_request), url)
    assert isinstance(result, ToolError)
    assert result.code == "invalid_input"


@pytest.mark.spec("SPEC-0003", "B-0006", "AT-0006")
async def test_rejects_redirect_to_private_address(tool_context: ToolContext) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "example.com"
        return httpx.Response(302, headers={"location": "http://10.0.0.5/admin"})

    result = await _fetch(_ctx(tool_context, handler), "https://example.com/")
    assert isinstance(result, ToolError)
    assert result.code == "invalid_input"


@pytest.mark.spec("SPEC-0003", "B-0006", "AT-0006")
async def test_rejects_non_text_content(tool_context: ToolContext) -> None:
    handler = _page("%PDF-1.7", "application/pdf")
    result = await _fetch(_ctx(tool_context, handler), "https://example.com/a.pdf")
    assert isinstance(result, ToolError)
    assert result.code == "upstream_error"


@pytest.mark.spec("SPEC-0003", "B-0006")
async def test_follows_up_to_three_redirects(tool_context: ToolContext) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        hops = int(request.url.params.get("hop", "0"))
        if hops < 3:
            return httpx.Response(
                301, headers={"location": f"https://next.example.com/?hop={hops + 1}"}
            )
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="end")

    result = await _fetch(_ctx(tool_context, handler), "https://example.com/")
    assert isinstance(result, FetchSiteOutput)
    assert result.text == "end"
    assert str(result.url) == "https://next.example.com/?hop=3"


@pytest.mark.spec("SPEC-0003", "B-0006")
async def test_too_many_redirects(tool_context: ToolContext) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.com/again"})

    result = await _fetch(_ctx(tool_context, handler), "https://example.com/")
    assert isinstance(result, ToolError)
    assert result.code == "upstream_error"
    assert "redirects" in result.message


@pytest.mark.spec("SPEC-0003", "B-0006")
@pytest.mark.parametrize(
    ("url", "handler"),
    [
        ("https://example.com/missing", lambda r: httpx.Response(404)),
        ("https://unknown.example/", _no_request),
        ("https://example.com/", _page("", content_type="")),
    ],
    ids=["http-404", "dns-failure", "no-content-type"],
)
async def test_upstream_failures(
    tool_context: ToolContext, url: str, handler: Handler
) -> None:
    result = await _fetch(_ctx(tool_context, handler), url)
    assert isinstance(result, ToolError)
    assert result.code == "upstream_error"


@pytest.mark.spec("SPEC-0003", "B-0006")
async def test_resolve_host_handles_ip_literals() -> None:
    assert await resolve_host("127.0.0.1") == ["127.0.0.1"]


@pytest.mark.spec("SPEC-0003", "B-0006")
def test_visible_text_handles_unclosed_tags() -> None:
    assert visible_text("<p>one<script>x<p>two") == "one"


@pytest.mark.spec("SPEC-0003", "B-0006")
@pytest.mark.parametrize(
    ("location", "code"),
    [
        ("ftp://example.com/file", "invalid_input"),
        ("http:///no-host", "invalid_input"),
        ("https://empty.example/", "upstream_error"),
    ],
    ids=["non-http", "no-host", "no-addresses"],
)
async def test_checks_every_redirect_target(
    tool_context: ToolContext, location: str, code: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "example.com"
        return httpx.Response(302, headers={"location": location})

    async def resolve(host: str) -> list[str]:
        return [] if host == "empty.example" else await fake_resolve(host)

    ctx = dataclasses.replace(_ctx(tool_context, handler), resolve=resolve)
    result = await _fetch(ctx, "https://example.com/")
    assert isinstance(result, ToolError)
    assert result.code == code


@pytest.mark.spec("SPEC-0003", "B-0006")
async def test_redirect_status_without_location(tool_context: ToolContext) -> None:
    handler: Handler = lambda request: httpx.Response(302)  # noqa: E731
    result = await _fetch(_ctx(tool_context, handler), "https://example.com/")
    assert isinstance(result, ToolError)
    assert result.code == "upstream_error"


@pytest.mark.spec("SPEC-0003", "B-0006")
async def test_reads_at_most_1mb(tool_context: ToolContext) -> None:
    body = "a" * 3_000_000
    result = await _fetch(
        _ctx(tool_context, _page(body, "text/plain")), "https://example.com/big"
    )
    assert isinstance(result, FetchSiteOutput)
    assert result.truncated and len(result.text) == 1000
