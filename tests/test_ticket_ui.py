"""Неденежная локальная заявка на общем экране сотрудника."""

from test_ticket_flow import ticket_service
from test_ui import EMPLOYEE, FakeService, visible_text

from support_agent.adapters.demo_model import DemoModel
from support_agent.proposals import Action, Proposal
from support_agent.storage import Store
from support_agent.workflow import SupportService

pytest_plugins = ["test_ui"]


def ticket_proposal(case_id):
    action = Action(
        name="create_support_ticket",
        arguments={
            "order_id": "ORDER-TICKET",
            "user_id": "client-one",
            "target": "local",
            "title": "Проверка результата обмена",
            "body": "Заявка на обмен зарегистрирована; исполнение не подтверждено.",
            "question": "Когда завершится обмен?",
        },
        amount=None,
        currency="",
        conditions="Нужна проверка исполнения",
        result_status="ticket created",
        description="Создать локальную заявку поддержки",
        evidence=[],
    )
    return Proposal(
        id="ticket-proposal",
        case_id=case_id,
        version=1,
        kind="action",
        text="Локальная учебная заявка зарегистрирована для проверки результата обмена.",
        action=action,
    )


def test_ticket_card_shows_local_target_title_body_question_without_money(open_app):
    service = FakeService()
    service.views[service.first].update(
        proposal=ticket_proposal(service.first), proposal_status="pending"
    )
    at = open_app(service)
    content = visible_text(at)
    for expected in (
        "Локальная учебная заявка",
        "Проверка результата обмена",
        "Когда завершится обмен?",
        "Заявка на обмен зарегистрирована; исполнение не подтверждено.",
        "Тема:",
        "Текст заявки:",
        "Нерешённый вопрос:",
    ):
        assert expected in content
    assert not at.metric
    assert "Способ оплаты" not in content
    assert not at.button(key="approve").disabled
    assert service.approvals == []


def test_local_ticket_can_be_registered_and_restored_from_same_screen(
    open_app, tmp_path, snapshot
):
    service = ticket_service(tmp_path, snapshot, DemoModel())
    case = service.create_case("u1", EMPLOYEE)
    at = open_app(service, case)
    at.text_area(key="request").set_value(
        "Проверьте результат обмена. Email user1@example.test"
    ).run()
    at.button(key="investigate").click().run()
    assert not at.exception
    prepared = service.view(case, EMPLOYEE)["proposal"]
    assert prepared.action.name == "create_support_ticket"
    assert prepared.action.arguments["title"] in visible_text(at)
    assert prepared.action.arguments["body"] in visible_text(at)
    assert service.store.ticket(prepared.id) is None
    at.button(key="approve").click().run()
    assert not at.exception
    assert "Локальная учебная заявка" in visible_text(at)
    assert "Номер заявки: T-1" in visible_text(at)
    assert "Статус заявки: open" in visible_text(at)
    assert "Ссылка на заявку" not in visible_text(at)
    assert service.view(case, EMPLOYEE)["deliveries"][0]["text"] == prepared.text
    restored = SupportService(
        Store(service.store.path), DemoModel(), service.adapter_factory
    )
    at = open_app(restored, case)
    assert "Номер заявки: T-1" in visible_text(at)
    assert len(restored.view(case, EMPLOYEE)["deliveries"]) == 1
    assert at.button(key="approve").disabled
