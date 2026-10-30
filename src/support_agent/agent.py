"""Один шаг диалога. Вызов инструмента и отправку сообщения выполняет вызывающая сторона."""

from typing import Protocol

from jsonschema import ValidationError, validate

from support_agent.models import AgentState, Generation, Message, ToolSpec

# Для сопоставимости прогона сохранена инструкция исходного LLMAgent.
# Источник: Sierra Research tau2-bench, MIT; см. THIRD_PARTY_NOTICES.md.
AGENT_INSTRUCTION = """You are a customer service agent that helps the user according to the <policy> provided below.
In each turn you can either:
- Send a message to the user.
- Make a tool call.
You cannot do both at the same time.

Try to be helpful and always follow the policy. Always make sure you generate valid JSON only."""


class ModelClient(Protocol):
    def generate(
        self, messages: list[Message], tools: list[ToolSpec]
    ) -> Generation: ...


class AgentCore:
    def __init__(
        self,
        *,
        model: ModelClient,
        policy: str,
        tools: list[ToolSpec],
        language: str | None = None,
    ):
        self.model = model
        self.tools = [tool.model_copy(deep=True) for tool in tools]
        if len({tool.name for tool in tools}) != len(tools):
            raise ValueError("duplicate tool names")
        self.system_prompt = f"<instructions>\n{AGENT_INSTRUCTION}\n</instructions>\n<policy>\n{policy}\n</policy>"
        if language:
            self.system_prompt += f"\nResponse language: {language}."

    def init_state(self, history: list[Message] | None = None) -> AgentState:
        if any(message.role == "system" for message in history or []):
            raise ValueError("system instructions must come from policy, not history")
        return AgentState(messages=history or []).model_copy(deep=True)

    def step(
        self, incoming: Message | list[Message], state: AgentState
    ) -> tuple[Generation, AgentState]:
        messages = incoming if isinstance(incoming, list) else [incoming]
        if not messages or any(m.role not in ("user", "tool") for m in messages):
            raise ValueError("expected user message or tool results")
        pending = {call.id for m in state.messages for call in m.tool_calls or []}
        pending -= {m.tool_call_id for m in state.messages if m.role == "tool"}
        if any(m.role == "tool" for m in messages):
            ids = [m.tool_call_id for m in messages]
            if (
                any(m.role != "tool" for m in messages)
                or len(set(ids)) != len(ids)
                or set(ids) != pending
            ):
                raise ValueError(
                    "tool results must match all pending calls exactly once"
                )
        elif pending:
            raise ValueError(
                "pending tool results are required before the next user turn"
            )
        next_state = state.model_copy(deep=True)
        next_state.messages.extend(m.model_copy(deep=True) for m in messages)
        return self._generate(next_state)

    def continue_with_event(self, text: str, state: AgentState):
        """Продолжить после события приложения, не создавая реплику клиента."""
        next_state = state.model_copy(deep=True)
        pending = {c.id for m in next_state.messages for c in m.tool_calls or []}
        pending -= {m.tool_call_id for m in next_state.messages if m.role == "tool"}
        if pending:
            raise ValueError("Сначала нужны результаты предыдущих инструментов")
        next_state.messages.append(
            Message(
                role="assistant",
                content="Служебное событие приложения (не сообщение клиента): " + text,
            )
        )
        return self._generate(next_state)

    def _generate(self, next_state):
        """Общий вызов модели и проверка её ответа для обоих источников событий."""
        request = [
            Message(role="system", content=self.system_prompt),
            *next_state.messages,
        ]
        result = self.model.generate(
            [m.model_copy(deep=True) for m in request],
            [t.model_copy(deep=True) for t in self.tools],
        )
        if result.message.role != "assistant":
            raise ValueError("model must return assistant message")
        specs = {tool.name: tool for tool in self.tools}
        calls = result.message.tool_calls or []
        if len({c.id for c in calls}) != len(calls):
            raise ValueError("duplicate tool call ids")
        for call in calls:
            if call.name not in specs:
                raise ValueError(f"unknown tool: {call.name}")
            try:
                validate(call.arguments, specs[call.name].parameters)
            except ValidationError as exc:
                raise ValueError(f"invalid arguments for tool: {call.name}") from exc
        next_state.messages.append(result.message.model_copy(deep=True))
        return result, next_state
