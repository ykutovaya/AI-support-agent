"""Проверка реального экрана через AppTest без подмены сервиса и без API.

Это автоматическая проверка Streamlit, не проход человеком или браузером.
"""

import argparse
import importlib.metadata
import json
import os
import sys
from pathlib import Path

from check_lifecycle import database_state, sha256


def visible_text(page):
    kinds = (
        "title",
        "header",
        "subheader",
        "caption",
        "text",
        "markdown",
        "metric",
        "info",
        "warning",
        "error",
        "success",
    )
    return "\n".join(str(item.value) for kind in kinds for item in page.get(kind))


def assert_screen(page):
    assert not page.exception, "Экран завершился исключением"
    assert not page.error, "На экране появилась ошибка"


def run(project, output):
    output.mkdir(parents=True, exist_ok=False)
    database = output / "workspace.sqlite3"
    fixture = project / "evals/reapproval/base_shop.json"
    policy = fixture.with_name("policy.md")
    hashes_before = {str(path): sha256(path) for path in (fixture, policy)}
    os.environ.update(
        SUPPORT_AGENT_MODE="demo",
        SUPPORT_AGENT_RECOVERY_MODE="manual",
        SUPPORT_AGENT_DB=str(database),
        SUPPORT_AGENT_RETAIL_DB=str(fixture),
        PYTHON_DOTENV_DISABLED="1",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
        STREAMLIT_BROWSER_GATHER_USAGE_STATS="false",
        STREAMLIT_SERVER_HEADLESS="true",
    )
    from streamlit.testing.v1 import AppTest

    ui = project / "src/support_agent/ui.py"
    page = AppTest.from_file(str(ui), default_timeout=30).run()
    assert_screen(page)
    assert "Учебный режим без API" in visible_text(page)
    page.selectbox(key="user_id").select("u1").run()
    page.button(key="new_case").click().run()
    assert_screen(page)
    case_id = page.session_state["case_id"]
    page.text_area(key="request").set_value(
        "Обменяйте товар на любой другой доступный вариант. Email user1@example.test"
    ).run()
    page.button(key="investigate").click().run()
    assert_screen(page)
    prepared_text = visible_text(page)
    assert "2.35 USD" in prepared_text
    assert "Ожидает согласования" in prepared_text
    assert "Точный текст ответа" in prepared_text
    assert "Подготовка новой версии по команде сотрудника" in prepared_text
    prepared = database_state(database)
    assert len(prepared["cases"]) == 1 and len(prepared["proposals"]) == 1
    saved = prepared["proposals"][0]
    proposal = json.loads(saved["body"])
    assert saved["status"] == "pending"
    assert proposal["action"] is None
    assert proposal["consent_request"]["amount"] == "2.35"
    assert proposal["text"] in prepared_text
    for table in ("operations", "deliveries", "approvals", "consents", "outbox"):
        assert not prepared[table], f"До согласования изменена таблица {table}"
    prepared_hash = sha256(database)
    (output / "prepared-screen.txt").write_text(prepared_text + "\n", "utf-8")

    page.run()
    assert_screen(page)
    assert page.session_state["case_id"] == case_id
    assert database_state(database) == prepared
    assert sha256(database) == prepared_hash

    fresh_page = AppTest.from_file(str(ui), default_timeout=30).run()
    assert_screen(fresh_page)
    assert fresh_page.session_state["case_id"] == case_id
    assert database_state(database) == prepared
    assert sha256(database) == prepared_hash
    restored_text = visible_text(fresh_page)
    assert proposal["text"] in restored_text and "2.35 USD" in restored_text
    (output / "restored-screen.txt").write_text(restored_text + "\n", "utf-8")
    hashes_after = {str(path): sha256(path) for path in (fixture, policy)}
    assert hashes_before == hashes_after

    result = {
        "status": "passed",
        "kind": "streamlit_apptest_real_bootstrap",
        "model": "DemoModel",
        "mode": "demo",
        "recovery_mode": "manual",
        "service_replaced": False,
        "api_calls": 0,
        "human_pass": False,
        "browser_pass": False,
        "python": sys.version,
        "executable": sys.executable,
        "prefix": sys.prefix,
        "streamlit_version": importlib.metadata.version("streamlit"),
        "ui_source": str(ui),
        "ui_sha256": sha256(ui),
        "case_id": case_id,
        "proposal_id": proposal["id"],
        "proposal_status": saved["status"],
        "amount_usd": proposal["consent_request"]["amount"],
        "rerun_unchanged": True,
        "new_apptest_session_unchanged": True,
        "database_sha256": prepared_hash,
        "counts": {name: len(rows) for name, rows in prepared.items()},
        "fixture_hashes_before": hashes_before,
        "fixture_hashes_after": hashes_after,
    }
    (output / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    print(json.dumps({"status": "passed", "kind": result["kind"], "api_calls": 0}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    run(args.project_root.resolve(), args.output.resolve())
