"""Обращение сохраняет проверенные факты и точные условия согласования."""

from copy import deepcopy
from decimal import Decimal

import pytest
from pydantic import ValidationError

from support_agent.adapters.retail import RetailAdapter
from support_agent.proposals import Action

POLICY = "Правила магазина, версия 1"
TARGET = "support-team"
QUESTION = "Когда будет завершён обмен?"


def ticket_adapter(snapshot, policy=POLICY, target=TARGET):
    from support_agent.adapters.ticket_retail import TicketRetailAdapter

    return TicketRetailAdapter(snapshot, policy, target=target)


def requested(snapshot, status="exchange requested"):
    snapshot["orders"]["#O1"]["status"] = status
    return snapshot


def plan(adapter, **args):
    return adapter.plan(
        "propose_support_ticket",
        {"order_id": "#O1", "question": QUESTION, **args},
        "u1",
    )


def test_ticket_action_has_no_money_and_retail_amount_remains_required():
    data = dict(
        name="create_support_ticket",
        arguments={},
        amount=None,
        currency="",
        conditions="snapshot",
        result_status="ticket created",
        description="Обращение специалисту",
        evidence=[],
    )
    action = Action(**data)
    assert Action.model_validate_json(action.model_dump_json()).amount is None
    for changes in (
        {"amount": "0"},
        {"currency": "USD"},
        {"result_status": "exchange requested"},
        {"name": "exchange_delivered_order_items"},
        {
            "name": "exchange_delivered_order_items",
            "result_status": "exchange requested",
        },
    ):
        with pytest.raises(ValidationError):
            Action(**{**data, **changes})
    retail = {
        **data,
        "name": "exchange_delivered_order_items",
        "result_status": "exchange requested",
    }
    retail.pop("amount")
    with pytest.raises(ValidationError):
        Action(**retail)


@pytest.mark.parametrize("status", ["exchange requested", "return requested"])
def test_ticket_plan_has_canonical_safe_facts_and_rule(snapshot, status):
    adapter = ticket_adapter(requested(snapshot, status))
    before = adapter.snapshot()
    action = plan(adapter)
    assert action.name == "create_support_ticket"
    assert action.result_status == "ticket created"
    assert action.amount is None and action.currency == ""
    assert set(action.arguments) == {
        "order_id",
        "user_id",
        "target",
        "title",
        "body",
        "question",
    }
    assert action.arguments["order_id"] == "#O1"
    assert action.arguments["user_id"] == "u1"
    assert action.arguments["target"] == TARGET
    assert action.arguments["question"] == QUESTION
    body = action.arguments["body"]
    assert all(
        value in body
        for value in ["#O1", "u1", status, QUESTION, "policy.md#requested-order-result"]
    )
    assert "версия" in body.lower()
    assert "принадлежность" in body.lower() and "статус" in body.lower()
    assert "зарегистрирована" in body.lower()
    assert "не установлено" in body.lower()
    assert "специалист" in body.lower()
    assert {item.source for item in action.evidence} >= {
        "orders/#O1",
        "policy.md#requested-order-result",
    }
    rendered = action.model_dump_json()
    for private_value in [
        "Учебная",
        "1111",
        "2222",
        "payment_history",
        "payment_methods",
        "user1@example.test",
        "#O2",
        "u2",
        "Чайник",
    ]:
        assert private_value not in rendered
    assert adapter.snapshot() == before
    restored = Action.model_validate_json(action.model_dump_json())
    assert adapter.check(restored, "u1").status == "unchanged"
    assert (
        adapter.plan(action.name, action.arguments, "u1").fingerprint()
        == action.fingerprint()
    )


@pytest.mark.parametrize("status", ["delivered", "pending", "processed", "cancelled"])
def test_only_registered_requests_can_be_escalated(snapshot, status):
    with pytest.raises(ValueError):
        plan(ticket_adapter(requested(snapshot, status)))


@pytest.mark.parametrize(
    "args",
    [
        {"question": ""},
        {"question": "   "},
        {"question": "x" * 501},
        {"question": 5},
        {"body": "Деньги возвращены"},
        {"order_id": "#O2"},
    ],
)
def test_ticket_input_rejects_invalid_questions_extra_facts_and_foreign_orders(
    snapshot, args
):
    with pytest.raises(ValueError):
        plan(ticket_adapter(requested(snapshot)), **args)


def test_ticket_tool_exposes_only_order_and_bounded_question(snapshot):
    adapter = ticket_adapter(snapshot)
    specs = {spec.name: spec for spec in adapter.specs()}
    assert set(specs) == {
        spec.name for spec in RetailAdapter(snapshot, POLICY).specs()
    } | {"propose_support_ticket"}
    schema = specs["propose_support_ticket"].parameters
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"order_id", "question"}
    assert set(schema["properties"]) == {"order_id", "question"}
    assert schema["properties"]["question"]["minLength"] == 1
    assert schema["properties"]["question"]["maxLength"] == 500


@pytest.mark.parametrize(
    "field,value",
    [
        ("body", "Возврат денег исполнен"),
        ("title", "Иной заголовок"),
        ("question", "Другой вопрос"),
        ("target", "other-team"),
        ("user_id", "u2"),
        ("order_id", "#O2"),
    ],
)
def test_changed_saved_ticket_parameters_are_forbidden(snapshot, field, value):
    adapter = ticket_adapter(requested(snapshot))
    action = plan(adapter)
    action.arguments[field] = value
    # Реальное изменение условий не должно скрыть подмену уже согласованных параметров.
    snapshot["orders"]["#O1"]["status"] = "return requested"
    check = ticket_adapter(snapshot).check(action, "u1")
    assert check.status == "forbidden"
    assert check.current is None and check.changes == []


@pytest.mark.parametrize(
    "field",
    [
        "description",
        "evidence",
        "conditions",
        "condition_values",
        "amount",
        "currency",
        "result_status",
    ],
)
def test_changed_saved_ticket_metadata_is_forbidden(snapshot, field):
    adapter = ticket_adapter(requested(snapshot))
    action = plan(adapter)
    values = {
        "description": "Иное",
        "evidence": [],
        "conditions": "forged",
        "condition_values": {},
        "amount": Decimal("1"),
        "currency": "USD",
        "result_status": "return requested",
    }
    action = action.model_copy(update={field: values[field]})
    assert adapter.check(action, "u1").status == "forbidden"


@pytest.mark.parametrize(
    "change,field,before,after,status",
    [
        ("status", "order.status", "exchange requested", "return requested", "changed"),
        (
            "forbidden_status",
            "order.status",
            "exchange requested",
            "delivered",
            "forbidden",
        ),
        ("policy", "policy", POLICY, "Новые правила", "changed"),
        ("target", "target", TARGET, "other-team", "changed"),
    ],
)
def test_relevant_condition_changes_require_new_approval(
    snapshot, change, field, before, after, status
):
    action = plan(ticket_adapter(requested(snapshot)))
    policy, target = POLICY, TARGET
    if change in {"status", "forbidden_status"}:
        snapshot["orders"]["#O1"]["status"] = after
    elif change == "policy":
        policy = after
    else:
        target = after
    check = ticket_adapter(snapshot, policy, target).check(action, "u1")
    assert check.status == status
    difference = next(value for value in check.changes if value.field == field)
    assert (difference.before, difference.after) == (before, after)
    assert difference.label
    assert (check.current is not None) == (status == "changed")


def test_other_orders_products_payment_and_address_do_not_change_ticket(snapshot):
    action = plan(ticket_adapter(requested(snapshot)))
    snapshot["orders"]["#O2"]["status"] = "cancelled"
    snapshot["orders"]["#O1"]["address"]["address1"] = "Иной адрес"
    snapshot["products"]["p1"]["variants"]["new"]["price"] = 999
    snapshot["users"]["u1"]["payment_methods"]["gift"]["balance"] = 999
    check = ticket_adapter(snapshot).check(action, "u1")
    assert check.status == "unchanged"
    assert check.changes == []


@pytest.mark.parametrize(
    "change", ["different_user", "owner_changed", "missing_membership", "missing_order"]
)
def test_ticket_access_is_rechecked_without_disclosing_other_customer(snapshot, change):
    action = plan(ticket_adapter(requested(snapshot)))
    user_id = "u1"
    if change == "different_user":
        user_id = "u2"
    elif change == "owner_changed":
        snapshot["orders"]["#O1"]["user_id"] = "u2"
    elif change == "missing_membership":
        snapshot["users"]["u1"]["orders"] = []
    else:
        del snapshot["orders"]["#O1"]
    check = ticket_adapter(snapshot).check(action, user_id)
    assert check.status == "forbidden"
    assert check.current is None and check.changes == []
    assert "u2" not in check.model_dump_json()


def test_existing_retail_actions_keep_original_plan_check_and_execution(snapshot):
    args = {
        "order_id": "#O1",
        "item_ids": ["old"],
        "new_item_ids": ["new"],
        "payment_method_id": "card",
    }
    retail = RetailAdapter(deepcopy(snapshot), POLICY)
    adapter = ticket_adapter(snapshot)
    action = adapter.plan("propose_exchange", args, "u1")
    original = retail.plan("propose_exchange", args, "u1")
    assert action.fingerprint() == original.fingerprint()
    assert adapter.check(action, "u1").status == "unchanged"
    assert adapter.execute(action, "u1") == retail.execute(original, "u1")
