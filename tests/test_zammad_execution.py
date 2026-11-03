"""Внешняя регистрация не повторяется при неизвестном исходе и повторе доставки."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest

pytest.importorskip("tau2")

from support_agent.adapters.ticket_retail import TicketRetailAdapter
from support_agent.execution import Executor, TestChannel
from support_agent.proposals import Proposal
from support_agent.storage import Store

DESTINATION = {
    "base_url": "https://support.example.test",
    "group_id": 2,
    "group_name": "Support",
    "customer_id": 4,
}


def adapter(snapshot, policy):
    return TicketRetailAdapter(
        snapshot,
        policy,
        target="zammad",
        destination={
            key: value for key, value in DESTINATION.items() if key != "customer_id"
        },
        customer_mapping={"u1": DESTINATION["customer_id"]},
    )


def local_adapter(snapshot, policy):
    return TicketRetailAdapter(snapshot, policy, target="local")


def add_ticket(store, case_id, question="Каков результат обмена?", factory=adapter):
    snapshot, policy = store.snapshot()
    action = factory(snapshot, policy).plan(
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


def approve(executor, proposal, **changes):
    arguments = dict(
        case_id=proposal.case_id,
        proposal_id=proposal.id,
        version=proposal.version,
        expected_digest=proposal.digest(),
        employee_id="employee",
    )
    arguments.update(changes)
    return executor.approve(**arguments)


def reconcile(executor, proposal):
    return executor.reconcile(
        proposal.case_id, proposal.id, proposal.version, proposal.digest(), "employee"
    )


def count(store, table):
    with store.connection() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class FakeBackend:
    base_url = DESTINATION["base_url"]

    def __init__(self, store):
        self.store = store
        self.creates = []
        self.finds = []
        self.error = None
        self.found = True
        self.entered = None
        self.release = None

    def result(self):
        return {
            "status": "ticket created",
            "target": "zammad",
            "ticket_id": 71,
            "ticket_number": "850071",
            "ticket_status": "new",
            "ticket_url": self.base_url + "/#ticket/zoom/71",
            "group_id": 2,
            "customer_id": 4,
            "duplicate": False,
        }

    def create(self, operation_id, request):
        # Независимая запись возможна только после фиксации короткой транзакции.
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM external_ticket_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            assert row["status"] == "submitting"
            assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
        self.creates.append((operation_id, request))
        if self.entered:
            self.entered.set()
            assert self.release.wait(5)
        if self.error:
            raise self.error
        return self.result()

    def find(self, operation_id, request, ticket_id=None):
        with self.store.transaction():
            pass
        self.finds.append((operation_id, request, ticket_id))
        return self.result() if self.found else None


@pytest.fixture
def remote_setup(tmp_path, snapshot):
    snapshot["orders"]["#O1"]["status"] = "exchange requested"
    store = Store(tmp_path / "remote.sqlite3")
    store.initialize(snapshot, "Правила магазина")
    case_id = store.create_case("u1", "employee")
    backend = FakeBackend(store)
    return store, backend, add_ticket(store, case_id)


def test_confirm_registers_once_and_delivers_exact_approved_text(remote_setup):
    store, backend, p = remote_setup
    executor = Executor(store, adapter, ticket_backend=backend)
    assert backend.creates == [] and count(store, "outbox") == 0
    first = approve(executor, p)
    assert first == {
        "status": "delivered",
        "proposal_id": p.id,
        "operation_result": backend.result(),
    }
    assert approve(executor, p) == first
    assert (
        len(backend.creates)
        == count(store, "operations")
        == count(store, "outbox")
        == 1
    )
    assert count(store, "support_tickets") == 0
    saved = store.external_ticket(p.id)
    assert saved["request"] == p.action.arguments and saved["digest"] == p.digest()
    assert saved["text"] == p.text and saved["result"] == backend.result()
    with store.connection() as conn:
        assert conn.execute("SELECT text FROM deliveries").fetchone()[0] == p.text


@pytest.mark.parametrize("invalid", ["employee", "version", "digest"])
def test_invalid_confirmation_cannot_call_backend(remote_setup, invalid):
    store, backend, p = remote_setup
    options = (
        {"employee_id": "intruder"}
        if invalid == "employee"
        else {"version": p.version + 1}
        if invalid == "version"
        else {"expected_digest": "changed"}
    )
    with pytest.raises(ValueError):
        approve(Executor(store, adapter, ticket_backend=backend), p, **options)
    assert backend.creates == [] and count(store, "outbox") == 0


@pytest.mark.parametrize(
    "change", ["policy", "status", "ownership", "body", "target", "destination"]
)
def test_changed_conditions_block_post(remote_setup, change):
    store, backend, p = remote_setup
    with store.transaction() as conn:
        snapshot, _ = store.retail(conn)
        if change == "policy":
            conn.execute("UPDATE retail_state SET policy='Новые правила'")
        elif change in {"body", "target", "destination"}:
            p.action.arguments[change] = (
                {**DESTINATION, "group_id": 8} if change == "destination" else "Подмена"
            )
            conn.execute(
                "UPDATE proposals SET body=? WHERE id=?", (p.model_dump_json(), p.id)
            )
        elif change == "ownership":
            snapshot["orders"]["#O1"]["user_id"] = "u2"
        else:
            snapshot["orders"]["#O1"]["status"] = "delivered"
        store.save_retail(conn, snapshot)
    assert (
        approve(Executor(store, adapter, ticket_backend=backend), p)["status"]
        == "stale"
    )
    assert backend.creates == [] and count(store, "outbox") == 0


@pytest.mark.parametrize("factory", [adapter, local_adapter])
def test_rephrasing_or_mode_change_cannot_register_completed_ticket_again(
    remote_setup, factory
):
    store, backend, p = remote_setup
    first = approve(Executor(store, adapter, ticket_backend=backend), p)
    another = add_ticket(store, p.case_id, "Есть ли новости?", factory)
    executor = Executor(
        store, factory, ticket_backend=backend if factory is adapter else None
    )
    outcome = approve(executor, another)
    assert outcome["status"] == "duplicate"
    assert outcome["operation_result"] == first["operation_result"]
    assert approve(executor, another) == outcome
    assert len(backend.creates) == count(store, "outbox") == 1
    assert count(store, "support_tickets") == 0


def test_timeout_after_write_restarts_with_get_only_and_same_request(remote_setup):
    from support_agent.adapters.zammad import TicketOutcomeUnknown

    store, backend, p = remote_setup
    backend.error = TicketOutcomeUnknown("TOKEN must not escape", ticket_id=71)
    assert (
        approve(Executor(store, adapter, ticket_backend=backend), p)["status"]
        == "unknown"
    )
    assert count(store, "operations") == count(store, "outbox") == 0
    restarted = Executor(Store(store.path), adapter, ticket_backend=backend)
    assert approve(restarted, p)["status"] == "unknown"
    assert reconcile(restarted, p)["status"] == "delivered"
    assert backend.finds == [(backend.creates[0][0], p.action.arguments, 71)]
    assert len(backend.creates) == count(store, "outbox") == 1
    assert "TOKEN" not in str(store.events(p.case_id))


@pytest.mark.parametrize("found", [False, "wrong_instance"])
def test_unresolved_or_changed_instance_stays_unknown_without_delivery(
    remote_setup, found
):
    from support_agent.adapters.zammad import TicketOutcomeUnknown

    store, backend, p = remote_setup
    backend.error = TicketOutcomeUnknown("readback mismatch", ticket_id=71)
    executor = Executor(store, adapter, ticket_backend=backend)
    assert approve(executor, p)["status"] == "unknown"
    backend.found = False
    if found == "wrong_instance":
        backend.base_url = "https://different.example.test"
    assert reconcile(executor, p)["status"] == "unknown"
    assert count(store, "operations") == count(store, "outbox") == 0
    assert len(backend.creates) == 1
    if found == "wrong_instance":
        assert backend.finds == []


def test_unknown_blocks_new_local_registration(remote_setup):
    from support_agent.adapters.zammad import TicketOutcomeUnknown

    store, backend, p = remote_setup
    backend.error = TicketOutcomeUnknown("timeout")
    approve(Executor(store, adapter, ticket_backend=backend), p)
    another = add_ticket(store, p.case_id, factory=local_adapter)
    outcome = approve(Executor(store, local_adapter), another)
    assert outcome["status"] == "unknown" and outcome["proposal_id"] == p.id
    assert count(store, "support_tickets") == count(store, "outbox") == 0
    assert len(backend.creates) == 1


def test_known_rejection_requires_approval_of_new_error_message(remote_setup):
    from support_agent.adapters.zammad import TicketRejected

    store, backend, p = remote_setup
    backend.error = TicketRejected("token=secret HTTP403")
    executor = Executor(store, adapter, ticket_backend=backend)
    assert approve(executor, p)["status"] == "failed"
    assert count(store, "operations") == count(store, "outbox") == 0
    failure = store.latest(p.case_id)
    assert failure["status"] == "pending" and failure["proposal"].kind == "message"
    assert failure["proposal"].id != p.id
    assert "secret" not in str(store.events(p.case_id))
    assert approve(executor, failure["proposal"])["status"] == "delivered"
    assert len(backend.creates) == 1


def test_delivery_retry_does_not_register_again(remote_setup):
    store, backend, p = remote_setup

    class Interrupted(TestChannel):
        def send(self, delivery_id, case_id, text):
            super().send(delivery_id, case_id, text)
            raise TimeoutError("Потерян ответ о доставке")

    executor = Executor(store, adapter, Interrupted(store), ticket_backend=backend)
    assert approve(executor, p)["status"] == "delivery_failed"
    assert (
        approve(Executor(Store(store.path), adapter, ticket_backend=backend), p)[
            "status"
        ]
        == "delivered"
    )
    assert (
        len(backend.creates)
        == count(store, "outbox")
        == count(store, "deliveries")
        == 1
    )


def test_concurrent_confirmation_has_one_post(remote_setup):
    store, backend, p = remote_setup
    backend.entered, backend.release = Event(), Event()
    executor = Executor(store, adapter, ticket_backend=backend)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(approve, executor, p)
        assert backend.entered.wait(5)
        try:
            second = pool.submit(approve, executor, p).result(timeout=5)
            assert second["status"] in {"submitting", "unknown"}
        finally:
            backend.release.set()
        assert first.result(timeout=5)["status"] == "delivered"
    assert len(backend.creates) == count(store, "outbox") == 1


def test_stopped_process_leaves_submitting_and_restarts_with_get_only(remote_setup):
    store, backend, p = remote_setup
    backend.error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        approve(Executor(store, adapter, ticket_backend=backend), p)
    assert store.external_ticket(p.id)["status"] == "submitting"
    restarted = Executor(Store(store.path), adapter, ticket_backend=backend)
    assert approve(restarted, p)["status"] == "unknown"
    assert reconcile(restarted, p)["status"] == "delivered"
    assert len(backend.creates) == count(store, "outbox") == 1


@pytest.mark.parametrize("change", ["group_id", "customer_id", "ticket_url"])
def test_mismatched_result_keeps_id_without_success_delivery(remote_setup, change):
    store, backend, p = remote_setup
    original = backend.result
    backend.result = lambda: {**original(), change: "wrong"}
    executor = Executor(store, adapter, ticket_backend=backend)
    assert approve(executor, p)["status"] == "unknown"
    assert store.external_ticket(p.id)["ticket_id"] == 71
    assert count(store, "operations") == count(store, "outbox") == 0
    backend.result = original
    assert reconcile(executor, p)["status"] == "delivered"
    assert backend.finds[0][2] == 71 and len(backend.creates) == 1


def test_local_registration_cannot_be_repeated_after_enabling_zammad(remote_setup):
    store, backend, p = remote_setup
    local = add_ticket(store, p.case_id, factory=local_adapter)
    first = approve(Executor(store, local_adapter), local)
    remote = add_ticket(store, p.case_id)
    result = approve(Executor(store, adapter, ticket_backend=backend), remote)
    assert result["status"] == "duplicate"
    assert result["operation_result"] == first["operation_result"]
    assert (
        backend.creates == []
        and count(store, "support_tickets") == count(store, "outbox") == 1
    )


@pytest.mark.parametrize("invalid", ["approval", "employee", "digest", "text"])
def test_reconcile_requires_original_approval_and_access(remote_setup, invalid):
    from support_agent.adapters.zammad import TicketOutcomeUnknown

    store, backend, p = remote_setup
    backend.error = TicketOutcomeUnknown()
    executor = Executor(store, adapter, ticket_backend=backend)
    approve(executor, p)
    if invalid in {"approval", "text"}:
        with store.transaction() as conn:
            if invalid == "approval":
                conn.execute("DELETE FROM approvals WHERE proposal_id=?", (p.id,))
            else:
                p.text = "Изменённый текст"
                conn.execute(
                    "UPDATE proposals SET body=? WHERE id=?",
                    (p.model_dump_json(), p.id),
                )
    with pytest.raises(ValueError):
        executor.reconcile(
            p.case_id,
            p.id,
            p.version,
            "wrong" if invalid == "digest" else p.digest(),
            "intruder" if invalid == "employee" else "employee",
        )
    assert backend.finds == [] and len(backend.creates) == 1
    assert count(store, "outbox") == 0
