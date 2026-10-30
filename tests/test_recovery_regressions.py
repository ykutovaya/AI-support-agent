"""Границы пересогласования: конкуренция, отмена и повторное изменение цены."""

from decimal import Decimal
from threading import Event, Thread

import pytest

pytest.importorskip("tau2")
from test_core import FakeModel
from test_recovery import change, prepared
from test_retail import exchange_args
from test_workflow import call, confirm

from support_agent.models import Message
from support_agent.storage import Store
from support_agent.workflow import SupportService


class PausedModel(FakeModel):
    def __init__(self):
        super().__init__(call("propose_exchange", exchange_args(), "paused-proposal"))
        self.entered = Event()
        self.release = Event()

    def generate(self, messages, tools):
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("Проверка не разрешила завершить ответ модели")
        return super().generate(messages, tools)


def start_resume(app, case, source_id):
    outcome = {}

    def run():
        try:
            outcome["proposal"] = app.resume(case, source_id, "employee")
        except Exception as error:
            outcome["error"] = error

    thread = Thread(target=run)
    thread.start()
    return thread, outcome


def replacement_model(suffix):
    return FakeModel(
        call("get_order_details", {"order_id": "#O1"}, f"order-{suffix}"),
        call("propose_exchange", exchange_args(), f"proposal-{suffix}"),
    )


def test_concurrent_resume_prepares_one_version_without_fabricating_customer_input(
    tmp_path, snapshot
):
    app, case, old = prepared(tmp_path, snapshot, "manual")
    change(app)
    confirm(app, old)
    customers_before = [
        message.content
        for message in app.store.case(case)["state"].messages
        if message.role == "user"
    ]
    app.model = model = PausedModel()
    other_session = SupportService(Store(app.store.path), model)
    thread, outcome = start_resume(app, case, old.id)
    try:
        assert model.entered.wait(5)
        with pytest.raises(ValueError):
            other_session.resume(case, old.id, "employee")
    finally:
        model.release.set()
        thread.join(10)

    assert not thread.is_alive()
    assert "error" not in outcome
    created = outcome["proposal"]
    assert other_session.resume(case, old.id, "employee").id == created.id
    assert len(model.requests) == 1
    messages, _ = model.requests[0]
    assert [
        message.content for message in messages if message.role == "user"
    ] == customers_before
    assert messages[-1].role == "assistant"
    assert "Служебное событие приложения" in messages[-1].content
    assert '"type": "conditions_changed"' in messages[-1].content
    events = app.store.events(case)
    assert sum(event["kind"] == "manual_resume" for event in events) == 1
    assert sum(event["kind"] == "recovery_prepared" for event in events) == 1
    assert not any(event["kind"] == "operation" for event in events)
    assert app.store.latest(case)["proposal"].id == created.id
    assert len(app.view(case, "employee")["deliveries"]) == 1


def test_customer_cancellation_during_recovery_discards_late_model_answer(
    tmp_path, snapshot
):
    app, case, old = prepared(tmp_path, snapshot, "manual")
    with app.store.connection() as conn:
        old_question_id = conn.execute(
            "SELECT proposal_id FROM outbox WHERE case_id=?", (case,)
        ).fetchone()[0]
    change(app)
    confirm(app, old)
    app.model = paused = PausedModel()
    thread, outcome = start_resume(app, case, old.id)
    try:
        assert paused.entered.wait(5)
        app.model = FakeModel(
            Message(role="assistant", content="Отказ от обмена принят.")
        )
        cancellation = app.investigate(case, "Передумал, обмен отменяю", "employee")
    finally:
        paused.release.set()
        thread.join(10)

    assert not thread.is_alive()
    assert isinstance(outcome.get("error"), ValueError)
    view = app.view(case, "employee")
    assert view["proposal"].id == cancellation.id
    assert view["proposal"].action is None and view["proposal"].consent_request is None
    assert view["recovery"]["status"] == "cancelled"
    assert view["waiting_consent"] is None
    with pytest.raises(ValueError):
        app.resume(case, old.id, "employee")
    with pytest.raises(ValueError):
        app.record_client_response(
            case, old_question_id, True, "Старое согласие", "employee"
        )
    confirm(app, old)
    assert app.store.snapshot()[0]["orders"]["#O1"]["status"] == "delivered"
    events = app.store.events(case)
    assert not any(
        event["kind"] in ("operation", "recovery_prepared") for event in events
    )
    assert len(view["deliveries"]) == 1


@pytest.mark.parametrize(
    "phase", ["before_question_delivery", "before_customer_consent"]
)
def test_second_price_change_prepares_fresh_question_without_accepting_old_consent(
    tmp_path, snapshot, phase
):
    app, case, old = prepared(tmp_path, snapshot, "auto")
    change(app)
    app.model = replacement_model("first-change")
    confirm(app, old)
    first_question = app.view(case, "employee")["proposal"]
    if phase == "before_customer_consent":
        confirm(app, first_question)
    with app.store.transaction() as conn:
        current, _ = app.store.retail(conn)
        current["products"]["p1"]["variants"]["new"]["price"] = 29.99
        app.store.save_retail(conn, current)
    app.model = replacement_model("second-change")

    if phase == "before_question_delivery":
        confirm(app, first_question)
    else:
        try:
            app.record_client_response(
                case,
                first_question.id,
                True,
                "Согласен с ранее показанной суммой",
                "employee",
            )
        except ValueError:
            # Старый ответ может быть отклонён, но обнаруженное изменение
            # должно запустить предусмотренную подготовку новых условий.
            pass

    view = app.view(case, "employee")
    fresh_question = view["proposal"]
    assert fresh_question.id != first_question.id
    assert fresh_question.action is None
    assert fresh_question.consent_request.amount == Decimal("19.99")
    assert view["proposal_status"] == "pending"
    assert view["waiting_consent"] is None
    assert view["recovery"]["status"] == "prepared"
    assert not any(event["kind"] == "operation" for event in app.store.events(case))
    with pytest.raises(ValueError):
        app.record_client_response(
            case, first_question.id, True, "Старое согласие", "employee"
        )
    confirm(app, fresh_question)
    action = app.record_client_response(
        case, fresh_question.id, True, "Согласен с 19.99 USD", "employee"
    )
    assert (
        confirm(app, action)["operation_result"]["exchange_price_difference"] == 19.99
    )


def delivered_question_with_changed_price(tmp_path, snapshot, mode):
    app, case, old = prepared(tmp_path, snapshot, mode)
    change(app)
    app.model = replacement_model("delivered-question")
    confirm(app, old)
    if mode == "manual":
        app.resume(case, old.id, "employee")
    question = app.view(case, "employee")["proposal"]
    confirm(app, question)
    with app.store.transaction() as conn:
        current, _ = app.store.retail(conn)
        current["products"]["p1"]["variants"]["new"]["price"] = 29.99
        app.store.save_retail(conn, current)
    app.model = FakeModel()
    return app, case, old, question


def test_manual_stale_customer_acceptance_saves_pending_recovery_before_error(
    tmp_path, snapshot
):
    app, case, _, question = delivered_question_with_changed_price(
        tmp_path, snapshot, "manual"
    )

    with pytest.raises(ValueError):
        app.record_client_response(
            case, question.id, True, "Согласен с прежней ценой", "employee"
        )

    view = app.view(case, "employee")
    assert view["proposal"].id == question.id
    assert view["proposal_status"] == "stale"
    assert view["waiting_consent"] is None
    assert view["recovery"]["source_proposal_id"] == question.id
    assert view["recovery"]["status"] == "pending"
    assert app.model.requests == []
    with app.store.connection() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM consents WHERE question_id=? AND accepted=1",
                (question.id,),
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
    assert len(view["deliveries"]) == 2


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_customer_refusal_after_price_change_is_saved_without_replanning(
    tmp_path, snapshot, mode
):
    app, case, old, question = delivered_question_with_changed_price(
        tmp_path, snapshot, mode
    )

    refusal = app.record_client_response(
        case, question.id, False, "Нет, больше не хочу обмен", "employee"
    )

    assert refusal.action is None and refusal.consent_request is None
    view = app.view(case, "employee")
    assert view["proposal"].id == refusal.id
    assert view["proposal_status"] == "pending"
    assert view["recovery"]["status"] == "cancelled"
    assert view["waiting_consent"] is None
    assert app.model.requests == []
    with app.store.connection() as conn:
        response = conn.execute(
            "SELECT accepted,response FROM consents WHERE question_id=?", (question.id,)
        ).fetchone()
        assert dict(response) == {
            "accepted": 0,
            "response": "Нет, больше не хочу обмен",
        }
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM consents WHERE accepted=1 AND revoked=0"
            ).fetchone()[0]
            == 0
        )
    with pytest.raises(ValueError):
        app.resume(case, old.id, "employee")
    confirm(app, old)
    assert app.model.requests == []
    assert len(view["deliveries"]) == 2
    assert app.store.snapshot()[0]["orders"]["#O1"]["status"] == "delivered"
