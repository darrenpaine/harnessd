"""The FastAPI app: metadata, lifespan, routers and error handling (SPEC-0002,
SPEC-0007). Routes live in `api/`."""

import asyncio
import tomllib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI

from agent.screen import preload
from api import agent, health, threads
from api.deps import Services, app_settings
from api.errors import add_error_handlers
from api.request_id import RequestIdMiddleware
from logs import configure_logging
from tracing import configure_tracing, instrument_app

SPECS_URL = "https://github.com/darrenpaine/harnessd/blob/main/docs/specs/README.md"

DESCRIPTION = (
    "A LangGraph agent with three tools (web search, fetch a page, add a note) "
    "behind one endpoint, plus endpoints to read, delete and compact a thread. "
    "Every run returns a validated `AgentResponse`; conversation state is kept "
    "per thread in Redis, and side-effecting tools wait for confirmation. "
    f"Behavior is specified in [the specs]({SPECS_URL})."
)


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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = app_settings(app)
    configure_logging(settings)
    tracing = configure_tracing(settings)
    services = Services()
    app.state.services = services
    # SPEC-0012 B-0017: load the screen's model without holding up startup.
    loading = asyncio.create_task(preload(services.screen(settings)))
    yield
    loading.cancel()
    with suppress(asyncio.CancelledError):
        await loading
    await services.aclose()
    tracing.shutdown()


app = FastAPI(
    title="harnessd",
    version=project_version(),
    description=DESCRIPTION,
    lifespan=lifespan,
)
add_error_handlers(app)
app.add_middleware(RequestIdMiddleware)  # outside the error handling
app.include_router(agent.router)
app.include_router(threads.router)
app.include_router(health.router)
instrument_app(app)  # outermost, so error responses are traced too
