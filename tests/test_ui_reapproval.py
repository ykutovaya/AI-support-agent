"""Показ изменившихся условий и ручное продолжение через существующую карточку."""

from decimal import Decimal

import pytest
import test_ui
from test_ui import EMPLOYEE, FakeService, action, proposal, visible_text

open_app = test_ui.open_app


class RecoveryService(FakeService):
    def __init__(self):
        super().__init__()
        self.resumes = []

    def resume(self, case_id, source_proposal_id, employee_id):
        self.resumes.append((case_id, source_proposal_id, employee_id))
        current = self.views[case_id]
        recovery = current["recovery"]
        assert employee_id == EMPLOYEE
        assert source_proposal_id == recovery["source_proposal_id"]
        assert recovery["status"] == "pending"
        replacement = new_question(case_id)
        recovery.update(status="prepared", proposal_id=replacement.id)
        current.update(proposal=replacement, proposal_status="pending", reason="")
        return replacement


def new_question(case_id):
    return proposal(
        case_id,
        kind="message",
        version=4,
        text="Подтвердите обновлённые условия обмена.",
    ).model_copy(
        update={
            "consent_request": action().model_copy(update={"amount": Decimal("21.10")})
        }
    )


def recovering_service(mode, status="pending", *, replaced=False):
    service = RecoveryService()
    old = proposal(service.first, version=3)
    selected = new_question(service.first) if replaced else old
    service.views[service.first].update(
        proposal=selected,
        proposal_status="pending" if replaced else "stale",
        reason="Прежние условия больше не действуют.",
        recovery_mode=mode,
        recovery={
            "source_proposal_id": old.id,
            "status": status,
            "proposal_id": selected.id if replaced else None,
            "check": {
                "status": "changed",
                "reason": "Изменилась сумма доплаты.",
                "changes": [
                    {
                        "field": "amount",
                        "label": "Доплата",
                        "before": "16.85 USD",
                        "after": "21.10 USD",
                    }
                ],
            },
        },
    )
    return service


@pytest.mark.parametrize("mode", ["manual", "auto"])
@pytest.mark.parametrize("replaced", [False, True])
def test_difference_is_visible_only_in_auto_mode_and_retained_for_new_version(
    open_app, mode, replaced
):
    service = recovering_service(
        mode, "prepared" if replaced else "pending", replaced=replaced
    )
    at = open_app(service)
    content = visible_text(at)
    assert "Изменилась сумма доплаты." in content
    if mode == "auto":
        assert "Автоматическая подготовка новой версии" in content
        assert "Было: 16.85 USD" in content
        assert "Стало: 21.10 USD" in content
    else:
        assert "Подготовка новой версии по команде сотрудника" in content
        assert "Было:" not in content and "Стало:" not in content
    assert service.resumes == []
    assert service.approvals == []
    assert service.operations == []
    assert service.views[service.first]["deliveries"] == []


@pytest.mark.parametrize(
    "mode,status,enabled",
    [
        ("manual", "pending", True),
        ("manual", "running", False),
        ("manual", "prepared", False),
        ("manual", "failed", False),
        ("manual", "cancelled", False),
        ("auto", "pending", False),
    ],
)
def test_resume_is_available_only_for_waiting_manual_recovery(
    open_app, mode, status, enabled
):
    service = recovering_service(mode, status)
    at = open_app(service)
    buttons = [button for button in at.button if button.key == "resume_recovery"]
    assert bool(buttons and not buttons[0].disabled) == enabled
    at.run()
    assert not at.exception
    assert service.resumes == []
    assert service.approvals == []


def test_manual_resume_passes_source_proposal_and_only_prepares_new_card(open_app):
    service = recovering_service("manual")
    view = service.views[service.first]
    source_id = view["recovery"]["source_proposal_id"]
    at = open_app(service)

    at.button(key="resume_recovery").click().run()

    assert not at.exception
    assert service.resumes == [(service.first, source_id, EMPLOYEE)]
    assert "Подтвердите обновлённые условия обмена." in visible_text(at)
    assert not at.button(key="approve").disabled
    assert service.approvals == []
    assert service.operations == []
    assert view["deliveries"] == []
    at.run()
    assert service.resumes == [(service.first, source_id, EMPLOYEE)]


def test_failed_recovery_shows_guidance_without_retrying_on_render(open_app):
    service = recovering_service("auto", "failed")
    at = open_app(service)
    assert any(
        "Не удалось подготовить новую версию" in error.value for error in at.error
    )
    assert "Проверьте историю обращения" in visible_text(at)
    at.run()
    assert not at.exception
    assert service.resumes == []
    assert service.approvals == []
