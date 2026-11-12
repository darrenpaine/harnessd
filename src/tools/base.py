"""The contract every tool follows (SPEC-0003)."""

import asyncio
import re
import socket
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpcore
import httpx
from pydantic import BaseModel, ValidationError

from schemas import ToolError, ToolErrorCode
from settings import Settings

Resolver = Callable[[str], Awaitable[list[str]]]

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class ToolFailure(Exception):
    """Raised inside a tool for an expected failure; becomes a ToolError."""

    def __init__(self, code: ToolErrorCode, message: str) -> None:
        super().__init__(message)
        self.error = ToolError(code=code, message=message)


async def resolve_host(host: str) -> list[str]:
    """IP addresses a host name resolves to, in the system's order of
    preference, so the first is the one to connect to."""
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, None, type=socket.SOCK_STREAM
    )
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


@dataclass(frozen=True)
class ToolContext:
    """What a tool may use. Tests replace the transport and resolver, or the
    `network` that opens fetch_site's connections when no transport is given."""

    settings: Settings
    transport: httpx.AsyncBaseTransport | None = None
    resolve: Resolver = resolve_host
    network: httpcore.AsyncNetworkBackend | None = None


@dataclass(frozen=True)
class Tool[InputT: BaseModel, OutputT: BaseModel]:
    """A registered tool and its metadata (SPEC-0003 B-0003)."""

    name: str
    description: str
    input_model: type[InputT]
    output_model: type[OutputT]
    side_effect: bool
    idempotent: bool
    timeout_seconds: float
    run: Callable[[InputT, ToolContext], Awaitable[OutputT]]

    def __post_init__(self) -> None:
        if not _NAME.match(self.name):
            raise ValueError(f"tool name {self.name!r} is not snake_case")
        if self.timeout_seconds <= 0:
            raise ValueError(f"tool {self.name!r} needs a positive timeout")


async def call_tool[InputT: BaseModel, OutputT: BaseModel](
    tool: Tool[InputT, OutputT], args: Mapping[str, Any], ctx: ToolContext
) -> OutputT | ToolError:
    """Run a tool; every failure becomes a ToolError (SPEC-0003 B-0001, B-0002)."""
    try:
        data = tool.input_model.model_validate(dict(args))
    except ValidationError as exc:
        return ToolError(code="invalid_input", message=summarize(exc))
    try:
        async with asyncio.timeout(tool.timeout_seconds):
            return await tool.run(data, ctx)
    except ToolFailure as exc:
        return exc.error
    except TimeoutError:
        return ToolError(
            code="timeout",
            message=f"{tool.name} took longer than {tool.timeout_seconds:g} s",
        )
    except Exception as exc:
        return ToolError(
            code="internal", message=f"{tool.name} failed: {type(exc).__name__}"
        )


def summarize(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc']) or 'input'}: {err['msg']}"
        for err in exc.errors()
    )
