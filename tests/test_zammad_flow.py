"""Один процесс подготовки, согласования и восстановления внешней заявки."""

from functools import partial

import pytest
from test_core import FakeModel
from test_zammad_execution import FakeBackend, adapter

from support_agent.adapters.demo_model import DemoModel
from support_agent.adapters.ticket_retail import TicketRetailAdapter
from support_agent.storage import Store
from support_agent.workflow import SupportService


def remote_service(tmp_path, snapshot, model=None):
    snapshot["orders"]["#O1"]["status"] = "exchange requested"
    store = Store(tmp_path / "remote-flow.sqlite3")
    store.initialize(snapshot, "Обмен зарегистрирован; исполнение проверяет поддержка.")
    backend = FakeBackend(store)
    service = SupportService(
        store, model or DemoModel(), adapter, ticket_backend=backend
    )
    service.ticket_target = "zammad"
    return service, backend


def prepare(service, employee="employee"):
    case = service.create_case("u1", employee)
    proposal = service.investigate(
        case, "Проверьте результат обмена. Email user1@example.test", employee
    )
    return case, proposal


def confirm(service, proposal, employee="employee"):
    return service.approve(
        proposal.case_id, proposal.id, proposal.version, proposal.digest(), employee
    )


def test_remote_flow_preserves_approved_text_and_result_after_restart(
    tmp_path, snapshot
):
    service, backend = remote_service(tmp_path, snapshot)
    before = service.store.snapshot()
    case, proposal = prepare(service)
    assert proposal.action.arguments["target"] == "zammad"
    assert proposal.consent_request is None
    assert "Zammad" in proposal.text and "локальн" not in proposal.text.casefold()
    assert "не подтверждено" in proposal.text
    assert backend.creates == []
    assert service.view(case, "employee")["deliveries"] == []
    assert confirm(service, proposal)["status"] == "delivered"
    restored = SupportService(
        Store(service.store.path), FakeModel(), adapter, ticket_backend=backend
    )
    view = restored.view(case, "employee")
    assert view["operation_result"]["ticket_number"] == "850071"
    assert view["operation_result"]["ticket_url"] == backend.result()["ticket_url"]
    assert [row["text"] for row in view["deliveries"]] == [proposal.text]
    assert "850071" not in proposal.text
    assert confirm(restored, proposal)["status"] == "delivered"
    assert len(backend.creates) == 1
    assert service.store.snapshot() == before


def test_unknown_blocks_replacement_and_restart_reconciles_without_model_or_reapproval(
    tmp_path, snapshot
):
    from support_agent.adapters.zammad import TicketOutcomeUnknown

    service, backend = remote_service(tmp_path, snapshot)
    backend.error = TicketOutcomeUnknown("Результат не получен")
    backend.found = False
    case, proposal = prepare(service)
    assert confirm(service, proposal)["status"] == "unknown"
    restored = SupportService(
        Store(service.store.path), FakeModel(), adapter, ticket_backend=backend
    )
    with pytest.raises(ValueError, match="результат.*Zammad"):
        restored.investigate(case, "Другой вопрос", "employee")
    assert restored.view(case, "employee")["proposal"].id == proposal.id
    assert restored.model.requests == []
    args = (case, proposal.id, proposal.version, proposal.digest(), "employee")
    assert restored.reconcile(*args)["status"] == "unknown"
    assert restored.view(case, "employee")["deliveries"] == []
    backend.found = True
    assert restored.reconcile(*args)["status"] == "delivered"
    assert len(backend.creates) == 1 and len(backend.finds) == 2
    assert restored.model.requests == []
    with restored.store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
    assert [row["text"] for row in restored.view(case, "employee")["deliveries"]] == [
        proposal.text
    ]


def test_duplicate_result_remains_visible_after_new_proposal_and_backend_switch(
    tmp_path, snapshot
):
    service, backend = remote_service(tmp_path, snapshot)
    case, proposal = prepare(service)
    confirm(service, proposal)
    service.adapter_factory = partial(TicketRetailAdapter, target="local")
    service.executor.adapter_factory = service.adapter_factory
    second = service.investigate(case, "Проверьте результат обмена ещё раз", "employee")
    result = confirm(service, second)
    assert result["status"] == "duplicate"
    restored = SupportService(
        Store(service.store.path), FakeModel(), service.adapter_factory
    )
    view = restored.view(case, "employee")
    assert view["proposal_status"] == "duplicate"
    assert view["operation_result"]["target"] == "zammad"
    assert view["operation_result"]["ticket_number"] == "850071"
    assert len(view["deliveries"]) == len(backend.creates) == 1
