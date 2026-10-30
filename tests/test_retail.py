"""Проверки ограниченного адаптера магазина на синтетических данных, без API."""

from copy import deepcopy
from decimal import Decimal

import pytest

pytest.importorskip("tau2")
from tau2.domains.retail.tools import RetailTools

from support_agent.adapters.retail import RetailAdapter


def exchange_args(**changes):
    return dict(
        order_id="#O1",
        item_ids=["old"],
        new_item_ids=["new"],
        payment_method_id="card",
        **changes,
    )


def test_specs_expose_only_scoped_reads_and_proposals(snapshot):
    adapter = RetailAdapter(snapshot, "Правила")
    specs = {s.name: s for s in adapter.specs()}
    assert set(specs) == {
        "find_user_id_by_email",
        "find_user_id_by_name_zip",
        "get_user_details",
        "get_order_details",
        "get_product_details",
        "propose_exchange",
        "propose_return",
    }
    assert set(specs["propose_exchange"].parameters["required"]) == {
        "order_id",
        "item_ids",
        "new_item_ids",
        "payment_method_id",
    }
    assert "return_delivered_order_items" not in specs


@pytest.mark.parametrize(
    "name,args",
    [
        ("get_user_details", {"user_id": "u2"}),
        ("get_order_details", {"order_id": "#O2"}),
        ("get_product_details", {"product_id": "p2"}),
        ("find_user_id_by_email", {"email": "user2@example.test"}),
        (
            "find_user_id_by_name_zip",
            {"first_name": "Имя2", "last_name": "Фамилия", "zip": "12345"},
        ),
        ("cancel_pending_order", {"order_id": "#O1", "reason": "no longer needed"}),
        (
            "propose_return",
            {"order_id": "#O1", "item_ids": ["old"], "payment_method_id": "card"},
        ),
    ],
)
def test_foreign_or_unlisted_reads_are_rejected_before_native_call(
    snapshot, monkeypatch, name, args
):
    adapter = RetailAdapter(snapshot, "Правила")

    def forbidden(*args, **kwargs):
        pytest.fail("Запрещённое чтение не должно доходить до RetailTools")

    if hasattr(RetailTools, name):
        monkeypatch.setattr(RetailTools, name, forbidden)
    with pytest.raises(ValueError):
        adapter.read(name, args, "u1")


def test_reads_and_snapshots_are_detached_from_storage(snapshot):
    adapter = RetailAdapter(snapshot, "Правила")
    snapshot["orders"]["#O1"]["status"] = "cancelled"
    order = adapter.read("get_order_details", {"order_id": "#O1"}, "u1")
    assert order["status"] == "delivered"
    order["status"] = "cancelled"
    exported = adapter.snapshot()
    exported["users"]["u1"]["email"] = "changed"
    assert adapter.snapshot()["orders"]["#O1"]["status"] == "delivered"
    assert (
        adapter.read("find_user_id_by_email", {"email": "USER1@example.test"}, "u1")
        == "u1"
    )
    assert (
        adapter.read("get_product_details", {"product_id": "p1"}, "u1")["product_id"]
        == "p1"
    )


@pytest.mark.parametrize(
    "name,native,args",
    [
        ("propose_exchange", "exchange_delivered_order_items", exchange_args()),
        (
            "propose_return",
            "return_delivered_order_items",
            {"order_id": "#O1", "item_ids": ["old"], "payment_method_id": "card"},
        ),
    ],
)
def test_proposal_never_calls_native_write(snapshot, monkeypatch, name, native, args):
    adapter = RetailAdapter(snapshot, "Правила")

    def forbidden(*args, **kwargs):
        pytest.fail("Подготовка предложения не должна вызывать запись даже на копии")

    monkeypatch.setattr(RetailTools, native, forbidden)
    assert adapter.plan(name, args, "u1").name == native


@pytest.mark.parametrize(
    "kind,args,amount,status",
    [
        ("propose_exchange", exchange_args(), Decimal("2.35"), "exchange requested"),
        (
            "propose_exchange",
            {**exchange_args(), "new_item_ids": ["cheap"]},
            Decimal("-1.10"),
            "exchange requested",
        ),
        (
            "propose_return",
            {"order_id": "#O1", "item_ids": ["old"], "payment_method_id": "gift"},
            Decimal("-10.00"),
            "return requested",
        ),
    ],
)
def test_plan_preserves_snapshot_and_execution_registers_only_request(
    snapshot, kind, args, amount, status
):
    adapter = RetailAdapter(snapshot, "Правила")
    before = adapter.snapshot()
    action = adapter.plan(kind, args, "u1")
    assert action.amount == amount and action.result_status == status
    assert action.name in {
        "exchange_delivered_order_items",
        "return_delivered_order_items",
    }
    assert any(e.source == "orders/#O1" for e in action.evidence)
    assert any(e.source.startswith("policy.md#") for e in action.evidence)
    assert adapter.snapshot() == before
    assert (
        adapter.plan(action.name, action.arguments, "u1").fingerprint()
        == action.fingerprint()
    )
    result = adapter.execute(action, "u1")
    after = adapter.snapshot()
    assert result["status"] == status
    assert after["users"] == before["users"]
    assert (
        after["orders"]["#O1"]["payment_history"]
        == before["orders"]["#O1"]["payment_history"]
    )
    assert after["orders"]["#O1"]["items"] == before["orders"]["#O1"]["items"]
    assert after["orders"]["#O2"] == before["orders"]["#O2"]
    with pytest.raises(ValueError):
        adapter.execute(action, "u1")
    assert adapter.snapshot() == after


@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "too_many",
        "unequal",
        "same_variant",
        "other_product",
        "unavailable",
        "foreign_order",
        "foreign_payment",
        "not_delivered",
        "gift_short",
        "return_card",
    ],
)
def test_invalid_action_is_rejected_without_mutation(snapshot, case):
    args = exchange_args()
    name = "propose_exchange"
    if case == "empty":
        args.update(item_ids=[], new_item_ids=[])
    elif case == "too_many":
        args.update(item_ids=["old", "old"], new_item_ids=["new", "new"])
    elif case == "unequal":
        args["new_item_ids"] = []
    elif case == "same_variant":
        args["new_item_ids"] = ["old"]
    elif case == "other_product":
        args["new_item_ids"] = ["foreign"]
    elif case == "unavailable":
        snapshot["products"]["p1"]["variants"]["new"]["available"] = False
    elif case == "foreign_order":
        args["order_id"] = "#O2"
    elif case == "foreign_payment":
        args["payment_method_id"] = "missing"
    elif case == "not_delivered":
        snapshot["orders"]["#O1"]["status"] = "pending"
    elif case == "gift_short":
        args["payment_method_id"] = "gift"
        snapshot["users"]["u1"]["payment_methods"]["gift"]["balance"] = 2.34
    elif case == "return_card":
        name = "propose_return"
        args.pop("new_item_ids")
        args["payment_method_id"] = "other_card"
    adapter = RetailAdapter(snapshot, "Правила")
    before = adapter.snapshot()
    with pytest.raises(ValueError):
        adapter.plan(name, args, "u1")
    assert adapter.snapshot() == before


def test_duplicate_items_are_counted_and_ambiguous_purchase_prices_rejected(snapshot):
    snapshot["orders"]["#O1"]["items"] *= 2
    adapter = RetailAdapter(snapshot, "Правила")
    args = {
        **exchange_args(),
        "item_ids": ["old", "old"],
        "new_item_ids": ["new", "cheap"],
    }
    assert adapter.plan("propose_exchange", args, "u1").amount == Decimal("1.25")
    snapshot["orders"]["#O1"]["items"] = deepcopy(snapshot["orders"]["#O1"]["items"])
    snapshot["orders"]["#O1"]["items"][1] = {
        **snapshot["orders"]["#O1"]["items"][1],
        "price": 9.0,
    }
    with pytest.raises(ValueError):
        RetailAdapter(snapshot, "Правила").plan("propose_exchange", args, "u1")


@pytest.mark.parametrize("change", ["price", "balance", "policy", "text", "amount"])
def test_execution_rejects_changed_conditions_or_action(snapshot, change):
    action = RetailAdapter(snapshot, "Правила").plan(
        "propose_exchange", exchange_args(), "u1"
    )
    policy = "Правила"
    if change == "price":
        snapshot["products"]["p1"]["variants"]["new"]["price"] = 13.0
    elif change == "balance":
        snapshot["users"]["u1"]["payment_methods"]["gift"]["balance"] = 99.0
    elif change == "policy":
        policy = "Новые правила"
    elif change == "text":
        action.description = "Подменённое действие"
    elif change == "amount":
        action.amount = Decimal("0")
    adapter = RetailAdapter(snapshot, policy)
    before = adapter.snapshot()
    with pytest.raises(ValueError):
        adapter.execute(action, "u1")
    assert adapter.snapshot() == before


def test_unexpected_native_mutation_rolls_back_adapter_snapshot(snapshot, monkeypatch):
    adapter = RetailAdapter(snapshot, "Правила")
    action = adapter.plan("propose_exchange", exchange_args(), "u1")
    before = adapter.snapshot()
    original = RetailTools.exchange_delivered_order_items

    def corrupt(self, **kwargs):
        result = original(self, **kwargs)
        self.db.users["u1"].payment_methods["gift"].balance = 0
        return result

    monkeypatch.setattr(RetailTools, "exchange_delivered_order_items", corrupt)
    with pytest.raises(ValueError):
        adapter.execute(action, "u1")
    assert adapter.snapshot() == before
