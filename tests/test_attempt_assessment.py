"""Корректный текст не заменяет согласование, доставку и фактический результат."""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def load_assessment():
    path = ROOT / "evals/reapproval/assessment.py"
    spec = importlib.util.spec_from_file_location("attempt_assessment", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def saved_attempt(name):
    folder = ROOT / "evals/reapproval/2026-10-31-revised/main/attempts" / name
    state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
    cases = json.loads(
        (ROOT / "evals/reapproval/scenarios.json").read_text(encoding="utf-8")
    )["main"]
    scenario = next(case for case in cases if name.startswith(case["id"] + "-"))
    return state, scenario


def test_correct_refusal_without_delivery_is_not_a_completed_attempt():
    state, scenario = saved_attempt("main-status-2-manual")
    result = load_assessment().assess_attempt(state, scenario)
    assert result["checks"]["reply_content"]["status"] == "pass"
    assert result["checks"]["action_admissibility"]["status"] == "pass"
    assert result["checks"]["final_state"]["status"] == "pass"
    assert result["checks"]["delivery"]["status"] == "fail"
    assert result["complete"] is False


@pytest.mark.parametrize(
    "damage,failed_check",
    [
        ("payment", "final_state"),
        ("delivery", "delivery"),
        ("text", "reply_content"),
        ("action", "action_admissibility"),
        ("approval", "delivery"),
    ],
)
def test_independent_checks_detect_different_failures(damage, failed_check):
    module = load_assessment()
    state, scenario = saved_attempt("main-unchanged-0-manual")
    assert module.assess_attempt(state, scenario)["complete"] is True
    changed = deepcopy(state)
    final = max(changed["tables"]["proposals"], key=lambda row: row["version"])
    body = json.loads(final["body"])
    if damage == "payment":
        changed["retail"]["orders"][scenario["order_id"]][
            "exchange_payment_method_id"
        ] = "other_card"
    elif damage == "delivery":
        changed["tables"]["deliveries"] = []
    elif damage == "text":
        body["text"] = "Обмен выполнен, деньги списаны."
    elif damage == "approval":
        next(
            r for r in changed["tables"]["approvals"] if r["proposal_id"] == final["id"]
        )["digest"] = "altered"
    else:
        body["action"]["arguments"]["new_item_ids"] = ["cheap"]
    final["body"] = json.dumps(body, ensure_ascii=False)
    assessment = module.assess_attempt(changed, scenario)
    assert assessment["checks"][failed_check]["status"] != "pass"
    assert assessment["complete"] is False


def test_refusal_about_another_order_requires_correction():
    state, scenario = saved_attempt("main-status-2-manual")
    final = max(state["tables"]["proposals"], key=lambda row: row["version"])
    body = json.loads(final["body"])
    body["text"] = body["text"].replace("#M203", "#OTHER")
    final["body"] = json.dumps(body, ensure_ascii=False)
    result = load_assessment().assess_attempt(state, scenario)
    assert result["checks"]["reply_content"]["status"] == "fail"
