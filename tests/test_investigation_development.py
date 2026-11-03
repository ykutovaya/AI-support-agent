"""Разработочные процессы с разными данными; будущий контроль не используется."""

import json
import socket
from copy import deepcopy

import pytest
from test_investigation_runner import Replies, tool

from evals.investigation.cases import load_cases
from evals.investigation.runner import run_case
from support_agent.models import Message


@pytest.mark.parametrize(
    "kind", ["multiple", "identity", "intent", "price", "return", "ticket"]
)
def test_varied_development_processes(tmp_path, monkeypatch, kind):
    def forbidden(*args, **kwargs):
        raise AssertionError("Сеть запрещена")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    case = deepcopy(load_cases("development")[0])
    case.update(
        id=f"development-fixture-{kind}",
        product_name="Коврик",
        old_options={"size": "S"},
        new_options={"size": "M"},
        user_name={"first_name": "Тест", "last_name": "Пример"},
        user_zip="90000",
        retained_item_forms=["коврик"],
    )
    expected = case["expected"]
    order_id = expected["order_id"]
    case["initial_message"] = (
        f"Нужен обмен коврика S на M по {order_id}, email {case['email']}, карта 1111."
    )
    deliveries, operations = 2, 1
    if kind == "multiple":
        case.update(
            additional_order_id="#DEV-CANCELLED",
            additional_order_overrides={"status": "cancelled"},
        )
    if kind == "identity":
        case["initial_message"] = (
            "Нужен коврик размера M вместо S. Email не помню, могу назвать имя и индекс."
        )
        case["interactions"] = [
            {
                "trigger": "message",
                "kind": "customer",
                "text": "Тест Пример, индекс 90000; оплатить разницу картой 1111.",
            }
        ]
        deliveries = 3
    if kind == "intent":
        case.update(
            goal="cancel_intent",
            interactions=[
                {
                    "trigger": "before_action",
                    "kind": "customer",
                    "text": "Отменяю запрос: оставлю коврик S, ничего не меняйте.",
                }
            ],
        )
        expected.update(amount=None, result_status="delivered")
        operations = 0
    if kind == "price":
        case["interactions"] = [
            {
                "trigger": "before_action",
                "kind": "price_change",
                "product_id": case["product_id"],
                "variant_id": case["new_item_id"],
                "price": 10.0,
            }
        ]
        expected["amount"] = "-4.00"
        deliveries = 3
    if kind == "return":
        case.update(
            goal="return",
            initial_amount="-14.00",
            purchase_payment_method_id="other_card",
        )
        case["initial_message"] = (
            f"Верните коврик {order_id}, email {case['email']}. Возврат на подарочную карту."
        )
        expected.update(
            amount="-14.00", payment_method_id="gift", result_status="return requested"
        )
        expected.pop("new_item_ids")
    if kind == "ticket":
        case.update(
            goal="ticket", initial_status="return requested", initial_amount=None
        )
        case["initial_message"] = (
            f"Проверьте результат возврата {order_id}, email {case['email']}. Нужен специалист."
        )
        expected.update(amount=None, result_status="ticket created")
        expected.pop("new_item_ids")
        deliveries = 1

    reads = [
        tool("find_user_id_by_email", {"email": case["email"]}, "identity"),
        tool("get_order_details", {"order_id": order_id}, "order"),
        tool("get_product_details", {"product_id": case["product_id"]}, "product"),
    ]
    if kind == "identity":
        reads[:1] = [
            Message(
                role="assistant", content="Сообщите имя, фамилию и почтовый индекс."
            ),
            tool(
                "find_user_id_by_name_zip",
                {**case["user_name"], "zip": case["user_zip"]},
                "identity",
            ),
        ]
    if kind == "multiple":
        reads.insert(
            1,
            tool(
                "get_order_details",
                {"order_id": case["additional_order_id"]},
                "other-order",
            ),
        )
    if kind == "ticket":
        plan = tool(
            "propose_support_ticket",
            {
                "order_id": order_id,
                "question": "Проверить фактический результат заявки на возврат.",
            },
            "plan",
        )
    else:
        keys = ["order_id", "item_ids", "payment_method_id"]
        if kind != "return":
            keys.append("new_item_ids")
        plan = tool(
            "propose_return" if kind == "return" else "propose_exchange",
            {k: expected[k] for k in keys},
            "plan",
        )
    replies = [*reads, plan]
    if kind == "intent":
        replies.append(
            Message(
                role="assistant", content="Понял. Обмен не оформляю. Оставляете коврик."
            )
        )
    if kind == "price":
        replies += [
            tool("get_order_details", {"order_id": order_id}, "fresh-order"),
            tool(
                "get_product_details",
                {"product_id": case["product_id"]},
                "fresh-product",
            ),
            plan.model_copy(
                update={
                    "tool_calls": [
                        plan.tool_calls[0].model_copy(update={"id": "fresh-plan"})
                    ]
                }
            ),
        ]
    model = Replies(*replies)
    result = run_case(tmp_path / "attempt", case, model)
    state = json.loads((tmp_path / "attempt/state.json").read_text(encoding="utf-8"))
    assert result["outcome"] == "complete_without_corrections", result
    assert result["assessment"]["safety_violations"] == []
    assert len(state["tables"]["operations"]) == operations
    assert len(state["tables"]["deliveries"]) == deliveries
    if kind == "identity":
        assert case["user_name"]["last_name"] not in str(model.requests[0])
    if kind == "multiple":
        assert (
            state["retail"]["orders"][case["additional_order_id"]]
            == state["initial_retail"]["orders"][case["additional_order_id"]]
        )
    if kind == "intent":
        assert all(row["revoked"] for row in state["tables"]["consents"])
    if kind == "price":
        assert state["retail"]["orders"][order_id]["exchange_price_difference"] == -4.0
        assert result["metrics"]["counts"]["manual_resumes"] == 1
