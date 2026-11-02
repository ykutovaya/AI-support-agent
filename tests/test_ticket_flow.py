"""Локальная заявка проходит общий процесс без отдельного согласия клиента."""

from functools import partial

import pytest

pytest.importorskip("tau2")

from test_core import FakeModel
from test_workflow import call, confirm

from support_agent.adapters.demo_model import DemoModel
from support_agent.adapters.retail import RetailAdapter
from support_agent.adapters.ticket_retail import TicketRetailAdapter
from support_agent.models import Message
from support_agent.storage import Store
from support_agent.workflow import SupportService


def ticket_service(tmp_path, snapshot, model):
    snapshot["orders"]["#CUSTOM"] = snapshot["orders"].pop("#O1")
    snapshot["orders"]["#CUSTOM"].update(
        order_id="#CUSTOM", status="exchange requested"
    )
    snapshot["users"]["u1"]["orders"] = ["#CUSTOM"]
    store = Store(tmp_path / "ticket-flow.sqlite3")
    store.initialize(snapshot, "Обмен зарегистрирован; исполнение проверяет поддержка.")
    return SupportService(store, model, partial(TicketRetailAdapter, target="local"))


def scripted_model(prefix=""):
    return FakeModel(
        call(
            "find_user_id_by_email",
            {"email": "user1@example.test"},
            prefix + "identity",
        ),
        call("get_order_details", {"order_id": "#CUSTOM"}, prefix + "order"),
        call(
            "propose_support_ticket",
            {"order_id": "#CUSTOM", "question": "Когда завершится обмен?"},
            prefix + "proposal",
        ),
    )


@pytest.mark.parametrize("model", [scripted_model, DemoModel])
def test_ticket_requires_employee_approval_then_survives_restart_without_duplicates(
    tmp_path, snapshot, model
):
    app = ticket_service(tmp_path, snapshot, model())
    before = app.store.snapshot()
    case = app.create_case("u1", "employee")
    prepared = app.investigate(
        case, "Проверьте результат обмена. Email user1@example.test", "employee"
    )
    assert prepared.kind == "action"
    assert prepared.action.name == "create_support_ticket"
    assert prepared.action.arguments["order_id"] == "#CUSTOM"
    assert prepared.consent_request is None
    assert prepared.action.amount is None
    assert "локальн" in prepared.text.casefold()
    assert "фактическое" in prepared.text.casefold()
    assert app.store.ticket(prepared.id) is None
    assert app.view(case, "employee")["deliveries"] == []
    with pytest.raises(ValueError):
        app.approve(case, prepared.id, prepared.version, prepared.digest(), "other")
    assert app.store.ticket(prepared.id) is None
    outcome = confirm(app, prepared)
    assert outcome["status"] == "delivered"
    assert outcome["operation_result"]["ticket_number"] == "T-1"
    assert outcome["operation_result"]["ticket_status"] == "open"
    restored = SupportService(Store(app.store.path), FakeModel(), app.adapter_factory)
    confirm(restored, prepared)
    view = restored.view(case, "employee")
    assert view["operation_result"]["ticket_number"] == "T-1"
    assert len(view["deliveries"]) == 1
    assert view["deliveries"][0]["text"] == prepared.text
    assert app.store.snapshot() == before
    with app.store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM consents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1


def test_repeated_question_does_not_create_second_ticket_or_delivery(
    tmp_path, snapshot
):
    app = ticket_service(tmp_path, snapshot, DemoModel())
    case = app.create_case("u1", "employee")
    first = app.investigate(
        case, "Проверьте результат обмена. Email user1@example.test", "employee"
    )
    confirm(app, first)
    second = app.investigate(
        case, "Проверьте результат обмена, пожалуйста.", "employee"
    )
    outcome = confirm(app, second)
    assert outcome["status"] == "duplicate"
    assert outcome["operation_result"]["ticket_number"] == "T-1"
    assert len(app.view(case, "employee")["deliveries"]) == 1
    with app.store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM support_tickets").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
    restored = SupportService(Store(app.store.path), FakeModel(), app.adapter_factory)
    view = restored.view(case, "employee")
    assert view["proposal_status"] == "duplicate"
    assert view["operation_result"]["ticket_number"] == "T-1"
    assert len(view["deliveries"]) == 1


def test_retail_adapter_does_not_advertise_ticket_tool_or_guidance(tmp_path, snapshot):
    store = Store(tmp_path / "retail.sqlite3")
    store.initialize(snapshot, "Правила магазина")
    model = FakeModel(Message(role="assistant", content="Уточните детали обращения."))
    app = SupportService(store, model, RetailAdapter)
    case = app.create_case("u1", "employee")
    app.investigate(case, "Проверьте результат обмена", "employee")
    messages, tools = model.requests[-1]
    assert "propose_support_ticket" not in {tool.name for tool in tools}
    assert "propose_support_ticket" not in str(messages)


def test_changed_ticket_conditions_use_manual_recovery_and_new_employee_approval(
    tmp_path, snapshot
):
    app = ticket_service(tmp_path, snapshot, scripted_model())
    case = app.create_case("u1", "employee")
    old = app.investigate(case, "Проверьте результат обмена", "employee")
    with app.store.transaction() as conn:
        conn.execute("UPDATE retail_state SET policy=? WHERE id=1", ("Новые правила",))
    app.model = scripted_model("recovery-")
    assert confirm(app, old)["status"] == "stale"
    assert app.store.ticket(old.id) is None
    assert app.model.requests == []
    assert app.view(case, "employee")["recovery"]["status"] == "pending"
    replacement = app.resume(case, old.id, "employee")
    assert replacement.action is not None
    assert replacement.consent_request is None
    assert replacement.digest() != old.digest()
    assert app.store.ticket(replacement.id) is None
    assert confirm(app, replacement)["status"] == "delivered"
