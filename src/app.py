"""FastAPI app and routes (SPEC-0002, SPEC-0007)."""

import asyncio
import logging
import tomllib
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import httpx
import redis.exceptions
from fastapi import Body, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from langchain_core.exceptions import (
    ModelAuthenticationError,
    ModelConnectionError,
    ModelError,
    ModelNotFoundError,
    ModelPermissionDeniedError,
    ModelRateLimitError,
    ModelTimeoutError,
)
from langgraph.graph.state import CompiledStateGraph
from ollama import ResponseError as OllamaResponseError
from redis.asyncio import Redis
from redisvl.exceptions import RedisSearchError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent.graph import AgentState, build_graph
from agent.llm import build_chat_model
from agent.persistence import RedisThreadGuard, ThreadBusyError, open_checkpointer
from agent.runner import AgentRunner, ConfirmationStateError, ThreadGuard
from logs import configure_logging, event, get_logger
from schemas import (
    REQUEST_EXAMPLES,
    AgentRequest,
    AgentResponse,
    ApiError,
    ApiErrorCode,
    ApiErrorDetail,
    HealthResponse,
)
from settings import Settings, get_settings
from tools import TOOLS
from tools.base import ToolContext

SPECS_URL = "https://github.com/darrenpaine/harnessd/blob/main/docs/specs/README.md"

DESCRIPTION = (
    "A LangGraph agent with three tools (web search, fetch a page, add a note) "
    "behind one endpoint. Every response is a validated `AgentResponse`; "
    "conversation state is kept per thread in Redis, and side-effecting tools wait "
    f"for confirmation. Behavior is specified in [the specs]({SPECS_URL})."
)

log = get_logger("app")

REDIS_ERRORS = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)
LLM_ERRORS = (httpx.TransportError, ModelConnectionError, ModelTimeoutError)


class ProviderRejectedError(Exception):
    """The LLM provider answered but refused the request (SPEC-0002 B-0007)."""


def provider_rejection(exc: Exception, model: str) -> str | None:
    """Why the provider refused the request, or None if it didn't.

    Anthropic and Gemini errors arrive as LangChain's ModelError types; Ollama
    raises its own ResponseError with the HTTP status. Messages are fixed text,
    so no provider detail (such as a key) reaches the client.
    """
    status = exc.status_code if isinstance(exc, OllamaResponseError) else None
    if isinstance(exc, ModelNotFoundError) or status == 404:
        return f"the LLM provider has no model {model!r}"
    if isinstance(exc, ModelAuthenticationError | ModelPermissionDeniedError) or (
        status in (401, 403)
    ):
        return f"the LLM provider rejected the API key for model {model!r}"
    if isinstance(exc, ModelRateLimitError) or status == 429:
        return f"the LLM provider is rate-limiting requests for model {model!r}"
    if isinstance(exc, ModelError | OllamaResponseError):
        return f"the LLM provider rejected the request for model {model!r}"
    return None


def project_version() -> str:
    """`[project].version` from pyproject.toml (SPEC-0007 B-0002).

    pyproject.toml sits beside the code in the Docker image and one level up
    in the repo.
    """
    here = Path(__file__).resolve().parent
    for folder in (here, here.parent):
        path = folder / "pyproject.toml"
        if path.exists():
            version: str = tomllib.loads(path.read_text())["project"]["version"]
            return version
    raise FileNotFoundError("pyproject.toml not found")


class Services:
    """Redis client and agent graph, created on first use.

    Created lazily so the app starts, and /health answers, while Redis is down;
    requests then get 503 until Redis is back (SPEC-0002 B-0005).
    """

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self._lock = asyncio.Lock()
        self._redis: Redis | None = None
        self._graph: CompiledStateGraph[AgentState] | None = None

    def redis(self, settings: Settings) -> Redis:
        if self._redis is None:
            self._redis = Redis.from_url(settings.redis_url)
        return self._redis

    async def graph(self, settings: Settings) -> CompiledStateGraph[AgentState]:
        async with self._lock:
            if self._graph is None:
                saver = await self._stack.enter_async_context(
                    open_checkpointer(settings)
                )
                self._graph = build_graph(
                    build_chat_model(settings),
                    ToolContext(settings=settings),
                    TOOLS,
                    saver,
                )
            return self._graph

    async def aclose(self) -> None:
        """Close the checkpointer and the Redis client.

        Redis may already be gone at shutdown. That is logged, not raised, so
        shutdown finishes and the client is closed even if the checkpointer fails.
        """
        try:
            await self._stack.aclose()
        except REDIS_ERRORS as exc:
            log.warning("closing the checkpointer failed: %r", exc)
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except REDIS_ERRORS as exc:
                log.warning("closing the Redis client failed: %r", exc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = app.dependency_overrides.get(get_settings, get_settings)()
    configure_logging(settings)
    app.state.services = Services()
    yield
    await app.state.services.aclose()


app = FastAPI(
    title="harnessd",
    version=project_version(),
    description=DESCRIPTION,
    lifespan=lifespan,
)

SettingsDep = Annotated[Settings, Depends(get_settings)]


async def get_graph(
    request: Request, settings: SettingsDep
) -> CompiledStateGraph[AgentState]:
    """The compiled agent graph; tests override this (SPEC-0002)."""
    services: Services = request.app.state.services
    return await services.graph(settings)


def get_guard(request: Request, settings: SettingsDep) -> ThreadGuard:
    services: Services = request.app.state.services
    return RedisThreadGuard(services.redis(settings), settings)


def get_runner(
    graph: Annotated[CompiledStateGraph[AgentState], Depends(get_graph)],
    guard: Annotated[ThreadGuard, Depends(get_guard)],
    settings: SettingsDep,
) -> AgentRunner:
    return AgentRunner(graph, settings, guard)


def _api_error(status: int, code: ApiErrorCode, message: str) -> JSONResponse:
    body = ApiError(error=ApiErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status, content=body.model_dump())


@app.exception_handler(ThreadBusyError)
async def thread_busy(request: Request, exc: ThreadBusyError) -> JSONResponse:
    event(log, logging.WARNING, "thread.busy", thread=exc.args[0] if exc.args else None)
    return _api_error(409, "thread_busy", "a run is already in progress on this thread")


def _unavailable(cause: str) -> JSONResponse:
    """503 dependency_unavailable (SPEC-0002 B-0005, B-0007), logged as a warning."""
    event(log, logging.WARNING, "dependency.unavailable", cause=cause)
    return _api_error(503, "dependency_unavailable", cause)


async def redis_unavailable(request: Request, exc: Exception) -> JSONResponse:
    return _unavailable("Redis is unreachable")


async def wrapped_redis_error(request: Request, exc: Exception) -> JSONResponse:
    """The checkpointer's search layer wraps connection errors; unwrap them."""
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, REDIS_ERRORS):
            return await redis_unavailable(request, exc)
        cause = cause.__cause__ or cause.__context__
    raise exc


async def llm_unavailable(request: Request, exc: Exception) -> JSONResponse:
    return _unavailable("the LLM provider is unreachable")


for redis_error in REDIS_ERRORS:
    app.add_exception_handler(redis_error, redis_unavailable)
app.add_exception_handler(RedisSearchError, wrapped_redis_error)
for llm_error in LLM_ERRORS:
    app.add_exception_handler(llm_error, llm_unavailable)


@app.exception_handler(ProviderRejectedError)
async def provider_rejected(
    request: Request, exc: ProviderRejectedError
) -> JSONResponse:
    return _unavailable(str(exc))


class InternalErrorMiddleware:
    """Turn an unhandled exception into a JSON 500 (SPEC-0002 B-0008).

    No exception text leaves the service; the exception is logged once, with
    its traceback (SPEC-0008 B-0005). Starlette's own 500 handler would re-raise
    it to the server, which logs it a second time. If the response has already
    started, the exception is re-raised, as a 500 can no longer be sent.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracked(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, receive, tracked)
        except Exception:
            event(
                log, logging.ERROR, "request.failed", exc_info=True, path=scope["path"]
            )
            if started:
                raise
            response = _api_error(500, "internal", "internal error")
            await response(scope, receive, send)


app.add_middleware(InternalErrorMiddleware)


API_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {"model": ApiError, "description": "A run is in progress on the thread."},
    500: {"model": ApiError, "description": "An unexpected error."},
    503: {
        "model": ApiError,
        "description": "Redis or the LLM provider is down, or the provider "
        "refused the request.",
    },
}


@app.post(
    "/v1/agent/run",
    tags=["agent"],
    operation_id="run_agent",
    summary="Run the agent on a thread",
    description=(
        "Send a user message, or confirm or reject a pending action, and run the "
        "agent until it answers, hits the tool-call limit, fails, or needs "
        "confirmation. Every completed run returns 200 whatever its `status`."
    ),
    responses=API_ERROR_RESPONSES,
)
async def run_agent(
    body: Annotated[AgentRequest, Body(openapi_examples=REQUEST_EXAMPLES)],
    runner: Annotated[AgentRunner, Depends(get_runner)],
    settings: SettingsDep,
) -> AgentResponse:
    try:
        return await runner.run(body)
    except ConfirmationStateError as exc:
        # SPEC-0002 B-0003: reported like any other invalid request.
        field = "confirm" if body.confirm is not None else "message"
        raise RequestValidationError(
            [
                {
                    "type": "confirmation_state",
                    "loc": ("body", field),
                    "msg": str(exc),
                    "input": body.model_dump(mode="json")[field],
                }
            ]
        ) from exc
    except Exception as exc:
        if reason := provider_rejection(exc, settings.llm_model):
            raise ProviderRejectedError(reason) from exc
        raise


@app.get(
    "/health",
    tags=["health"],
    operation_id="health_check",
    summary="Check the service is up",
    description="Returns ok without calling Redis or the LLM provider.",
)
async def health_check() -> HealthResponse:
    return HealthResponse()
