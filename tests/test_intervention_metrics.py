"""Счётчики вмешательств объясняются записями, а не успешностью попытки."""

import importlib.util
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


def state(rows, approvals=()):
    return {
        "case": {"id": "case", "employee_id": "employee"},
        "tables": {"proposals": rows, "approvals": list(approvals)},
    }


def event(identifier, kind, text="", case_id="case"):
    return {"id": identifier, "case_id": case_id, "kind": kind, "text": text}


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
