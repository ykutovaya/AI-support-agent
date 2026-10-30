"""Пример приложения без API: синтетические записи и заранее заданные ответы модели."""

import json

from support_agent.agent import AgentCore, ModelClient
from support_agent.models import Generation, Message, ToolCall, ToolSpec

PARAMETERS = {
    "type": "object",
    "properties": {"order_id": {"type": "string"}},
    "required": ["order_id"],
    "additionalProperties": False,
}
TOOLS = [
    ToolSpec(name="get_order", description="Прочитать заказ", parameters=PARAMETERS),
    ToolSpec(
        name="request_cancellation",
        description="Предложить отмену заказа",
        parameters=PARAMETERS,
    ),
]


class DemonstrationModel:
    """Заданные ответы позволяют проверить подключение ядра; качество модели здесь не оценивается."""

    def __init__(self):
        self.replies = iter(
            [
                Message(
                    role="assistant",
                    tool_calls=[
                        ToolCall(
                            id="read",
                            name="get_order",
                            arguments={"order_id": "DEMO-1"},
                        )
                    ],
                ),
                Message(
                    role="assistant",
                    tool_calls=[
                        ToolCall(
                            id="proposal",
                            name="request_cancellation",
                            arguments={"order_id": "DEMO-1"},
                        )
                    ],
                ),
            ]
        )

    def generate(self, messages, tools):
        return Generation(message=next(self.replies))


def run_example(model: ModelClient | None = None) -> dict:
    core = AgentCore(
        model=model if model is not None else DemonstrationModel(),
        tools=TOOLS,
        policy="Проверяй статус заказа. Действие и ответ передай сотруднику для согласования.",
        language="ru",
    )
    state = core.init_state()
    incoming = Message(role="user", content="Хочу отменить заказ DEMO-1.")
    for _ in range(4):
        generation, state = core.step(incoming, state)
        message = generation.message
        if not message.tool_calls:
            return {
                "kind": "message_draft",
                "message": message.model_dump(),
                "executed": False,
                "sent": False,
            }
        # В примере доступно только учебное чтение; запись и отправка не подключены.
        if any(call.name != "get_order" for call in message.tool_calls):
            return {
                "kind": "action_proposal",
                "message": message.model_dump(),
                "executed": False,
                "sent": False,
            }
        incoming = [
            Message(
                role="tool",
                tool_call_id=call.id,
                content=json.dumps({"order_id": "DEMO-1", "status": "pending"})
                if call.arguments["order_id"] == "DEMO-1"
                else "Unknown order",
                error=call.arguments["order_id"] != "DEMO-1",
            )
            for call in message.tool_calls
        ]
    raise RuntimeError("example exceeded its step limit")


def main():
    print(
        "Учебный пример: данные и ответы модели заданы заранее; отправка и запись отключены."
    )
    print(json.dumps(run_example(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
