import copy

import pytest
from pydantic import ValidationError

from support_agent.agent import AgentCore
from support_agent.models import AgentState, Generation, Message, ToolCall, ToolSpec


class FakeModel:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.requests = []

    def generate(self, messages, tools):
        self.requests.append(copy.deepcopy((messages, tools)))
        return Generation(message=next(self.replies))


TOOL = ToolSpec(
    name="lookup",
    description="Read a ticket",
    parameters={
        "type": "object",
        "properties": {"ticket": {"type": "string"}},
        "required": ["ticket"],
        "additionalProperties": False,
    },
)


@pytest.mark.parametrize(
    "fields",
    [
        {"role": "assistant", "content": ""},
        {"role": "assistant", "tool_calls": []},
        {
            "role": "assistant",
            "content": "Done",
            "tool_calls": [
                {"id": "c1", "name": "lookup", "arguments": {"ticket": "a"}}
            ],
        },
        {"role": "tool", "content": "result"},
        {
            "role": "user",
            "content": "Hi",
            "tool_calls": [
                {"id": "c1", "name": "lookup", "arguments": {"ticket": "a"}}
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": "denied",
            "execution_status": "rejected",
            "error": False,
        },
    ],
)
def test_invalid_messages_cannot_cross_the_boundary(fields):
    with pytest.raises(ValidationError):
        Message(**fields)


@pytest.mark.parametrize(
    "error,status,content",
    [
        (False, "executed", "ticket is open"),
        (True, "executed", "upstream unavailable"),
        (True, "rejected", "access denied"),
    ],
)
def test_tool_cycle_preserves_result_and_does_not_mutate_previous_state(
    error, status, content
):
    model = FakeModel(
        Message(
            role="assistant",
            tool_calls=[ToolCall(id="c1", name="lookup", arguments={"ticket": "a"})],
        ),
        Message(role="assistant", content="Draft response"),
    )
    core = AgentCore(
        model=model, policy="Use authorized records", tools=[TOOL], language="ru"
    )
    initial = core.init_state()
    first, state = core.step(Message(role="user", content="Check ticket a"), initial)
    assert first.message.tool_calls[0].id == "c1"
    assert initial.messages == []
    result = Message(
        role="tool",
        tool_call_id="c1",
        content=content,
        error=error,
        execution_status=status,
    )
    second, final = core.step(result, state)
    assert second.message.content == "Draft response"
    assert len(state.messages) == 2
    assert len(final.messages) == 4
    sent = model.requests[-1][0]
    assert sent[-1] == result
    assert "Use authorized records" in sent[0].content
    assert "ru" in sent[0].content
    assert model.requests[-1][1] == [TOOL]


def test_histories_are_copied_and_conversations_are_isolated():
    model = FakeModel(
        Message(role="assistant", content="First"),
        Message(role="assistant", content="Second"),
    )
    core = AgentCore(model=model, policy="Rules", tools=[])
    history = [Message(role="assistant", content="Hello")]
    first = core.init_state(history)
    second = core.init_state()
    history[0].content = "changed outside"
    core.step(Message(role="user", content="PRIVATE_A"), first)
    core.step(Message(role="user", content="Customer B"), second)
    assert first.messages[0].content == "Hello"
    assert "PRIVATE_A" not in str(model.requests[-1])
    assert second.messages == []
    assert AgentState.model_validate_json(first.model_dump_json()) == first


@pytest.mark.parametrize(
    "call",
    [
        ToolCall(id="x", name="delete_everything", arguments={}),
        ToolCall(id="x", name="lookup", arguments={"ticket": 3}),
    ],
)
def test_unknown_tool_or_invalid_arguments_are_rejected(call):
    core = AgentCore(
        model=FakeModel(Message(role="assistant", tool_calls=[call])),
        policy="Rules",
        tools=[TOOL],
    )
    with pytest.raises(ValueError):
        core.step(Message(role="user", content="Hi"), core.init_state())


def test_unmatched_tool_result_is_rejected_before_model_call():
    model = FakeModel()
    core = AgentCore(model=model, policy="Rules", tools=[])
    with pytest.raises(ValueError, match="tool"):
        core.step(
            Message(role="tool", tool_call_id="unknown", content="ok"),
            core.init_state(),
        )
    assert model.requests == []
