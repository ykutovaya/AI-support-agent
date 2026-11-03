"""Закреплённые обращения и независимые синтетические снимки магазина."""

import json
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _validate_case(case, index):
    case_id = (
        case.get("id")
        if isinstance(case, dict)
        and isinstance(case.get("id"), str)
        and case["id"].strip()
        else f"[{index}]"
    )

    def invalid(field, reason):
        raise ValueError(f"Случай {case_id}, поле {field}: {reason}")

    def text(value, field):
        if not isinstance(value, str) or not value.strip():
            invalid(field, "ожидается непустая строка")

    def strings(value, field):
        if not isinstance(value, list) or not value:
            invalid(field, "ожидается непустой список строк")
        for i, item in enumerate(value):
            text(item, f"{field}[{i}]")

    def amount(value, field, *, nullable=False):
        if value is None and nullable:
            return
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            invalid(field, "ожидается конечное число или его строковая запись")
        try:
            finite = Decimal(str(value)).is_finite()
        except InvalidOperation:
            finite = False
        if not finite:
            invalid(field, "ожидается конечное число или его строковая запись")

    if not isinstance(case, dict):
        invalid(f"[{index}]", "ожидается объект случая")
    for field in (
        "id",
        "split",
        "user_id",
        "email",
        "product_id",
        "old_item_id",
        "new_item_id",
        "initial_status",
        "goal",
        "initial_message",
    ):
        text(case.get(field), field)
    if case["split"] not in ("development", "control"):
        invalid("split", "ожидается development или control")
    if case["goal"] not in ("exchange", "return", "ticket", "refusal", "cancel_intent"):
        invalid("goal", "неизвестная цель обращения")
    for field in ("purchase_price", "variant_price", "initial_amount"):
        if field not in case:
            invalid(field, "обязательное поле отсутствует")
        amount(case[field], field, nullable=field == "initial_amount")

    expected = case.get("expected")
    if not isinstance(expected, dict):
        invalid("expected", "ожидается объект ожидаемого результата")
    for field in ("order_id", "payment_method_id", "result_status"):
        text(expected.get(field), f"expected.{field}")
    strings(expected.get("item_ids"), "expected.item_ids")
    if case["goal"] in ("exchange", "cancel_intent") or "new_item_ids" in expected:
        strings(expected.get("new_item_ids"), "expected.new_item_ids")
    if "amount" not in expected:
        invalid("expected.amount", "обязательное поле отсутствует")
    amount(expected["amount"], "expected.amount", nullable=True)

    interactions = case.get("interactions")
    if not isinstance(interactions, list):
        invalid("interactions", "ожидается список событий")
    for i, event in enumerate(interactions):
        field = f"interactions[{i}]"
        if not isinstance(event, dict):
            invalid(field, "ожидается объект события")
        if event.get("kind") not in ("customer", "price_change"):
            invalid(f"{field}.kind", "ожидается customer или price_change")
        if event.get("trigger") not in ("message", "before_action"):
            invalid(f"{field}.trigger", "ожидается message или before_action")
        if "customer_intent" in event:
            if event["kind"] != "customer" or event["customer_intent"] not in (
                "refusal",
                "request_change",
                "unknown",
            ):
                invalid(
                    f"{field}.customer_intent",
                    "для customer ожидается refusal, request_change или unknown",
                )
        if event["kind"] == "customer":
            text(event.get("text"), f"{field}.text")
        else:
            if event["trigger"] != "before_action":
                invalid(f"{field}.trigger", "для price_change ожидается before_action")
            amount(event.get("price"), f"{field}.price")
            for key in ("product_id", "variant_id"):
                text(event.get(key), f"{field}.{key}")

    for field in (
        "product_name",
        "user_zip",
        "purchase_payment_method_id",
        "additional_order_id",
    ):
        if field in case:
            text(case[field], field)
    if "user_name" in case:
        if not isinstance(case["user_name"], dict):
            invalid("user_name", "ожидается объект имени клиента")
        for key in ("first_name", "last_name"):
            text(case["user_name"].get(key), f"user_name.{key}")
    for field in ("old_options", "new_options"):
        if field in case:
            if not isinstance(case[field], dict):
                invalid(field, "ожидается объект характеристик товара")
            for key, value in case[field].items():
                text(key, field)
                text(value, f"{field}.{key}")
    if "additional_order_overrides" in case and not isinstance(
        case["additional_order_overrides"], dict
    ):
        invalid("additional_order_overrides", "ожидается объект полей заказа")
    if "retained_item_forms" in case:
        # Здесь только структура. Целые именные обозначения в именительном и
        # винительном падежах, с исходным цветом/размером и без глаголов,
        # проверяются на разработочных примерах оценщика, не морфологией загрузчика.
        strings(case["retained_item_forms"], "retained_item_forms")


def load_cases(split=None, *, path=None):
    cases = json.loads(Path(path or HERE / "cases.json").read_text(encoding="utf-8"))
    if split not in (None, "development", "control"):
        raise ValueError("Неизвестная выборка")
    if not isinstance(cases, list):
        raise ValueError("Случай <набор>, поле $: ожидается список случаев")
    seen = set()
    for index, case in enumerate(cases):
        _validate_case(case, index)
        if case["id"] in seen:
            raise ValueError(f"Случай {case['id']}, поле id: повтор идентификатора")
        seen.add(case["id"])
    return [c for c in cases if split is None or c["split"] == split]


def snapshot_for(case):
    shop = json.loads(
        (HERE.parent / "reapproval/base_shop.json").read_text(encoding="utf-8")
    )
    user = shop["users"].pop("u1")
    order = shop["orders"].pop("#O1")
    product = shop["products"].pop("p1")
    uid = case["user_id"]
    oid = case["expected"]["order_id"]
    pid = case["product_id"]
    old = case["old_item_id"]
    new = case["new_item_id"]
    user.update(user_id=uid, email=case["email"], orders=[oid])
    user["name"] = deepcopy(
        case.get("user_name", {"first_name": "Учебный", "last_name": case["id"]})
    )
    user["address"]["zip"] = case.get("user_zip", user["address"]["zip"])
    product["name"] = case.get("product_name", product["name"])
    old_options = deepcopy(
        case.get("old_options", product["variants"]["old"]["options"])
    )
    new_options = deepcopy(
        case.get("new_options", product["variants"]["new"]["options"])
    )
    order.update(order_id=oid, user_id=uid, status=case["initial_status"])
    order["items"][0].update(
        product_id=pid,
        item_id=old,
        price=float(case["purchase_price"]),
        name=product["name"],
        options=deepcopy(old_options),
    )
    order["payment_history"][0]["amount"] = float(case["purchase_price"])
    order["payment_history"][0]["payment_method_id"] = case.get(
        "purchase_payment_method_id", "card"
    )
    product["product_id"] = pid
    product["variants"] = {
        old: {
            **product["variants"]["old"],
            "item_id": old,
            "price": float(case["purchase_price"]),
            "options": old_options,
        },
        new: {
            **product["variants"]["new"],
            "item_id": new,
            "price": float(case["variant_price"]),
            "options": new_options,
        },
    }
    if order["status"] == "exchange requested":
        order.update(
            exchange_items=[old],
            exchange_new_items=[new],
            exchange_payment_method_id="card",
            exchange_price_difference=2.2,
        )
    if order["status"] == "return requested":
        order.update(return_items=[old], return_payment_method_id="card")
    shop["users"][uid] = user
    shop["orders"][oid] = order
    shop["products"][pid] = product
    if case.get("additional_order_id"):
        other = deepcopy(order)
        other.update(deepcopy(case.get("additional_order_overrides", {})))
        other["order_id"] = case["additional_order_id"]
        other["user_id"] = uid
        shop["orders"][other["order_id"]] = other
        user["orders"].append(other["order_id"])
    from tau2.domains.retail.data_model import RetailDB

    return RetailDB.model_validate(shop).model_dump(mode="json")
