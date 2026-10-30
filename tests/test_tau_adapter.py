from types import SimpleNamespace

import pytest

pytest.importorskip("tau2")
from tau2.data_model.message import (
    AssistantMessage,
    MultiToolMessage,
    ToolMessage,
    UserMessage,
)
from tau2.environment.tool import Tool
from tau2.runner.build import build_agent
from test_core import FakeModel

from support_agent.adapters.tau_bench import TauAgent, register_agent
from support_agent.models import Generation, Message, ToolCall


def test_environment_alone_executes_tool_and_adapter_preserves_history():
    executed = []

    def lookup(ticket: str) -> str:
        """Прочитать обращение.

        Args:
            ticket: Идентификатор обращения.
        """
        executed.append(ticket)
        return "open"

    tool = Tool(lookup)
    model = FakeModel(
        Message(
            role="assistant",
            tool_calls=[ToolCall(id="c1", name="lookup", arguments={"ticket": "A"})],
        ),
        Message(role="assistant", content="Draft"),
    )
    observed = []
    adapter = TauAgent(
        [tool], "Policy", "fake", model_client=model, observer=observed.append
    )
    initial = adapter.get_init_state(
        [AssistantMessage(role="assistant", content="Hello")]
    )
    call, state = adapter.generate_next_message(
        UserMessage(role="user", content="Check A"), initial
    )
    assert executed == []
    assert call.content is None
    answer = tool(**call.tool_calls[0].arguments)  # Исполнение принадлежит среде.
    reply, final = adapter.generate_next_message(
        ToolMessage(role="tool", id="c1", content=answer), state
    )
    assert executed == ["A"]
    assert reply.content == "Draft" and reply.tool_calls is None
    assert [m.content for m in final.messages].count("Hello") == 1
    assert len(initial.messages) == 1
    assert adapter.get_init_state().messages == []
    assert len(observed) == 2
    assert observed[0]["response"]["message"]["tool_calls"][0]["id"] == "c1"
    assert observed[1]["request"]["messages"][-1]["content"] == "open"


@pytest.mark.parametrize("count", [1, 2])
def test_tool_errors_and_batch_results_keep_ids_and_status(count):
    model = FakeModel(Message(role="assistant", content="Need assistance"))
    adapter = TauAgent([], "Policy", "fake", model_client=model)
    from tau2.data_model.message import ToolCall as TauCall

    state = adapter.get_init_state(
        [
            AssistantMessage(
                role="assistant",
                tool_calls=[
                    TauCall(id=f"c{i}", name="lookup", arguments={})
                    for i in range(count)
                ],
            )
        ]
    )
    results = [
        ToolMessage(
            role="tool",
            id=f"c{i}",
            content="denied",
            error=True,
            execution_status="rejected",
        )
        for i in range(count)
    ]
    incoming = (
        results[0]
        if count == 1
        else MultiToolMessage(role="tool", tool_messages=results)
    )
    adapter.generate_next_message(incoming, state)
    seen = model.requests[0][0][-count:]
    assert [m.tool_call_id for m in seen] == [f"c{i}" for i in range(count)]
    assert all(m.error and m.execution_status == "rejected" for m in seen)


def test_factory_excludes_task_and_keeps_localized_policy(monkeypatch):
    model = FakeModel(Message(role="assistant", content="Draft"))
    monkeypatch.setattr(
        "support_agent.adapters.tau_bench.LiteLLMClient", lambda *a, **kw: model
    )
    register_agent("test_support_core")
    environment = SimpleNamespace(
        get_tools=lambda: [],
        get_policy=lambda: "VISIBLE_POLICY",
        get_domain_name=lambda: "retail",
    )
    task = SimpleNamespace(
        user_scenario="HIDDEN_USER", evaluation_criteria="HIDDEN_CRITERIA"
    )
    agent = build_agent(
        "test_support_core",
        environment,
        llm="fake",
        llm_args={},
        task=task,
        language_guidance="VISIBLE_LANGUAGE_GUIDANCE",
        language="en",
    )
    agent.generate_next_message(
        UserMessage(role="user", content="Hi"), agent.get_init_state()
    )
    request = str(model.requests)
    assert "VISIBLE_POLICY" in request and "VISIBLE_LANGUAGE_GUIDANCE" in request
    assert "HIDDEN_" not in request


def test_metadata_and_seed_reach_framework_and_model():
    class Model:
        llm_args = {}

        def generate(self, messages, tools):
            return Generation(
                message=Message(role="assistant", content="Draft"),
                cost=0.01,
                usage={"prompt_tokens": 10},
                raw_data={"model": "fake"},
                elapsed_seconds=0.2,
            )

    model = Model()
    agent = TauAgent([], "Policy", "fake", model_client=model)
    agent.set_seed(42)
    assert model.llm_args["seed"] == 42
    answer, _ = agent.generate_next_message(
        UserMessage(role="user", content="Hi"), agent.get_init_state()
    )
    assert answer.cost == 0.01 and answer.usage == {"prompt_tokens": 10}
    assert (
        answer.raw_data == {"model": "fake"} and answer.generation_time_seconds == 0.2
    )
