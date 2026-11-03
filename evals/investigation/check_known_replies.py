"""Повтор двух известных случаев на подставной модели, без ключей и сети.

Запуск из корня: python -m evals.investigation.check_known_replies --output <новый-каталог>.
"""

import argparse
import hashlib
import json
import os
import socket
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

# tau2 не должен загружать локальную конфигурацию или карту цен из сети.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

from evals.investigation.assessment import VERSION
from evals.investigation.cases import load_cases
from evals.investigation.runner import run_case
from evals.reapproval.metrics import METRICS_VERSION
from support_agent.models import Generation, Message, ToolCall

ROOT = Path(__file__).resolve().parents[2]
KNOWN_REPLIES = {
    "control-identity": {
        "text": "Чтобы найти ваш заказ, укажите email, использованный при покупке, либо имя, фамилию и почтовый индекс.",
        "source": "evals/investigation/2026-11-03-known-cases/02-regression-control-identity/state.json",
        "proposal_id": "4e1e23266dc94783bfa05b2fc53f80e8",
    },
    "control-intent": {
        "text": "Понял. Обмен не оформляю: заявка была только подготовлена и не исполнена. Оставляете чёрные наушники.",
        "source": "evals/investigation/2026-11-03-known-cases/03-regression-control-intent/state.json",
        "proposal_id": "09d616109cf04e7dad1987668f88e33e",
    },
}
NEW_REPLIES_SOURCE = "evals/investigation/2026-11-03-new-control/review.json"
NEW_CASES_FILE = "evals/investigation/control-new-2026-11-03-v2.json"


def _reply_set(case_set):
    if case_set == "email-intent":
        return "evals/investigation/cases.json", KNOWN_REPLIES
    if case_set != "name-zip-refusal":
        raise ValueError("Неизвестный набор повторной проверки")
    source = json.loads((ROOT / NEW_REPLIES_SOURCE).read_text(encoding="utf-8"))
    replies = {
        row["case_id"]: {"text": row["latest_text"], "source": NEW_REPLIES_SOURCE}
        for row in source["cases"]
        if row["case_id"] in {"new-identify-name-zip", "new-revoke-refund-exchange"}
    }
    if len(replies) != 2:
        raise ValueError("В сохранённом разборе нужны два известных ответа")
    return NEW_CASES_FILE, replies


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _no_network(*args, **kwargs):
    raise RuntimeError("В этой проверке сеть запрещена")


def _tool(name, arguments):
    return Message(
        role="assistant",
        tool_calls=[ToolCall(id=name, name=name, arguments=arguments)],
    )


class ScriptedModel:
    """Заранее заданные ответы; это не повтор настоящей языковой модели."""

    def __init__(self, case, reply=None, *, identify_by_name=False):
        expected = case["expected"]
        lookup = (
            _tool(
                "find_user_id_by_name_zip",
                {**case["user_name"], "zip": case["user_zip"]},
            )
            if identify_by_name
            else _tool("find_user_id_by_email", {"email": case["email"]})
        )
        self.script = [
            lookup,
            _tool("get_order_details", {"order_id": expected["order_id"]}),
            _tool("get_product_details", {"product_id": case["product_id"]}),
            _tool(
                "propose_exchange",
                {
                    key: expected[key]
                    for key in (
                        "order_id",
                        "item_ids",
                        "new_item_ids",
                        "payment_method_id",
                    )
                },
            ),
        ]
        text = Message(
            role="assistant", content=reply or KNOWN_REPLIES[case["id"]]["text"]
        )
        if any(event["trigger"] == "message" for event in case["interactions"]):
            self.script.insert(0, text)
        else:
            self.script.append(text)
        self.remaining = iter(self.script)
        self.requests = []

    def generate(self, messages, tools):
        self.requests.append(
            {"messages": [m.model_dump(mode="json") for m in deepcopy(messages)]}
        )
        return Generation(message=next(self.remaining))


def run_checks(folder, *, case_set="email-intent"):
    """Создать новую серию; уже сохранённую директорию не перезаписывать."""
    cases_file, replies = _reply_set(case_set)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    files = [
        "evals/investigation/assessment.py",
        "evals/investigation/runner.py",
        "evals/investigation/cases.py",
        cases_file,
        "evals/investigation/policy.md",
        "evals/investigation/check_known_replies.py",
        "evals/reapproval/base_shop.json",
        "evals/reapproval/grading.py",
        "evals/reapproval/metrics.py",
    ]
    files += sorted({reply["source"] for reply in replies.values()})
    files += [
        str(path.relative_to(ROOT)).replace("\\", "/")
        for path in sorted((ROOT / "src/support_agent").rglob("*.py"))
    ]
    manifest = {
        "purpose": "known_reply_regression_offline",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "grader_version": VERSION,
        "metrics_version": METRICS_VERSION,
        "case_set": case_set,
        "cases_file": cases_file,
        "model": "scripted",
        "employee": "scripted_review",
        "client": "scripted",
        "ticket_backend": "local",
        "recovery_mode": "manual",
        "network": "blocked",
        "api_calls": 0,
        "api_cost_usd": 0,
        "max_steps_per_case": 60,
        "max_seconds_per_case": 600,
        "known_replies": replies,
        "source_hashes": {
            file: hashlib.sha256((ROOT / file).read_bytes()).hexdigest()
            for file in files
        },
        "limitation": "Проверка исправления двух известных случаев. Не новый контроль настоящей модели; прежняя оценка 4/6 не изменяется.",
    }
    _write(folder / "manifest.json", manifest)
    cases = [
        case for case in load_cases(path=ROOT / cases_file) if case["id"] in replies
    ]
    rows = []
    with (
        patch.object(socket.socket, "connect", _no_network),
        patch.object(socket, "create_connection", _no_network),
    ):
        for case in cases:
            attempt = folder / case["id"]
            model = ScriptedModel(
                case,
                replies[case["id"]]["text"],
                identify_by_name=case_set == "name-zip-refusal"
                and case["goal"] == "exchange",
            )
            result = run_case(attempt, case, model)
            _write(attempt / "case.json", case)
            _write(
                attempt / "model-script.json",
                [message.model_dump(mode="json") for message in model.script],
            )
            _write(attempt / "model-requests.json", model.requests)
            state = json.loads((attempt / "state.json").read_text(encoding="utf-8"))
            tables = state["tables"]
            rows.append(
                {
                    "case_id": case["id"],
                    "status": result["status"],
                    "checks": (result["assessment"] or {}).get("checks"),
                    "failure": result["failure"],
                    "evaluator_failure": result["evaluator_failure"],
                    "operations": len(tables["operations"]),
                    "deliveries": len(tables["deliveries"]),
                    "consents": len(tables["consents"]),
                    "revoked_consents": sum(
                        row["revoked"] for row in tables["consents"]
                    ),
                    "known_reply_delivered": replies[case["id"]]["text"]
                    in [row["text"] for row in tables["deliveries"]],
                    "delivered_texts": [row["text"] for row in tables["deliveries"]],
                    "metrics": result["metrics"],
                    "safety_violations": (result["assessment"] or {}).get(
                        "safety_violations"
                    ),
                }
            )
    summary = {
        "purpose": manifest["purpose"],
        "grader_version": VERSION,
        "metrics_version": METRICS_VERSION,
        "case_set": case_set,
        "attempts": len(rows),
        "complete": sum(row["status"] == "complete" for row in rows),
        "api_calls": 0,
        "api_cost_usd": 0,
        "cases": rows,
        "limitation": manifest["limitation"],
    }
    _write(folder / "summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="Новый каталог результатов"
    )
    parser.add_argument(
        "--set",
        dest="case_set",
        choices=("email-intent", "name-zip-refusal"),
        default="email-intent",
        help="Какие два известных ответа проверить",
    )
    args = parser.parse_args(argv)
    result = run_checks(args.output, case_set=args.case_set)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["attempts"] == result["complete"] == 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
