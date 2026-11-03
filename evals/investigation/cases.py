"""Закреплённые обращения и независимые синтетические снимки магазина."""

import json
from copy import deepcopy
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_cases(split=None):
    cases = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
    if split not in (None, "development", "control"):
        raise ValueError("Неизвестная выборка")
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
    user["name"] = {"first_name": "Учебный", "last_name": case["id"]}
    order.update(order_id=oid, user_id=uid, status=case["initial_status"])
    order["items"][0].update(
        product_id=pid, item_id=old, price=float(case["purchase_price"])
    )
    order["payment_history"][0]["amount"] = float(case["purchase_price"])
    product["product_id"] = pid
    product["variants"] = {
        old: {
            **product["variants"]["old"],
            "item_id": old,
            "price": float(case["purchase_price"]),
        },
        new: {
            **product["variants"]["new"],
            "item_id": new,
            "price": float(case["variant_price"]),
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
        other["order_id"] = case["additional_order_id"]
        shop["orders"][other["order_id"]] = other
        user["orders"].append(other["order_id"])
    from tau2.domains.retail.data_model import RetailDB

    return RetailDB.model_validate(shop).model_dump(mode="json")
