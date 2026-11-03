"""Пересогласование: один исполнитель, разные способы продолжить расследование."""

import pytest

pytest.importorskip("tau2")
from test_core import FakeModel
from test_retail import exchange_args
from test_workflow import call, confirm, model_for_exchange

from support_agent.agent import AgentCore
from support_agent.models import Message
from support_agent.storage import Store
from support_agent.workflow import SupportService


def prepared(tmp_path, snapshot, mode):
    store = Store(tmp_path / "recovery.sqlite3")
    store.initialize(snapshot, "Правила магазина")
    app = SupportService(store, model_for_exchange(), recovery_mode=mode)
    case = app.create_case("u1", "employee")
    q = app.investigate(case, "Хочу обмен. user1@example.test", "employee")
    confirm(app, q)
    p = app.record_client_response(case, q.id, True, "Согласен", "employee")
    return app, case, p


def change(app, *, status=False):
    with app.store.transaction() as conn:
        data, _ = app.store.retail(conn)
        if status:
            data["orders"]["#O1"]["status"] = "cancelled"
        else:
            data["products"]["p1"]["variants"]["new"]["price"] = 19.99
        app.store.save_retail(conn, data)


def recovery_model():
    return FakeModel(
        call("get_order_details", {"order_id": "#O1"}, "fresh-order"),
        call("propose_exchange", exchange_args(), "fresh-proposal"),
    )


def test_application_event_does_not_become_customer_message():
    model = FakeModel(Message(role="assistant", content="Нужна новая сумма."))
    core = AgentCore(model=model, policy="Правила", tools=[])
    state = core.init_state([Message(role="user", content="Обменять товар")])
    _, updated = core.continue_with_event("Цена изменилась", state)
    assert [m.content for m in updated.messages if m.role == "user"] == [
        "Обменять товар"
    ]
    assert "Цена изменилась" in model.requests[0][0][-1].content
    assert model.requests[0][0][-1].role == "assistant"
    assert len(state.messages) == 1


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_changed_price_needs_new_consents_in_both_modes(tmp_path, snapshot, mode):
    app, case, old = prepared(tmp_path, snapshot, mode)
    change(app)
    model = recovery_model()
    app.model = model
    result = confirm(app, old)
    assert result["status"] == "stale"
    assert result["check"]["status"] == "changed"
    if mode == "manual":
        assert model.requests == []
        assert app.view(case, "employee")["recovery"]["status"] == "pending"
        app.resume(case, old.id, "employee")
    q = app.view(case, "employee")["proposal"]
    assert q.consent_request and q.id != old.id
    assert str(q.consent_request.amount) == "9.99"
    assert app.store.snapshot()[0]["orders"]["#O1"]["status"] == "delivered"
    assert len(app.view(case, "employee")["deliveries"]) == 1
    with pytest.raises(ValueError):
        app.record_client_response(case, q.id, True, "Да", "employee")
    confirm(app, q)
    p = app.record_client_response(case, q.id, True, "Да, новая сумма", "employee")
    assert confirm(app, p)["operation_result"]["exchange_price_difference"] == 9.99
    kinds = [e["kind"] for e in app.store.events(case)]
    assert kinds.count("customer") == 3
    assert kinds.count("manual_resume") == (mode == "manual")
    assert kinds.count("automatic_resume") == (mode == "auto")
    assert kinds.count("operation") == 1


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_unchanged_conditions_do_not_replan(tmp_path, snapshot, mode):
    app, case, p = prepared(tmp_path, snapshot, mode)
    app.model = FakeModel()
    assert confirm(app, p)["status"] == "delivered"
    assert app.model.requests == []
    assert app.view(case, "employee")["recovery"] is None


def test_same_event_after_restart_returns_same_proposal(tmp_path, snapshot):
    app, case, p = prepared(tmp_path, snapshot, "auto")
    change(app)
    app.model = recovery_model()
    confirm(app, p)
    q = app.view(case, "employee")["proposal"]
    restored = SupportService(Store(app.store.path), FakeModel(), recovery_mode="auto")
    confirm(restored, p)
    assert restored.resume(case, p.id, "employee").id == q.id
    assert restored.model.requests == []
    assert restored.view(case, "employee")["proposal"].id == q.id


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_forbidden_status_prepares_only_explanation(tmp_path, snapshot, mode):
    app, case, p = prepared(tmp_path, snapshot, mode)
    change(app, status=True)
    app.model = FakeModel(
        Message(role="assistant", content="Заказ отменён, обмен невозможен.")
    )
    assert confirm(app, p)["check"]["status"] == "forbidden"
    if mode == "manual":
        app.resume(case, p.id, "employee")
    explanation = app.view(case, "employee")["proposal"]
    assert explanation.action is None and explanation.consent_request is None
    assert len(app.view(case, "employee")["deliveries"]) == 1
    confirm(app, explanation)
    assert not any(e["kind"] == "operation" for e in app.store.events(case))


def test_new_customer_message_cancels_pending_recovery(tmp_path, snapshot):
    app, case, p = prepared(tmp_path, snapshot, "manual")
    change(app)
    confirm(app, p)
    app.model = FakeModel(Message(role="assistant", content="Отказ принят."))
    app.investigate(case, "Я передумал", "employee")
    with pytest.raises(ValueError):
        app.resume(case, p.id, "employee")
    assert app.view(case, "employee")["recovery"]["status"] == "cancelled"


def test_failed_recovery_is_recorded_without_automatic_retries(tmp_path, snapshot):
    app, case, p = prepared(tmp_path, snapshot, "auto")
    change(app)
    app.model = FakeModel()
    confirm(app, p)
    assert app.view(case, "employee")["recovery"]["status"] == "failed"
    count = len(app.model.requests)
    confirm(app, p)
    assert len(app.model.requests) == count
    assert not any(e["kind"] == "operation" for e in app.store.events(case))


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize(
    "reply",
    [
        "Цена изменилась. Доплата теперь 9.99 USD. Подтвердите новые условия.",
        "Уточните, какой вариант товара вы хотите получить вместо выбранного.",
    ],
    ids=["unstructured_terms", "clarification"],
)
def test_recovery_text_does_not_become_action_or_consent_request(
    tmp_path, snapshot, mode, reply
):
    app, case, old = prepared(tmp_path, snapshot, mode)
    change(app)
    app.model = model = FakeModel(Message(role="assistant", content=reply))

    assert confirm(app, old)["status"] == "stale"
    if mode == "manual":
        app.resume(case, old.id, "employee")

    view = app.view(case, "employee")
    message = view["proposal"]
    assert message.text == reply
    assert message.action is None and message.consent_request is None
    assert view["proposal_status"] == "pending"
    assert view["waiting_consent"] is None
    with pytest.raises(ValueError):
        app.record_client_response(case, message.id, True, "Согласен", "employee")
    with app.store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM consents WHERE accepted=1 AND revoked=0"
        ).fetchone()[0] == 0
    assert app.store.snapshot()[0]["orders"]["#O1"]["status"] == "delivered"
    assert len(view["deliveries"]) == 1
    assert len(model.requests) == 1

    restored = SupportService(Store(app.store.path), FakeModel(), recovery_mode=mode)
    assert restored.view(case, "employee")["proposal"].id == message.id
    assert len(restored.view(case, "employee")["deliveries"]) == 1
    assert restored.model.requests == []
