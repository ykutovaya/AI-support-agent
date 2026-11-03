"""Удалённая заявка и чтение неизвестного результата в существующей карточке."""

import pytest
import test_ui
from test_ticket_ui import ticket_proposal
from test_ui import EMPLOYEE, FakeService, visible_text

open_app = test_ui.open_app


def remote_proposal(case_id):
    proposal = ticket_proposal(case_id)
    proposal.action.arguments.update(
        target="zammad",
        destination={
            "base_url": "https://support.example.test",
            "group_id": 7,
            "group_name": "Тестовая группа",
            "customer_id": 31,
        },
    )
    proposal.action.description = "Создать заявку в Zammad"
    proposal.text = (
        "Заявка в Zammad зарегистрирована; исполнение обмена не подтверждено."
    )
    return proposal


def test_remote_card_shows_exact_contents_and_destination_without_local_claim(open_app):
    service = FakeService()
    service.ticket_target = "zammad"
    proposal = remote_proposal(service.first)
    service.views[service.first].update(proposal=proposal, proposal_status="pending")
    at = open_app(service)
    content = visible_text(at)
    for expected in (
        "Заявка в Zammad",
        "support.example.test",
        "Тестовая группа",
        "31",
        proposal.action.arguments["title"],
        proposal.action.arguments["body"],
        proposal.text,
    ):
        assert expected in content
    assert "Локальная учебная заявка" not in content
    assert "Для локальной учебной заявки" not in content
    assert not at.metric
    assert not at.button(key="approve").disabled


@pytest.mark.parametrize("status", ["submitting", "unknown"])
def test_unknown_card_reconciles_original_approval_without_recreating(open_app, status):
    service = FakeService()
    proposal = remote_proposal(service.first)
    service.views[service.first].update(proposal=proposal, proposal_status=status)
    reads = []

    def reconcile(*args):
        reads.append(args)
        return {"status": "unknown", "reason": "Заявка пока не найдена"}

    service.reconcile = reconcile
    at = open_app(service)
    assert (
        "Исход создания неизвестен"
        if status == "unknown"
        else "Создание в Zammad начато"
    ) in visible_text(at)
    assert at.button(key="approve").disabled
    assert at.button(key="reject").disabled
    at.button(key="reconcile").click().run()
    assert reads == [
        (proposal.case_id, proposal.id, proposal.version, proposal.digest(), EMPLOYEE)
    ]
    assert service.approvals == [] and service.investigations == []
    assert "Заявка пока не найдена" in visible_text(at)
    assert not at.success


def test_remote_registration_and_readback_restore_on_same_screen(
    open_app, tmp_path, snapshot
):
    from test_core import FakeModel
    from test_zammad_flow import remote_service

    from support_agent.storage import Store
    from support_agent.workflow import SupportService

    service, backend = remote_service(tmp_path, snapshot)
    case = service.create_case("u1", EMPLOYEE)
    at = open_app(service, case)
    at.text_area(key="request").set_value(
        "Проверьте результат обмена. Email user1@example.test"
    ).run()
    at.button(key="investigate").click().run()
    proposal = service.view(case, EMPLOYEE)["proposal"]
    assert proposal.action.arguments["body"] in visible_text(at)
    assert not backend.creates
    at.button(key="approve").click().run()
    assert not at.exception
    assert "Номер заявки: 850071" in visible_text(at)
    assert "Статус заявки: new" in visible_text(at)
    assert any(backend.result()["ticket_url"] in block.value for block in at.markdown)
    restored = SupportService(
        Store(service.store.path),
        FakeModel(),
        service.adapter_factory,
        ticket_backend=backend,
    )
    restored.ticket_target = "zammad"
    at = open_app(restored, case)
    assert "Номер заявки: 850071" in visible_text(at)
    assert [row["text"] for row in restored.view(case, EMPLOYEE)["deliveries"]] == [
        proposal.text
    ]
    assert "850071" not in proposal.text
    assert at.button(key="approve").disabled and len(backend.creates) == 1


def test_unknown_screen_restores_then_only_reads_result(open_app, tmp_path, snapshot):
    from test_core import FakeModel
    from test_zammad_flow import prepare, remote_service

    from support_agent.adapters.zammad import TicketOutcomeUnknown
    from support_agent.storage import Store
    from support_agent.workflow import SupportService

    service, backend = remote_service(tmp_path, snapshot)
    case, proposal = prepare(service, EMPLOYEE)
    backend.error = TicketOutcomeUnknown("Не получен ответ")
    backend.found = False
    at = open_app(service, case)
    at.button(key="approve").click().run()
    assert "Исход создания неизвестен" in visible_text(at)
    assert not service.view(case, EMPLOYEE)["deliveries"]
    restored = SupportService(
        Store(service.store.path),
        FakeModel(),
        service.adapter_factory,
        ticket_backend=backend,
    )
    at = open_app(restored, case)
    assert at.button(key="approve").disabled
    backend.found = True
    at.button(key="reconcile").click().run()
    assert not at.exception
    assert "Номер заявки: 850071" in visible_text(at)
    assert len(backend.creates) == len(backend.finds) == 1
    assert restored.model.requests == []
    with restored.store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
    assert [row["text"] for row in restored.view(case, EMPLOYEE)["deliveries"]] == [
        proposal.text
    ]


def test_duplicate_remote_result_displays_verified_number_status_and_link(open_app):
    service = FakeService()
    service.views[service.first].update(
        proposal=remote_proposal(service.first),
        proposal_status="duplicate",
        operation_result={
            "status": "ticket created",
            "target": "zammad",
            "ticket_number": "83001",
            "ticket_status": "new",
            "ticket_url": "https://support.example.test/#ticket/zoom/42",
        },
    )
    at = open_app(service)
    content = visible_text(at)
    assert "Номер заявки: 83001" in content
    assert "Статус заявки: new" in content
    assert "Локальная учебная заявка" not in content
    assert any(
        "https://support.example.test/#ticket/zoom/42" in block.value
        for block in at.markdown
    )
    assert at.button(key="approve").disabled
