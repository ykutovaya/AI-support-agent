from uuid import uuid4

import pytest

pytest.importorskip("tau2")
from test_retail import exchange_args

from support_agent.adapters.retail import RetailAdapter
from support_agent.execution import Executor, TestChannel
from support_agent.proposals import Proposal
from support_agent.storage import Store


@pytest.fixture
def setup(tmp_path, snapshot):
    store = Store(tmp_path / "app.sqlite3")
    store.initialize(snapshot, "Правила магазина")
    case = store.create_case("u1", "employee")
    return store, case, Executor(store, RetailAdapter)


def add(store, case, kind="message", **kwargs):
    with store.transaction() as conn:
        p = Proposal(
            id=uuid4().hex,
            case_id=case,
            version=store.next_version(conn, case),
            kind=kind,
            text="Заявка создана." if kind == "action" else "Уточните товар.",
            **kwargs,
        )
        store.add_proposal(conn, p)
    return p


def approve(executor, p, actor="employee"):
    return executor.approve(p.case_id, p.id, p.version, p.digest(), actor)


def prepared(setup, consent=True):
    store, case, executor = setup
    state, policy = store.snapshot()
    action = RetailAdapter(state, policy).plan(
        "propose_exchange", exchange_args(), "u1"
    )
    q = add(store, case, consent_request=action)
    approve(executor, q)
    if consent:
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO consents(id,case_id,question_id,action_hash,accepted,response) VALUES (?,?,?,?,1,?)",
                (uuid4().hex, case, q.id, action.fingerprint(), "Согласен"),
            )
    return add(store, case, "action", action=action)


def test_message_is_delivered_only_after_confirmation_once(setup):
    store, case, executor = setup
    before = store.snapshot()
    p = add(store, case)
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
    assert approve(executor, p)["status"] == "delivered"
    assert (
        approve(Executor(Store(store.path), RetailAdapter), p)["status"] == "delivered"
    )
    with store.connection() as conn:
        assert [r[0] for r in conn.execute("SELECT text FROM deliveries")] == [p.text]
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
    assert store.snapshot() == before


def test_employee_approval_cannot_replace_customer_consent(setup):
    store, _, executor = setup
    p = prepared(setup, consent=False)
    assert approve(executor, p)["status"] == "stale"
    assert store.snapshot()[0]["orders"]["#O1"]["status"] == "delivered"


def test_action_executes_once_after_both_consents_across_restart(setup):
    store, _, executor = setup
    p = prepared(setup)
    first = approve(executor, p)
    assert first["status"] == "delivered"
    assert first["operation_result"]["status"] == "exchange requested"
    repeated = approve(Executor(Store(store.path), RetailAdapter), p)
    assert repeated == first
    assert store.snapshot()[0]["orders"]["#O1"]["status"] == "exchange requested"
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM deliveries WHERE text=?", (p.text,)
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("change", ["price", "status", "policy"])
def test_changed_conditions_block_old_approval(setup, change):
    store, _, executor = setup
    p = prepared(setup)
    with store.transaction() as conn:
        data, _ = store.retail(conn)
        if change == "price":
            data["products"]["p1"]["variants"]["new"]["price"] = 99.0
        elif change == "status":
            data["orders"]["#O1"]["status"] = "pending"
        else:
            conn.execute("UPDATE retail_state SET policy='Новые правила'")
        store.save_retail(conn, data)
    assert approve(executor, p)["status"] == "stale"
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0


@pytest.mark.parametrize("wrong", ["employee", "version", "digest"])
def test_wrong_employee_or_proposal_binding_cannot_confirm(setup, wrong):
    _, _, executor = setup
    p = prepared(setup)
    with pytest.raises(ValueError):
        executor.approve(
            p.case_id,
            p.id,
            p.version + (wrong == "version"),
            "changed" if wrong == "digest" else p.digest(),
            "intruder" if wrong == "employee" else "employee",
        )


def test_failed_write_rolls_back_and_requires_new_response_approval(setup):
    store, _, _ = setup
    p = prepared(setup)
    before = store.snapshot()

    class Broken(RetailAdapter):
        def execute(self, action, user_id):
            super().execute(action, user_id)
            raise RuntimeError("Сбой после изменения рабочей копии")

    result = approve(Executor(store, Broken), p)
    assert result["status"] == "failed"
    assert store.snapshot() == before
    latest = store.latest(p.case_id)
    assert latest["proposal"].id != p.id and latest["status"] == "pending"
    assert latest["proposal"].kind == "message"
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE proposal_id=?", (p.id,)
            ).fetchone()[0]
            == 0
        )


def test_delivery_failure_and_retry_never_repeat_write_or_delivery(setup):
    store, _, _ = setup
    p = prepared(setup)

    class InterruptedChannel(TestChannel):
        failed = False

        def send(self, delivery_id, case_id, text):
            super().send(delivery_id, case_id, text)
            if not self.failed:
                self.failed = True
                raise RuntimeError("Обрыв после приёма сообщения")

    executor = Executor(store, RetailAdapter, channel=InterruptedChannel(store))
    assert approve(executor, p)["status"] == "delivery_failed"
    assert approve(executor, p)["status"] == "delivered"
    events = store.events(p.case_id)
    assert sum(event["kind"] == "approval" for event in events) == 2
    assert sum(event["kind"] == "delivery_retry" for event in events) == 1
    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM deliveries WHERE text=?", (p.text,)
            ).fetchone()[0]
            == 1
        )
