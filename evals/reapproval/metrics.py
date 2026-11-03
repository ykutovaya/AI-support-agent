"""Метрики по сохранённым предложениям и событиям, без повторного исполнения."""

from collections import Counter

METRICS_VERSION = "interventions-v1"
ERROR_CATEGORIES = ("application", "evaluator", "external", "limit", "unclassified")
EVENT_KINDS = {
    "staff_rejections": "rejected",
    "manual_resumes": "manual_resume",
    "automatic_resumes": "automatic_resume",
    "client_agreements": "consent",
    "client_refusals": "consent_refused",
    "outdated_client_agreements": "consent_outdated",
    "condition_changes": "conditions_changed",
    "delivery_retries": "delivery_retry",
    "external_unknown": "ticket_unknown",
}


def _ratio(numerator, denominator):
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator if denominator else None,
    }


def evaluate_interventions(
    state,
    events,
    *,
    baseline_state=None,
    baseline_proposal_id=None,
    event_boundary=None,
    correct=None,
    error_category=None,
):
    """Посчитать одну попытку или полное обращение.

    При baseline_state учитываются новые версии и последнее несогласованное
    pending-предложение на границе. Для старых серий preparation.json хранит
    baseline_proposal_id: его версия и последующие составляют знаменатель.
    events должны относиться к тому же интервалу; при полном журнале задаётся
    event_boundary. Правильность передаётся отдельно из оценки результата.

    «Без правок» означает совпадение подписи утверждённой версии. Это не
    доказывает правильность предложения или отсутствие правок в прошлых версиях.
    """
    from support_agent.proposals import Proposal

    if correct is not None and type(correct) is not bool:
        raise ValueError("Правильность должна быть True, False или None")
    if error_category is not None and error_category not in ERROR_CATEGORIES:
        raise ValueError("Неизвестная категория ошибки")
    if baseline_state is not None and baseline_proposal_id is not None:
        raise ValueError("Задайте один способ определения границы")
    case = state["case"]
    if baseline_state is not None and baseline_state["case"]["id"] != case["id"]:
        raise ValueError("Контрольная точка относится к другому обращению")
    selected_events = sorted(
        (
            e
            for e in events
            if e.get("case_id") == case["id"]
            and (event_boundary is None or e["id"] > event_boundary)
        ),
        key=lambda e: e["id"],
    )
    event_ids = {
        key: [e["id"] for e in selected_events if e["kind"] == kind]
        for key, kind in EVENT_KINDS.items()
    }
    # reject() пишет rejected и затем employee с той же причиной. Это одна команда.
    paired, followups = [], []
    for index, event in enumerate(selected_events):
        if event["kind"] != "employee":
            continue
        previous = selected_events[index - 1] if index else None
        paired_rejection = (
            previous
            and previous["kind"] == "rejected"
            and previous.get("text") == event.get("text")
        )
        (paired if paired_rejection else followups).append(event["id"])
    event_ids["paired_rejection_messages"] = paired
    event_ids["employee_followup_commands"] = followups
    event_ids["unclassified_errors"] = [
        e["id"] for e in selected_events if e["kind"] in {"error", "recovery_failed"}
    ]
    counts = {key: len(ids) for key, ids in event_ids.items()}
    counts["additional_staff_commands"] = sum(
        counts[key]
        for key in (
            "staff_rejections",
            "employee_followup_commands",
            "manual_resumes",
            "delivery_retries",
        )
    )
    # Эти события могут описывать один сбой. Их количество не называется числом сбоев.
    category = error_category or (
        "unclassified"
        if event_ids["unclassified_errors"] or event_ids["external_unknown"]
        else None
    )
    scope = (
        "since_checkpoint"
        if baseline_state is not None or baseline_proposal_id
        else "full_case"
    )
    tables = state.get("tables", {})
    available = "proposals" in tables and "approvals" in tables
    rows = sorted(
        (r for r in tables.get("proposals", []) if r["case_id"] == case["id"]),
        key=lambda r: r["version"],
    )
    baseline_approval_ids = set()
    if baseline_state is not None:
        before = baseline_state.get("tables", {})
        available = available and "proposals" in before and "approvals" in before
        old = [r for r in before.get("proposals", []) if r["case_id"] == case["id"]]
        old_ids = {r["id"] for r in old}
        baseline_approval_ids = {r["proposal_id"] for r in before.get("approvals", [])}
        latest = max(old, key=lambda r: r["version"], default=None)
        carryover = (
            latest["id"]
            if latest
            and latest["status"] == "pending"
            and latest["id"] not in baseline_approval_ids
            else None
        )
        rows = [r for r in rows if r["id"] not in old_ids or r["id"] == carryover]
    elif baseline_proposal_id is not None:
        boundary = next((r for r in rows if r["id"] == baseline_proposal_id), None)
        if boundary is None:
            raise ValueError("Предложение границы не найдено в истории обращения")
        rows = [r for r in rows if r["version"] >= boundary["version"]]
    offered_ids = [r["id"] for r in rows]
    accepted_ids, invalid_approval_ids = [], []
    approval_by_id = {r["proposal_id"]: r for r in tables.get("approvals", [])}
    for row in rows:
        record = approval_by_id.get(row["id"])
        if record is None or row["id"] in baseline_approval_ids:
            continue
        proposal = Proposal.model_validate_json(row["body"])
        valid = (
            record["digest"] == proposal.digest()
            and record["employee_id"] == case["employee_id"]
            and proposal.case_id == case["id"]
            and proposal.id == row["id"]
        )
        (accepted_ids if valid else invalid_approval_ids).append(row["id"])
    # Сохраняем и некорректную запись подтверждения: она не становится принятием
    # без правок, но исчезать из счётчика вмешательств не должна.
    counts["mandatory_staff_approvals"] = (
        len(accepted_ids) + len(invalid_approval_ids) if available else None
    )
    limitations = [
        "Считаются версии предложений; принятие не означает правильность.",
        "Причина исправления и ошибка модели не выводятся из изменения условий.",
        "События error/recovery_failed могут описывать один сбой; источник без разбора неизвестен.",
        "Время человека по этим событиям не измеряется.",
    ]
    if not available:
        limitations.append(
            "История предложений или согласований неполна; доля принятия недоступна."
        )
    return {
        "version": METRICS_VERSION,
        "case_id": case["id"],
        "scope": scope,
        "counts": counts,
        "event_ids": event_ids,
        "proposals": {
            "offered_ids": offered_ids,
            "accepted_ids": accepted_ids,
            "invalid_approval_ids": invalid_approval_ids,
        },
        "accepted_without_edits": _ratio(len(accepted_ids), len(rows))
        if available
        else _ratio(None, None),
        "outcome": {"correct": correct, "error_category": category},
        "correction_reasons": "not_classified",
        "human_time_seconds": None,
        "limitations": limitations,
    }


def summarize_interventions(rows):
    """Суммировать все попытки, сохраняя неизвестные и неуспешные в знаменателе."""
    rows = list(rows)
    known = [r for r in rows if r["accepted_without_edits"]["denominator"] is not None]
    approved = sum(r["accepted_without_edits"]["numerator"] for r in known)
    offered = sum(r["accepted_without_edits"]["denominator"] for r in known)
    assessed = sum(r["outcome"]["correct"] is not None for r in rows)
    correct = sum(r["outcome"]["correct"] is True for r in rows)
    category_counts = Counter(r["outcome"]["error_category"] for r in rows)
    total_counts = {}
    for key in sorted(set().union(*(r["counts"] for r in rows))):
        values = [r["counts"].get(key) for r in rows]
        total_counts[key] = (
            sum(v for v in values if v is not None)
            if all(v is not None for v in values)
            else None
        )
    return {
        "version": METRICS_VERSION,
        "attempts": len(rows),
        "correct_results": {
            **_ratio(correct, len(rows)),
            "assessed": assessed,
            "unassessed": len(rows) - assessed,
        },
        "accepted_without_edits": {
            **_ratio(approved, offered),
            "covered_attempts": len(known),
            "unavailable_attempts": len(rows) - len(known),
        },
        "counts": total_counts,
        "error_categories": {key: category_counts[key] for key in ERROR_CATEGORIES},
        "human_time_seconds": None,
    }
