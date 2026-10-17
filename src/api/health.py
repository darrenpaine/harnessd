"""GET /health (SPEC-0002 B-0006)."""

from fastapi import APIRouter

from schemas import HealthResponse

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    operation_id="health_check",
    summary="Check the service is up",
    description="Returns ok without calling Redis or the LLM provider.",
)
async def health_check() -> HealthResponse:
    return HealthResponse()
