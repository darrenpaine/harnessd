"""An httpx transport that connects only to addresses it was given
(SPEC-0003 B-0006).

fetch_site checks the addresses a host name resolves to, but a stock httpx
transport resolves the name again when it connects, and a hostile DNS server
can answer differently the second time (DNS rebinding). `PinnedTransport`
connects to the address pinned for a host and never resolves a name. The URL
keeps the host name, so the `Host` header, the TLS server name and the
certificate check are those of a normal request.

It is built on httpcore, which httpx itself uses, because httpx's own transport
takes no network backend.
"""

from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpcore
import httpx


class PinnedBackend(httpcore.AsyncNetworkBackend):
    """Opens connections for `network`, to the pinned address of each host."""

    def __init__(self, network: httpcore.AsyncNetworkBackend) -> None:
        self._network = network
        self._pins: dict[str, str] = {}

    def pin(self, host: str, address: str) -> None:
        self._pins[host] = address

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        address = self._pins.get(host)
        if address is None:
            raise httpcore.ConnectError(f"no checked address for {host}")
        return await self._network.connect_tcp(
            address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._network.sleep(seconds)


class _Body(httpx.AsyncByteStream):
    def __init__(self, stream: Any, request: httpx.Request) -> None:
        self._stream = stream
        self._request = request

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for part in self._stream:
                yield part
        except Exception as exc:
            if (error := _as_httpx(exc, self._request)) is None:
                raise
            raise error from exc

    async def aclose(self) -> None:
        await self._stream.aclose()


class PinnedTransport(httpx.AsyncBaseTransport):
    """Sends requests through an httpcore pool that connects only to pinned
    addresses. `network` opens the connections; tests pass a fake."""

    def __init__(self, network: httpcore.AsyncNetworkBackend | None = None) -> None:
        self._backend = PinnedBackend(network or httpcore.AnyIOBackend())
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(), network_backend=self._backend
        )

    def pin(self, url: httpx.URL, address: str) -> None:
        """Connect to `address` for requests to `url`'s host."""
        self._backend.pin(url.raw_host.decode("ascii"), address)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        outgoing = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        try:
            incoming = await self._pool.handle_async_request(outgoing)
        except Exception as exc:
            if (error := _as_httpx(exc, request)) is None:
                raise
            raise error from exc
        return httpx.Response(
            status_code=incoming.status,
            headers=incoming.headers,
            stream=_Body(incoming.stream, request),
            extensions=incoming.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


def _as_httpx(exc: Exception, request: httpx.Request) -> Exception | None:
    """httpcore's error as the httpx error of the same name, which is what
    callers of an httpx transport expect; None for any other exception."""
    if not type(exc).__module__.startswith("httpcore"):
        return None
    kind = getattr(httpx, type(exc).__name__, None)
    if not (isinstance(kind, type) and issubclass(kind, httpx.TransportError)):
        kind = httpx.TransportError
    return kind(str(exc), request=request)
