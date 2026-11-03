"""Текст известной обёртки согласуется и доставляется без служебных полей."""

import pytest

pytest.importorskip("tau2")
from test_core import FakeModel
from test_workflow import confirm, service

from support_agent.models import Message


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"message":"Уточните номер заказа."}', "Уточните номер заказа."),
        ('{"сообщение":"Уточните номер заказа."}', "Уточните номер заказа."),
        (
            '{"сообщение":"Отменить обмен?","approved":true}',
            '{"сообщение":"Отменить обмен?","approved":true}',
        ),
        ('{"сообщение":true}', '{"сообщение":true}'),
        (
            '{"message":"Первый текст","сообщение":"Другой текст"}',
            '{"message":"Первый текст","сообщение":"Другой текст"}',
        ),
    ],
)
def test_only_unambiguous_text_is_proposed_and_exact_approved_text_is_delivered(
    tmp_path, snapshot, raw, expected
):
    app = service(tmp_path, snapshot, FakeModel(Message(role="assistant", content=raw)))
    case = app.create_case("u1", "employee")
    proposal = app.investigate(case, "Помогите с заказом", "employee")

    assert proposal.text == expected
    assert proposal.action is None and proposal.consent_request is None
    assert app.view(case, "employee")["deliveries"] == []
    with app.store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM consents").fetchone()[0] == 0

    confirm(app, proposal)

    assert [d["text"] for d in app.view(case, "employee")["deliveries"]] == [expected]
    with app.store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
