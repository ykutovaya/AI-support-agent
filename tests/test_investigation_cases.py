"""Отложенные задания, изоляция снимков и исходный магазин."""

import importlib.util
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parents[1]


def load():
    spec = importlib.util.spec_from_file_location(
        "investigation_cases", ROOT / "evals/investigation/cases.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixed_development_and_control_cases_are_distinct():
    module = load()
    cases = module.load_cases()
    assert Counter(c["split"] for c in cases) == {"development": 4, "control": 6}
    for field in ("id", "user_id", "email", "initial_message"):
        assert len({c[field] for c in cases}) == 10
    assert {c["goal"] for c in cases} == {
        "exchange",
        "return",
        "ticket",
        "refusal",
        "cancel_intent",
    }


def test_every_snapshot_is_valid_and_independent():
    from tau2.domains.retail.data_model import RetailDB

    module = load()
    for case in module.load_cases():
        shop = module.snapshot_for(case)
        RetailDB.model_validate(shop)
        assert shop["users"][case["user_id"]]["email"] == case["email"]
        assert (
            shop["orders"][case["expected"]["order_id"]]["user_id"] == case["user_id"]
        )
        shop["orders"][case["expected"]["order_id"]]["status"] = "cancelled"
        assert (
            module.snapshot_for(case)["orders"][case["expected"]["order_id"]]["status"]
            == case["initial_status"]
        )
    original = json.loads(
        (ROOT / "evals/reapproval/base_shop.json").read_text(encoding="utf-8")
    )
    assert original["orders"]["#O1"]["status"] == "delivered"
