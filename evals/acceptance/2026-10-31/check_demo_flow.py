"""Два программных прохода с DemoModel; не заменяют проверку через интерфейс."""

import argparse
import json
import os
from decimal import Decimal
from pathlib import Path


def run(project, output, changed):
    name = "changed" if changed else "normal"
    database = output / (name + ".sqlite3")
    if database.exists():
        raise ValueError("Для повторного прохода нужен новый каталог результатов")
    os.environ.update(
        SUPPORT_AGENT_MODE="demo",
        SUPPORT_AGENT_RECOVERY_MODE="manual",
        SUPPORT_AGENT_DB=str(database),
        SUPPORT_AGENT_RETAIL_DB=str(project / "evals/reapproval/base_shop.json"),
        PYTHON_DOTENV_DISABLED="1",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
    )
    from support_agent.bootstrap import create_service

    app = create_service(project)
    employee = "demo-employee"
    case = app.create_case("u1", employee)

    def approve(proposal):
        return app.approve(
            case, proposal.id, proposal.version, proposal.digest(), employee
        )

    def operation_count():
        with app.store.connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]

    question = app.investigate(
        case,
        "Обменяйте товар на любой другой доступный вариант. Email user1@example.test",
        employee,
    )
    assert question.consent_request.amount == Decimal("2.35")
    assert not app.view(case, employee)["deliveries"] and operation_count() == 0
    approve(question)
    action = app.record_client_response(
        case, question.id, True, "Согласен на обмен и доплату 2.35 USD", employee
    )
    assert operation_count() == 0
    expected = Decimal("2.35")
    if changed:
        with app.store.transaction() as conn:
            snapshot, _ = app.store.retail(conn)
            snapshot["products"]["p1"]["variants"]["new"]["price"] = 19.99
            app.store.save_retail(conn, snapshot)
        blocked = approve(action)
        assert blocked["status"] == "stale" and operation_count() == 0
        assert len(app.view(case, employee)["deliveries"]) == 1
        question = app.resume(case, action.id, employee)
        expected = Decimal("9.99")
        assert question.consent_request.amount == expected
        assert operation_count() == 0
        approve(question)
        action = app.record_client_response(
            case,
            question.id,
            True,
            "Согласен на новые условия и доплату 9.99 USD",
            employee,
        )
        assert operation_count() == 0
    approve(action)
    final = app.view(case, employee)
    assert final["proposal_status"] == "delivered"
    assert operation_count() == 1
    assert final["deliveries"][-1]["text"] == action.text
    order = app.store.snapshot()[0]["orders"]["#O1"]
    assert order["status"] == "exchange requested"
    assert Decimal(str(order["exchange_price_difference"])) == expected
    with app.store.connection() as conn:
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("operations", "approvals", "consents", "deliveries")
        }
    return {
        "scenario": name,
        "kind": "programmatic_service_flow",
        "mode": "demo",
        "recovery_mode": "manual",
        "human_or_ui_pass": False,
        "api_calls": 0,
        "case_id": case,
        "status": "passed",
        "amount_usd": str(expected),
        "order_status": order["status"],
        "counts": counts,
        "exact_approved_text_delivered": True,
        "delivered_text": action.text,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    results = [
        run(args.project_root.resolve(), args.output.resolve(), changed)
        for changed in (False, True)
    ]
    (args.output / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {"programmatic_passes": len(results), "ui_passes": 0, "api_calls": 0}
        )
    )
