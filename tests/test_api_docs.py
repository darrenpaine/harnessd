"""Swagger UI, ReDoc and the OpenAPI schema (SPEC-0007)."""

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app import app
from export_openapi import OUTPUT, render
from schemas import REQUEST_EXAMPLES, AgentRequest, AgentResponse

ROOT = Path(__file__).resolve().parent.parent

OPERATIONS = {
    ("post", "/v1/agent/run"): ("agent", "run_agent"),
    ("get", "/health"): ("health", "health_check"),
}


def schema() -> dict[str, Any]:
    return app.openapi()


def ref_name(response: dict[str, Any]) -> str:
    ref: str = response["content"]["application/json"]["schema"]["$ref"]
    return ref.rsplit("/", 1)[-1]


@pytest.mark.spec("SPEC-0007", "B-0001", "AT-0001")
@pytest.mark.parametrize("path", ["/docs", "/redoc"])
def test_docs_pages_served(path: str) -> None:
    response = TestClient(app).get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "/openapi.json" in response.text


@pytest.mark.spec("SPEC-0007", "B-0002", "AT-0002")
def test_schema_metadata() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    info = schema()["info"]
    assert info["title"] == "harnessd"
    assert info["version"] == pyproject["project"]["version"]
    assert "docs/specs/README.md" in info["description"]


@pytest.mark.spec("SPEC-0007", "B-0003", "AT-0003")
def test_operations_documented() -> None:
    paths = schema()["paths"]
    found = {
        (method, path): op for path, ops in paths.items() for method, op in ops.items()
    }
    assert set(found) == set(OPERATIONS)
    for key, (tag, operation_id) in OPERATIONS.items():
        op = found[key]
        assert op["summary"] and op["description"]
        assert op["tags"] == [tag]
        assert op["operationId"] == operation_id
    ids = [op["operationId"] for op in found.values()]
    assert len(ids) == len(set(ids))


@pytest.mark.spec("SPEC-0007", "B-0004", "AT-0004")
def test_error_responses_documented() -> None:
    spec = schema()
    responses = spec["paths"]["/v1/agent/run"]["post"]["responses"]
    assert set(responses) == {"200", "409", "422", "503"}
    assert ref_name(responses["200"]) == "AgentResponse"
    assert ref_name(responses["409"]) == "ApiError"
    assert ref_name(responses["422"]) == "HTTPValidationError"
    assert ref_name(responses["503"]) == "ApiError"
    assert "ApiError" in spec["components"]["schemas"]
    health = spec["paths"]["/health"]["get"]["responses"]
    assert ref_name(health["200"]) == "HealthResponse"


@pytest.mark.spec("SPEC-0007", "B-0005", "AT-0005")
@pytest.mark.parametrize("model", [AgentRequest, AgentResponse])
def test_model_examples_validate(model: type[BaseModel]) -> None:
    json_schema = model.model_json_schema()
    for name, field in json_schema["properties"].items():
        assert field.get("description"), f"{model.__name__}.{name} has no description"
    examples = json_schema["examples"]
    for example in examples:
        model.model_validate(example)
    if model is AgentResponse:
        statuses = {example["status"] for example in examples}
        assert statuses == {"ok", "truncated", "error", "awaiting_confirmation"}
    else:
        assert any("message" in e for e in examples)
        assert any("confirm" in e for e in examples)


@pytest.mark.spec("SPEC-0007", "B-0005", "AT-0005")
def test_request_examples_offered() -> None:
    body = schema()["paths"]["/v1/agent/run"]["post"]["requestBody"]
    offered = body["content"]["application/json"]["examples"]
    assert set(offered) == {"new", "continue", "confirm"}
    for example in offered.values():
        assert example["summary"]
        AgentRequest.model_validate(example["value"])
    # One thread id, so the examples can be run in order on the same thread.
    ids = {example["value"]["thread_id"] for example in offered.values()}
    assert len(ids) == 1
    assert {"thread_id", "message"} <= set(offered["continue"]["value"])
    assert REQUEST_EXAMPLES.keys() == offered.keys()


@pytest.mark.spec("SPEC-0007", "B-0006", "AT-0006")
def test_committed_schema_up_to_date() -> None:
    assert json.loads(OUTPUT.read_text()) == schema(), "run `make openapi`"
    assert OUTPUT.read_text() == render(schema())
