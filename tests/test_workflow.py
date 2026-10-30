import pytest

pytest.importorskip("tau2")
from test_core import FakeModel
from test_retail import exchange_args

from support_agent.adapters.retail import RetailAdapter
from support_agent.models import Message, ToolCall
from support_agent.storage import Store
from support_agent.workflow import SupportService


def call(name, args, identifier):
    return Message(
        role="assistant",
        tool_calls=[ToolCall(id=identifier, name=name, arguments=args)],
    )


def model_for_exchange():
    return FakeModel(
        call("find_user_id_by_email", {"email": "user1@example.test"}, "identity"),
        call("get_order_details", {"order_id": "#O1"}, "order"),
        call("propose_exchange", exchange_args(), "proposal"),
    )


def service(tmp_path, snapshot, model):
    store = Store(tmp_path / "app.sqlite3")
    store.initialize(snapshot, "Правила магазина")
    return SupportService(store, model, RetailAdapter)


def confirm(app, p):
    return app.approve(p.case_id, p.id, p.version, p.digest(), "employee")


def test_full_exchange_flow_needs_question_delivery_then_client_and_employee(
    tmp_path, snapshot
):
    app = service(tmp_path, snapshot, model_for_exchange())
    case = app.create_case("u1", "employee")
    question = app.investigate(
        case, "Обменять на белые. Email user1@example.test", "employee"
    )
    assert question.kind == "message" and str(question.consent_request.amount) == "2.35"
    assert "2.35" in question.text and "card" in question.text
    assert app.store.snapshot()[0]["orders"]["#O1"]["status"] == "delivered"
    with pytest.raises(ValueError):
        app.record_client_response(case, question.id, True, "Да", "employee")
    confirm(app, question)
    proposal = app.record_client_response(
        case, question.id, True, "Подтверждаю", "employee"
    )
    assert proposal.kind == "action"
    assert app.store.snapshot()[0]["orders"]["#O1"]["status"] == "delivered"
    assert confirm(app, proposal)["status"] == "delivered"
    assert app.store.snapshot()[0]["orders"]["#O1"]["status"] == "exchange requested"
    assert "Зарегистрирована заявка" in proposal.text
    assert "деньги не списывались" in proposal.text
    assert all(m.role != "system" for m in app.store.case(case)["state"].messages)


def test_message_only_draft_and_rejection_stay_unsent_until_approved(
    tmp_path, snapshot
):
    model = FakeModel(
        Message(role="assistant", content='{"message":"Уточните товар."}'),
        Message(role="assistant", content="Уточните номер заказа."),
    )
    app = service(tmp_path, snapshot, model)
    case = app.create_case("u1", "employee")
    first = app.investigate(case, "Помогите", "employee")
    assert first.text == "Уточните товар."
    second = app.reject(
        case,
        first.id,
        first.version,
        first.digest(),
        "Сначала спросить заказ",
        "employee",
    )
    assert second.version > first.version and second.text == "Уточните номер заказа."
    assert app.view(case, "employee")["deliveries"] == []
    with pytest.raises(ValueError):
        confirm(app, first)
    confirm(app, second)
    assert [d["text"] for d in app.view(case, "employee")["deliveries"]] == [
        second.text
    ]


@pytest.mark.parametrize("accepted", [False, True])
def test_consent_is_single_use_and_refusal_cannot_write(tmp_path, snapshot, accepted):
    app = service(tmp_path, snapshot, model_for_exchange())
    case = app.create_case("u1", "employee")
    q = app.investigate(case, "Обмен", "employee")
    confirm(app, q)
    p = app.record_client_response(
        case, q.id, accepted, "Да" if accepted else "Нет", "employee"
    )
    with pytest.raises(ValueError):
        app.record_client_response(case, q.id, True, "Повтор", "employee")
    if not accepted:
        assert p.kind == "message"
        confirm(app, p)
        assert app.store.snapshot()[0]["orders"]["#O1"]["status"] == "delivered"


def test_new_customer_intent_invalidates_waiting_action(tmp_path, snapshot):
    model = model_for_exchange()
    app = service(tmp_path, snapshot, model)
    case = app.create_case("u1", "employee")
    q = app.investigate(case, "Обмен", "employee")
    confirm(app, q)
    p = app.record_client_response(case, q.id, True, "Да", "employee")
    app.model = FakeModel(Message(role="assistant", content="Отмена обмена учтена."))
    app.investigate(case, "Не надо обменивать", "employee")
    with pytest.raises(ValueError):
        confirm(app, p)
    assert app.store.snapshot()[0]["orders"]["#O1"]["status"] == "delivered"


def test_case_context_and_employee_access_are_isolated(tmp_path, snapshot):
    model = FakeModel(
        Message(role="assistant", content="Уточните сведения."),
        Message(role="assistant", content="Здравствуйте."),
    )
    app = service(tmp_path, snapshot, model)
    first = app.create_case("u1", "employee")
    second = app.create_case("u2", "other")
    app.investigate(first, "PRIVATE_FIRST", "employee")
    with pytest.raises(ValueError):
        app.view(second, "employee")
    app.investigate(second, "Другой клиент", "other")
    assert "PRIVATE_FIRST" not in str(model.requests[-1])


def test_read_before_identity_returns_error_without_customer_data(tmp_path, snapshot):
    model = FakeModel(
        call("get_order_details", {"order_id": "#O1"}, "bad"),
        Message(role="assistant", content="Уточните email."),
    )
    app = service(tmp_path, snapshot, model)
    case = app.create_case("u1", "employee")
    app.investigate(case, "Заказ", "employee")
    result = model.requests[-1][0][-1]
    assert result.error and "email" in result.content
    assert "payment_history" not in str(model.requests)
