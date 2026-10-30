"""Собственные типы приложения без зависимости от типов tau2 и провайдера модели."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolCall(Contract):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, Any]


class ToolSpec(Contract):
    name: str = Field(min_length=1)
    description: str
    parameters: dict[str, Any]


class Message(Contract):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    error: bool = False
    execution_status: Literal["executed", "rejected"] = "executed"

    @model_validator(mode="after")
    def valid_shape(self):
        has_text = bool(self.content and self.content.strip())
        if self.role == "assistant":
            if has_text == bool(self.tool_calls) or self.tool_calls == []:
                raise ValueError(
                    "assistant must return text or nonempty tool calls, exclusively"
                )
        elif self.tool_calls is not None:
            raise ValueError("only assistant may request tools")
        elif self.role != "tool" and not has_text:
            raise ValueError("message requires text")
        if self.role == "tool":
            if not self.tool_call_id:
                raise ValueError("tool result requires its call id")
            if self.execution_status == "rejected" and not self.error:
                raise ValueError("rejected tool result must carry error=True")
        elif self.tool_call_id or self.error or self.execution_status != "executed":
            raise ValueError("tool metadata belongs only to a tool result")
        return self


class AgentState(Contract):
    messages: list[Message] = Field(default_factory=list)


class Generation(Contract):
    message: Message
    usage: dict[str, Any] | None = None
    raw_data: dict[str, Any] | None = None
    cost: float | None = None
    elapsed_seconds: float | None = None
