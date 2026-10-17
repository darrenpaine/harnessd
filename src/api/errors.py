"""Error responses: exception handlers and the 500 middleware (SPEC-0002).

Every error body is an ApiError. Handlers are registered on the app by
`add_error_handlers`, so routes raise and never build error responses.
"""

import logging
from typing import Any

import httpx
from fastapi import FastAPI, Request
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
from ollama import ResponseError as OllamaResponseError
from redisvl.exceptions import RedisSearchError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent.persistence import REDIS_ERRORS, ThreadBusyError
from agent.threads import (
    SummaryError,
    ThreadAwaitingConfirmationError,
    ThreadNotFoundError,
)
from api.deps import app_settings
from logs import event, get_logger
from schemas import ApiError, ApiErrorCode, ApiErrorDetail

log = get_logger("app")

LLM_ERRORS = (httpx.TransportError, ModelConnectionError, ModelTimeoutError)
# The provider answered but refused the request (SPEC-0002 B-0007). Handlers for
# the more specific LLM_ERRORS subclasses of ModelError take precedence.
PROVIDER_ERRORS = (ModelError, OllamaResponseError)

UNEXPECTED = "An unexpected error."
UNAVAILABLE = "Redis or the LLM provider is down, or the provider refused the request."
BUSY = "A run is in progress on the thread."
NOT_FOUND = "No such thread: unknown, expired or deleted."


def error_responses(descriptions: dict[int, str]) -> dict[int | str, dict[str, Any]]:
    """OpenAPI `responses` for a route's error codes, each with an ApiError body
    (SPEC-0007 B-0004)."""
    return {
        code: {"model": ApiError, "description": text}
        for code, text in descriptions.items()
    }


def api_error(status: int, code: ApiErrorCode, message: str) -> JSONResponse:
    body = ApiError(error=ApiErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status, content=body.model_dump())


def provider_rejection(exc: Exception, model: str) -> str:
    """Why the provider refused the request.

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
    return f"the LLM provider rejected the request for model {model!r}"


async def thread_busy(request: Request, exc: Exception) -> JSONResponse:
    event(log, logging.WARNING, "thread.busy", thread=exc.args[0] if exc.args else None)
    return api_error(409, "thread_busy", "a run is already in progress on this thread")


async def thread_not_found(request: Request, exc: Exception) -> JSONResponse:
    return api_error(404, "thread_not_found", "no such thread")


async def thread_awaiting_confirmation(
    request: Request, exc: Exception
) -> JSONResponse:
    return api_error(
        409,
        "thread_awaiting_confirmation",
        "the thread is awaiting confirmation; confirm or reject the action first",
    )


async def summary_failed(request: Request, exc: Exception) -> JSONResponse:
    """SPEC-0009 B-0008: our own fixed text, so no provider detail leaks."""
    return api_error(500, "internal", str(exc))


def _unavailable(cause: str) -> JSONResponse:
    """503 dependency_unavailable (SPEC-0002 B-0005, B-0007), logged as a warning."""
    event(log, logging.WARNING, "dependency.unavailable", cause=cause)
    return api_error(503, "dependency_unavailable", cause)


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


async def provider_rejected(request: Request, exc: Exception) -> JSONResponse:
    model = app_settings(request.app).llm_model
    return _unavailable(provider_rejection(exc, model))


def add_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ThreadBusyError, thread_busy)
    app.add_exception_handler(ThreadNotFoundError, thread_not_found)
    app.add_exception_handler(
        ThreadAwaitingConfirmationError, thread_awaiting_confirmation
    )
    app.add_exception_handler(SummaryError, summary_failed)
    for redis_error in REDIS_ERRORS:
        app.add_exception_handler(redis_error, redis_unavailable)
    app.add_exception_handler(RedisSearchError, wrapped_redis_error)
    for llm_error in LLM_ERRORS:
        app.add_exception_handler(llm_error, llm_unavailable)
    for provider_error in PROVIDER_ERRORS:
        app.add_exception_handler(provider_error, provider_rejected)
    app.add_middleware(InternalErrorMiddleware)


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
            response = api_error(500, "internal", "internal error")
            await response(scope, receive, send)
