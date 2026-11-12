"""PinnedTransport: connections go only to pinned addresses (SPEC-0003 B-0006)."""

from collections.abc import Iterable
from typing import Any

import httpcore
import httpx
import pytest

from tools.pinned import PinnedTransport

URL = httpx.URL("http://example.com/")


class Network(httpcore.AsyncNetworkBackend):
    """Answers each connection with `response`, or raises `error`."""

    def __init__(self, response: bytes = b"", error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.connected: list[str] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connected.append(host)
        if self.error is not None:
            raise self.error
        return httpcore.AsyncMockStream([self.response])


@pytest.mark.spec("SPEC-0003", "B-0006")
async def test_host_without_a_pin_is_refused() -> None:
    network = Network()
    async with httpx.AsyncClient(transport=PinnedTransport(network)) as client:
        with pytest.raises(httpx.ConnectError, match="no checked address"):
            await client.get(URL)
    assert network.connected == []


@pytest.mark.spec("SPEC-0003", "B-0006")
@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (httpcore.ConnectTimeout("slow"), httpx.ConnectTimeout),
        (httpcore.ConnectError("refused"), httpx.ConnectError),
    ],
)
async def test_network_errors_become_httpx_errors(
    raised: Exception, expected: type[Exception]
) -> None:
    transport = PinnedTransport(Network(error=raised))
    transport.pin(URL, "93.184.215.14")
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(expected):
            await client.get(URL)


@pytest.mark.spec("SPEC-0003", "B-0006")
async def test_error_while_reading_the_body() -> None:
    # The body is shorter than Content-Length says.
    cut_short = b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\nhello"
    network = Network(cut_short)
    transport = PinnedTransport(network)
    transport.pin(URL, "93.184.215.14")
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(httpx.RemoteProtocolError):
            await client.get(URL)
    assert network.connected == ["93.184.215.14"]
