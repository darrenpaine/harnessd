"""The shared API key (SPEC-0013 B-0001, B-0002)."""

from secrets import compare_digest
from typing import Annotated

from fastapi import Security
from fastapi.security import APIKeyHeader

from api.deps import SettingsDep

api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description="Required on `/v1/*` when the service has `API_KEY` set.",
)


class UnauthorizedError(Exception):
    """The request has no API key, or the wrong one."""


def require_api_key(
    settings: SettingsDep,
    key: Annotated[str | None, Security(api_key_header)] = None,
) -> None:
    """Refuse the request unless it carries the service's key. With no key
    set, every request passes. Runs before a route's other dependencies, so a
    refused request reaches neither Redis nor the model."""
    if settings.api_key is None:
        return
    expected = settings.api_key.get_secret_value().encode()
    if key is None or not compare_digest(key.encode(), expected):
        raise UnauthorizedError
