import copy

import pytest

from support_agent.adapters.llm import LiteLLMClient
from support_agent.models import Message, ToolCall, ToolSpec


def response(message, finish="stop"):
    return {
        "model": "provider-model",
        "choices": [{"finish_reason": finish, "message": message}],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 4,
            "prompt_tokens_details": {"cached_tokens": 5},
        },
    }


def test_provider_roundtrip_preserves_calls_usage_and_configuration():
    requests = []

    def completion(**kwargs):
        requests.append(copy.deepcopy(kwargs))
        return response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "next",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"ticket":"B"}'},
                    }
                ],
            },
            "tool_calls",
        )

    client = LiteLLMClient(
        "openai/responses/example",
        {"reasoning_effort": "low", "max_tokens": 8192},
        completion=completion,
    )
    result = client.generate(
        [
            Message(role="system", content="Policy"),
            Message(
                role="assistant",
                tool_calls=[
                    ToolCall(id="old", name="lookup", arguments={"ticket": "A"})
                ],
            ),
            Message(
                role="tool",
                tool_call_id="old",
                content="denied",
                error=True,
                execution_status="rejected",
            ),
        ],
        [ToolSpec(name="lookup", description="Read", parameters={"type": "object"})],
    )
    assert result.message.tool_calls[0].arguments == {"ticket": "B"}
    assert result.usage["prompt_tokens_details"]["cached_tokens"] == 5
    assert result.raw_data["model"] == "provider-model"
    assert result.cost is None  # Неизвестная стоимость не означает бесплатный запрос.
    sent = requests[0]
    assert sent["messages"][1]["tool_calls"][0]["id"] == "old"
    assert sent["messages"][2]["tool_call_id"] == "old"
    assert '"execution_status": "rejected"' in sent["messages"][2]["content"]
    assert sent["tool_choice"] == "auto"
    assert "tool_choice" in sent["allowed_openai_params"]
    assert sent["reasoning_effort"] == "low"
    assert sent["max_tokens"] == 8192


@pytest.mark.parametrize(
    "message,finish",
    [
        ({"role": "assistant", "content": "partial"}, "length"),
        ({"role": "assistant", "content": ""}, "stop"),
        (
            {
                "role": "assistant",
                "content": "done",
                "tool_calls": [
                    {"id": "a", "function": {"name": "write", "arguments": "{}"}}
                ],
            },
            "tool_calls",
        ),
        (
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "a",
                        "function": {"name": "write", "arguments": "broken JSON"},
                    }
                ],
            },
            "tool_calls",
        ),
    ],
)
def test_invalid_or_truncated_response_is_not_an_action(message, finish):
    client = LiteLLMClient("model", completion=lambda **kw: response(message, finish))
    with pytest.raises(ValueError):
        client.generate([Message(role="user", content="Hi")], [])


def test_transport_failure_is_propagated_without_fabricating_a_reply():
    def offline(**kwargs):
        raise TimeoutError("offline")

    client = LiteLLMClient("model", completion=offline)
    with pytest.raises(TimeoutError):
        client.generate([Message(role="user", content="Hi")], [])
