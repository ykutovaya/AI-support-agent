"""Учебная модель читает только диалог и проходит общий процесс приложения."""

import pytest

pytest.importorskip("tau2")

from support_agent.adapters.demo_model import DemoModel
from support_agent.adapters.retail import RetailAdapter
from support_agent.models import Message, ToolSpec
from support_agent.storage import Store
from support_agent.workflow import SupportService


def test_demo_requests_email_and_describes_its_limited_mode():
    model = DemoModel()
    result = model.generate([Message(role="user", content="Здравствуйте")], [])
    assert result.message.tool_calls is None
    assert "учебн" in result.message.content.lower()
    assert "Email" in result.message.content
    assert result.raw_data["synthetic"] is True


@pytest.mark.parametrize(
    "customer_text,expected",
    [
        (
            "Обменяйте товар на любой другой доступный вариант. Email user1@example.test",
            "exchange requested",
        ),
        ("Хочу вернуть товар. Email user1@example.test", "return requested"),
    ],
)
def test_demo_uses_tool_results_and_never_approves_or_executes(
    tmp_path, snapshot, customer_text, expected
):
    store = Store(tmp_path / "demo.sqlite3")
    # Идентификаторы не совпадают с исходными задачами и извлекаются из чтений.
    snapshot["orders"]["#CUSTOM"] = snapshot["orders"].pop("#O1")
    snapshot["orders"]["#CUSTOM"]["order_id"] = "#CUSTOM"
    snapshot["users"]["u1"]["orders"] = ["#CUSTOM"]
    store.initialize(snapshot, "Учебные правила")
    app = SupportService(store, DemoModel(), RetailAdapter)
    case = app.create_case("u1", "employee")
    question = app.investigate(case, customer_text, "employee")
    assert question.kind == "message"
    assert question.consent_request.result_status == expected
    assert question.consent_request.arguments["order_id"] == "#CUSTOM"
    assert store.snapshot()[0]["orders"]["#CUSTOM"]["status"] == "delivered"
    assert app.view(case, "employee")["deliveries"] == []
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM consents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


@pytest.mark.parametrize(
    "text", ["Не надо обменивать", "Я передумал", "Отмените предложение"]
)
def test_new_cancellation_intent_returns_only_text(text):
    history = [
        Message(role="user", content="Обменять. Email a@example.test"),
        Message(role="assistant", content="Уточните заказ"),
        Message(role="user", content=text),
    ]
    result = DemoModel().generate(history, [])
    assert result.message.tool_calls is None
    assert "Новое действие не предложено" in result.message.content


def test_cancellation_does_not_deny_previously_executed_operation():
    history = [
        Message(role="user", content="Обменять. Email a@example.test"),
        Message(
            role="assistant",
            content="Зарегистрирована заявка на обмен. Статус exchange requested.",
        ),
        Message(role="user", content="Отмена"),
    ]
    result = DemoModel().generate(history, [])
    assert result.message.tool_calls is None
    assert "Новое действие не предложено" in result.message.content
    assert "фактический результат" in result.message.content
    assert "не выполнялась" not in result.message.content


def test_demo_cannot_use_absent_tools_or_invent_missing_records():
    messages = [Message(role="user", content="Обменять. Email a@example.test")]
    result = DemoModel().generate(messages, [])
    assert result.message.tool_calls is None
    tools = [
        ToolSpec(
            name="find_user_id_by_email",
            description="Поиск",
            parameters={"type": "object"},
        )
    ]
    first = DemoModel().generate(messages, tools).message
    assert first.tool_calls[0].arguments == {"email": "a@example.test"}
    messages.extend(
        [
            first,
            Message(
                role="tool",
                tool_call_id=first.tool_calls[0].id,
                content="Клиент не найден",
                error=True,
                execution_status="rejected",
            ),
        ]
    )
    assert DemoModel().generate(messages, tools).message.tool_calls is None


def test_two_conversations_do_not_share_email_or_ids():
    model = DemoModel()
    tools = [
        ToolSpec(
            name="find_user_id_by_email",
            description="Поиск",
            parameters={"type": "object"},
        )
    ]
    first = model.generate(
        [Message(role="user", content="Обмен. Email first@example.test")], tools
    )
    second = model.generate(
        [Message(role="user", content="Возврат. Email second@example.test")], tools
    )
    assert first.message.tool_calls[0].arguments == {"email": "first@example.test"}
    assert second.message.tool_calls[0].arguments == {"email": "second@example.test"}
    assert first.message.tool_calls[0].id != second.message.tool_calls[0].id
