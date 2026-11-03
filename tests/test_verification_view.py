"""Карточка показывает согласования и результат только своей версии предложения."""

from datetime import datetime, timezone

import pytest

pytest.importorskip("tau2")

from test_core import FakeModel
from test_workflow import confirm, model_for_exchange, service
from test_zammad_execution import FakeBackend, adapter, add_ticket, approve, reconcile

from support_agent.execution import Executor
from support_agent.models import Message
from support_agent.storage import Store
from support_agent.workflow import SupportService


def prepared_exchange(tmp_path, snapshot):
    app = service(tmp_path, snapshot, model_for_exchange())
    case = app.create_case("u1", "employee")
    question = app.investigate(case, "Обмен на белые", "employee")
    return app, case, question


def test_current_approvals_and_verification_survive_replay_without_new_time(
    tmp_path, snapshot
):
    app, case, question = prepared_exchange(tmp_path, snapshot)
    pending = app.view(case, "employee")
    assert pending["approval"] is None
    assert pending["current_delivery"] is None
    assert pending["client_consent"]["status"] == "waiting"
    assert pending["verification"]["status"] == "not_checked"
    confirm(app, question)
    delivered = app.view(case, "employee")
    assert delivered["approval"]["valid"]
    assert delivered["current_delivery"]["text"] == question.text
    assert delivered["current_delivery"]["delivered"]
    assert delivered["verification"]["source"] == "saved_delivery"
    proposal = app.record_client_response(case, question.id, True, "Да", "employee")
    consent = app.view(case, "employee")
    assert consent["client_consent"]["status"] == "confirmed"
    assert consent["approval"] is None
    assert consent["current_delivery"] is None
    before = datetime.now(timezone.utc)
    confirm(app, proposal)
    after = datetime.now(timezone.utc)
    verified = app.view(case, "employee")
    timestamp = datetime.fromisoformat(verified["verification"]["verified_at"])
    assert before <= timestamp <= after
    assert verified["verification"]["status"] == "confirmed"
    assert verified["verification"]["source"] == "saved_execution"
    assert verified["client_consent"]["status"] == "used"
    reopened = SupportService(Store(app.store.path), FakeModel(), app.adapter_factory)
    confirm(reopened, proposal)
    assert reopened.view(case, "employee")["verification"] == verified["verification"]


def test_new_draft_does_not_borrow_previous_result_or_approval(tmp_path, snapshot):
    app, case, question = prepared_exchange(tmp_path, snapshot)
    confirm(app, question)
    proposal = app.record_client_response(case, question.id, True, "Да", "employee")
    confirm(app, proposal)
    app.model = FakeModel(Message(role="assistant", content="Уточните новый вопрос."))
    app.investigate(case, "Новый вопрос", "employee")
    view = app.view(case, "employee")
    assert view["operation_result"] is None
    assert view["approval"] is None
    assert view["current_delivery"] is None
    assert view["client_consent"]["status"] == "not_required"
    assert view["verification"]["status"] == "not_checked"
    assert view["verification"]["verified_at"] is None


def test_historical_operation_has_no_invented_verification_time(tmp_path, snapshot):
    app, case, question = prepared_exchange(tmp_path, snapshot)
    confirm(app, question)
    proposal = app.record_client_response(case, question.id, True, "Да", "employee")
    confirm(app, proposal)
    with app.store.transaction() as conn:
        conn.execute("DROP TABLE operation_verifications")
    restored = SupportService(Store(app.store.path), FakeModel(), app.adapter_factory)
    view = restored.view(case, "employee")
    assert view["verification"]["status"] == "confirmed"
    assert view["verification"]["verified_at"] is None
    confirm(restored, proposal)
    assert restored.view(case, "employee")["verification"]["verified_at"] is None


@pytest.mark.parametrize(
    "table,column",
    [("deliveries", "text"), ("outbox", "text"), ("approvals", "digest")],
)
def test_saved_data_mismatch_cannot_look_confirmed(tmp_path, snapshot, table, column):
    app, case, question = prepared_exchange(tmp_path, snapshot)
    confirm(app, question)
    with app.store.transaction() as conn:
        conn.execute(f"UPDATE {table} SET {column}='Изменённое содержимое'")
    view = app.view(case, "employee")
    assert view["verification"]["status"] == "mismatch"
    assert view["verification"]["verified_at"] is None


def test_unknown_external_result_gets_time_only_after_verified_recovery(
    tmp_path, snapshot
):
    snapshot["orders"]["#O1"]["status"] = "exchange requested"
    store = Store(tmp_path / "remote.sqlite3")
    store.initialize(snapshot, "Правила")
    case = store.create_case("u1", "employee")
    proposal = add_ticket(store, case)
    backend = FakeBackend(store)
    backend.error = TimeoutError("Ответ потерян")
    executor = Executor(store, adapter, ticket_backend=backend)
    app = SupportService(store, FakeModel(), adapter, ticket_backend=backend)
    approve(executor, proposal)
    view = app.view(case, "employee")
    assert view["approval"]["valid"]
    assert view["client_consent"]["status"] == "not_required"
    assert view["verification"]["status"] == "unknown"
    assert view["verification"]["verified_at"] is None
    assert view["current_delivery"] is None
    reconcile(executor, proposal)
    verified = app.view(case, "employee")["verification"]
    assert verified["status"] == "confirmed"
    assert verified["verified_at"] is not None
    reconcile(executor, proposal)
    assert app.view(case, "employee")["verification"] == verified
    assert len(backend.creates) == 1


def test_failed_transaction_does_not_keep_verification_time(tmp_path, snapshot):
    app, case, question = prepared_exchange(tmp_path, snapshot)
    confirm(app, question)
    proposal = app.record_client_response(case, question.id, True, "Да", "employee")
    with app.store.transaction() as conn:
        conn.execute(
            "CREATE TRIGGER fail_delivery BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'Проверка отката'); END"
        )
    confirm(app, proposal)
    with app.store.connection() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM operation_verifications").fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
