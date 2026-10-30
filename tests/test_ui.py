"""Экран сотрудника: действия через виджеты, служба и доставка подменены."""

from copy import deepcopy
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from support_agent.proposals import Action, Evidence, Proposal

EMPLOYEE = "demo-employee"
APP = Path(__file__).parents[1] / "src/support_agent/ui.py"


def action():
    return Action(
        name="exchange_delivered_order_items",
        arguments={
            "order_id": "ORDER-1",
            "item_ids": ["old-item"],
            "new_item_ids": ["new-item"],
            "payment_method_id": "paypal-demo",
        },
        amount="16.85",
        currency="USD",
        conditions="Заказ доставлен; замена доступна; оплата PayPal",
        result_status="exchange requested",
        description="Зарегистрировать заявку на обмен наушников",
        evidence=[
            Evidence(title="Заказ", source="orders/ORDER-1", detail="Статус delivered"),
            Evidence(
                title="Правило обмена",
                source="policy/retail#exchange",
                detail="Нужно согласие клиента",
            ),
        ],
    )


def proposal(case_id, *, kind="action", version=1, text=None):
    return Proposal(
        id=f"proposal-{case_id}-{version}",
        case_id=case_id,
        version=version,
        kind=kind,
        text=text or "Заявка на обмен зарегистрирована. Доплата 16,85 USD.",
        action=action() if kind == "action" else None,
        evidence=[
            Evidence(
                title="Обращение",
                source=f"cases/{case_id}",
                detail="Клиент попросил обмен",
            )
        ],
    )


class FakeStore:
    def __init__(self):
        self.cases = []

    def list_cases(self, employee_id):
        assert employee_id == EMPLOYEE
        return [{"id": case["id"], "user_id": case["user_id"]} for case in self.cases]

    def snapshot(self):
        return {
            "users": {
                "client-one": {
                    "user_id": "client-one",
                    "email": "one@example.test",
                    "name": {"first_name": "Первый", "last_name": "Клиент"},
                },
                "client-two": {
                    "user_id": "client-two",
                    "email": "two@example.test",
                    "name": {"first_name": "Второй", "last_name": "Клиент"},
                },
            },
            "orders": {},
            "products": {},
        }, "Синтетические правила для проверки экрана"


class FakeService:
    def __init__(self):
        self.store = FakeStore()
        self.views = {}
        self.investigations = []
        self.approvals = []
        self.rejections = []
        self.client_responses = []
        self.created = []
        self.operations = []
        self.next_kind = "action"
        self.first = self.create_case("client-one", EMPLOYEE)
        self.second = self.create_case("client-two", EMPLOYEE)
        self.created.clear()

    def create_case(self, user_id, employee_id):
        assert employee_id == EMPLOYEE
        assert user_id in self.store.snapshot()[0]["users"]
        case_id = f"case-{len(self.store.cases) + 1}"
        case = {"id": case_id, "user_id": user_id, "employee_id": employee_id}
        self.store.cases.append(case)
        self.views[case_id] = {
            "case": case,
            "proposal": None,
            "proposal_status": "",
            "reason": "",
            "events": [],
            "deliveries": [],
            "waiting_consent": None,
        }
        self.created.append((user_id, employee_id))
        return case_id

    def view(self, case_id, employee_id):
        assert employee_id == EMPLOYEE
        return deepcopy(self.views[case_id])

    def investigate(self, case_id, text, employee_id):
        assert employee_id == EMPLOYEE
        self.investigations.append((case_id, text, employee_id))
        current = self.views[case_id]
        prepared = proposal(case_id, kind=self.next_kind)
        current["events"].append({"kind": "user", "text": text})
        current.update(proposal=prepared, proposal_status="pending", reason="")
        return prepared

    def approve(self, case_id, pid, version, digest, employee_id):
        self.approvals.append((case_id, pid, version, digest, employee_id))
        current = self.views[case_id]
        selected = current["proposal"]
        assert employee_id == EMPLOYEE
        assert (pid, version, digest) == (
            selected.id,
            selected.version,
            selected.digest(),
        )
        result = {"status": "exchange requested"} if selected.action else {"sent": True}
        if selected.action:
            self.operations.append(result)
        current["deliveries"].append(
            {"id": "delivery-1", "case_id": case_id, "text": selected.text}
        )
        current["events"].append({"kind": "operation", "text": str(result)})
        current["proposal_status"] = "delivered"
        return {
            "status": current["proposal_status"],
            "result": result,
            "delivered": True,
        }

    def reject(self, case_id, pid, version, digest, reason, employee_id):
        self.rejections.append((case_id, pid, version, digest, reason, employee_id))
        current = self.views[case_id]
        selected = current["proposal"]
        assert employee_id == EMPLOYEE
        assert (pid, version, digest) == (
            selected.id,
            selected.version,
            selected.digest(),
        )
        replacement = proposal(
            case_id,
            kind="message",
            version=version + 1,
            text="Уточните подходящий способ оплаты.",
        )
        current["events"].append({"kind": "rejection", "text": reason})
        current.update(proposal=replacement, proposal_status="pending", reason=reason)
        return replacement

    def record_client_response(self, case_id, qid, accepted, response, employee_id):
        self.client_responses.append((case_id, qid, accepted, response, employee_id))
        current = self.views[case_id]
        assert employee_id == EMPLOYEE
        assert qid == current["waiting_consent"].id
        replacement = proposal(
            case_id,
            kind="action" if accepted else "message",
            version=2,
            text="Новый проект после ответа клиента.",
        )
        current["events"].append({"kind": "client", "text": response})
        current.update(
            proposal=replacement, proposal_status="pending", waiting_consent=None
        )
        return replacement


@pytest.fixture
def service():
    return FakeService()


@pytest.fixture
def open_app(monkeypatch):
    import dotenv
    import litellm

    def forbidden(*args, **kwargs):
        pytest.fail("UI-тест не должен читать ключи или обращаться к настоящей модели")

    monkeypatch.setattr(dotenv, "load_dotenv", forbidden)
    monkeypatch.setattr(dotenv, "dotenv_values", forbidden)
    monkeypatch.setattr(litellm, "completion", forbidden)

    def start(service, case_id=None):
        at = AppTest.from_file(str(APP), default_timeout=10)
        at.session_state["_service"] = service
        at.session_state["case_id"] = case_id or service.first
        at.run()
        assert not at.exception
        return at

    return start


def visible_text(at):
    kinds = (
        "title",
        "header",
        "subheader",
        "markdown",
        "caption",
        "text",
        "code",
        "json",
        "success",
        "warning",
        "info",
        "error",
        "metric",
    )
    return "\n".join(str(element.value) for kind in kinds for element in at.get(kind))


@pytest.mark.parametrize("mode", [None, "demo", "api"])
def test_screen_identifies_model_mode_and_explains_demo_limits(open_app, service, mode):
    from support_agent.adapters.demo_model import INSTRUCTION

    if mode is not None:
        service.mode = mode
    at = open_app(service)
    content = visible_text(at)
    if mode == "api":
        assert "Режим API: подготовка обращается к настроенной модели" in content
        assert INSTRUCTION not in content
    else:
        assert "Учебный режим без API: ответы задаёт заглушка" in content
        assert INSTRUCTION in content
    assert service.investigations == []


def test_bootstrap_configuration_error_is_displayed_without_traceback(monkeypatch):
    from support_agent import bootstrap

    def unavailable():
        raise ValueError(
            "Не удалось прочитать исходный магазин. Проверьте путь к данным."
        )

    monkeypatch.setattr(bootstrap, "create_service", unavailable)
    at = AppTest.from_file(str(APP), default_timeout=10).run()
    assert not at.exception
    assert any("Проверьте путь к данным" in error.value for error in at.error)
    assert not at.button
    assert "_service" not in at.session_state


def test_investigation_shows_history_evidence_amount_and_exact_draft_without_sending(
    open_app, service
):
    at = open_app(service)
    request = "Прошу обменять наушники из ORDER-1."
    at.text_area(key="request").set_value(request).run()
    assert service.investigations == []
    at.button(key="investigate").click().run()
    assert not at.exception
    prepared = service.views[service.first]["proposal"]
    content = visible_text(at)
    for expected in (
        request,
        prepared.text,
        prepared.action.description,
        "orders/ORDER-1",
        "policy/retail#exchange",
        "ORDER-1",
        "USD",
    ):
        assert expected in content
    assert "16.85" in content or "16,85" in content
    assert not at.button(key="approve").disabled
    assert not at.button(key="reject").disabled
    assert service.views[service.first]["deliveries"] == []
    assert service.operations == []
    at.run()
    assert service.investigations == [(service.first, request, EMPLOYEE)]


def test_approval_uses_displayed_version_and_digest_and_shows_execution_result(
    open_app, service
):
    prepared = proposal(service.first, version=3)
    service.views[service.first].update(proposal=prepared, proposal_status="pending")
    at = open_app(service)
    at.button(key="approve").click().run()
    assert not at.exception
    expected = (service.first, prepared.id, 3, prepared.digest(), EMPLOYEE)
    assert service.approvals == [expected]
    assert "exchange requested" in visible_text(at)
    assert service.views[service.first]["deliveries"][0]["text"] == prepared.text
    at.run()
    assert service.approvals == [expected]


def test_rejection_passes_reason_and_replaces_the_card_without_sending(
    open_app, service
):
    prepared = proposal(service.first, version=3)
    service.views[service.first].update(proposal=prepared, proposal_status="pending")
    at = open_app(service)
    reason = "Способ оплаты не подтверждён клиентом."
    at.text_area(key="rejection_reason").set_value(reason).run()
    at.button(key="reject").click().run()
    assert not at.exception
    assert service.rejections == [
        (service.first, prepared.id, 3, prepared.digest(), reason, EMPLOYEE)
    ]
    assert "Уточните подходящий способ оплаты." in visible_text(at)
    assert reason in visible_text(at)
    assert service.views[service.first]["deliveries"] == []
    assert service.operations == []


def test_message_only_draft_is_not_sent_before_employee_approval(open_app, service):
    service.next_kind = "message"
    at = open_app(service)
    at.text_area(key="request").set_value("Нужно уточнить условия обмена.").run()
    at.button(key="investigate").click().run()
    assert not at.exception
    prepared = service.views[service.first]["proposal"]
    assert prepared.text in visible_text(at)
    assert service.views[service.first]["deliveries"] == []
    assert service.operations == []
    at.button(key="approve").click().run()
    assert not at.exception
    assert service.views[service.first]["deliveries"][0]["text"] == prepared.text
    assert service.operations == []


def test_selecting_another_case_does_not_keep_previous_history_or_draft(
    open_app, service
):
    first_text, second_text = (
        "Личные сведения первого клиента",
        "Обращение второго клиента",
    )
    for case_id, text in ((service.first, first_text), (service.second, second_text)):
        service.views[case_id].update(
            proposal=proposal(case_id, kind="message", text=text),
            proposal_status="pending",
            events=[{"kind": "user", "text": text}],
        )
    at = open_app(service)
    at.text_area(key="request").set_value(
        "Неотправленное уточнение первому клиенту"
    ).run()
    at.selectbox(key="case_selector").select(service.second).run()
    assert not at.exception
    assert at.session_state["case_id"] == service.second
    assert second_text in visible_text(at)
    assert first_text not in visible_text(at)
    assert at.text_area(key="request").value == ""
    selected = service.views[service.second]["proposal"]
    at.button(key="approve").click().run()
    assert service.approvals == [
        (service.second, selected.id, selected.version, selected.digest(), EMPLOYEE)
    ]


def test_new_case_starts_without_previous_proposal_and_rerun_can_restore_saved_case(
    open_app, service
):
    service.views[service.first].update(
        proposal=proposal(service.first, text="Сохранённое предложение"),
        proposal_status="pending",
        events=[{"kind": "user", "text": "Сохранённая история"}],
    )
    at = open_app(service)
    at.selectbox(key="user_id").select("client-two").run()
    at.button(key="new_case").click().run()
    assert not at.exception
    assert service.created == [("client-two", EMPLOYEE)]
    assert at.session_state["case_id"] not in (service.first, service.second)
    assert "Сохранённое предложение" not in visible_text(at)
    assert "Сохранённая история" not in visible_text(at)
    restored = open_app(service, service.first)
    assert "Сохранённое предложение" in visible_text(restored)
    assert "Сохранённая история" in visible_text(restored)
    assert service.investigations == []


@pytest.mark.parametrize(
    "accepted,button", [(True, "client_accept"), (False, "client_reject")]
)
def test_client_response_is_explicitly_a_test_channel_and_does_not_approve_action(
    open_app, service, accepted, button
):
    question = proposal(
        service.first, kind="message", text="Подтверждаете обмен с доплатой 16,85 USD?"
    )
    question = question.model_copy(update={"consent_request": action()})
    service.views[service.first].update(
        proposal=question,
        proposal_status="sent",
        waiting_consent=question,
        deliveries=[
            {"id": "question-delivery", "case_id": service.first, "text": question.text}
        ],
    )
    at = open_app(service)
    assert "тестов" in visible_text(at).lower()
    response = (
        "Согласен на указанные условия." if accepted else "Не согласен с доплатой."
    )
    at.text_area(key="client_response").set_value(response).run()
    at.button(key=button).click().run()
    assert not at.exception
    assert service.client_responses == [
        (service.first, question.id, accepted, response, EMPLOYEE)
    ]
    assert "Новый проект после ответа клиента." in visible_text(at)
    assert service.approvals == []
    assert service.operations == []
    assert len(service.views[service.first]["deliveries"]) == 1
