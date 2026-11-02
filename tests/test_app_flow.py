"""Полный путь через Streamlit, общее ядро, RetailTools и отдельный SQLite без API."""

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

pytest.importorskip("tau2")

from support_agent.bootstrap import create_service


def test_ui_exchange_and_restart_preserve_one_operation(
    tmp_path, snapshot, monkeypatch
):
    source = tmp_path / "source" / "db.json"
    source.parent.mkdir()
    source.write_text(json.dumps(snapshot), encoding="utf-8")
    policy = source.with_name("policy.md")
    policy.write_text(
        "Учебные правила: обмен требует согласия клиента.", encoding="utf-8"
    )
    original = (source.read_bytes(), policy.read_bytes())
    monkeypatch.setenv("SUPPORT_AGENT_MODE", "demo")
    monkeypatch.setenv("SUPPORT_AGENT_RETAIL_DB", str(source))
    monkeypatch.setenv("SUPPORT_AGENT_DB", str(tmp_path / "app.sqlite3"))
    app_service = create_service(tmp_path)
    ui = Path(__file__).parents[1] / "src/support_agent/ui.py"
    at = AppTest.from_file(str(ui), default_timeout=15)
    at.session_state["_service"] = app_service
    at.run()
    at.button(key="new_case").click().run()
    case_id = at.session_state["case_id"]
    at.text_area(key="request").set_value(
        "Обменяйте товар на любой другой вариант. Email user1@example.test"
    ).run()
    at.button(key="investigate").click().run()
    assert not at.exception
    question = app_service.view(case_id, "demo-employee")["proposal"]
    assert question.consent_request is not None
    assert app_service.store.snapshot()[0]["orders"]["#O1"]["status"] == "delivered"
    at.button(key="approve").click().run()
    at.text_area(key="client_response").set_value(
        "Да, подтверждаю товар, сумму и способ оплаты."
    ).run()
    at.button(key="client_accept").click().run()
    assert app_service.view(case_id, "demo-employee")["proposal"].kind == "action"
    assert app_service.store.snapshot()[0]["orders"]["#O1"]["status"] == "delivered"
    at.button(key="approve").click().run()
    at.run()
    assert not at.exception
    reopened = create_service(tmp_path)
    fresh_page = AppTest.from_file(str(ui), default_timeout=15)
    fresh_page.session_state["_service"] = reopened
    fresh_page.run()
    assert not fresh_page.exception
    assert fresh_page.session_state["case_id"] == case_id
    assert reopened.view(case_id, "demo-employee")["proposal_status"] == "delivered"
    assert (
        reopened.store.snapshot()[0]["orders"]["#O1"]["status"] == "exchange requested"
    )
    with reopened.store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 2
    assert (source.read_bytes(), policy.read_bytes()) == original


@pytest.mark.parametrize(
    "kind,committed_status", [("message", "approved"), ("action", "executed")]
)
def test_restart_continues_committed_delivery_without_reapproval_or_execution(
    tmp_path, snapshot, monkeypatch, kind, committed_status
):
    source = tmp_path / "source" / "db.json"
    source.parent.mkdir()
    source.write_text(json.dumps(snapshot), encoding="utf-8")
    source.with_name("policy.md").write_text("Учебные правила", encoding="utf-8")
    monkeypatch.setenv("SUPPORT_AGENT_MODE", "demo")
    monkeypatch.setenv("SUPPORT_AGENT_RETAIL_DB", str(source))
    monkeypatch.setenv("SUPPORT_AGENT_DB", str(tmp_path / "app.sqlite3"))
    service = create_service(tmp_path)
    employee = "demo-employee"
    case_id = service.create_case("u1", employee)
    if kind == "action":
        question = service.investigate(
            case_id,
            "Обменяйте товар на другой вариант. Email user1@example.test",
            employee,
        )
        service.approve(
            case_id, question.id, question.version, question.digest(), employee
        )
        prepared = service.record_client_response(
            case_id, question.id, True, "Подтверждаю эти условия", employee
        )
    else:
        prepared = service.investigate(case_id, "Нужна помощь", employee)

    def interrupted(delivery_id):
        raise SystemExit("Процесс остановлен после commit и до доставки")

    monkeypatch.setattr(service.executor, "_deliver", interrupted)
    with pytest.raises(SystemExit):
        service.approve(
            case_id, prepared.id, prepared.version, prepared.digest(), employee
        )
    assert service.view(case_id, employee)["proposal_status"] == committed_status
    with service.store.connection() as conn:
        pending = dict(
            conn.execute(
                "SELECT * FROM outbox WHERE proposal_id=?", (prepared.id,)
            ).fetchone()
        )
        approvals_before = [
            tuple(row) for row in conn.execute("SELECT * FROM approvals")
        ]
        operations_before = [
            tuple(row) for row in conn.execute("SELECT * FROM operations")
        ]
        assert (
            conn.execute(
                "SELECT 1 FROM deliveries WHERE id=?", (pending["id"],)
            ).fetchone()
            is None
        )
    assert pending["status"] == "pending"
    assert pending["text"] == prepared.text
    assert len(operations_before) == (1 if kind == "action" else 0)
    committed_snapshot = service.store.snapshot()

    # Новая служба и новый AppTest имитируют перезапуск процесса и браузерной сессии.
    reopened = create_service(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail(
            "Продолжение доставки не должно заново расследовать, согласовывать или исполнять действие"
        )

    monkeypatch.setattr(reopened.model, "generate", forbidden)
    monkeypatch.setattr(reopened, "record_client_response", forbidden)
    monkeypatch.setattr(reopened.executor, "adapter_factory", forbidden)
    ui = Path(__file__).parents[1] / "src/support_agent/ui.py"
    page = AppTest.from_file(str(ui), default_timeout=15)
    page.session_state["_service"] = reopened
    page.session_state["case_id"] = case_id
    page.run()
    assert not page.exception
    button = page.button(key="approve")
    assert not button.disabled
    assert button.label == "Продолжить отправку"
    button.click().run()
    page.run()
    assert not page.exception
    assert page.button(key="approve").disabled
    assert reopened.view(case_id, employee)["proposal_status"] == "delivered"
    assert reopened.store.snapshot() == committed_snapshot
    with reopened.store.connection() as conn:
        assert [
            tuple(row) for row in conn.execute("SELECT * FROM approvals")
        ] == approvals_before
        assert [
            tuple(row) for row in conn.execute("SELECT * FROM operations")
        ] == operations_before
        sent = dict(
            conn.execute(
                "SELECT * FROM outbox WHERE proposal_id=?", (prepared.id,)
            ).fetchone()
        )
        deliveries = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM deliveries WHERE id=?", (pending["id"],)
            )
        ]
    assert sent["id"] == pending["id"] and sent["status"] == "delivered"
    assert deliveries == [
        {"id": pending["id"], "case_id": case_id, "text": prepared.text}
    ]
