"""Условия показывают реальные изменения и не разрешают устаревшее действие."""

from copy import deepcopy
from decimal import Decimal

import pytest

from support_agent.adapters.retail import RetailAdapter
from support_agent.proposals import Action, Evidence, Proposal


def exchange_args(**changes):
    return {
        "order_id": "#O1",
        "item_ids": ["old"],
        "new_item_ids": ["new"],
        "payment_method_id": "card",
        **changes,
    }


def legacy_action():
    return Action(
        name="exchange_delivered_order_items",
        arguments={"order_id": "A"},
        amount="16.85",
        conditions="snapshot-a",
        result_status="exchange requested",
        description="Заявка на обмен",
        evidence=[Evidence(title="Заказ", source="orders/A", detail="delivered")],
    )


def test_legacy_serialization_preserves_saved_signatures():
    action = legacy_action()
    assert "condition_values" not in action.model_dump(mode="json")
    assert action.fingerprint() == (
        "90b1140b8e727b30e18f9bf9a29882549980695ee486d0b7d01f467b95c1a302"
    )
    for kind, key, expected in [
        (
            "action",
            "action",
            "e68a0bca7d5afdbcb888a3d2af815b9f50fed058c45cc4c6a93de2b8ee6209b4",
        ),
        (
            "message",
            "consent_request",
            "447949c6eee646f877055825b9e6904227caacac5c2090aea06dc8f93dc4ffa9",
        ),
    ]:
        proposal = Proposal(
            id="p", case_id="c", version=1, kind=kind, text="Обмен", **{key: action}
        )
        assert proposal.digest() == expected
        restored = Proposal.model_validate_json(proposal.model_dump_json())
        assert restored.digest() == expected


def test_plan_preserves_before_values_after_json_roundtrip(snapshot):
    adapter = RetailAdapter(snapshot, "Правила")
    action = adapter.plan("propose_exchange", exchange_args(), "u1")
    restored = Action.model_validate_json(action.model_dump_json())
    assert restored.condition_values["order"]["status"] == "delivered"
    assert (
        restored.condition_values["products"]["p1"]["variants"]["new"]["price"] == 12.35
    )
    assert restored.condition_values["amount"] == "2.35"
    restored.condition_values["order"]["status"] = "cancelled"
    assert adapter.snapshot()["orders"]["#O1"]["status"] == "delivered"


def test_unrelated_order_does_not_invalidate_conditions(snapshot):
    action = RetailAdapter(snapshot, "Правила").plan(
        "propose_exchange", exchange_args(), "u1"
    )
    snapshot["orders"]["#O2"]["status"] = "cancelled"
    check = RetailAdapter(snapshot, "Правила").check(action, "u1")
    assert check.status == "unchanged"
    assert check.changes == []
    assert check.current.fingerprint() == action.fingerprint()


def test_price_change_shows_price_and_delta_but_cannot_execute_old_action(snapshot):
    action = RetailAdapter(snapshot, "Правила").plan(
        "propose_exchange", exchange_args(), "u1"
    )
    snapshot["products"]["p1"]["variants"]["new"]["price"] = 13.35
    adapter = RetailAdapter(snapshot, "Правила")
    before_check = adapter.snapshot()
    check = adapter.check(action, "u1")
    assert check.status == "changed"
    changes = {change.field: change for change in check.changes}
    assert changes["products.p1.variants.new.price"].before == 12.35
    assert changes["products.p1.variants.new.price"].after == 13.35
    assert changes["amount"].before == "2.35"
    assert changes["amount"].after == "3.35"
    assert all(change.label for change in check.changes)
    assert check.current.amount == Decimal("3.35")
    with pytest.raises(ValueError, match="новое согласование"):
        adapter.execute(action, "u1")
    assert adapter.snapshot() == before_check


@pytest.mark.parametrize(
    "field,before,after,reason",
    [
        ("order.status", "delivered", "cancelled", "доставленного"),
        ("products.p1.variants.new.available", True, False, "доступный"),
        ("payment_methods.gift.balance", 5.0, 1.0, "Недостаточно"),
    ],
)
def test_forbidden_conditions_show_before_after_without_current_action(
    snapshot, field, before, after, reason
):
    action = RetailAdapter(snapshot, "Правила").plan(
        "propose_exchange", exchange_args(payment_method_id="gift"), "u1"
    )
    if field == "order.status":
        snapshot["orders"]["#O1"]["status"] = after
    elif field.endswith("available"):
        snapshot["products"]["p1"]["variants"]["new"]["available"] = after
    else:
        snapshot["users"]["u1"]["payment_methods"]["gift"]["balance"] = after
    check = RetailAdapter(snapshot, "Правила").check(action, "u1")
    assert check.status == "forbidden"
    assert check.current is None
    assert reason in check.reason
    change = next(change for change in check.changes if change.field == field)
    assert (change.before, change.after) == (before, after)


@pytest.mark.parametrize("legacy", [True, False])
def test_missing_or_tampered_old_values_fail_closed(snapshot, legacy):
    adapter = RetailAdapter(snapshot, "Правила")
    data = adapter.plan("propose_exchange", exchange_args(), "u1").model_dump(
        mode="json"
    )
    if legacy:
        data.pop("condition_values", None)
    else:
        data["condition_values"]["order"]["status"] = "cancelled"
    action = Action.model_validate(data)
    check = adapter.check(action, "u1")
    assert check.status == "forbidden"
    assert check.current is None
    assert check.changes == []
    assert check.reason


def test_ownership_checked_before_diagnostic_read(snapshot, monkeypatch):
    adapter = RetailAdapter(snapshot, "Правила")
    action = adapter.plan("propose_exchange", exchange_args(), "u1")
    monkeypatch.setattr(
        adapter, "plan", lambda *_: pytest.fail("Чтение до проверки доступа")
    )
    check = adapter.check(action, "u2")
    assert check.status == "forbidden"
    assert check.current is None
    assert check.changes == []
    assert "недоступен" in check.reason


def test_check_never_executes_native_tools(snapshot, monkeypatch):
    from tau2.domains.retail.tools import RetailTools

    adapter = RetailAdapter(snapshot, "Правила")
    action = adapter.plan("propose_exchange", exchange_args(), "u1")
    initial = deepcopy(adapter.snapshot())
    monkeypatch.setattr(
        RetailTools,
        "exchange_delivered_order_items",
        lambda *_args, **_kwargs: pytest.fail("Проверка вызвала исполнение"),
    )
    assert adapter.check(action, "u1").status == "unchanged"
    assert adapter.snapshot() == initial
