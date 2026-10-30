"""Регрессии отмены, повторного подтверждения и продолжения диалога без API."""

from contextlib import contextmanager
from threading import Event, Thread, current_thread

import pytest

pytest.importorskip("tau2")
from test_core import FakeModel
from test_workflow import confirm, model_for_exchange, service

from support_agent.adapters.retail import RetailAdapter
from support_agent.execution import Executor, TestChannel
from support_agent.models import Message


def test_customer_cancellation_invalidates_delivered_question_even_when_model_fails(
    tmp_path, snapshot
):
    app = service(tmp_path, snapshot, model_for_exchange())
    case = app.create_case("u1", "employee")
    question = app.investigate(case, "Хочу обменять товар", "employee")
    confirm(app, question)
    app.model = FakeModel()

    with pytest.raises(ValueError, match="Не удалось подготовить предложение"):
        app.investigate(case, "Передумал, не нужно обменивать", "employee")

    with pytest.raises(ValueError):
        app.record_client_response(
            case, question.id, True, "Согласен из старой вкладки", "employee"
        )
    assert app.store.snapshot()[0]["orders"]["#O1"]["status"] == "delivered"
    assert app.view(case, "employee")["waiting_consent"] is None


def test_concurrent_repeat_after_write_failure_requires_new_proposal(
    tmp_path, snapshot, monkeypatch
):
    app = service(tmp_path, snapshot, model_for_exchange())
    case = app.create_case("u1", "employee")
    question = app.investigate(case, "Хочу обменять товар", "employee")
    confirm(app, question)
    proposal = app.record_client_response(
        case, question.id, True, "Подтверждаю условия", "employee"
    )
    first_executing = Event()
    repeat_entered = Event()
    repeat_finished = Event()
    synchronization = {}
    original_transaction = app.store.transaction

    @contextmanager
    def coordinated_transaction():
        if current_thread().name == "approval-repeat":
            repeat_entered.set()
        try:
            with original_transaction() as conn:
                yield conn
        except RuntimeError:
            if current_thread().name == "approval-first":
                # Увеличиваем окно уже завершённого отката. При обработке сбоя
                # внутри транзакции этот участок вообще не выполняется.
                repeat_finished.wait(5)
            raise

    monkeypatch.setattr(app.store, "transaction", coordinated_transaction)

    class FailingFirstAdapter(RetailAdapter):
        def execute(self, action, user_id):
            if current_thread().name == "approval-first":
                first_executing.set()
                synchronization["repeat_waited"] = repeat_entered.wait(5)
                super().execute(action, user_id)
                raise RuntimeError("Сбой первой попытки записи")
            return super().execute(action, user_id)

    executor = Executor(app.store, FailingFirstAdapter)
    outcomes = {}
    unexpected_errors = []

    def approve_in_thread(key):
        try:
            outcomes[key] = confirm(executor, proposal)
        except ValueError as error:
            outcomes[key] = {"status": "blocked", "reason": str(error)}
        except BaseException as error:
            unexpected_errors.append(error)
        finally:
            if key == "repeat":
                repeat_finished.set()

    first = Thread(target=approve_in_thread, args=("first",), name="approval-first")
    repeat = Thread(target=approve_in_thread, args=("repeat",), name="approval-repeat")
    first.start()
    reached_execution = first_executing.wait(5)
    repeat.start()
    first.join(15)
    repeat.join(15)

    assert reached_execution and synchronization["repeat_waited"]
    assert not first.is_alive() and not repeat.is_alive()
    assert unexpected_errors == []
    assert outcomes["first"]["status"] == "failed"
    assert outcomes["repeat"]["status"] != "delivered"
    assert app.store.snapshot()[0]["orders"]["#O1"]["status"] == "delivered"
    with app.store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
    view = app.view(case, "employee")
    assert [row["text"] for row in view["deliveries"]] == [question.text]
    assert view["proposal"].id != proposal.id
    assert view["proposal_status"] == "pending"


def test_continued_dialogue_receives_delivered_conditions_and_operation_result(
    tmp_path, snapshot
):
    app = service(tmp_path, snapshot, model_for_exchange())
    case = app.create_case("u1", "employee")
    question = app.investigate(case, "Хочу обменять товар", "employee")
    confirm(app, question)
    proposal = app.record_client_response(
        case, question.id, True, "Подтверждаю условия", "employee"
    )
    confirm(app, proposal)
    continuation = FakeModel(
        Message(role="assistant", content="Заявка на обмен уже зарегистрирована.")
    )
    app.model = continuation

    app.investigate(case, "Что произошло с моим обменом?", "employee")

    messages, _ = continuation.requests[-1]
    conversation = "\n".join(
        message.content or "" for message in messages if message.role != "system"
    )
    assert question.text in conversation
    assert proposal.text in conversation
    assert conversation.index(question.text) < conversation.index(proposal.text)


def test_late_delivery_error_cannot_overwrite_successful_concurrent_retry(
    tmp_path, snapshot
):
    app = service(tmp_path, snapshot, model_for_exchange())
    case = app.create_case("u1", "employee")
    question = app.investigate(case, "Хочу обменять товар", "employee")
    confirm(app, question)
    proposal = app.record_client_response(
        case, question.id, True, "Подтверждаю условия", "employee"
    )
    first_received = Event()
    retry_finished = Event()
    synchronization = {}

    class InterruptedFirstChannel(TestChannel):
        def send(self, delivery_id, case_id, text):
            super().send(delivery_id, case_id, text)
            if current_thread().name == "delivery-first":
                first_received.set()
                synchronization["retry_finished"] = retry_finished.wait(5)
                raise RuntimeError("Запоздалый сбой первой отправки")

    executor = Executor(
        app.store, RetailAdapter, channel=InterruptedFirstChannel(app.store)
    )
    outcomes = {}
    unexpected_errors = []

    def first_approval():
        try:
            outcomes["first"] = confirm(executor, proposal)
        except BaseException as error:
            unexpected_errors.append(error)

    first = Thread(target=first_approval, name="delivery-first")
    first.start()
    received = first_received.wait(5)
    try:
        outcomes["repeat"] = confirm(executor, proposal)
    finally:
        retry_finished.set()
        first.join(10)

    assert received and synchronization["retry_finished"]
    assert not first.is_alive()
    assert unexpected_errors == []
    assert outcomes["repeat"]["status"] == "delivered"
    view = app.view(case, "employee")
    assert view["proposal_status"] == "delivered"
    assert outcomes["first"]["status"] == "delivered"
    assert app.store.snapshot()[0]["orders"]["#O1"]["status"] == "exchange requested"
    with app.store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
        delivered = conn.execute(
            "SELECT text FROM deliveries WHERE case_id=? AND text=?",
            (case, proposal.text),
        ).fetchall()
        assert [row["text"] for row in delivered] == [proposal.text]
        assert (
            conn.execute(
                "SELECT status FROM outbox WHERE proposal_id=?", (proposal.id,)
            ).fetchone()[0]
            == "delivered"
        )
