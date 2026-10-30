from decimal import Decimal

import pytest
from pydantic import ValidationError

from support_agent.proposals import Action, Evidence, Proposal


def action():
    return Action(
        name="exchange_delivered_order_items",
        arguments={"order_id": "A"},
        amount="16.85",
        conditions="snapshot-a",
        result_status="exchange requested",
        description="Заявка на обмен",
        evidence=[Evidence(title="Заказ", source="orders/A", detail="delivered")],
    )


def test_approval_digest_covers_text_version_arguments_and_amount():
    original = Proposal(
        id="p",
        case_id="c",
        version=1,
        kind="action",
        text="Заявка создана.",
        action=action(),
    )
    variations = [
        original.model_copy(update={"text": "Другой текст"}),
        original.model_copy(update={"version": 2}),
        original.model_copy(
            update={"action": action().model_copy(update={"amount": Decimal("20.00")})}
        ),
        original.model_copy(
            update={
                "action": action().model_copy(update={"arguments": {"order_id": "B"}})
            }
        ),
    ]
    assert all(p.digest() != original.digest() for p in variations)
    assert (
        Proposal.model_validate_json(original.model_dump_json()).digest()
        == original.digest()
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "action"},
        {"kind": "message", "action": action()},
        {"kind": "action", "action": action(), "consent_request": action()},
        {"kind": "message", "text": " "},
    ],
)
def test_incomplete_or_ambiguous_proposal_is_rejected(fields):
    with pytest.raises(ValidationError):
        Proposal(**{"id": "p", "case_id": "c", "version": 1, "text": "Текст", **fields})


def test_client_consent_covers_conditions_but_not_employee_message_text():
    a = action()
    assert (
        a.fingerprint()
        != a.model_copy(update={"conditions": "new-price"}).fingerprint()
    )
    assert (
        a.fingerprint()
        != a.model_copy(update={"arguments": {"order_id": "B"}}).fingerprint()
    )
    question = Proposal(
        id="q",
        case_id="c",
        version=1,
        kind="message",
        text="Подтверждаете?",
        consent_request=a,
    )
    assert question.consent_request.fingerprint() == a.fingerprint()
