"""Сохранённое согласование проверяется повторно непосредственно перед POST."""

from copy import deepcopy

import pytest

pytest.importorskip("tau2")

from test_zammad_execution import FakeBackend, adapter, add_ticket, count

from support_agent.execution import Executor
from support_agent.storage import Store, encode


class SubmissionBackend(FakeBackend):
    def create(self, operation_id, request):
        self.creates.append((operation_id, request))
        return self.result()


@pytest.fixture
def reserved_ticket(tmp_path, snapshot):
    snapshot["orders"]["#O1"]["status"] = "exchange requested"
    store = Store(tmp_path / "integrity.sqlite3")
    store.initialize(snapshot, "Правила магазина")
    case_id = store.create_case("u1", "employee")
    proposal = add_ticket(store, case_id)
    backend = SubmissionBackend(store)
    executor = Executor(store, adapter, ticket_backend=backend)
    result = executor._approve(
        case_id, proposal.id, proposal.version, proposal.digest(), "employee"
    )
    assert result["status"] == "submitting"
    assert backend.creates == []
    return store, backend, proposal, executor


@pytest.mark.parametrize("change", ["body", "approval"])
def test_modified_saved_approval_blocks_post_after_reservation(reserved_ticket, change):
    store, backend, proposal, executor = reserved_ticket
    with store.transaction() as conn:
        if change == "body":
            request = deepcopy(proposal.action.arguments)
            request["body"] = "Неподтверждённый текст"
            conn.execute(
                "UPDATE external_ticket_operations SET request=? WHERE proposal_id=?",
                (encode(request), proposal.id),
            )
        else:
            conn.execute("DELETE FROM approvals WHERE proposal_id=?", (proposal.id,))
    with pytest.raises(ValueError, match="Сохранённые условия"):
        executor._submit_external(proposal.id)
    assert backend.creates == []
    assert count(store, "operations") == count(store, "outbox") == 0


def test_changed_backend_instance_blocks_post_after_reservation(reserved_ticket):
    store, backend, proposal, executor = reserved_ticket
    backend.base_url = "https://other.example.test"
    outcome = executor._submit_external(proposal.id)
    assert outcome["status"] == "unknown"
    assert backend.creates == []
    assert count(store, "operations") == count(store, "outbox") == 0
