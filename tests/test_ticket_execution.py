"""Локальная заявка атомарна, требует согласования и не дублируется."""

from uuid import uuid4

import pytest

pytest.importorskip("tau2")

from support_agent.adapters.ticket_retail import TicketRetailAdapter
from support_agent.execution import Executor, TestChannel
from support_agent.proposals import Proposal
from support_agent.storage import Store


def adapter(snapshot, policy):
    return TicketRetailAdapter(snapshot, policy, target="local")


def add_ticket(store, case_id, question="Каков результат обмена?"):
    snapshot, policy = store.snapshot()
    action = adapter(snapshot, policy).plan(
        "propose_support_ticket", {"order_id": "#O1", "question": question}, "u1"
    )
    with store.transaction() as conn:
        proposal = Proposal(
            id=uuid4().hex,
            case_id=case_id,
            version=store.next_version(conn, case_id),
            kind="action",
            text="Обращение зарегистрировано для проверки специалистом.",
            action=action,
        )
        store.add_proposal(conn, proposal)
    return proposal


@pytest.fixture
def ticket_setup(tmp_path, snapshot):
    snapshot["orders"]["#O1"]["status"] = "exchange requested"
    store = Store(tmp_path / "tickets.sqlite3")
    store.initialize(snapshot, "Правила магазина")
    case_id = store.create_case("u1", "employee")
    return store, Executor(store, adapter), add_ticket(store, case_id)


def approve(executor, proposal):
    return executor.approve(
        proposal.case_id, proposal.id, proposal.version, proposal.digest(), "employee"
    )


def count(store, table):
    with store.connection() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_employee_approval_registers_verified_local_ticket_without_customer_consent(
    ticket_setup,
):
    store, executor, p = ticket_setup
    before = store.snapshot()
    outcome = approve(executor, p)
    assert outcome["status"] == "delivered"
    result = outcome["operation_result"]
    assert result == {
        "status": "ticket created",
        "ticket_id": 1,
        "ticket_number": "T-1",
        "ticket_status": "open",
        "target": "local",
        "duplicate": False,
    }
    ticket = store.ticket(p.id)
    assert ticket["case_id"] == p.case_id
    assert ticket["user_id"] == "u1" and ticket["order_id"] == "#O1"
    assert ticket["title"] == p.action.arguments["title"]
    assert ticket["body"] == p.action.arguments["body"]
    assert ticket["status"] == "open"
    assert count(store, "consents") == 0
    assert store.snapshot() == before
    with store.connection() as conn:
        assert conn.execute("SELECT text FROM deliveries").fetchone()[0] == p.text


@pytest.mark.parametrize("invalid", ["employee", "version", "digest"])
def test_no_ticket_or_message_before_valid_employee_approval(ticket_setup, invalid):
    store, executor, p = ticket_setup
    assert count(store, "support_tickets") == 0
    with pytest.raises(ValueError):
        executor.approve(
            p.case_id,
            p.id,
            p.version + (invalid == "version"),
            "wrong" if invalid == "digest" else p.digest(),
            "intruder" if invalid == "employee" else "employee",
        )
    assert count(store, "support_tickets") == count(store, "outbox") == 0


@pytest.mark.parametrize("change", ["policy", "status", "ownership", "body"])
def test_changed_or_tampered_conditions_block_registration(ticket_setup, change):
    store, executor, p = ticket_setup
    with store.transaction() as conn:
        snapshot, _ = store.retail(conn)
        if change == "policy":
            conn.execute("UPDATE retail_state SET policy='Новые правила'")
        elif change == "body":
            p.action.arguments["body"] = "Непроверенные сведения"
            conn.execute(
                "UPDATE proposals SET body=? WHERE id=?", (p.model_dump_json(), p.id)
            )
        elif change == "ownership":
            snapshot["orders"]["#O1"]["user_id"] = "u2"
        else:
            snapshot["orders"]["#O1"]["status"] = "delivered"
        store.save_retail(conn, snapshot)
    assert approve(executor, p)["status"] == "stale"
    assert count(store, "support_tickets") == count(store, "outbox") == 0


def test_foreign_target_is_blocked_even_if_adapter_recognizes_it(ticket_setup):
    store, _, p = ticket_setup
    snapshot, policy = store.snapshot()

    def foreign(data, rules):
        return TicketRetailAdapter(data, rules, target="foreign")

    p.action = foreign(snapshot, policy).plan(
        "propose_support_ticket",
        {"order_id": "#O1", "question": "Каков результат?"},
        "u1",
    )
    with store.transaction() as conn:
        conn.execute(
            "UPDATE proposals SET body=? WHERE id=?", (p.model_dump_json(), p.id)
        )
    assert approve(Executor(store, foreign), p)["status"] == "stale"
    assert count(store, "support_tickets") == count(store, "outbox") == 0


def test_restart_and_repeated_confirmation_keep_one_ticket_and_delivery(ticket_setup):
    store, executor, p = ticket_setup
    first = approve(executor, p)
    assert approve(Executor(Store(store.path), adapter), p) == first
    assert first["status"] == "delivered"
    assert count(store, "support_tickets") == count(store, "deliveries") == 1


def test_rephrasing_same_problem_cannot_create_or_announce_second_ticket(ticket_setup):
    store, executor, p = ticket_setup
    first = approve(executor, p)
    next_proposal = add_ticket(
        store, p.case_id, "Есть ли новости по выполнению обмена?"
    )
    outcome = approve(executor, next_proposal)
    assert outcome["status"] == "duplicate"
    assert (
        outcome["operation_result"]["ticket_id"]
        == first["operation_result"]["ticket_id"]
    )
    assert count(store, "support_tickets") == count(store, "deliveries") == 1
    assert count(store, "operations") == 1
    assert outcome["operation_result"] == first["operation_result"]
    assert approve(Executor(Store(store.path), adapter), next_proposal) == outcome


def test_failed_transaction_rolls_back_ticket_and_requires_new_message_approval(
    ticket_setup, monkeypatch
):
    import support_agent.execution as execution

    store, executor, p = ticket_setup
    original = execution.register_ticket

    def fail(conn, case, proposal):
        original(conn, case, proposal)
        raise RuntimeError("Сбой после записи")

    monkeypatch.setattr(execution, "register_ticket", fail)
    assert approve(executor, p)["status"] == "failed"
    assert (
        count(store, "support_tickets")
        == count(store, "outbox")
        == count(store, "operations")
        == 0
    )
    failure = store.latest(p.case_id)
    assert failure["proposal"].id != p.id
    assert failure["proposal"].kind == "message" and failure["status"] == "pending"
    assert approve(executor, failure["proposal"])["status"] == "delivered"


def test_delivery_retry_keeps_ticket_and_approved_text_unchanged(ticket_setup):
    store, _, p = ticket_setup

    class Interrupted(TestChannel):
        failed = False

        def send(self, delivery_id, case_id, text):
            super().send(delivery_id, case_id, text)
            if not self.failed:
                self.failed = True
                raise TimeoutError("Ответ о доставке потерян")

    executor = Executor(store, adapter, Interrupted(store))
    assert approve(executor, p)["status"] == "delivery_failed"
    assert approve(Executor(Store(store.path), adapter), p)["status"] == "delivered"
    assert count(store, "support_tickets") == count(store, "deliveries") == 1
    with store.connection() as conn:
        assert conn.execute("SELECT text FROM deliveries").fetchone()[0] == p.text
