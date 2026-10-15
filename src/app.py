"""FastAPI app and routes (SPEC-0002, SPEC-0007)."""

import asyncio
import tomllib
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import httpx
import redis.exceptions
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from langchain_core.exceptions import ModelConnectionError
from langgraph.graph.state import CompiledStateGraph
from redis.asyncio import Redis
from redisvl.exceptions import RedisSearchError

from agent.graph import AgentState, build_graph
from agent.llm import build_chat_model
from agent.persistence import RedisThreadGuard, ThreadBusyError, open_checkpointer
from agent.runner import AgentRunner, ConfirmationStateError, ThreadGuard
from schemas import (
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

REDIS_ERRORS = (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError)
LLM_ERRORS = (httpx.TransportError, ModelConnectionError)


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
        await self._stack.aclose()
        if self._redis is not None:
            await self._redis.aclose()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
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
    return _api_error(409, "thread_busy", "a run is already in progress on this thread")


async def redis_unavailable(request: Request, exc: Exception) -> JSONResponse:
    return _api_error(503, "dependency_unavailable", "Redis is unreachable")


async def wrapped_redis_error(request: Request, exc: Exception) -> JSONResponse:
    """The checkpointer's search layer wraps connection errors; unwrap them."""
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, REDIS_ERRORS):
            return await redis_unavailable(request, exc)
        cause = cause.__cause__ or cause.__context__
    raise exc


async def llm_unavailable(request: Request, exc: Exception) -> JSONResponse:
    return _api_error(503, "dependency_unavailable", "the LLM provider is unreachable")


for redis_error in REDIS_ERRORS:
    app.add_exception_handler(redis_error, redis_unavailable)
app.add_exception_handler(RedisSearchError, wrapped_redis_error)
for llm_error in LLM_ERRORS:
    app.add_exception_handler(llm_error, llm_unavailable)

API_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    409: {"model": ApiError, "description": "A run is in progress on the thread."},
    503: {"model": ApiError, "description": "Redis or the LLM provider is down."},
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
    body: AgentRequest, runner: Annotated[AgentRunner, Depends(get_runner)]
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


@app.get(
    "/health",
    tags=["health"],
    operation_id="health_check",
    summary="Check the service is up",
    description="Returns ok without calling Redis or the LLM provider.",
)
async def health_check() -> HealthResponse:
    return HealthResponse()
