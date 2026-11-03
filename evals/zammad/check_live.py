"""Проверка настоящего Zammad через AppTest; создаёт одну тестовую заявку за запуск."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"


def save(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def counts(store):
    with store.connection() as conn:
        return {
            name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in (
                "approvals",
                "operations",
                "outbox",
                "deliveries",
                "support_tickets",
            )
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="store_true",
        help="Разрешить настоящую запись в тестовую группу",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Только восстановить прежнюю попытку; нового POST нет",
    )
    parser.add_argument(
        "--scenario", choices=("normal", "lost-response"), required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--group-id", type=int, required=True)
    parser.add_argument("--group-name", default="Retail Demo")
    parser.add_argument("--customer-id", type=int, required=True)
    args = parser.parse_args()
    if not args.run:
        parser.error("Нужен --run: стенд обращается к настоящему API")
    root = Path(__file__).resolve().parents[2]
    output = args.output.resolve()
    if args.resume:
        report = json.loads((output / "result.json").read_text(encoding="utf-8"))
        if report["scenario"] != args.scenario:
            parser.error("Сценарий не совпадает с сохранённой попыткой")
    else:
        output.mkdir(parents=True, exist_ok=False)
        (output / "artifacts").mkdir()
        report = {
            "scenario": args.scenario,
            "checker": "assistant_streamlit_apptest",
            "calls": [],
            "checkpoints": [],
        }
    report_path = output / "result.json"
    database = output / "artifacts/work.sqlite3"

    from dotenv import dotenv_values

    values = dotenv_values(args.env_file or root / ".env")
    for name in ("ZAMMAD_URL", "ZAMMAD_TOKEN"):
        if not os.environ.get(name) and values.get(name):
            os.environ[name] = values[name]
    os.environ.update(
        SUPPORT_AGENT_MODE="demo",
        SUPPORT_AGENT_RECOVERY_MODE="manual",
        SUPPORT_AGENT_TICKET_BACKEND="zammad",
        ZAMMAD_GROUP_ID=str(args.group_id),
        ZAMMAD_GROUP_NAME=args.group_name,
        ZAMMAD_CUSTOMER_MAP=json.dumps({"u1": args.customer_id}),
        SUPPORT_AGENT_RETAIL_DB=str(root / "evals/zammad/base_shop.json"),
        SUPPORT_AGENT_DB=str(database),
    )

    from streamlit.testing.v1 import AppTest

    from support_agent.adapters.llm import LiteLLMClient
    from support_agent.adapters.zammad import UrllibTransport
    from support_agent.bootstrap import create_service
    from support_agent.ui import EMPLOYEE

    def forbidden_model(*unused, **kwargs):
        raise AssertionError("Платная модель запрещена в этой проверке")

    LiteLLMClient.generate = forbidden_model
    real_transport = UrllibTransport()

    def transport(method, url, **kwargs):
        if method == "POST":
            assert not args.resume, "Возобновление не разрешает POST"
            assert not any(c["method"] == "POST" for c in report["calls"]), (
                "Повторный POST запрещён"
            )
        call = {
            "method": method,
            "path": urlsplit(url).path,
            "query": urlsplit(url).query,
        }
        report["calls"].append(call)
        save(report_path, report)
        status, body = real_transport(method, url, **kwargs)
        call["status"] = status
        if method == "POST" and status == 201:
            call["created_id"] = json.loads(body)["id"]
        save(report_path, report)
        if method == "POST" and status == 201 and args.scenario == "lost-response":
            call["injection"] = "Ответ после успешной записи скрыт от адаптера"
            save(report_path, report)
            raise TimeoutError("Управляемая потеря ответа после записи")
        return status, body

    def service():
        result = create_service(root)
        result.executor.ticket_backend._transport = transport
        return result

    def screen(svc, case):
        app = AppTest.from_file(
            str(root / "src/support_agent/ui.py"), default_timeout=120
        )
        app.session_state["_service"] = svc
        app.session_state["case_id"] = case
        app.run()
        assert not app.exception, "Ошибка запуска экрана"
        return app

    def checkpoint(name, svc):
        record = {"name": name, "counts": counts(svc.store)}
        report["checkpoints"].append(record)
        save(report_path, report)
        return record["counts"]

    try:
        svc = service()
        report["config"] = {
            "url": svc.executor.ticket_backend.base_url,
            "group_id": args.group_id,
            "group_name": args.group_name,
            "customer_id": args.customer_id,
            "model": "DemoModel",
            "api_model_calls": 0,
            "channel": "local_test",
        }
        report["code_hashes"] = {
            str(path.relative_to(root)).replace("\\", "/"): digest(path)
            for path in sorted((root / "src/support_agent").rglob("*.py"))
        }
        if args.resume:
            case = report["case_id"]
            app = screen(svc, case)
        else:
            case = svc.create_case("u1", EMPLOYEE)
            report["case_id"] = case
            app = screen(svc, case)
            app.text_area(key="request").set_value(
                "Проверьте результат обмена. Email user1@example.test"
            ).run()
            app.button(key="investigate").click().run()
            assert not app.exception, "Ошибка подготовки предложения"
            proposal = svc.view(case, EMPLOYEE)["proposal"]
            assert proposal.action.arguments["target"] == "zammad"
            report["proposal_id"] = proposal.id
            report["approved_request"] = proposal.action.arguments
            report["approved_reply"] = proposal.text
            assert checkpoint("before_approval", svc)["operations"] == 0
            assert report["calls"] == [], "Подготовка не должна обращаться к Zammad"
            app.button(key="approve").click().run()
            assert not app.exception, "Ошибка подтверждения"
            report["first_outcome"] = svc.view(case, EMPLOYEE)["proposal_status"]
            checkpoint("after_approval", svc)

        if args.scenario == "lost-response" and not args.resume:
            assert report["first_outcome"] == "unknown", (
                "Неизвестный исход должен блокировать отправку"
            )
            assert not svc.view(case, EMPLOYEE)["deliveries"]
            assert app.button(key="approve").disabled
            report["unknown_saved"] = (
                svc.store.external_ticket(report["proposal_id"])["ticket_id"] is None
            )
            assert report["unknown_saved"], (
                "Адаптер не должен знать скрытый ID из ответа"
            )

        # Новый сервис и новая сессия экрана восстанавливаются из той же SQLite.
        svc = service()
        app = screen(svc, case)
        checkpoint("restored_before_reconcile", svc)
        view = svc.view(case, EMPLOYEE)
        if view["proposal_status"] in {"unknown", "submitting"}:
            app.button(key="reconcile").click().run()
            assert not app.exception, "Ошибка чтения результата"
        view = svc.view(case, EMPLOYEE)
        assert view["proposal_status"] == "delivered", (
            "Исход пока не подтверждён; продолжать только с --resume"
        )
        result = view["operation_result"]
        assert (
            result["group_id"] == args.group_id
            and result["customer_id"] == args.customer_id
        )
        assert result["ticket_status"] in {"new", "open"}
        assert [row["text"] for row in view["deliveries"]] == [report["approved_reply"]]
        assert any(result["ticket_url"] in block.value for block in app.markdown)
        proposal = view["proposal"]
        before = len(report["calls"])
        svc.approve(case, proposal.id, proposal.version, proposal.digest(), EMPLOYEE)
        assert len(report["calls"]) == before, (
            "Повтор подтверждения не должен делать сетевые запросы"
        )
        final = checkpoint("after_restart_and_repeat", svc)
        assert final == {
            "approvals": 1,
            "operations": 1,
            "outbox": 1,
            "deliveries": 1,
            "support_tickets": 0,
        }
        assert sum(call["method"] == "POST" for call in report["calls"]) == 1
        report.update(status="passed", result=result, error=None)
        report["external_operation"] = svc.store.external_ticket(proposal.id)[
            "operation_id"
        ]
    except Exception as error:
        report.update(
            status="incomplete",
            error={
                "type": type(error).__name__,
                "message": str(error)
                if isinstance(error, AssertionError)
                else "Ошибка проверки; секреты и тело исключения не сохранены",
            },
        )
        save(report_path, report)
        print(
            json.dumps(
                {"status": report["status"], "error": report["error"]},
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from None
    save(report_path, report)
    print(
        json.dumps(
            {"status": report["status"], "result": report["result"]}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
