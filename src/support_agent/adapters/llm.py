"""Обращение к модели через LiteLLM. Ключи берутся из переменных окружения."""

import json
import time
from copy import deepcopy
from typing import Callable

from support_agent.models import Generation, Message, ToolCall, ToolSpec


def provider_message(message: Message) -> dict:
    result = {"role": message.role, "content": message.content}
    if message.role == "assistant":
        result["tool_calls"] = (
            [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    },
                }
                for call in message.tool_calls
            ]
            if message.tool_calls
            else None
        )
    elif message.role == "tool":
        result["tool_call_id"] = message.tool_call_id
        if message.error:
            result["content"] = json.dumps(
                {
                    "error": True,
                    "execution_status": message.execution_status,
                    "content": message.content,
                },
                ensure_ascii=False,
            )
    return result


class LiteLLMClient:
    def __init__(
        self,
        model: str,
        llm_args: dict | None = None,
        *,
        completion: Callable | None = None,
    ):
        self.model = model
        self.llm_args = deepcopy(llm_args or {})
        self._completion = completion

    def generate(self, messages: list[Message], tools: list[ToolSpec]) -> Generation:
        complete = self._completion
        if complete is None:
            from litellm import completion

            complete = completion
        args = deepcopy(self.llm_args)
        if self.model.startswith("openai/responses/") and tools:
            args["allowed_openai_params"] = list(
                dict.fromkeys(
                    [
                        *args.get("allowed_openai_params", []),
                        "tool_choice",
                    ]
                )
            )
        start = time.perf_counter()
        response = complete(
            model=self.model,
            messages=[provider_message(message) for message in messages],
            tools=[
                {"type": "function", "function": tool.model_dump()} for tool in tools
            ]
            or None,
            tool_choice="auto" if tools else None,
            **args,
        )
        elapsed = time.perf_counter() - start
        raw = response if isinstance(response, dict) else response.to_dict()
        choice = raw["choices"][0]
        if choice.get("finish_reason") not in ("stop", "tool_calls", "function_call"):
            raise ValueError(
                f"incomplete model response: {choice.get('finish_reason')}"
            )
        answer = choice["message"]
        calls = [
            ToolCall(
                id=call["id"],
                name=call["function"]["name"],
                arguments=json.loads(call["function"]["arguments"]),
            )
            for call in answer.get("tool_calls") or []
        ]
        message = Message(
            role=answer["role"], content=answer.get("content"), tool_calls=calls or None
        )
        if message.role != "assistant":
            raise ValueError("expected assistant response")
        # Если стоимость не получена, она остаётся неизвестной; оценка считается в отчёте.
        cost = getattr(response, "_hidden_params", {}).get("response_cost")
        return Generation(
            message=message,
            usage=raw.get("usage"),
            raw_data=raw,
            cost=cost,
            elapsed_seconds=elapsed,
        )
