"""The scripted fake chat model (SPEC-0005 B-0002)."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from fakes import ScriptedChatModel


@pytest.mark.spec("SPEC-0005", "B-0002", "AT-0002")
def test_scripted_model_replays_and_records() -> None:
    tool_reply = AIMessage(
        content="", tool_calls=[{"name": "web_search", "args": {"q": "x"}, "id": "1"}]
    )
    model = ScriptedChatModel(replies=[tool_reply, AIMessage(content="done")])

    first = model.invoke([HumanMessage(content="hi")])
    second = model.invoke([HumanMessage(content="again")])

    assert first.tool_calls[0]["name"] == "web_search"
    assert second.content == "done"
    assert [m[0].content for m in model.calls] == ["hi", "again"]
    with pytest.raises(AssertionError, match="only 2 replies scripted"):
        model.invoke([HumanMessage(content="third")])


@pytest.mark.spec("SPEC-0005", "B-0002")
async def test_scripted_model_async_and_bind_tools() -> None:
    model = ScriptedChatModel(replies=[AIMessage(content="ok")])
    bound = model.bind_tools([])
    reply = await bound.ainvoke([HumanMessage(content="hi")])
    assert reply.content == "ok"
    assert len(model.calls) == 1
