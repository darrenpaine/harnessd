"""GET, DELETE and compact on /v1/threads (SPEC-0009).

Unit tests drive the API over an in-memory graph and a scripted model; tests
marked `redis` use the Redis checkpointer and guard (SPEC-0005 B-0003).
"""

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
from langchain_core.exceptions import ModelRateLimitError
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from redis.asyncio import Redis

from agent.graph import SYSTEM_PROMPT
from agent.persistence import (
    RedisThreadGuard,
    checkpoint_keys,
    open_checkpointer,
    ttl_seconds,
)
from agent_support import Capture, MakeClient, fetch, note, tool_turn
from api.deps import get_model
from app import app
from fakes import RaisingChatModel, ScriptedChatModel
from logs import ROOT
from settings import Settings, get_settings
from tools.base import ToolContext

RUN = "/v1/agent/run"


def thread(thread_id: str, action: str = "") -> str:
    return f"/v1/threads/{thread_id}{action}"


async def run(client: httpx.AsyncClient, message: str, thread_id: str = "") -> str:
    """Send a message; return the thread id."""
    body: dict[str, Any] = {"message": message}
    if thread_id:
        body["thread_id"] = thread_id
    response = await client.post(RUN, json=body)
    assert response.status_code == 200, response.text
    thread_id = response.json()["thread_id"]
    assert isinstance(thread_id, str)
    return thread_id


async def turns(client: httpx.AsyncClient, *messages: str) -> str:
    """Run one turn per message on a new thread; return its id."""
    thread_id = await run(client, messages[0])
    for message in messages[1:]:
        await run(client, message, thread_id)
    return thread_id


def replies(*texts: str) -> list[AIMessage]:
    return [AIMessage(content=text) for text in texts]


@pytest.fixture
def redis_settings(tool_context: ToolContext, redis_url: str) -> Settings:
    return tool_context.settings.model_copy(update={"redis_url": redis_url})


@pytest.fixture
async def redis(redis_url: str) -> AsyncGenerator[Redis]:
    client = Redis.from_url(redis_url)
    yield client
    await client.aclose()


async def key_ttls(redis: Redis, thread_id: str) -> dict[bytes, int]:
    return {k: await redis.pttl(k) async for k in checkpoint_keys(redis, thread_id)}


@pytest.mark.spec("SPEC-0009", "B-0001", "AT-0001")
async def test_get_thread_state(make_client: MakeClient) -> None:
    search = tool_turn(("web_search", {"query": "news"}))
    client, _ = make_client([search, AIMessage(content="done"), tool_turn(note("x"))])
    thread_id = await run(client, "search")

    response = await client.get(thread(thread_id))

    assert response.status_code == 200
    state = response.json()
    assert state["thread_id"] == thread_id
    assert state["status"] == "idle"
    assert state["pending_action"] is None
    assert state["summary"] is None
    assert state["expires_in_seconds"] is None  # the in-memory store never expires
    messages = state["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]
    assert messages[0]["content"] == "search"
    [call] = messages[1]["tool_calls"]
    assert call == {
        "id": "call-web_search-0",
        "name": "web_search",
        "args": {"query": "news"},
    }
    assert messages[2]["tool_call_id"] == "call-web_search-0"
    assert messages[3]["content"] == "done"
    assert "You are" not in response.text  # no system prompt

    paused_id = await run(client, "note x")
    paused = (await client.get(thread(paused_id))).json()
    assert paused["status"] == "awaiting_confirmation"
    assert paused["pending_action"] == {"tool": "add_note", "input": {"text": "x"}}


@pytest.mark.spec("SPEC-0009", "B-0001", "AT-0002")
async def test_get_unknown_thread(make_client: MakeClient) -> None:
    client, _ = make_client([])

    unknown = await client.get(thread(str(uuid.uuid4())))
    invalid = await client.get(thread("not-a-uuid"))

    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "thread_not_found"
    assert invalid.status_code == 422


@pytest.mark.redis
@pytest.mark.spec("SPEC-0009", "B-0001", "AT-0003")
async def test_get_does_not_touch_ttl(
    make_client: MakeClient, redis_settings: Settings, redis: Redis
) -> None:
    guard = RedisThreadGuard(redis, redis_settings)
    async with open_checkpointer(redis_settings) as saver:
        client, _ = make_client(replies("hi"), checkpointer=saver, guard=guard)
        thread_id = await run(client, "hello")
        before = await key_ttls(redis, thread_id)
        await asyncio.sleep(1.1)

        response = await client.get(thread(thread_id))
        after = await key_ttls(redis, thread_id)

    assert response.status_code == 200
    assert before and after.keys() == before.keys()
    # Each TTL kept counting down; a reset would have put it back near the limit.
    assert all(after[key] <= before[key] - 1000 for key in before)
    expires = response.json()["expires_in_seconds"]
    assert 0 < expires <= ttl_seconds(redis_settings)


@pytest.mark.redis
@pytest.mark.spec("SPEC-0009", "B-0002", "AT-0004")
async def test_delete_thread(
    make_client: MakeClient, redis_settings: Settings, redis: Redis
) -> None:
    guard = RedisThreadGuard(redis, redis_settings)
    async with open_checkpointer(redis_settings) as saver:
        client, agent = make_client(
            replies("first", "again"), checkpointer=saver, guard=guard
        )
        thread_id = await run(client, "hello")
        assert [k async for k in redis.scan_iter(match=f"*{thread_id}*")]

        deleted = await client.delete(thread(thread_id))
        remaining = [k async for k in redis.scan_iter(match=f"*{thread_id}*")]
        after = await client.get(thread(thread_id))
        await run(client, "new start", thread_id)
        missing = await client.delete(thread(str(uuid.uuid4())))

    assert deleted.status_code == 204
    assert deleted.content == b""
    assert remaining == []  # checkpoint keys and the lock are all gone
    assert after.status_code == 404
    assert [m.content for m in agent.model.calls[-1][1:]] == ["new start"]
    assert missing.status_code == 404


@pytest.mark.spec("SPEC-0009", "B-0003", "AT-0005")
async def test_busy_thread_409(make_client: MakeClient) -> None:
    client, agent = make_client(replies("first", "second"), delay=0.2)
    thread_id = await run(client, "one")
    before = (await client.get(thread(thread_id))).json()

    second = asyncio.create_task(run(client, "two", thread_id))
    await asyncio.sleep(0.05)
    delete = await client.delete(thread(thread_id))
    compact = await client.post(thread(thread_id, "/compact"), json={"keep_turns": 0})
    await second

    for response in (delete, compact):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "thread_busy"
    after = (await client.get(thread(thread_id))).json()["messages"]
    assert after[:-2] == before["messages"]
    assert [(m["role"], m["content"]) for m in after[-2:]] == [
        ("user", "two"),
        ("assistant", "second"),
    ]
    assert len(agent.model.calls) == 2  # no summary call was made


@pytest.mark.spec("SPEC-0009", "B-0003")
async def test_run_during_compaction_409(make_client: MakeClient) -> None:
    client, agent = make_client(replies("a", "b"))
    thread_id = await turns(client, "one", "two")
    app.dependency_overrides[get_model] = lambda: ScriptedChatModel(
        replies=replies("summary"), delay=0.2
    )

    compact = asyncio.create_task(
        client.post(thread(thread_id, "/compact"), json={"keep_turns": 1})
    )
    await asyncio.sleep(0.05)
    busy = await client.post(RUN, json={"thread_id": thread_id, "message": "three"})

    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "thread_busy"
    assert (await compact).status_code == 200


@pytest.mark.spec("SPEC-0009", "B-0004", "B-0005", "AT-0006")
async def test_compact_thread(make_client: MakeClient) -> None:
    script = [
        tool_turn(fetch("a")),
        *replies("a", "b", "c"),
        tool_turn(fetch("d")),
        AIMessage(content="d"),
        AIMessage(content="the summary"),
        AIMessage(content="e"),
    ]
    client, agent = make_client(script)
    thread_id = await turns(client, "one", "two", "three", "four")

    response = await client.post(thread(thread_id, "/compact"), json={"keep_turns": 1})

    assert response.status_code == 200
    assert response.json() == {
        "thread_id": thread_id,
        "compacted": True,
        "messages_removed": 8,
        "summary": "the summary",
    }
    # The summary call saw every removed message and none of the kept turn.
    summary_input = str(agent.model.calls[-1][-1].content)
    for text in ("one", "a", "two", "b", "three", "c"):
        assert f": {text}\n" in summary_input + "\n"
    assert "assistant called fetch_site with" in summary_input
    assert "four" not in summary_input

    state = (await client.get(thread(thread_id))).json()
    assert state["summary"] == "the summary"
    # The last turn is kept whole: the call and its result stay together.
    assert [m["role"] for m in state["messages"]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert state["messages"][0]["content"] == "four"

    await run(client, "five", thread_id)
    system, *history = agent.model.calls[-1]
    assert isinstance(system, SystemMessage)
    # The full system prompt is kept; the summary only follows it.
    assert system.content == (
        f"{SYSTEM_PROMPT}\n\nSummary of the earlier conversation: the summary"
    )
    users = [m.content for m in history if isinstance(m, HumanMessage)]
    assert users == ["four", "five"]


@pytest.mark.spec("SPEC-0009", "B-0004", "AT-0007")
async def test_compact_includes_previous_summary(make_client: MakeClient) -> None:
    script = [*replies("a", "b", "first summary", "c", "second summary")]
    client, agent = make_client(script)
    thread_id = await turns(client, "one", "two")
    compact = thread(thread_id, "/compact")

    first = await client.post(compact, json={"keep_turns": 1})
    await run(client, "three", thread_id)
    second = await client.post(compact, json={"keep_turns": 1})

    assert first.json()["summary"] == "first summary"
    assert second.json()["summary"] == "second summary"
    summary_input = str(agent.model.calls[-1][-1].content)
    assert "Earlier summary: first summary" in summary_input
    assert "two" in summary_input


@pytest.mark.spec("SPEC-0009", "B-0006", "AT-0008")
async def test_compact_nothing_to_do(make_client: MakeClient) -> None:
    client, agent = make_client(replies("a"))
    thread_id = await run(client, "one")
    before = (await client.get(thread(thread_id))).json()

    response = await client.post(thread(thread_id, "/compact"))

    assert response.status_code == 200
    assert response.json() == {
        "thread_id": thread_id,
        "compacted": False,
        "messages_removed": 0,
        "summary": None,
    }
    assert len(agent.model.calls) == 1
    assert (await client.get(thread(thread_id))).json() == before


@pytest.mark.spec("SPEC-0009", "B-0004")
async def test_compact_keep_zero_and_bounds(make_client: MakeClient) -> None:
    client, _ = make_client([*replies("a", "the summary")])
    thread_id = await run(client, "one")
    compact = thread(thread_id, "/compact")

    for keep_turns in (-1, 21):
        invalid = await client.post(compact, json={"keep_turns": keep_turns})
        assert invalid.status_code == 422
    response = await client.post(compact, json={"keep_turns": 0})

    assert response.json()["messages_removed"] == 2
    state = (await client.get(thread(thread_id))).json()
    assert state["messages"] == [] and state["summary"] == "the summary"


@pytest.mark.spec("SPEC-0009", "B-0007", "AT-0009")
async def test_compact_awaiting_confirmation(make_client: MakeClient) -> None:
    client, agent = make_client([AIMessage(content="a"), tool_turn(note("x"))])
    thread_id = await turns(client, "one", "note x")

    response = await client.post(thread(thread_id, "/compact"), json={"keep_turns": 0})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "thread_awaiting_confirmation"
    assert len(agent.model.calls) == 2


@pytest.mark.spec("SPEC-0009", "B-0008", "AT-0010")
@pytest.mark.parametrize(
    ("failure", "status"), [("timeout", 500), ("provider", 503), ("empty", 500)]
)
async def test_compact_failure_leaves_state(
    make_client: MakeClient,
    tool_context: ToolContext,
    capture: Capture,
    caplog: pytest.LogCaptureFixture,
    failure: str,
    status: int,
) -> None:
    client, _ = make_client(replies("a", "b"))
    thread_id = await turns(client, "one", "two")
    before = (await client.get(thread(thread_id))).json()
    capture("info")
    if failure == "timeout":
        fast = tool_context.settings.model_copy(update={"run_timeout_seconds": 0.05})
        app.dependency_overrides[get_settings] = lambda: fast
        model: Any = ScriptedChatModel(replies=replies("late"), delay=1)
    elif failure == "provider":
        model = RaisingChatModel(error=ModelRateLimitError("slow down"))
    else:
        model = ScriptedChatModel(replies=replies(""))
    app.dependency_overrides[get_model] = lambda: model

    response = await client.post(thread(thread_id, "/compact"), json={"keep_turns": 1})

    assert response.status_code == status
    expected = "dependency_unavailable" if status == 503 else "internal"
    assert response.json()["error"]["code"] == expected
    assert (await client.get(thread(thread_id))).json() == before
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.name.startswith(ROOT) and r.levelno == logging.WARNING
    ]
    if failure == "provider":
        assert not any(w.startswith("thread.compact_failed") for w in warnings)
    else:
        [failed] = [w for w in warnings if w.startswith("thread.compact_failed ")]
        assert f" thread={thread_id} reason={failure}" in failed


@pytest.mark.spec("SPEC-0009", "B-0011", "AT-0013")
async def test_compact_unfinished_run(
    make_client: MakeClient, tool_context: ToolContext
) -> None:
    client, agent = make_client(replies("one", "two", "the summary", "three"))
    thread_id = await run(client, "first")
    fast = tool_context.settings.model_copy(update={"run_timeout_seconds": 0.1})
    app.dependency_overrides[get_settings] = lambda: fast
    agent.model.delay = 1
    timed_out = await client.post(RUN, json={"thread_id": thread_id, "message": "slow"})
    assert timed_out.json()["status"] == "error"
    agent.model.delay = 0
    before = (await client.get(thread(thread_id))).json()
    compact = thread(thread_id, "/compact")

    refused = await client.post(compact, json={"keep_turns": 1})

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "thread_run_unfinished"
    assert (await client.get(thread(thread_id))).json() == before

    await run(client, "again", thread_id)
    done = await client.post(compact, json={"keep_turns": 1})
    assert done.status_code == 200 and done.json()["compacted"] is True
    await run(client, "after", thread_id)
    system = agent.model.calls[-1][0]
    assert str(system.content).endswith("earlier conversation: the summary")


@pytest.mark.spec("SPEC-0009", "B-0009", "AT-0012")
async def test_thread_events_logged(
    make_client: MakeClient, capture: Capture, caplog: pytest.LogCaptureFixture
) -> None:
    client, _ = make_client(replies("a", "b", "the summary"))
    thread_id = await turns(client, "one", "two")
    capture("info")
    caplog.clear()

    await client.get(thread(thread_id))
    await client.post(thread(thread_id, "/compact"), json={"keep_turns": 1})
    await client.delete(thread(thread_id))

    events = [
        r.getMessage().split(" ")
        for r in caplog.records
        if r.name.startswith(ROOT) and r.levelno == logging.INFO
    ]
    assert [e[0] for e in events] == [
        "thread.read",
        "thread.compacted",
        "thread.deleted",
    ]
    for fields in events:
        assert f"thread={thread_id}" in fields
    compacted = dict(f.split("=", 1) for f in events[1][1:])
    assert compacted["messages_removed"] == "2"
    assert int(compacted["ms"]) >= 0
