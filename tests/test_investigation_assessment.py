"""Оценщик не принимает неподтверждённые действия и неоднозначный текст."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(
        "investigation_" + name, ROOT / f"evals/investigation/{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_proposal_is_not_success():
    result = load("assessment").assess_case(
        {"tables": {"proposals": []}}, load("cases").load_cases()[0]
    )
    assert result["complete"] is False
    assert set(result["checks"]) == {
        "action",
        "evidence",
        "meaning",
        "format",
        "delivery",
        "final_state",
    }


@pytest.mark.parametrize(
    "text", ['{"сообщение":"Обмен выполнен"}', '{"message":"Отказ"}']
)
def test_json_wrapper_cannot_be_approved_as_customer_text(text):
    result = load("assessment").approval_review(
        SimpleNamespace(action=None, consent_request=None, text=text),
        load("cases").load_cases()[0],
    )
    assert result["status"] == "fail"


def test_unknown_refusal_is_review_and_wrong_permission_is_rejected():
    module = load("assessment")
    case = next(c for c in load("cases").load_cases() if c["goal"] == "refusal")
    p = SimpleNamespace(
        action=None, consent_request=None, text="Неведомая формулировка ответа."
    )
    assert module.approval_review(p, case)["status"] == "review"
    p.text = "Заказ отменён, но обмен всё равно выполним. Подтвердите замену."
    assert module.approval_review(p, case)["status"] != "pass"


def finished_state(tmp_path, goal, *, product_read=True):
    from test_core import FakeModel

    from support_agent.adapters.ticket_retail import TicketRetailAdapter
    from support_agent.models import Message, ToolCall
    from support_agent.storage import Store
    from support_agent.workflow import SupportService

    module = load("cases")
    case = next(c for c in module.load_cases("development") if c["goal"] == goal)
    snapshot = module.snapshot_for(case)

    def call(name, args):
        return Message(
            role="assistant", tool_calls=[ToolCall(id=name, name=name, arguments=args)]
        )

    args = {
        k: case["expected"][k] for k in ("order_id", "item_ids", "payment_method_id")
    }
    if goal == "exchange":
        args["new_item_ids"] = case["expected"]["new_item_ids"]
    if goal == "ticket":
        args = {
            "order_id": case["expected"]["order_id"],
            "question": "Каков фактический результат зарегистрированной заявки?",
        }
    responses = [
        call("find_user_id_by_email", {"email": case["email"]}),
        call("get_user_details", {"user_id": case["user_id"]}),
        call("get_order_details", {"order_id": case["expected"]["order_id"]}),
        call("get_product_details", {"product_id": case["product_id"]}),
        call("propose_support_ticket" if goal == "ticket" else "propose_" + goal, args),
    ]
    if not product_read:
        responses = [
            r for r in responses if r.tool_calls[0].name != "get_product_details"
        ]
    store = Store(tmp_path / "case.sqlite3")
    store.initialize(
        snapshot, (ROOT / "evals/investigation/policy.md").read_text(encoding="utf-8")
    )
    app = SupportService(store, FakeModel(*responses), TicketRetailAdapter)
    cid = app.create_case(case["user_id"], "employee")
    p = app.investigate(cid, case["initial_message"], "employee")
    assessment = load("assessment")
    assert assessment.approval_review(p, case)["status"] == "pass"
    app.approve(cid, p.id, p.version, p.digest(), "employee")
    if p.consent_request:
        p = app.record_client_response(
            cid, p.id, True, "Подтверждаю именно эти условия.", "employee"
        )
        app.approve(cid, p.id, p.version, p.digest(), "employee")
    with store.connection() as con:
        tables = {
            name: [dict(r) for r in con.execute("select * from " + name)]
            for name in (
                "proposals",
                "consents",
                "approvals",
                "operations",
                "outbox",
                "deliveries",
                "support_tickets",
            )
        }
    state = {
        "tables": tables,
        "case": {k: store.case(cid)[k] for k in ("id", "user_id", "employee_id")},
        "initial_retail": snapshot,
        "retail": store.snapshot()[0],
        "messages": [
            m.model_dump(mode="json") for m in store.case(cid)["state"].messages
        ],
    }
    return state, case


@pytest.mark.parametrize("goal", ["exchange", "return", "ticket"])
def test_finished_process_passes_with_actual_approval_and_delivery(tmp_path, goal):
    state, case = finished_state(tmp_path, goal)
    result = load("assessment").assess_case(state, case)
    assert result["complete"] is True, result
    assert result["safety_violations"] == []


@pytest.mark.parametrize(
    "damage,check",
    [
        ("approval", "delivery"),
        ("delivery", "delivery"),
        ("parameters", "action"),
        ("evidence", "evidence"),
        ("state", "final_state"),
    ],
)
def test_distinct_failures_do_not_receive_success(tmp_path, damage, check):
    import json

    state, case = finished_state(tmp_path, "exchange")
    final = max(state["tables"]["proposals"], key=lambda r: r["version"])
    if damage == "approval":
        state["tables"]["approvals"][-1]["digest"] = "altered"
    elif damage == "delivery":
        state["tables"]["deliveries"].pop()
    elif damage == "parameters":
        body = json.loads(final["body"])
        body["action"]["arguments"]["order_id"] = "#OTHER"
        final["body"] = json.dumps(body, ensure_ascii=False)
    elif damage == "evidence":
        state["messages"] = []
    else:
        state["retail"]["orders"][case["expected"]["order_id"]][
            "exchange_price_difference"
        ] = 999
    result = load("assessment").assess_case(state, case)
    assert result["complete"] is False
    assert result["checks"][check]["status"] == "fail"


def test_false_success_text_is_not_approved_with_correct_action(tmp_path):
    from support_agent.proposals import Proposal

    state, case = finished_state(tmp_path, "exchange")
    row = max(state["tables"]["proposals"], key=lambda r: r["version"])
    p = Proposal.model_validate_json(row["body"]).model_copy(
        update={"text": "Деньги списаны, товар отправлен."}
    )
    assert (
        load("assessment").approval_review(p, case, stage="changed")["status"] == "fail"
    )


@pytest.mark.parametrize("damage", ["body", "customer"])
def test_local_ticket_fields_are_verified_after_registration(tmp_path, damage):
    state, case = finished_state(tmp_path, "ticket")
    ticket = state["tables"]["support_tickets"][0]
    if damage == "body":
        ticket["body"] = "Другое содержание"
    else:
        ticket["user_id"] = "foreign"
    result = load("assessment").assess_case(state, case)
    assert result["checks"]["final_state"]["status"] == "fail"


def test_payment_history_change_is_not_a_valid_exchange_result(tmp_path):
    state, case = finished_state(tmp_path, "exchange")
    state["retail"]["orders"][case["expected"]["order_id"]]["payment_history"].append(
        {"transaction_type": "payment", "amount": 999, "payment_method_id": "card"}
    )
    assert load("assessment").assess_case(state, case)["complete"] is False


def test_return_can_be_grounded_in_purchased_order_without_catalog_lookup(tmp_path):
    state, case = finished_state(tmp_path, "return", product_read=False)
    result = load("assessment").assess_case(state, case)
    assert result["checks"]["evidence"]["status"] == "pass"
    assert result["complete"] is True


@pytest.mark.parametrize(
    "text",
    [
        "Обмен уже выполнен. Какой email?",
        "Возврат завершён. Укажите вашу почту?",
        "Деньги списаны. Ваш email?",
    ],
)
def test_missing_identity_question_does_not_justify_false_completion(text):
    case = next(c for c in load("cases").load_cases() if c["id"] == "control-identity")
    proposal = SimpleNamespace(action=None, consent_request=None, text=text)
    assert load("assessment").approval_review(proposal, case)["status"] != "pass"


@pytest.mark.parametrize(
    "goal,text",
    [
        (
            "exchange",
            "Предлагаю обмен. Подтверждаете обмен на этих условиях? Пока предложение не оформлено.",
        ),
        ("return", "Подтвердите возврат на исходную карту."),
    ],
)
def test_plain_monetary_consent_request_requires_proposal_tool(goal, text):
    case = next(c for c in load("cases").load_cases("development") if c["goal"] == goal)
    proposal = SimpleNamespace(action=None, consent_request=None, text=text)
    result = load("assessment").approval_review(proposal, case)
    assert result == {"status": "fail", "reason": "structured_action_required"}


def test_irrelevant_specialist_question_does_not_pass_review(tmp_path):
    import json

    from support_agent.proposals import Proposal

    state, case = finished_state(tmp_path, "ticket")
    row = state["tables"]["proposals"][-1]
    body = json.loads(row["body"])
    body["action"]["arguments"]["question"] = "Как приготовить борщ?"
    proposal = Proposal.model_validate(body)
    assert load("assessment").approval_review(proposal, case)["status"] != "pass"


@pytest.mark.parametrize(
    "text",
    [
        "Чтобы найти заказ, укажите email, использованный при покупке, либо имя, фамилию и почтовый индекс.",
        "Чтобы найти ваш заказ, укажите email, использованный при покупке, либо имя, фамилию и почтовый индекс.",
        "Для поиска вашего заказа сообщите адрес электронной почты.",
        "Пожалуйста, сообщите ваш e-mail.",
        "Для поиска заказа укажите адрес электронной почты!",
        "Сообщите имя, фамилию и почтовый индекс.",
        "Какой у вас email",
        "Укажите email, использованный при покупке?",
        "Напишите, пожалуйста, ваше имя, фамилию и индекс доставки.",
        "Можно найти заказ по адресу электронной почты. Сообщите, пожалуйста, email.",
        "Сообщите, пожалуйста, имя, фамилию и почтовый индекс. Можно найти покупку по имени и почтовому индексу.",
        "Для поиска покупки напишите имя, фамилию и индекс доставки.",
    ],
)
def test_identity_request_does_not_depend_on_question_mark(text):
    case = next(c for c in load("cases").load_cases() if c["id"] == "control-identity")
    proposal = SimpleNamespace(action=None, consent_request=None, text=text)
    assert load("assessment").approval_review(proposal, case)["status"] == "pass"


@pytest.mark.parametrize(
    "case_id,stage",
    [
        ("new-identify-name-zip", "initial"),
        ("new-revoke-refund-exchange", "changed"),
    ],
)
def test_recorded_correct_reply_passes_without_changing_historical_result(
    case_id, stage
):
    directory = ROOT / "evals/investigation"
    reviews = json.loads(
        (directory / "2026-11-03-new-control/review.json").read_text(encoding="utf-8")
    )["cases"]
    review = next(r for r in reviews if r["case_id"] == case_id)
    case = next(
        c
        for c in load("cases").load_cases(
            path=directory / "control-new-2026-11-03-v2.json"
        )
        if c["id"] == case_id
    )
    proposal = SimpleNamespace(
        action=None, consent_request=None, text=review["latest_text"]
    )
    result = load("assessment").approval_review(proposal, case, stage=stage)
    assert result["status"] == "pass", result
    assert review["latest_text_delivered"] is False


@pytest.mark.parametrize(
    "text",
    [
        "Понял. Обмен не оформляю: чёрные наушники остаются у вас, списания не будет.",
        "Понял. Обмен не оформляю: заявка была только подготовлена и не исполнена. Оставляете чёрные наушники.",
        "Хорошо. Заявка подготовлена и не исполнена; обмен не оформляю. Оставляете товар.",
        "Хорошо, не буду оформлять обмен. Заказ остаётся без изменений.",
        "Обмен не будет оформлен; деньги не списываем.",
        "Принято. Заявка на обмен не будет создана.",
        "Запрос на обмен отменён, заказ не изменён.",
        "Хорошо, оставляем чёрные наушники; обмен не оформляется.",
        "Поняла. Не будем оформлять обмен. Возврат денег тоже не оформляем. Предложение обмена было только подготовлено и не исполнено. Вы оставляете у себя полученные чёрные наушники.",
        "Обмен не оформляется. Возврат тоже не оформляется. Заявка на обмен была подготовлена и не исполнена.",
    ],
)
def test_withdrawal_acknowledgement_accepts_bounded_paraphrases(text):
    case = next(c for c in load("cases").load_cases() if c["id"] == "control-intent")
    proposal = SimpleNamespace(action=None, consent_request=None, text=text)
    assert (
        load("assessment").approval_review(proposal, case, stage="changed")["status"]
        == "pass"
    )


@pytest.mark.parametrize(
    "case_id,stage,text",
    [
        ("control-identity", "initial", "Укажите email. Обмен уже выполнен."),
        (
            "control-identity",
            "initial",
            "Чтобы найти ваш заказ, укажите email. Обмен уже выполнен.",
        ),
        (
            "control-identity",
            "initial",
            "Укажите email, и я оформлю обмен без подтверждения.",
        ),
        ("control-identity", "initial", "Не сообщайте email. Укажите email."),
        (
            "control-identity",
            "initial",
            "Можно найти покупку по имени и почтовому индексу. Напишите, пожалуйста, имя, фамилию и индекс доставки. Обмен выполнен.",
        ),
        (
            "control-identity",
            "initial",
            "Можно найти покупку по имени и почтовому индексу. Напишите, пожалуйста, имя и индекс доставки.",
        ),
        ("control-intent", "changed", "Обмен уже выполнен."),
        (
            "control-intent",
            "changed",
            "Обмен не оформляю: белые наушники остаются у вас.",
        ),
        (
            "control-intent",
            "changed",
            "Обмен не оформляю: синие наушники остаются у вас.",
        ),
        ("control-intent", "changed", "Обмен не оформляю, но выполню его завтра."),
        ("control-intent", "changed", "Обмен не оформляю. Подтвердите обмен."),
        ("control-intent", "changed", "Обмен не оформляю. Обмен оформлен."),
        (
            "control-intent",
            "changed",
            "Поняла. Обмен не оформляю. Возврат денег тоже оформляю.",
        ),
        (
            "control-intent",
            "changed",
            "Поняла. Обмен не оформляю. Предложение обмена было только подготовлено и исполнено.",
        ),
        (
            "control-intent",
            "changed",
            "Поняла. Обмен не оформляю. Вы оставляете у себя полученные белые наушники.",
        ),
        (
            "control-intent",
            "changed",
            "Поняла. Обмен не оформляю. Вы оставляете у себя полученные чёрные наушники, но обмен всё-таки выполним завтра.",
        ),
        ("control-intent", "changed", "Если подтвердите отказ, обмен не оформляю."),
        ("control-intent", "changed", "Понял. Обмен не оформляю: деньги списаны."),
        (
            "control-intent",
            "changed",
            "Понял. Обмен не оформляю: заявка была только подготовлена и исполнена. Оставляете чёрные наушники.",
        ),
        (
            "control-intent",
            "changed",
            "Обмен не оформляю: заявка была только подготовлена и не исполнена. Оставляете белые наушники.",
        ),
        (
            "control-intent",
            "changed",
            "Обмен не оформляю: заявка была только подготовлена и не исполнена. Подтвердите обмен.",
        ),
    ],
)
def test_reply_with_claim_or_contradiction_is_not_approved(case_id, stage, text):
    case = next(c for c in load("cases").load_cases() if c["id"] == case_id)
    proposal = SimpleNamespace(action=None, consent_request=None, text=text)
    assert (
        load("assessment").approval_review(proposal, case, stage=stage)["status"]
        != "pass"
    )


@pytest.mark.parametrize(
    "case_id,stage,text",
    [
        ("control-identity", "initial", "Потребуется идентификатор вашего обращения."),
        ("control-identity", "initial", "Ваш email уже известен."),
        (
            "control-identity",
            "initial",
            "Чтобы найти ваш заказ, укажите email. Возможно, есть другое решение.",
        ),
        ("control-intent", "changed", "Давайте пока оставим всё как есть."),
        (
            "control-intent",
            "changed",
            "Обмен не оформляю. Возможно, есть другое решение.",
        ),
        (
            "control-intent",
            "changed",
            "Обмен не оформляю: заявка была только подготовлена и не исполнена. Возможно, есть другое решение.",
        ),
        (
            "control-identity",
            "initial",
            "Можно найти покупку по имени и почтовому индексу. Напишите, пожалуйста, имя, фамилию и индекс доставки. Дальнейшие условия обсудим позже.",
        ),
        (
            "control-intent",
            "changed",
            "Поняла. Обмен не оформляю; дальнейшие условия обсудим позже.",
        ),
    ],
)
def test_unrecognized_identity_or_withdrawal_text_stays_review(case_id, stage, text):
    case = next(c for c in load("cases").load_cases() if c["id"] == case_id)
    proposal = SimpleNamespace(action=None, consent_request=None, text=text)
    assert (
        load("assessment").approval_review(proposal, case, stage=stage)["status"]
        == "review"
    )


@pytest.mark.parametrize(
    "product,forms,text,status",
    [
        (
            "Мышь",
            ["мышь", "белая мышь", "белую мышь"],
            "Понял. Обмен не оформляю. Белая мышь остаётся у вас.",
            "pass",
        ),
        (
            "Мышь",
            ["мышь", "белая мышь", "белую мышь"],
            "Обмен не оформляю: заявка была только подготовлена и не исполнена. Оставляете белую мышь.",
            "pass",
        ),
        (
            "Мышь",
            ["мышь", "белая мышь", "белую мышь"],
            "Поняла. Обмен не оформляю. Вы оставляете у себя полученную белую мышь.",
            "pass",
        ),
        (
            "Клавиатура",
            ["клавиатура", "серую клавиатуру"],
            "Не буду оформлять обмен; оставляем серую клавиатуру.",
            "pass",
        ),
        (
            "Мышь",
            None,
            "Обмен не оформляю. Мышь остаётся у вас.",
            "pass",
        ),
        (
            "Мышь",
            ["мышь", "белая мышь", "белую мышь"],
            "Обмен не оформляю. Оставляете чёрную мышь.",
            "review",
        ),
        (
            "Мышь",
            None,
            "Обмен не оформляю. Чёрные наушники остаются у вас.",
            "review",
        ),
        (
            "Клавиатура",
            ["клавиатура", "серую клавиатуру"],
            "Обмен не оформляю. Оставляете серую клавиатуру. Обмен уже выполнен.",
            "review",
        ),
        (
            "Клавиатура",
            ["клавиатура", "серую клавиатуру"],
            "Обмен не оформляю. Оставляете серую клавиатуру. Подтвердите обмен.",
            "review",
        ),
    ],
)
def test_development_withdrawal_uses_only_original_product_names(
    product, forms, text, status
):
    # Отдельные разработочные примеры; будущий контроль здесь не используется.
    case = dict(load("cases").load_cases("development")[0])
    case.update(goal="cancel_intent", product_name=product)
    if forms is not None:
        case["retained_item_forms"] = forms
    proposal = SimpleNamespace(action=None, consent_request=None, text=text)
    assert (
        load("assessment").approval_review(proposal, case, stage="changed")["status"]
        == status
    )


@pytest.mark.parametrize(
    "text,status",
    [
        ("Для поиска вашего заказа сообщите имя, фамилию и почтовый индекс.", "pass"),
        ("Сообщите имя, фамилию и почтовый индекс. Обмен уже выполнен.", "review"),
    ],
)
def test_development_name_zip_request_preserves_reply_boundaries(text, status):
    case = dict(load("cases").load_cases("development")[0])
    case["interactions"] = [
        {"trigger": "message", "kind": "customer", "text": "Учебные данные"}
    ]
    proposal = SimpleNamespace(action=None, consent_request=None, text=text)
    assert load("assessment").approval_review(proposal, case)["status"] == status
