"""Счётчики вмешательств объясняются записями, а не успешностью попытки."""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

from support_agent.proposals import Proposal


def module():
    path = Path(__file__).parents[1] / "evals/reapproval/metrics.py"
    spec = importlib.util.spec_from_file_location("intervention_metrics", path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def proposal(version, status="pending", *, case_id="case"):
    value = Proposal(
        id=f"{case_id}-{version}",
        case_id=case_id,
        version=version,
        kind="message",
        text=f"Сообщение {version}",
    )
    return {
        "id": value.id,
        "case_id": case_id,
        "version": version,
        "body": value.model_dump_json(),
        "status": status,
        "reason": "",
    }


def approval(row):
    return {
        "proposal_id": row["id"],
        "employee_id": "employee",
        "digest": Proposal.model_validate_json(row["body"]).digest(),
    }


def state(rows, approvals=(), *, consents=None):
    result = {
        "case": {"id": "case", "employee_id": "employee"},
        "tables": {"proposals": rows, "approvals": list(approvals)},
    }
    if consents is not None:
        result["tables"]["consents"] = consents
    return result


def event(identifier, kind, text="", case_id="case"):
    return {"id": identifier, "case_id": case_id, "kind": kind, "text": text}


def consent(identifier, *, accepted=1, revoked=1, case_id="case"):
    return {
        "id": identifier,
        "case_id": case_id,
        "accepted": accepted,
        "revoked": revoked,
    }


def annotation(event_id, reason, revoked_ids=(), *, case_id="case"):
    return {
        "case_id": case_id,
        "event_id": event_id,
        "reason": reason,
        "revoked_consent_ids": list(revoked_ids),
    }


def test_checkpoint_excludes_preparation_but_includes_pending_offer_and_failures():
    metrics = module()
    old = proposal(1, "delivered")
    pending = proposal(2)
    baseline = state([old, pending], [approval(old)])
    stale = {**pending, "status": "stale", "reason": "Условия изменились"}
    accepted, unreviewed = proposal(3, "delivered"), proposal(4)
    foreign = proposal(1, "delivered", case_id="another")
    final = state(
        [old, stale, accepted, unreviewed, foreign],
        [approval(old), approval(accepted), approval(foreign)],
    )
    result = metrics.evaluate_interventions(
        final, [event(12, "conditions_changed")], baseline_state=baseline, correct=False
    )
    assert result["scope"] == "since_checkpoint"
    assert result["proposals"]["offered_ids"] == ["case-2", "case-3", "case-4"]
    assert result["proposals"]["accepted_ids"] == ["case-3"]
    assert result["accepted_without_edits"] == {
        "numerator": 1,
        "denominator": 3,
        "value": 1 / 3,
    }
    assert result["counts"]["mandatory_staff_approvals"] == 1
    assert result["counts"]["condition_changes"] == 1
    assert result["event_ids"]["condition_changes"] == [12]
    assert result["outcome"]["correct"] is False


def test_rejection_and_its_employee_message_are_one_command():
    metrics = module()
    events = [
        event(1, "rejected", "Уточнить сумму"),
        event(2, "employee", "Уточнить сумму"),
        event(3, "employee", "Проверить источник"),
        event(4, "manual_resume"),
        event(5, "automatic_resume"),
        event(6, "consent"),
        event(7, "consent_refused"),
        event(8, "conditions_changed"),
        event(9, "delivery_retry"),
        event(10, "manual_resume", case_id="another"),
    ]
    result = metrics.evaluate_interventions(state([proposal(1, "rejected")]), events)
    assert result["counts"]["staff_rejections"] == 1
    assert result["counts"]["employee_followup_commands"] == 1
    assert result["counts"]["manual_resumes"] == 1
    assert result["counts"]["delivery_retries"] == 1
    assert result["counts"]["additional_staff_commands"] == 4
    assert result["counts"]["automatic_resumes"] == 1
    assert result["counts"]["client_agreements"] == 1
    assert result["counts"]["client_refusals"] == 1
    assert result["event_ids"]["employee_followup_commands"] == [3]
    assert result["event_ids"]["paired_rejection_messages"] == [2]
    assert result["correction_reasons"] == "not_classified"


@pytest.mark.parametrize("mutate", ["text", "employee", "digest"])
def test_approval_for_other_contents_or_employee_is_not_acceptance(mutate):
    metrics = module()
    row = proposal(1, "delivered")
    record = approval(row)
    if mutate == "text":
        body = Proposal.model_validate_json(row["body"]).model_copy(
            update={"text": "Другой ответ"}
        )
        row["body"] = body.model_dump_json()
    else:
        record["employee_id" if mutate == "employee" else "digest"] = "changed"
    result = metrics.evaluate_interventions(state([row], [record]), [])
    assert result["accepted_without_edits"]["numerator"] == 0
    assert result["accepted_without_edits"]["denominator"] == 1
    assert result["proposals"]["invalid_approval_ids"] == [row["id"]]
    assert result["counts"]["mandatory_staff_approvals"] == 1


def test_correctness_denominator_keeps_failed_and_unclassified_attempts():
    metrics = module()
    row = proposal(1, "delivered")
    accepted_but_wrong = metrics.evaluate_interventions(
        state([row], [approval(row)]), [], correct=False, error_category="application"
    )
    correct = metrics.evaluate_interventions(state([proposal(1)]), [], correct=True)
    unknown = metrics.evaluate_interventions(
        state([proposal(1, "rejected")]), [event(1, "error")]
    )
    result = metrics.summarize_interventions([accepted_but_wrong, correct, unknown])
    assert result["attempts"] == 3
    assert result["correct_results"] == {
        "numerator": 1,
        "denominator": 3,
        "value": 1 / 3,
        "assessed": 2,
        "unassessed": 1,
    }
    assert result["accepted_without_edits"] == {
        "numerator": 1,
        "denominator": 3,
        "value": 1 / 3,
        "covered_attempts": 3,
        "unavailable_attempts": 0,
    }
    assert result["error_categories"]["application"] == 1
    assert result["error_categories"]["unclassified"] == 1
    assert result["human_time_seconds"] is None


def test_unknown_error_events_are_not_assigned_to_application_or_provider():
    metrics = module()
    result = metrics.evaluate_interventions(
        state([]),
        [
            event(1, "error", "ValueError"),
            event(2, "recovery_failed", "ValueError"),
            event(3, "ticket_unknown"),
        ],
    )
    assert result["outcome"]["error_category"] == "unclassified"
    assert result["event_ids"]["unclassified_errors"] == [1, 2]
    assert result["event_ids"]["external_unknown"] == [3]
    assert result["accepted_without_edits"]["value"] is None
    assert result["counts"]["additional_staff_commands"] == 0


def test_missing_history_is_unavailable_not_zero_and_foreign_baseline_rejected():
    metrics = module()
    missing = {"case": {"id": "case", "employee_id": "employee"}, "tables": {}}
    result = metrics.evaluate_interventions(missing, [])
    assert result["accepted_without_edits"]["denominator"] is None
    aggregate = metrics.summarize_interventions([result])
    assert aggregate["accepted_without_edits"]["unavailable_attempts"] == 1
    wrong = deepcopy(state([]))
    wrong["case"]["id"] = "another"
    with pytest.raises(ValueError, match="обращени"):
        metrics.evaluate_interventions(state([]), [], baseline_state=wrong)


def test_saved_preparation_boundary_keeps_attempt_offer_and_filters_old_events():
    metrics = module()
    first, pending, refused = (
        proposal(1, "delivered"),
        proposal(2, "stale"),
        proposal(3),
    )
    result = metrics.evaluate_interventions(
        state([first, pending, refused], [approval(first)]),
        [
            event(1, "approval"),
            event(2, "conditions_changed"),
            event(3, "manual_resume"),
        ],
        baseline_proposal_id=pending["id"],
        event_boundary=1,
        correct=False,
        error_category="evaluator",
    )
    assert result["proposals"]["offered_ids"] == ["case-2", "case-3"]
    assert result["accepted_without_edits"] == {
        "numerator": 0,
        "denominator": 2,
        "value": 0.0,
    }
    assert result["counts"]["mandatory_staff_approvals"] == 0
    assert result["event_ids"]["manual_resumes"] == [3]
    assert result["outcome"]["error_category"] == "evaluator"


def test_refusal_sources_and_recovery_do_not_count_one_refusal_twice():
    events = [
        event(1, "consent"),
        event(2, "customer", "Не подтверждаю"),
        event(3, "consent_refused"),
        event(4, "manual_resume"),
        event(5, "application", "Продолжение"),
        event(6, "customer", "Оставлю прежний товар"),
        event(7, "automatic_resume"),
    ]
    annotations = [annotation(2, "refusal", ["a"]), annotation(6, "refusal")]
    result = module().evaluate_interventions(
        state([], consents=[consent("a")]),
        events + [events[2]],
        customer_annotations=annotations + [annotations[0]],
    )
    assert result["version"] == "interventions-v3"
    assert result["scope"] == "full_case"
    assert result["counts"]["client_button_refusals"] == 1
    assert result["counts"]["client_text_refusals"] == 1
    assert result["counts"]["client_refusals"] == 2
    assert result["event_ids"]["client_refusals"] == [3, 6]
    assert result["event_ids"]["client_button_refusals"] == [3]
    assert result["event_ids"]["client_text_refusals"] == [6]
    assert result["counts"]["client_consent_revocations"] == 1
    assert result["counts"]["client_consent_revocations_unknown_reason"] == 0
    assert result["consent_revocations"]["by_reason"]["refusal"] == ["a"]
    assert result["counts"]["additional_staff_commands"] == 1
    assert result["counts"]["mandatory_staff_approvals"] == 0
    assert result["client_response_sources"]["reason_basis"] == "explicit_annotation"


@pytest.mark.parametrize("reason", ["request_change", "unknown", None])
def test_new_message_revokes_consent_without_implying_refusal(reason):
    annotations = [] if reason is None else [annotation(2, reason, ["a"])]
    result = module().evaluate_interventions(
        state([], consents=[consent("a"), consent("refused", accepted=0)]),
        [event(1, "consent"), event(2, "customer", "Не надо белый, хочу синий")],
        customer_annotations=annotations,
    )
    assert result["counts"]["client_refusals"] == 0
    assert result["counts"]["client_consent_revocations"] == int(reason is not None)
    assert result["counts"]["client_consent_revocations_unknown_reason"] == int(
        reason == "unknown"
    )
    assert result["counts"]["consent_invalidations"] == 1
    assert result["counts"]["consent_invalidations_unattributed"] == int(reason is None)
    assert result["consent_revocations"]["consent_ids"] == (
        [] if reason is None else ["a"]
    )
    assert result["consent_revocations"]["by_reason"][reason or "unknown"] == (
        [] if reason is None else ["a"]
    )


def test_explicit_text_refusal_does_not_require_previous_consent():
    result = module().evaluate_interventions(
        state([], consents=[]),
        [event(1, "customer", "Лучше оставлю прежний товар")],
        customer_annotations=[annotation(1, "refusal")],
    )
    assert result["counts"]["client_text_refusals"] == 1
    assert result["counts"]["client_refusals"] == 1
    assert result["counts"]["client_consent_revocations"] == 0


def test_checkpoint_and_case_filter_refusals_and_already_revoked_consents():
    baseline = state([], consents=[consent("old"), consent("new", revoked=0)])
    events = [
        event(1, "customer"),
        event(2, "customer", case_id="another"),
        event(3, "customer"),
        event(4, "automatic_resume"),
    ]
    result = module().evaluate_interventions(
        state(
            [],
            consents=[
                consent("old"),
                consent("new"),
                consent("foreign", case_id="another"),
            ],
        ),
        events,
        baseline_state=baseline,
        event_boundary=1,
        customer_annotations=[
            annotation(1, "refusal", ["old"]),
            annotation(2, "refusal", ["foreign"], case_id="another"),
            annotation(3, "request_change", ["new"]),
        ],
    )
    assert result["scope"] == "since_checkpoint"
    assert result["counts"]["client_refusals"] == 0
    assert result["counts"]["client_consent_revocations"] == 1
    assert result["consent_revocations"]["consent_ids"] == ["new"]
    assert result["consent_revocations"]["coverage"] == "confirmed_customer_events_only"
    assert result["consent_invalidations"]["coverage"] == "baseline_delta"
    assert result["client_response_sources"]["annotations"] == [
        annotation(3, "request_change", ["new"])
    ]


@pytest.mark.parametrize("history", ["missing", "snapshot", "event_boundary"])
def test_old_incomplete_history_does_not_invent_revocation_reason_or_interval(history):
    kwargs = {"event_boundary": 1} if history == "event_boundary" else {}
    result = module().evaluate_interventions(
        state([], consents=None if history == "missing" else [consent("a")]),
        [event(2, "customer", "Отказываюсь от обмена")],
        **kwargs,
    )
    assert result["counts"]["client_refusals"] == 0
    assert result["counts"]["client_consent_revocations"] is None
    assert result["counts"]["client_consent_revocations_unknown_reason"] is None
    assert result["consent_revocations"]["coverage"] == "unavailable"
    assert result["counts"]["consent_invalidations"] == (
        1 if history == "snapshot" else None
    )
    assert result["client_response_sources"]["unclassified_customer_event_ids"] == [2]
    assert (
        result["consent_invalidations"]["coverage"]
        == {
            "missing": "unavailable",
            "snapshot": "complete_snapshot",
            "event_boundary": "event_annotations_only",
        }[history]
    )


@pytest.mark.parametrize(
    "bad_annotation,events,consents",
    [
        (annotation(2, "refusal"), [event(1, "customer")], []),
        (annotation(1, "refusal"), [event(1, "application")], []),
        (annotation(1, "maybe"), [event(1, "customer")], []),
        (
            annotation(1, "refusal", ["a"]),
            [event(1, "customer")],
            [consent("a", case_id="another")],
        ),
        (
            annotation(1, "refusal", ["a"]),
            [event(1, "customer")],
            [consent("a", revoked=0)],
        ),
        (
            annotation(1, "refusal", ["a"]),
            [event(1, "customer")],
            [consent("a", accepted=0)],
        ),
    ],
)
def test_annotations_require_existing_customer_event_and_actual_revocation(
    bad_annotation, events, consents
):
    with pytest.raises(ValueError):
        module().evaluate_interventions(
            state([], consents=consents),
            events,
            customer_annotations=[bad_annotation],
        )


@pytest.mark.parametrize("same_event", [True, False])
def test_conflicting_refusal_annotations_are_rejected(same_event):
    with pytest.raises(ValueError):
        module().evaluate_interventions(
            state([], consents=[consent("a")]),
            [event(1, "customer"), event(2, "customer")],
            customer_annotations=[
                annotation(1, "refusal", ["a"]),
                annotation(1 if same_event else 2, "request_change", ["a"]),
            ],
        )


def test_saved_annotation_is_reused_unless_explicitly_overridden():
    saved = state([], consents=[consent("a")])
    saved["customer_annotations"] = [annotation(1, "refusal", ["a"])]
    events = [event(1, "customer", "Оставлю как было")]
    restored = module().evaluate_interventions(saved, events)
    assert restored["counts"]["client_text_refusals"] == 1
    assert restored["counts"]["client_consent_revocations_unknown_reason"] == 0
    unannotated = module().evaluate_interventions(
        saved, events, customer_annotations=[]
    )
    assert unannotated["counts"]["client_text_refusals"] == 0
    assert unannotated["counts"]["client_consent_revocations"] == 0
    assert unannotated["counts"]["client_consent_revocations_unknown_reason"] == 0
    assert unannotated["counts"]["consent_invalidations_unattributed"] == 1


def test_saved_annotations_follow_sliced_checkpoint_events_but_explicit_links_are_validated():
    baseline = state([], consents=[consent("a", revoked=0)])
    saved = state([], consents=[consent("a")])
    saved["customer_annotations"] = [
        annotation(1, "request_change"),
        annotation(5, "refusal", ["a"]),
    ]
    sliced_events = [event(5, "customer", "Оставлю прежний товар")]
    result = module().evaluate_interventions(
        saved, sliced_events, baseline_state=baseline
    )
    assert result["scope"] == "since_checkpoint"
    assert result["counts"]["client_text_refusals"] == 1
    assert result["counts"]["client_consent_revocations"] == 1
    assert result["client_response_sources"]["annotations"] == [
        annotation(5, "refusal", ["a"])
    ]
    with pytest.raises(ValueError, match="customer"):
        module().evaluate_interventions(
            saved,
            sliced_events,
            baseline_state=baseline,
            customer_annotations=saved["customer_annotations"],
        )


def test_summary_rejects_mixed_versions_and_keeps_historical_version():
    metrics = module()
    current = metrics.evaluate_interventions(state([], consents=[]), [])
    historical = deepcopy(current)
    historical["version"] = "interventions-v1"
    historical["counts"] = {"client_refusals": 0}
    with pytest.raises(ValueError, match="верс"):
        metrics.summarize_interventions([historical, current])
    summary = metrics.summarize_interventions([historical, historical])
    assert summary["version"] == "interventions-v1"
    assert summary["counts"]["client_refusals"] == 0


@pytest.mark.parametrize(
    "case_id,client_revocations,unattributed",
    [("new-identify-name-zip", 0, 1), ("new-revoke-refund-exchange", 1, 0)],
)
def test_used_consent_is_not_a_client_revocation_in_saved_full_flow(
    case_id, client_revocations, unattributed
):
    folder = (
        Path(__file__).parents[1]
        / "evals/investigation/2026-11-03-final-check/flows"
        / case_id
    )
    saved = json.loads((folder / "state.json").read_text(encoding="utf-8"))
    events = json.loads((folder / "events.json").read_text(encoding="utf-8"))
    historical = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    assert historical["metrics"]["version"] == "interventions-v2"
    assert historical["metrics"]["counts"]["client_consent_revocations"] == 1
    metrics = module()
    result = metrics.evaluate_interventions(saved, events)
    assert result["counts"]["client_consent_revocations"] == client_revocations
    assert result["counts"]["consent_invalidations"] == 1
    assert result["counts"]["consent_invalidations_unattributed"] == unattributed
    assert result["counts"]["client_refusals"] == client_revocations
    assert result["consent_revocations"]["coverage"] == "confirmed_customer_events_only"
    # Повторное чтение и повтор одной записи журнала не добавляют отзывов.
    restored = metrics.evaluate_interventions(saved, events + [events[-1]])
    assert restored == result
