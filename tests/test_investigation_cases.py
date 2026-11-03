"""Отложенные задания, изоляция снимков и исходный магазин."""

import importlib.util
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest

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


def test_explicit_dataset_does_not_mix_in_original_cases(tmp_path):
    module = load()
    case = deepcopy(module.load_cases("development")[0])
    case.update(id="fixture-dataset", split="control")
    source = tmp_path / "dataset.json"
    source.write_text(json.dumps([case]), encoding="utf-8")
    assert module.load_cases("control", path=source) == [case]
    assert module.load_cases("development", path=source) == []
    assert len(module.load_cases()) == 10


def test_snapshot_preserves_distinct_product_customer_and_payment_facts():
    module = load()
    case = deepcopy(module.load_cases("development")[0])
    case.update(
        product_name="Клавиатура",
        old_options={"layout": "RU", "color": "grey"},
        new_options={"layout": "US", "color": "grey"},
        user_name={"first_name": "Тест", "last_name": "Разработка"},
        user_zip="94000",
        purchase_payment_method_id="other_card",
        additional_order_id="#DEV-OTHER",
        additional_order_overrides={"status": "cancelled"},
    )
    shop = module.snapshot_for(case)
    customer = shop["users"][case["user_id"]]
    order = shop["orders"][case["expected"]["order_id"]]
    product = shop["products"][case["product_id"]]
    assert customer["name"] == {"first_name": "Тест", "last_name": "Разработка"}
    assert customer["address"]["zip"] == "94000"
    assert product["name"] == order["items"][0]["name"] == "Клавиатура"
    assert product["variants"][case["old_item_id"]]["options"] == {
        "layout": "RU",
        "color": "grey",
    }
    assert order["items"][0]["options"] == {"layout": "RU", "color": "grey"}
    assert product["variants"][case["new_item_id"]]["options"] == {
        "layout": "US",
        "color": "grey",
    }
    assert order["payment_history"][0]["payment_method_id"] == "other_card"
    assert order["status"] == "delivered"
    assert shop["orders"]["#DEV-OTHER"]["status"] == "cancelled"
    product["variants"][case["old_item_id"]]["options"]["layout"] = "changed"
    assert case["old_options"]["layout"] == "RU"
    assert order["items"][0]["options"]["layout"] == "RU"


def write_cases(tmp_path, cases):
    source = tmp_path / "development.json"
    source.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    return source


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "split",
        "user_id",
        "email",
        "product_id",
        "old_item_id",
        "new_item_id",
        "initial_status",
        "purchase_price",
        "variant_price",
        "initial_amount",
        "goal",
        "initial_message",
        "expected",
        "interactions",
        "expected.order_id",
        "expected.item_ids",
        "expected.new_item_ids",
        "expected.payment_method_id",
        "expected.amount",
        "expected.result_status",
    ],
)
def test_missing_required_field_identifies_case_and_field(tmp_path, field):
    module = load()
    case = deepcopy(module.load_cases("development")[0])
    parts = field.split(".")
    target = case if len(parts) == 1 else case[parts[0]]
    del target[parts[-1]]
    with pytest.raises(ValueError) as error:
        module.load_cases(path=write_cases(tmp_path, [case]))
    assert ("[0]" if field == "id" else "dev-exchange") in str(error.value)
    assert field in str(error.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", " "),
        ("initial_message", 42),
        ("split", "training"),
        ("goal", "delete"),
        ("expected", []),
        ("interactions", {}),
        ("purchase_price", True),
        ("variant_price", "NaN"),
        ("initial_amount", "unknown"),
        ("expected.amount", "Infinity"),
        ("expected.item_ids", "old"),
        ("expected.item_ids", []),
        ("expected.new_item_ids", [None]),
        ("expected.order_id", ""),
        ("retained_item_forms", "синий рюкзак"),
        ("retained_item_forms", []),
        ("retained_item_forms", [" "]),
        ("retained_item_forms", [42]),
        ("old_options", []),
        ("new_options", {"color": None}),
        ("user_name", {"first_name": "Тест"}),
        ("user_zip", 12345),
        ("additional_order_overrides", []),
    ],
)
def test_invalid_field_identifies_case_and_field(tmp_path, field, value):
    module = load()
    case = deepcopy(module.load_cases("development")[0])
    parts = field.split(".")
    target = case if len(parts) == 1 else case[parts[0]]
    target[parts[-1]] = value
    with pytest.raises(ValueError) as error:
        module.load_cases(path=write_cases(tmp_path, [case]))
    assert ("[0]" if field == "id" else "dev-exchange") in str(error.value)
    assert field in str(error.value)


@pytest.mark.parametrize(
    "event,field",
    [
        (None, "interactions[0]"),
        ({"kind": "customer", "text": "Данные"}, "trigger"),
        ({"trigger": "message", "text": "Данные"}, "kind"),
        ({"trigger": "after_action", "kind": "customer", "text": "Данные"}, "trigger"),
        ({"trigger": "message", "kind": "unknown"}, "kind"),
        ({"trigger": "message", "kind": "customer"}, "text"),
        ({"trigger": "message", "kind": "customer", "text": " "}, "text"),
        ({"trigger": "before_action", "kind": "price_change"}, "price"),
        (
            {
                "trigger": "message",
                "kind": "price_change",
                "price": 10,
                "product_id": "p",
                "variant_id": "v",
            },
            "trigger",
        ),
        (
            {
                "trigger": "before_action",
                "kind": "price_change",
                "price": 10,
                "variant_id": "v",
            },
            "product_id",
        ),
        (
            {
                "trigger": "before_action",
                "kind": "price_change",
                "price": 10,
                "product_id": "p",
            },
            "variant_id",
        ),
        (
            {
                "trigger": "message",
                "kind": "customer",
                "text": "Стоп",
                "customer_intent": "accept",
            },
            "customer_intent",
        ),
        (
            {
                "trigger": "before_action",
                "kind": "price_change",
                "price": 10,
                "product_id": "p",
                "variant_id": "v",
                "customer_intent": "refusal",
            },
            "customer_intent",
        ),
    ],
)
def test_invalid_interaction_identifies_case_and_field(tmp_path, event, field):
    module = load()
    case = deepcopy(module.load_cases("development")[0])
    case["interactions"] = [event]
    with pytest.raises(ValueError) as error:
        module.load_cases(path=write_cases(tmp_path, [case]))
    assert "dev-exchange" in str(error.value)
    assert "interactions[0]" in str(error.value)
    assert field in str(error.value)


@pytest.mark.parametrize("payload,field", [(None, "$"), ({}, "$"), ([None], "[0]")])
def test_invalid_dataset_container_has_location(tmp_path, payload, field):
    with pytest.raises(ValueError) as error:
        load().load_cases(path=write_cases(tmp_path, payload))
    assert field in str(error.value)


def test_duplicate_case_id_is_rejected_before_split_filtering(tmp_path):
    module = load()
    case = module.load_cases("development")[0]
    source = write_cases(tmp_path, [case, case])
    with pytest.raises(ValueError) as error:
        module.load_cases("control", path=source)
    assert "dev-exchange" in str(error.value)
    assert "id" in str(error.value)


@pytest.mark.parametrize("intent", ["refusal", "request_change", "unknown"])
def test_valid_development_contract_preserves_explicit_customer_intent(
    tmp_path, intent
):
    module = load()
    case = deepcopy(module.load_cases("development")[0])
    case.update(
        product_name="Сумка",
        old_options={"color": "синий", "size": "S"},
        new_options={"color": "синий", "size": "M"},
        retained_item_forms=["синяя сумка размера S", "синюю сумку размера S"],
        interactions=[
            {
                "trigger": "before_action",
                "kind": "customer",
                "text": "Стоп",
                "customer_intent": intent,
            }
        ],
    )
    assert module.load_cases(path=write_cases(tmp_path, [case])) == [case]


def test_historical_dataset_remains_loadable_without_semantic_form_validation():
    source = ROOT / "evals/investigation/control-new-2026-11-03.json"
    # Исторические отдельные слова проходят структурную проверку; это не
    # подтверждает, что они обозначают весь оставленный товар для оценщика.
    assert load().load_cases(path=source) == json.loads(
        source.read_text(encoding="utf-8")
    )
