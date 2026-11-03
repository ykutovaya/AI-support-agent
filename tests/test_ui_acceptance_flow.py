"""Три приёмочных обращения через настоящий сервис и экран, без API."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest
from test_ui import visible_text
from test_ui_acceptance_setup import setup_module_file

EMPLOYEE = "demo-employee"
APP = Path(__file__).parents[1] / "src/support_agent/ui.py"


@pytest.mark.parametrize(
    "scenario,delivery_count", [("exchange", 2), ("price", 3), ("ticket", 1)]
)
def test_prepared_acceptance_flow_and_refresh(tmp_path, scenario, delivery_count):
    service, cases = setup_module_file().prepare(tmp_path / "acceptance.sqlite3")
    case_id = cases[scenario]
    page = AppTest.from_file(str(APP), default_timeout=15)
    page.session_state["_service"] = service
    page.session_state["case_id"] = case_id
    page.run()
    assert not page.exception
    if scenario == "price":
        page.button(key="approve").click().run()
        assert service.view(case_id, EMPLOYEE)["proposal_status"] == "stale"
        assert "2.35" in visible_text(page) and "9.99" in visible_text(page)
        page.button(key="resume_recovery").click().run()
    else:
        request = (
            "Проверьте результат обмена" if scenario == "ticket" else "Обменяйте товар"
        )
        page.text_area(key="request").set_value(
            f"{request}. Email {scenario}@example.test"
        ).run()
        page.button(key="investigate").click().run()
    assert not page.exception
    page.button(key="approve").click().run()
    if scenario != "ticket":
        page.text_area(key="client_response").set_value(
            "Согласен с указанными условиями"
        ).run()
        page.button(key="client_accept").click().run()
        page.button(key="approve").click().run()
    assert not page.exception
    view = service.view(case_id, EMPLOYEE)
    assert view["proposal_status"] == "delivered"
    assert len(view["deliveries"]) == delivery_count
    content = visible_text(page)
    for heading in (
        "Действие в магазине",
        "Заявка специалисту",
        "Ответ клиенту",
        "Проверка результата",
    ):
        assert heading in content
    assert view["verification"]["status"] == "confirmed"
    assert view["verification"]["verified_at"]
    assert view["current_delivery"]["text"] == view["proposal"].text
    assert page.query_params["case"] == [case_id]
    before = service.store.path.read_bytes()
    restored = AppTest.from_file(str(APP), default_timeout=15)
    restored.session_state["_service"] = service
    restored.query_params["case"] = case_id
    restored.run()
    restored.run()
    assert not restored.exception
    assert restored.session_state["case_id"] == case_id
    assert service.store.path.read_bytes() == before
    assert service.view(case_id, EMPLOYEE)["deliveries"] == view["deliveries"]
