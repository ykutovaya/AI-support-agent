"""Внешнее назначение принадлежит конфигурации и согласуется вместе с содержанием."""

from copy import deepcopy

import pytest
from test_ticket_planning import POLICY, plan, requested

from support_agent.adapters.ticket_retail import TicketRetailAdapter

DESTINATION = {
    "base_url": "https://support.example.test",
    "group_id": 7,
    "group_name": "Проверка",
}
CUSTOMERS = {"u1": 31}


def remote(snapshot, *, destination=None, customers=None, target="zammad"):
    return TicketRetailAdapter(
        snapshot,
        POLICY,
        target=target,
        destination=DESTINATION if destination is None else destination,
        customer_mapping=CUSTOMERS if customers is None else customers,
    )


def test_destination_and_customer_are_bound_to_approval_without_extra_model_parameters(
    snapshot,
):
    adapter = remote(requested(snapshot))
    action = plan(adapter)
    expected = {**DESTINATION, "customer_id": 31}
    assert action.arguments["target"] == "zammad"
    assert action.arguments["destination"] == expected
    assert action.condition_values["destination"] == expected
    assert adapter.check(action, "u1").status == "unchanged"
    assert (
        adapter.plan(action.name, action.arguments, "u1").fingerprint()
        == action.fingerprint()
    )
    schema = next(
        s.parameters for s in adapter.specs() if s.name == "propose_support_ticket"
    )
    assert set(schema["properties"]) == {"order_id", "question"}
    for field, value in (
        ("destination", expected),
        ("target", "local"),
        ("customer_id", 99),
    ):
        with pytest.raises(ValueError):
            plan(adapter, **{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("base_url", "https://other.example.test"),
        ("group_id", 9),
        ("group_name", "Другая"),
        ("customer_id", 32),
        ("target", "local"),
    ],
)
def test_changed_remote_destination_requires_new_approval(snapshot, field, value):
    action = plan(remote(requested(snapshot)))
    destination, customers, target = deepcopy(DESTINATION), dict(CUSTOMERS), "zammad"
    if field == "customer_id":
        customers["u1"] = value
    elif field == "target":
        target = value
    else:
        destination[field] = value
    check = remote(
        snapshot, destination=destination, customers=customers, target=target
    ).check(action, "u1")
    assert check.status == "changed"
    assert check.current.fingerprint() != action.fingerprint()
    assert any(
        c.field == ("target" if field == "target" else f"destination.{field}")
        for c in check.changes
    )


def test_missing_mapping_does_not_prepare_external_ticket(snapshot):
    adapter = remote(requested(snapshot), customers={})
    with pytest.raises(ValueError, match="соответствие"):
        plan(adapter)
    old = plan(remote(snapshot))
    assert adapter.check(old, "u1").status == "forbidden"


@pytest.mark.parametrize(
    "field,value",
    [("customer_id", 99), ("group_id", 99), ("base_url", "https://other.example.test")],
)
def test_tampered_saved_destination_is_rejected_before_current_change(
    snapshot, field, value
):
    action = plan(remote(requested(snapshot)))
    action.arguments["destination"][field] = value
    snapshot["orders"]["#O1"]["status"] = "return requested"
    check = remote(snapshot).check(action, "u1")
    assert check.status == "forbidden"
    assert check.current is None and check.changes == []


def test_default_local_action_keeps_legacy_fields_and_fingerprint(snapshot):
    action = plan(TicketRetailAdapter(requested(snapshot), POLICY))
    assert "destination" not in action.arguments
    assert "destination" not in action.condition_values
    assert action.arguments["target"] == "local"
    assert (
        action.fingerprint()
        == "94edf61a651d0d8f09f8c4db40424fac651f6803f4eadad3817062fbefda09de"
    )
