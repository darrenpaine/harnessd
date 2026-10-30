"""POST /v1/agent/run (SPEC-0002)."""

from typing import Annotated

from fastapi import APIRouter, Body, Depends
from fastapi.exceptions import RequestValidationError

from agent.agent_threads import ConfirmationStateError
from agent.runner import AgentRunner
from api.deps import get_runner
from api.errors import BUSY, UNAVAILABLE, UNEXPECTED, error_responses
from schemas import REQUEST_EXAMPLES, AgentRequest, AgentResponse

router = APIRouter(tags=["agent"])


@router.post(
    "/v1/agent/run",
    operation_id="run_agent",
    summary="Run the agent on a thread",
    description=(
        "Send a user message, or confirm or reject a pending action, and run the "
        "agent until it answers, hits the tool-call limit, fails, or needs "
        "confirmation. Every completed run returns 200 whatever its `status`."
    ),
    responses=error_responses(
        {
            409: BUSY,
            500: UNEXPECTED,
            503: UNAVAILABLE,
        }
    ),
)
async def run_agent(
    body: Annotated[AgentRequest, Body(openapi_examples=REQUEST_EXAMPLES)],
    runner: Annotated[AgentRunner, Depends(get_runner)],
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
