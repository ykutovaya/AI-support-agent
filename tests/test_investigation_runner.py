"""Проверки полного обращения на подставной модели без API."""

import json
from copy import deepcopy

import pytest

from evals.investigation import runner
from support_agent.models import Generation, Message, ToolCall


class Replies:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.requests = []

    def generate(self, messages, tools):
        self.requests.append(deepcopy(messages))
        return Generation(message=next(self.replies))


def tool(name, arguments, identifier):
    return Message(
        role="assistant",
        tool_calls=[ToolCall(id=identifier, name=name, arguments=arguments)],
    )


@pytest.fixture
def exchange_case(snapshot, monkeypatch):
    monkeypatch.setattr(runner, "snapshot_for", lambda case: deepcopy(snapshot))
    return {
        "id": "runner-exchange",
        "split": "development",
        "user_id": "u1",
        "email": "user1@example.test",
        "initial_message": "Хочу обменять чёрные на белые. Email user1@example.test. Доплату внесу прежней картой.",
        "goal": "exchange",
        "product_id": "p1",
        "initial_status": "delivered",
        "initial_amount": "2.35",
        "expected": {
            "order_id": "#O1",
            "item_ids": ["old"],
            "new_item_ids": ["new"],
            "payment_method_id": "card",
            "amount": "2.35",
            "result_status": "exchange requested",
        },
        "interactions": [],
        "hidden_test_marker": "НЕ ПЕРЕДАВАТЬ МОДЕЛИ",
    }


def exchange_model():
    return Replies(
        tool("find_user_id_by_email", {"email": "user1@example.test"}, "id"),
        tool("get_order_details", {"order_id": "#O1"}, "order"),
        tool("get_product_details", {"product_id": "p1"}, "product"),
        tool(
            "propose_exchange",
            {
                "order_id": "#O1",
                "item_ids": ["old"],
                "new_item_ids": ["new"],
                "payment_method_id": "card",
            },
            "plan",
        ),
    )


def test_fresh_full_case_reaches_result_with_two_deliveries_and_no_hidden_data(
    tmp_path, exchange_case
):
    model = exchange_model()
    result = runner.run_case(tmp_path / "attempt", exchange_case, model)
    state = json.loads((tmp_path / "attempt/state.json").read_text(encoding="utf-8"))
    assert result["failure"] is None
    assert state["retail"]["orders"]["#O1"]["status"] == "exchange requested"
    assert len(state["tables"]["operations"]) == 1
    assert len(state["tables"]["deliveries"]) == 2
    assert result["metrics"]["scope"] == "full_case"
    assert result["outcome"] == "complete_without_corrections"
    assert result["structured_action_hint_used"] is False
    assert not any(
        exchange_case["hidden_test_marker"] in str(messages)
        for messages in model.requests
    )
    assert model.requests[0][-1].content == exchange_case["initial_message"]
    assert all(message.role != "tool" for message in model.requests[0])


def test_existing_attempt_directory_is_not_overwritten(tmp_path, exchange_case):
    folder = tmp_path / "attempt"
    folder.mkdir()
    (folder / "kept.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        runner.run_case(folder, exchange_case, exchange_model())
    assert (folder / "kept.txt").read_text(encoding="utf-8") == "keep"


def test_model_failure_preserves_state_and_separates_technical_failure(
    tmp_path, exchange_case
):
    class Broken:
        def generate(self, messages, tools):
            raise TimeoutError("timeout")

    result = runner.run_case(tmp_path / "failure", exchange_case, Broken())
    state = json.loads((tmp_path / "failure/state.json").read_text(encoding="utf-8"))
    assert result["failure"]["category"] == "external"
    assert result["outcome"] == "failed"
    assert result["structured_action_hint_used"] is False
    assert not state["tables"]["operations"]
    assert not state["tables"]["deliveries"]
    assert state["messages"][0]["content"] == exchange_case["initial_message"]


def test_price_change_blocks_old_action_then_requires_new_question_and_consent(
    tmp_path, exchange_case
):
    case = deepcopy(exchange_case)
    case["expected"]["amount"] = "9.99"
    case["interactions"] = [
        {
            "trigger": "before_action",
            "kind": "price_change",
            "product_id": "p1",
            "variant_id": "new",
            "price": 19.99,
        }
    ]
    model = exchange_model()
    model.replies = iter(
        [
            tool("find_user_id_by_email", {"email": "user1@example.test"}, "id"),
            tool("get_order_details", {"order_id": "#O1"}, "order"),
            tool("get_product_details", {"product_id": "p1"}, "product"),
            tool(
                "propose_exchange",
                {
                    "order_id": "#O1",
                    "item_ids": ["old"],
                    "new_item_ids": ["new"],
                    "payment_method_id": "card",
                },
                "plan",
            ),
            tool("get_order_details", {"order_id": "#O1"}, "fresh-order"),
            tool(
                "propose_exchange",
                {
                    "order_id": "#O1",
                    "item_ids": ["old"],
                    "new_item_ids": ["new"],
                    "payment_method_id": "card",
                },
                "fresh-plan",
            ),
        ]
    )
    result = runner.run_case(tmp_path / "price", case, model)
    state = json.loads((tmp_path / "price/state.json").read_text(encoding="utf-8"))
    assert result["failure"] is None
    assert len(state["tables"]["operations"]) == 1
    assert len(state["tables"]["deliveries"]) == 3
    assert len(state["tables"]["consents"]) == 2
    assert state["retail"]["orders"]["#O1"]["exchange_price_difference"] == 9.99
    assert any(p["status"] == "stale" for p in state["tables"]["proposals"])
    assert result["metrics"]["counts"]["manual_resumes"] == 1
    assert result["status"] == "complete"
    assert result["outcome"] == "complete_without_corrections"
    assert result["structured_action_hint_used"] is False


@pytest.mark.parametrize("reason", ["refusal", "request_change", None])
def test_customer_changed_mind_stops_action_and_records_actual_new_message(
    tmp_path, exchange_case, reason
):
    case = deepcopy(exchange_case)
    case["goal"] = "cancel_intent"
    case["expected"]["amount"] = None
    case["expected"]["result_status"] = "delivered"
    case["interactions"] = [
        {
            "trigger": "before_action",
            "kind": "customer",
            "text": "Передумал. Не оформляйте обмен.",
        }
    ]
    if reason is not None:
        case["interactions"][0]["customer_intent"] = reason
    if reason == "request_change":
        case["interactions"][0]["text"] = (
            "Пока не оформляйте обмен. Хочу сначала уточнить другой размер."
        )
    initial = list(exchange_model().replies)
    model = Replies(
        *initial, Message(role="assistant", content="Хорошо, обмен не оформляю.")
    )
    result = runner.run_case(tmp_path / "intent", case, model)
    state = json.loads((tmp_path / "intent/state.json").read_text(encoding="utf-8"))
    assert result["failure"] is None
    assert not state["tables"]["operations"]
    assert state["retail"]["orders"]["#O1"]["status"] == "delivered"
    assert len(state["tables"]["deliveries"]) == 2
    assert any(
        m["content"] == case["interactions"][0]["text"] for m in state["messages"]
    )
    annotations = json.loads(
        (tmp_path / "intent/customer-annotations.json").read_text(encoding="utf-8")
    )
    actual = next(row for row in annotations if row["revoked_consent_ids"])
    assert actual["reason"] == (reason or "unknown")
    assert actual["case_id"] == state["case"]["id"]
    assert actual["revoked_consent_ids"] == [state["tables"]["consents"][0]["id"]]
    event = next(e for e in state["events"] if e["id"] == actual["event_id"])
    assert event["kind"] == "customer"
    assert event["text"] == case["interactions"][0]["text"]
    assert "customer_intent" not in str(model.requests)
    assert "revoked_consent_ids" not in str(model.requests)
    assert result["evaluator_failure"] is None
    counts = result["metrics"]["counts"]
    assert counts["client_refusals"] == int(reason == "refusal")
    assert counts["client_text_refusals"] == int(reason == "refusal")
    assert counts["client_button_refusals"] == 0
    assert counts["client_consent_revocations"] == 1
    assert counts["client_consent_revocations_unknown_reason"] == int(reason is None)


def test_missing_identity_is_answered_only_after_delivered_question(
    tmp_path, exchange_case
):
    case = deepcopy(exchange_case)
    case["initial_message"] = "Хочу белые наушники вместо чёрных."
    case["interactions"] = [
        {
            "trigger": "message",
            "kind": "customer",
            "text": "Email user1@example.test, разницу прежней картой.",
        }
    ]
    model = Replies(
        Message(role="assistant", content="Какой у вас email?"),
        *list(exchange_model().replies),
    )
    result = runner.run_case(tmp_path / "identity", case, model)
    state = json.loads((tmp_path / "identity/state.json").read_text(encoding="utf-8"))
    assert result["failure"] is None
    assert len(state["tables"]["deliveries"]) == 3
    assert state["tables"]["deliveries"][0]["text"] == "Какой у вас email?"
    assert all("user1@example.test" not in str(m) for m in model.requests[0])


def test_unknown_refusal_is_not_sent_or_counted_as_success(tmp_path, exchange_case):
    case = deepcopy(exchange_case)
    case["goal"] = "refusal"
    result = runner.run_case(
        tmp_path / "review",
        case,
        Replies(
            Message(
                role="assistant", content="Возможно, получится что-нибудь придумать."
            )
        ),
    )
    state = json.loads((tmp_path / "review/state.json").read_text(encoding="utf-8"))
    assert result["review_required"] is True
    assert result["outcome"] == "review"
    assert result["structured_action_hint_used"] is False
    assert result["metrics"]["outcome"]["correct"] is None
    assert not state["tables"]["deliveries"]
    assert not state["tables"]["operations"]


def test_evaluator_failure_does_not_erase_completed_application_result(
    tmp_path, exchange_case, monkeypatch
):
    def broken(*args):
        raise ValueError("broken grader")

    monkeypatch.setattr(runner, "assess_case", broken)
    result = runner.run_case(tmp_path / "grader", exchange_case, exchange_model())
    state = json.loads((tmp_path / "grader/state.json").read_text(encoding="utf-8"))
    assert result["failure"] is None
    assert result["evaluator_failure"]["category"] == "evaluator"
    assert len(state["tables"]["operations"]) == 1
    assert result["metrics"]["outcome"]["correct"] is None


def test_step_limit_stops_before_any_approval_or_delivery(tmp_path, exchange_case):
    result = runner.run_case(
        tmp_path / "limited", exchange_case, exchange_model(), max_steps=1
    )
    state = json.loads((tmp_path / "limited/state.json").read_text(encoding="utf-8"))
    assert result["failure"]["category"] == "limit"
    assert not state["tables"]["operations"]
    assert not state["tables"]["deliveries"]


def test_approval_evaluator_failure_is_separate_and_never_sends(
    tmp_path, exchange_case, monkeypatch
):
    def broken(*args, **kwargs):
        raise ValueError("approval evaluator broken")

    monkeypatch.setattr(runner, "approval_review", broken)
    result = runner.run_case(
        tmp_path / "broken-review", exchange_case, exchange_model()
    )
    assert result["failure"] is None
    assert result["evaluator_failure"]["category"] == "evaluator"
    assert result["status"] == "failed"
    assert result["metrics"]["outcome"]["correct"] is None


def test_success_status_requires_complete_assessment(tmp_path, exchange_case):
    result = runner.run_case(
        tmp_path / "success-status", exchange_case, exchange_model()
    )
    assert result["status"] == "complete"


def test_textual_monetary_consent_gets_structured_correction_without_hidden_answer(
    tmp_path, exchange_case
):
    scripted = list(exchange_model().replies)
    model = Replies(
        *scripted[:-1],
        Message(
            role="assistant",
            content="Подтвердите обмен чёрных наушников на белые с доплатой 2.35 USD на исходную карту.",
        ),
        scripted[-1],
    )
    result = runner.run_case(tmp_path / "structured-correction", exchange_case, model)
    state = json.loads(
        (tmp_path / "structured-correction/state.json").read_text(encoding="utf-8")
    )
    assert result["status"] == "complete"
    assert result["metrics"]["counts"]["staff_rejections"] == 1
    assert len(state["tables"]["deliveries"]) == 2
    employee = [
        event["text"] for event in state["events"] if event["kind"] == "employee"
    ]
    assert employee == [
        "Для согласования операции подготовьте предложение доступным инструментом; согласие клиента регистрирует приложение отдельно. Не заменяйте подготовку операции текстовым запросом подтверждения."
    ]
    last_request = str(model.requests[-1])
    assert employee[0] in last_request
    assert exchange_case["hidden_test_marker"] not in last_request
    assert "new_item_ids" not in employee[0] and "2.35" not in employee[0]
    saved = json.loads(
        (tmp_path / "structured-correction/result.json").read_text(encoding="utf-8")
    )
    assert saved["outcome"] == "completed_with_help"
    assert saved["structured_action_hint_used"] is True


def test_corrected_payment_is_help_without_a_structured_action_hint(
    tmp_path, exchange_case
):
    scripted = list(exchange_model().replies)
    wrong_payment = deepcopy(scripted[-1])
    wrong_payment.tool_calls[0].arguments["payment_method_id"] = "other_card"
    model = Replies(*scripted[:-1], wrong_payment, scripted[-1])

    runner.run_case(tmp_path / "payment-correction", exchange_case, model)
    saved = json.loads(
        (tmp_path / "payment-correction/result.json").read_text(encoding="utf-8")
    )
    assert saved["status"] == "complete"
    assert saved["metrics"]["counts"]["staff_rejections"] == 1
    assert saved["outcome"] == "completed_with_help"
    assert saved["structured_action_hint_used"] is False
