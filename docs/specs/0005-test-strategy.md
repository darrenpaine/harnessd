# SPEC-0005: Test strategy

Status: implemented
Requirements: R-0024
Design: [design.md](../design/design.md)

## Goal
Keep the test suite deterministic, fast and free to run: no real LLM calls, no API
keys in CI, real Redis only where persistence is what's being tested.

## Inputs / Outputs
### Test layers
| Layer | Covers | Model | Checkpointer | External HTTP |
|-------|--------|-------|--------------|---------------|
| Unit | schemas, settings, tools | none | none | mocked with `httpx.MockTransport` |
| Graph | agent loop (SPEC-0001) | scripted fake | LangGraph `InMemorySaver` | none |
| API | endpoints (SPEC-0002) | scripted fake | overridden via FastAPI dependency | none |
| Redis | persistence, TTL, locks (SPEC-0004) | scripted fake | real Redis | none |

### Shared fixtures
- `tests/fakes.py`: `ScriptedChatModel` replays a fixed list of AI messages (text or
  tool calls) in order and records every input it was given.
- `tests/conftest.py`: an autouse fixture that clears provider and Tavily keys; a
  `redis_url` fixture for `@pytest.mark.redis` tests.

### Running
- Locally: `docker compose up -d` for Redis, then `uv run pytest --cov=src ./`.
- CI: a `redis` service container; same pytest command.

## Behavior
- B-0001: Tests never call a real LLM or external HTTP service and need no API keys.
  Provider and Tavily keys are cleared for every test.
- B-0002: The scripted fake model returns its scripted messages in order, records the
  messages it received, and fails the test if called more times than scripted.
- B-0003: Tests that check persistence, TTL or locks use real Redis, never a
  checkpointer mock, and are marked `@pytest.mark.redis`. Locally they skip with a
  reason when Redis is unreachable; in CI (`CI=true`) unreachable Redis is a failure.
- B-0004: Branch coverage of `src/` must be at least 80%; below that the test run fails.
- B-0005: Every test covering a spec behavior carries a `spec` marker (SPEC-0006).

## Acceptance tests
- AT-0001 (B-0001): Inside a test, provider and Tavily key env vars are unset.
  -> `tests/test_conftest.py::test_provider_keys_cleared`
- AT-0002 (B-0002): ScriptedChatModel replays two messages, records inputs, and fails
  on a third call. -> `tests/test_fakes.py::test_scripted_model_replays_and_records`
- AT-0003 (B-0003): With Redis unreachable, a `redis` test skips when CI is unset and
  fails when CI=true. -> `tests/test_conftest.py::test_redis_marker_policy`
- AT-0004 (B-0004): `fail_under = 80` and `branch = true` are set in coverage config.
  -> `tests/test_conftest.py::test_coverage_threshold_configured`
- AT-0005 (B-0005): The traceability check reports no unmarked spec tests (SPEC-0006).
  -> `tests/scripts/test_trace.py::test_repo_has_no_marker_errors`

## Non-goals
Load testing, mutation testing, tests against real LLM providers (could be a separate,
manually-run suite later).

## Open questions
None.
