"""Полный проход реального рабочего места с подставной моделью и SQLite."""

from decimal import Decimal

import pytest

pytest.importorskip("tau2")
import test_ui
from test_core import FakeModel
from test_retail import exchange_args
from test_ui import EMPLOYEE, visible_text
from test_workflow import call, model_for_exchange

from support_agent.storage import Store
from support_agent.workflow import SupportService

open_app = test_ui.open_app


def changed_conditions_model(suffix):
    return FakeModel(
        call("get_order_details", {"order_id": "#O1"}, f"order-{suffix}"),
        call("propose_exchange", exchange_args(), f"proposal-{suffix}"),
    )


def set_price(app, price):
    with app.store.transaction() as conn:
        snapshot, _ = app.store.retail(conn)
        snapshot["products"]["p1"]["variants"]["new"]["price"] = price
        app.store.save_retail(conn, snapshot)


def test_real_screen_reapproves_changed_price_and_rejects_outdated_customer_consent(
    tmp_path, snapshot, open_app
):
    store = Store(tmp_path / "screen.sqlite3")
    store.initialize(snapshot, "Правила магазина")
    app = SupportService(store, model_for_exchange(), recovery_mode="auto")
    case = app.create_case("u1", EMPLOYEE)
    at = open_app(app, case)

    at.text_area(key="request").set_value(
        "Хочу обменять товар. Email user1@example.test"
    ).run()
    at.button(key="investigate").click().run()
    assert not at.exception
    at.button(key="approve").click().run()
    at.text_area(key="client_response").set_value("Согласен с условиями").run()
    at.button(key="client_accept").click().run()
    old_action = app.view(case, EMPLOYEE)["proposal"]
    assert old_action.action is not None

    set_price(app, 19.99)
    app.model = changed_conditions_model("first-price")
    at.button(key="approve").click().run()
    assert not at.exception
    first_question = app.view(case, EMPLOYEE)["proposal"]
    assert first_question.id != old_action.id
    assert first_question.consent_request.amount == Decimal("9.99")
    assert len(app.view(case, EMPLOYEE)["deliveries"]) == 1
    assert "Было: 2.35" in visible_text(at)
    assert "Стало: 9.99" in visible_text(at)
    at.button(key="approve").click().run()

    # Ответ на уже отправленную сумму не даёт согласия на новую цену.
    set_price(app, 29.99)
    app.model = changed_conditions_model("second-price")
    at.text_area(key="client_response").set_value("Подтверждаю доплату 9.99 USD").run()
    at.button(key="client_accept").click().run()
    assert not at.exception
    current = app.view(case, EMPLOYEE)
    fresh_question = current["proposal"]
    assert fresh_question.id != first_question.id
    assert fresh_question.action is None
    assert fresh_question.consent_request.amount == Decimal("19.99")
    assert current["proposal_status"] == "pending"
    assert current["waiting_consent"] is None
    assert "Устаревшее согласие" in visible_text(at)
    assert "Было: 9.99" in visible_text(at)
    assert "Стало: 19.99" in visible_text(at)
    assert len(current["deliveries"]) == 2
    assert not any(event["kind"] == "operation" for event in current["events"])
    with store.connection() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM consents WHERE question_id=? AND accepted=1",
                (first_question.id,),
            ).fetchone()[0]
            == 0
        )

    at.button(key="approve").click().run()
    at.text_area(key="client_response").set_value("Подтверждаю доплату 19.99 USD").run()
    at.button(key="client_accept").click().run()
    assert not at.exception
    ready = app.view(case, EMPLOYEE)["proposal"]
    assert ready.action.amount == Decimal("19.99")
    assert not any(
        event["kind"] == "operation" for event in app.view(case, EMPLOYEE)["events"]
    )
    at.button(key="approve").click().run()
    assert not at.exception
    assert "Фактический результат" in visible_text(at)
    calls = len(app.model.requests)
    at.run()
    assert not at.exception
    assert len(app.model.requests) == calls
    final = app.view(case, EMPLOYEE)
    assert final["proposal_status"] == "delivered"
    assert sum(event["kind"] == "operation" for event in final["events"]) == 1
    assert len(final["deliveries"]) == 4
    assert final["deliveries"][-1]["text"] == ready.text
    order = store.snapshot()[0]["orders"]["#O1"]
    assert order["status"] == "exchange requested"
    assert order["exchange_price_difference"] == 19.99
