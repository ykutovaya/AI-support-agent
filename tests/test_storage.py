import pytest

from support_agent.models import AgentState, Message
from support_agent.proposals import Proposal
from support_agent.storage import Store


def test_restart_restores_case_proposal_and_working_snapshot(tmp_path):
    path = tmp_path / "app.sqlite3"
    store = Store(path)
    source = {"users": {"u": {}}, "orders": {"a": {"status": "delivered"}}}
    store.initialize(source, "Правила")
    case = store.create_case("u", "employee")
    with store.transaction() as conn:
        current = store.load_case(conn, case)
        current["state"] = AgentState(
            messages=[Message(role="user", content="Возврат")]
        )
        store.save_case(conn, current, current["revision"])
        store.add_proposal(
            conn,
            Proposal(
                id="p", case_id=case, version=1, kind="message", text="Уточните товар."
            ),
        )
        snapshot, _ = store.retail(conn)
        snapshot["orders"]["a"]["status"] = "return requested"
        store.save_retail(conn, snapshot)
    reopened = Store(path)
    reopened.initialize(source, "Другие правила не заменяют сохранённые автоматически")
    assert reopened.case(case)["state"].messages[0].content == "Возврат"
    assert reopened.latest(case)["proposal"].text == "Уточните товар."
    assert reopened.snapshot()[0]["orders"]["a"]["status"] == "return requested"
    assert source["orders"]["a"]["status"] == "delivered"


def test_failed_transaction_saves_neither_snapshot_nor_operation(tmp_path):
    store = Store(tmp_path / "app.sqlite3")
    store.initialize({"users": {"u": {}}, "orders": {}}, "Правила")
    case = store.create_case("u", "employee")
    with pytest.raises(RuntimeError):
        with store.transaction() as conn:
            store.save_retail(conn, {"corrupted": True})
            store.event(conn, case, "operation", "Частичное изменение")
            raise RuntimeError("сбой")
    assert "corrupted" not in store.snapshot()[0]
    assert store.events(case) == []


def test_old_model_response_cannot_overwrite_new_case_state(tmp_path):
    store = Store(tmp_path / "app.sqlite3")
    store.initialize({"users": {"u": {}}}, "Правила")
    case = store.create_case("u", "employee")
    old = store.case(case)
    with store.transaction() as conn:
        store.save_case(conn, old, old["revision"])
    with pytest.raises(ValueError, match="изменилось"):
        with store.transaction() as conn:
            store.save_case(conn, old, old["revision"])
