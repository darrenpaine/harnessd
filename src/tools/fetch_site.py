"""fetch_site: visible text from a public web page (SPEC-0003 B-0006)."""

import ipaddress
from html.parser import HTMLParser

import httpx

from schemas import FetchSiteInput, FetchSiteOutput
from tools.base import Tool, ToolContext, ToolFailure
from tools.pinned import PinnedTransport

MAX_TEXT_CHARS = 1000
MAX_REDIRECTS = 3
MAX_BODY_BYTES = 1_000_000
HTML_TYPES = {"text/html", "application/xhtml+xml"}
TEXT_TYPES = HTML_TYPES | {"text/plain"}


class _TextExtractor(HTMLParser):
    """Collects text a reader would see, skipping scripts, styles and the like,
    and elements hidden by attribute or inline style (SPEC-0012 B-0016).
    Comments are never collected."""

    HIDDEN = {"script", "style", "noscript", "template", "svg"}
    VOID = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "source", "track", "wbr",
    }  # fmt: skip

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        # The hidden element's tag, and how many of that tag are open inside it,
        # so other tags left unclosed don't keep the rest of the page hidden.
        self._hidden_tag: str | None = None
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._hidden_tag is not None:
            if tag == self._hidden_tag:
                self._hidden_depth += 1
        elif tag not in self.VOID and (tag in self.HIDDEN or _hidden(attrs)):
            self._hidden_tag, self._hidden_depth = tag, 1

    def handle_endtag(self, tag: str) -> None:
        if tag == self._hidden_tag:
            self._hidden_depth -= 1
            if not self._hidden_depth:
                self._hidden_tag = None

    def handle_data(self, data: str) -> None:
        if self._hidden_tag is None:
            self.parts.append(data)


def _hidden(attrs: list[tuple[str, str | None]]) -> bool:
    """The `hidden` attribute, `aria-hidden="true"`, or an inline style with
    `display: none` or `visibility: hidden`."""
    values = {name.lower(): (value or "").lower() for name, value in attrs}
    style = "".join(values.get("style", "").split())
    return (
        "hidden" in values
        or values.get("aria-hidden") == "true"
        or "display:none" in style
        or "visibility:hidden" in style
    )


def visible_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return " ".join(" ".join(parser.parts).split())


async def _require_public(url: httpx.URL, ctx: ToolContext) -> str:
    """Refuse URLs whose host is not a public internet address: every address
    it resolves to must be one. Returns the first, the one to connect to."""
    if url.scheme not in ("http", "https"):
        raise ToolFailure("invalid_input", f"only http(s) URLs are allowed: {url}")
    host = url.host
    if not host:
        raise ToolFailure("invalid_input", f"URL has no host: {url}")
    try:
        addresses = await ctx.resolve(host)
    except OSError as exc:
        raise ToolFailure("upstream_error", f"cannot resolve {host}") from exc
    if not addresses:
        raise ToolFailure("upstream_error", f"cannot resolve {host}")
    for address in addresses:
        if not ipaddress.ip_address(address.split("%")[0]).is_global:
            raise ToolFailure("invalid_input", f"{host} is not a public address")
    return addresses[0]


async def _read_limited(response: httpx.Response) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) >= MAX_BODY_BYTES:
            break
    return bytes(body[:MAX_BODY_BYTES])


async def fetch_site(data: FetchSiteInput, ctx: ToolContext) -> FetchSiteOutput:
    url = httpx.URL(str(data.url))
    # Each hop connects to the address that was checked, never resolving the
    # name again (B-0006). A transport given by a test makes no connection.
    pinned = PinnedTransport(ctx.network) if ctx.transport is None else None
    async with httpx.AsyncClient(transport=ctx.transport or pinned) as client:
        for _ in range(MAX_REDIRECTS + 1):
            address = await _require_public(url, ctx)
            if pinned is not None:
                pinned.pin(url, address)
            async with client.stream("GET", url) as response:
                if response.has_redirect_location:
                    url = url.join(response.headers["location"])
                    continue
                if response.status_code >= 300:
                    raise ToolFailure(
                        "upstream_error", f"{url} returned HTTP {response.status_code}"
                    )
                content_type = (
                    response.headers.get("content-type", "").split(";")[0].strip()
                )
                if content_type.lower() not in TEXT_TYPES:
                    raise ToolFailure(
                        "upstream_error",
                        f"not a text page: {content_type or 'no content type'}",
                    )
                raw = await _read_limited(response)
                decoded = raw.decode(response.charset_encoding or "utf-8", "replace")
                text = (
                    visible_text(decoded)
                    if content_type.lower() in HTML_TYPES
                    else " ".join(decoded.split())
                )
                return FetchSiteOutput.model_validate(
                    {
                        "url": str(url),
                        "text": text[:MAX_TEXT_CHARS],
                        "truncated": len(text) > MAX_TEXT_CHARS,
                    }
                )
    raise ToolFailure("upstream_error", f"more than {MAX_REDIRECTS} redirects")


TOOL = Tool(
    name="fetch_site",
    description="Fetch a public web page and return up to 1000 chars of its text.",
    input_model=FetchSiteInput,
    output_model=FetchSiteOutput,
    side_effect=False,
    idempotent=True,
    timeout_seconds=10,
    run=fetch_site,
)
