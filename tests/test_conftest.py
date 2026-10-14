"""Test-suite rules from SPEC-0005."""

import os
import tomllib
from pathlib import Path

import pytest

import redis_support
from conftest import SECRET_ENV_VARS

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


@pytest.mark.spec("SPEC-0005", "B-0001", "AT-0001")
def test_provider_keys_cleared() -> None:
    for name in SECRET_ENV_VARS:
        assert name not in os.environ


@pytest.mark.spec("SPEC-0005", "B-0003", "AT-0003")
def test_redis_marker_policy() -> None:
    problem = redis_support.redis_problem("redis://localhost:1")
    assert problem is not None and "unreachable" in problem

    with pytest.raises(pytest.skip.Exception):
        redis_support.unavailable(problem, ci=False)
    with pytest.raises(pytest.fail.Exception):
        redis_support.unavailable(problem, ci=True)


@pytest.mark.spec("SPEC-0005", "B-0003")
@pytest.mark.parametrize(("value", "expected"), [("true", True), ("", False)])
def test_running_in_ci(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("CI", value)
    assert redis_support.running_in_ci() is expected


@pytest.mark.spec("SPEC-0005", "B-0004", "AT-0004")
def test_coverage_threshold_configured() -> None:
    coverage = tomllib.loads(PYPROJECT.read_text())["tool"]["coverage"]
    assert coverage["run"]["branch"] is True
    assert coverage["report"]["fail_under"] >= 80
