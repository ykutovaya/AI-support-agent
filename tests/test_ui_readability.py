"""Следующий шаг и итог обращения видны без чтения технического журнала."""

import pytest
import test_ui
from streamlit.testing.v1 import AppTest
from test_ui import APP, FakeService, proposal, visible_text

open_app = test_ui.open_app


def test_selected_case_survives_a_new_browser_session_without_side_effects(open_app):
    service = FakeService()
    at = open_app(service)
    at.selectbox(key="case_selector").select(service.second).run()
    assert at.query_params["case"] == [service.second]
    reopened = AppTest.from_file(str(APP), default_timeout=10)
    reopened.session_state["_service"] = service
    reopened.query_params["case"] = service.second
    reopened.run()
    assert not reopened.exception
    assert reopened.selectbox(key="case_selector").value == service.second
    assert "Второй Клиент" in visible_text(reopened)
    assert service.approvals == service.created == service.investigations == []


def test_foreign_case_from_url_is_not_opened_and_does_not_hide_saved_cases():
    service = FakeService()
    at = AppTest.from_file(str(APP), default_timeout=10)
    at.session_state["_service"] = service
    at.query_params["case"] = "foreign-case"
    at.run()
    assert not at.exception
    assert at.selectbox(key="case_selector").value == service.first
    assert "Недоступное обращение" in visible_text(at)
    assert at.query_params["case"] == [service.first]
    assert service.approvals == service.investigations == []


@pytest.mark.parametrize(
    "status,step",
    [
        ("pending", "Проверьте действие и точный текст"),
        ("stale", "Прежнее подтверждение недействительно"),
        ("delivery_failed", "Повторите доставку согласованного текста"),
        ("unknown", "Проверьте результат в Zammad"),
    ],
)
def test_current_case_order_and_required_step_are_explicit(open_app, status, step):
    service = FakeService()
    service.views[service.first].update(
        proposal=proposal(service.first), proposal_status=status
    )
    at = open_app(service)
    content = visible_text(at)
    assert "Выбрано обращение" in content
    assert "Заказ: ORDER-1" in content
    assert "Следующий шаг" in content
    assert step in content


def test_raw_journal_is_collapsed_and_previous_operation_is_not_current_success(
    open_app,
):
    service = FakeService()
    raw = '{"status":"exchange requested","private_trace":"raw-journal"}'
    service.views[service.first].update(
        proposal=proposal(service.first, version=2),
        proposal_status="failed",
        events=[{"kind": "operation", "text": raw}],
    )
    at = open_app(service)
    details = next(item for item in at.expander if item.label == "Подробности")
    assert not details.proto.expanded
    assert raw in visible_text(details)
    assert not at.success
    assert "Действие не выполнено" in visible_text(at)


@pytest.mark.parametrize("timestamp", [None, "2026-11-03T10:20:30+00:00"])
def test_four_result_blocks_distinguish_saved_verification_from_delivery(
    open_app, timestamp
):
    service = FakeService()
    prepared = proposal(service.first)
    service.views[service.first].update(
        proposal=prepared,
        proposal_status="delivery_failed",
        operation_result={"order_id": "ORDER-1", "status": "exchange requested"},
        approval={"valid": True},
        client_consent={"status": "used"},
        current_delivery={
            "text": prepared.text,
            "status": "failed",
            "delivered": False,
        },
        verification={
            "status": "confirmed",
            "verified_at": timestamp,
            "source": "saved_execution",
            "reason": "",
        },
    )
    at = open_app(service)
    content = visible_text(at)
    for label in (
        "Действие в магазине",
        "Заявка специалисту",
        "Ответ клиенту",
        "Проверка результата",
    ):
        assert label in content
    assert "Не создавалась: для этого действия не требуется" in content
    assert "Доставка не подтверждена" in content
    assert prepared.text in content
    assert "Сотрудник: подтверждено" in content
    assert "Клиент: согласие использовано" in content
    assert (
        "Время прежней проверки не сохранено"
        if timestamp is None
        else "03.11.2026 10:20:30 UTC"
    ) in content
    assert service.approvals == service.investigations == []


def test_old_delivery_does_not_mark_current_reply_as_sent(open_app):
    service = FakeService()
    service.views[service.first].update(
        proposal=proposal(service.first, version=2, text="Новый текст"),
        proposal_status="pending",
        deliveries=[{"id": "old", "text": "Прежний текст"}],
        current_delivery=None,
    )
    at = open_app(service)
    assert "Текущий ответ ещё не отправлен" in visible_text(at)
    assert not at.success


def test_card_explains_items_and_payment_with_names_not_only_identifiers(open_app):
    service = FakeService()
    prepared = proposal(service.first)
    prepared.action.condition_values = {
        "products": {
            "headphones": {
                "name": "Наушники",
                "variants": {
                    "old-item": {"options": {"color": "black"}},
                    "new-item": {"options": {"color": "white"}},
                },
            }
        },
        "payment_methods": {"paypal-demo": {"source": "paypal", "id": "paypal-demo"}},
    }
    service.views[service.first].update(proposal=prepared, proposal_status="pending")
    at = open_app(service)
    content = visible_text(at)
    assert "Позиции: Наушники (цвет: чёрный)" in content
    assert "Замена на: Наушники (цвет: белый)" in content
    assert "Способ оплаты: PayPal" in content
    details = next(item for item in at.expander if item.label == "Подробности")
    assert "new-item" in visible_text(details)


@pytest.mark.parametrize(
    "recovery_status,next_step",
    [
        ("failed", "Подготовьте решение заново"),
        ("running", "Дождитесь новой версии"),
        ("cancelled", "Введите уточнение клиента"),
    ],
)
def test_recovery_hint_does_not_direct_to_disabled_resume(
    open_app, recovery_status, next_step
):
    from test_ui_reapproval import recovering_service

    service = recovering_service("manual", recovery_status)
    at = open_app(service)
    assert next_step in next(
        info.value for info in at.info if info.value.startswith("Следующий шаг:")
    )
    assert at.button(key="resume_recovery").disabled


def test_stale_without_recovery_offers_existing_prepare_control(open_app):
    service = FakeService()
    service.views[service.first].update(
        proposal=proposal(service.first), proposal_status="stale"
    )
    at = open_app(service)
    hint = next(
        info.value for info in at.info if info.value.startswith("Следующий шаг:")
    )
    assert "Введите уточнение" in hint and "Подготовить решение" in hint
    assert not [button for button in at.button if button.key == "resume_recovery"]


def test_completed_proposal_does_not_keep_old_recovery_warning(open_app):
    from test_ui_reapproval import recovering_service

    service = recovering_service("manual", "prepared", replaced=True)
    view = service.views[service.first]
    view.update(proposal_status="delivered", reason="")
    at = open_app(service)
    assert "Изменилась сумма доплаты" not in "\n".join(
        item.value for item in at.warning
    )
    assert not [button for button in at.button if button.key == "resume_recovery"]
