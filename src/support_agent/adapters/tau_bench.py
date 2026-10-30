"""Текстовый адаптер tau2. Инструменты исполняет испытательная среда."""

from tau2.agent.base import LLMConfigMixin
from tau2.agent.base_agent import HalfDuplexAgent
from tau2.agent.llm_agent import is_valid_agent_history_message
from tau2.data_model.message import (
    AssistantMessage,
    MultiToolMessage,
    ToolMessage,
    UserMessage,
)
from tau2.data_model.message import ToolCall as TauToolCall
from tau2.registry import registry

from support_agent.adapters.llm import LiteLLMClient
from support_agent.agent import AgentCore
from support_agent.models import AgentState, Message, ToolCall, ToolSpec


def to_core(message) -> Message:
    if isinstance(message, ToolMessage):
        if message.requestor != "assistant":
            raise ValueError("unexpected user tool result")
        return Message(
            role="tool",
            content=message.content,
            tool_call_id=message.id,
            error=message.error,
            execution_status=message.execution_status,
        )
    if not isinstance(message, (UserMessage, AssistantMessage)) or message.is_audio:
        raise ValueError("only text user/assistant messages are supported")
    return Message(
        role=message.role,
        content=message.content,
        tool_calls=[
            ToolCall(id=c.id, name=c.name, arguments=c.arguments)
            for c in message.tool_calls
        ]
        if message.tool_calls
        else None,
    )


class TauAgent(LLMConfigMixin, HalfDuplexAgent[AgentState]):
    def __init__(
        self,
        tools,
        domain_policy,
        llm,
        llm_args=None,
        *,
        model_client=None,
        additional_stop_tool_names=frozenset(),
        observer=None,
    ):
        super().__init__(
            tools=tools, domain_policy=domain_policy, llm=llm, llm_args=llm_args
        )
        self.additional_stop_tool_names = additional_stop_tool_names
        self.client = (
            model_client
            if model_client is not None
            else LiteLLMClient(llm, self.llm_args)
        )
        specs = [ToolSpec(**tool.openai_schema["function"]) for tool in tools]
        # Переданные средой правила уже содержат указания о выбранном языке.
        self.core = AgentCore(model=self.client, policy=domain_policy, tools=specs)
        self.observer = observer

    def set_seed(self, seed):
        super().set_seed(seed)
        if hasattr(self.client, "llm_args"):
            self.client.llm_args["seed"] = seed

    def get_init_state(self, message_history=None):
        history = message_history or []
        if not all(is_valid_agent_history_message(m) for m in history):
            raise ValueError("invalid initial agent history")
        return self.core.init_state([to_core(message) for message in history])

    def generate_next_message(self, message, state):
        incoming = (
            [to_core(m) for m in message.tool_messages]
            if isinstance(message, MultiToolMessage)
            else to_core(message)
        )
        generation, next_state = self.core.step(incoming, state)
        answer = generation.message
        if self.observer:
            self.observer(
                {
                    "request": {
                        "messages": [
                            Message(
                                role="system", content=self.core.system_prompt
                            ).model_dump(),
                            *[m.model_dump() for m in next_state.messages[:-1]],
                        ],
                        "tools": [tool.model_dump() for tool in self.core.tools],
                    },
                    "response": generation.model_dump(mode="json"),
                }
            )
        return AssistantMessage(
            role="assistant",
            content=answer.content,
            tool_calls=[
                TauToolCall(id=c.id, name=c.name, arguments=c.arguments)
                for c in answer.tool_calls
            ]
            if answer.tool_calls
            else None,
            usage=generation.usage,
            cost=generation.cost,
            raw_data=generation.raw_data,
            generation_time_seconds=generation.elapsed_seconds,
        ), next_state

    def is_stop(self, message):
        return any(
            call.name in self.additional_stop_tool_names
            for call in message.tool_calls or []
        )


def register_agent(name="support_agent", observer=None):
    def factory(tools, domain_policy, **kwargs):
        # build_agent также передаёт Task со скрытыми данными и настройки аудио.
        # В ядро передаются только явно перечисленные настройки модели.
        return TauAgent(
            tools,
            domain_policy,
            kwargs.get("llm"),
            kwargs.get("llm_args"),
            additional_stop_tool_names=kwargs.get(
                "additional_stop_tool_names", frozenset()
            ),
            observer=observer,
        )

    registry.register_agent_factory(factory, name)
