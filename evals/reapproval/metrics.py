"""Метрики по сохранённым предложениям и событиям, без повторного исполнения."""

from collections import Counter

METRICS_VERSION = "interventions-v3"
ERROR_CATEGORIES = ("application", "evaluator", "external", "limit", "unclassified")
CUSTOMER_REASONS = ("refusal", "request_change", "unknown")
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


def _client_responses(
    state, events, annotations, baseline_state, partial_scope, annotations_available
):
    """Причины задаёт разметка оценщика; текст сообщения не классифицируется."""
    case_id = state["case"]["id"]
    events_by_id = {e["id"]: e for e in events}
    tables = state.get("tables", {})
    consents = {
        row["id"]: row
        for row in tables.get("consents", [])
        if row.get("case_id") == case_id
    }
    revoked_ids = {
        identifier
        for identifier, row in consents.items()
        if row.get("accepted") == 1 and row.get("revoked") == 1
    }
    available = "consents" in tables and all(
        "accepted" in row and "revoked" in row for row in consents.values()
    )
    consents_available = available
    coverage = "complete_snapshot" if available else "unavailable"
    if baseline_state is not None:
        before = baseline_state.get("tables", {})
        old = [r for r in before.get("consents", []) if r.get("case_id") == case_id]
        available = (
            available
            and "consents" in before
            and all("accepted" in r and "revoked" in r for r in old)
        )
        revoked_ids -= {
            r["id"] for r in old if r.get("accepted") == 1 and r.get("revoked") == 1
        }
        coverage = "baseline_delta" if available else "unavailable"
    elif partial_scope and available:
        available = False
        coverage = "event_annotations_only"

    # record_client_response пишет customer и затем подтверждённый выбор.
    choices = {
        previous["id"]: current["kind"]
        for previous, current in zip(events, events[1:])
        if previous["kind"] == "customer"
        and current["kind"] in {"consent", "consent_refused", "consent_outdated"}
    }
    by_event, reasons = {}, {}
    for annotation in annotations:
        identifier = annotation["event_id"]
        event = events_by_id.get(identifier)
        if event is None or event["kind"] != "customer":
            raise ValueError("Аннотация должна ссылаться на событие customer")
        reason = annotation["reason"]
        if reason not in CUSTOMER_REASONS:
            raise ValueError("Неизвестная причина ответа клиента")
        linked_ids = sorted(set(annotation["revoked_consent_ids"]))
        if any(
            identifier not in consents
            or consents[identifier].get("accepted") != 1
            or consents[identifier].get("revoked") != 1
            for identifier in linked_ids
        ):
            raise ValueError("Аннотация отзыва требует отозванного согласия обращения")
        normalized = {**annotation, "revoked_consent_ids": linked_ids}
        if identifier in by_event and by_event[identifier] != normalized:
            raise ValueError("Противоречивые аннотации одного сообщения")
        by_event[identifier] = normalized
        choice = choices.get(identifier)
        if (choice == "consent_refused" and reason == "request_change") or (
            choice in {"consent", "consent_outdated"} and reason == "refusal"
        ):
            raise ValueError("Аннотация противоречит подтверждённому ответу клиента")
        confirmed_reason = "refusal" if choice == "consent_refused" else reason
        for consent_id in linked_ids:
            if consent_id in reasons and reasons[consent_id] != confirmed_reason:
                raise ValueError("Противоречивые причины отзыва одного согласия")
            reasons[consent_id] = confirmed_reason
    if not available:
        # Один конечный снимок не датирует отзыв внутри частичного интервала.
        revoked_ids &= reasons.keys()
    # revoked отмечает также использованное или недействительное после ошибки
    # согласие. Связь с новым сообщением клиента подтверждает только аннотация.
    client_revoked_ids = revoked_ids & reasons.keys()
    button_ids = [e["id"] for e in events if e["kind"] == "consent_refused"]
    text_ids = [
        identifier
        for identifier, annotation in sorted(by_event.items())
        if annotation["reason"] == "refusal" and identifier not in choices
    ]
    by_reason = {
        reason: sorted(
            identifier
            for identifier in client_revoked_ids
            if reasons[identifier] == reason
        )
        for reason in CUSTOMER_REASONS
    }
    return (
        {
            "client_button_refusals": button_ids,
            "client_text_refusals": text_ids,
            "client_refusals": sorted(button_ids + text_ids),
        },
        {
            "available": annotations_available and consents_available,
            "coverage": "confirmed_customer_events_only"
            if annotations_available and consents_available
            else "unavailable",
            "consent_ids": sorted(client_revoked_ids),
            "by_reason": by_reason,
        },
        {
            "available": available,
            "coverage": coverage,
            "consent_ids": sorted(revoked_ids),
            "unattributed_consent_ids": sorted(revoked_ids - client_revoked_ids),
        },
        {
            "button_basis": "consent_refused",
            "reason_basis": "explicit_annotation",
            "annotations": [by_event[identifier] for identifier in sorted(by_event)],
            "unclassified_customer_event_ids": [
                e["id"]
                for e in events
                if e["kind"] == "customer"
                and e["id"] not in choices
                and (
                    e["id"] not in by_event or by_event[e["id"]]["reason"] == "unknown"
                )
            ],
        },
    )


def evaluate_interventions(
    state,
    events,
    *,
    baseline_state=None,
    baseline_proposal_id=None,
    event_boundary=None,
    customer_annotations=None,
    correct=None,
    error_category=None,
):
    """Посчитать одну попытку или полное обращение.

    При baseline_state учитываются новые версии и последнее несогласованное
    pending-предложение на границе. Для старых серий preparation.json хранит
    baseline_proposal_id: его версия и последующие составляют знаменатель.
    events должны относиться к тому же интервалу; при полном журнале задаётся
    event_boundary. Правильность передаётся отдельно из оценки результата.

    customer_annotations привязывает явно заданную оценщиком причину к customer:
    case_id, event_id, reason (refusal/request_change/unknown), revoked_consent_ids.
    Это разметка сценария, а не распознавание смысла сообщения. Отзыв вследствие
    сообщения клиента требует связи с событием и accepted=1/revoked=1. Остальные
    revoked согласия учитываются отдельно, без предположения об их причине.
    Если аргумент не задан, используется customer_annotations сохранённого state.
    При границе неявные аннотации ограничиваются переданным интервалом событий.

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
    unique_events = {}
    for event in events:
        if event.get("case_id") != case["id"] or (
            event_boundary is not None and event["id"] <= event_boundary
        ):
            continue
        if event["id"] in unique_events and unique_events[event["id"]] != event:
            raise ValueError("Противоречивые записи одного события")
        unique_events[event["id"]] = event
    selected_events = sorted(unique_events.values(), key=lambda e: e["id"])
    annotations_available = (
        customer_annotations is not None or "customer_annotations" in state
    )
    if customer_annotations is None:
        customer_annotations = state.get("customer_annotations", [])
        if (
            baseline_state is not None
            or baseline_proposal_id is not None
            or event_boundary is not None
        ):
            customer_annotations = [
                annotation
                for annotation in customer_annotations
                if annotation["event_id"] in unique_events
            ]
    event_ids = {
        key: [e["id"] for e in selected_events if e["kind"] == kind]
        for key, kind in EVENT_KINDS.items()
    }
    responses, revocations, invalidations, response_sources = _client_responses(
        state,
        selected_events,
        [
            annotation
            for annotation in customer_annotations
            if annotation["case_id"] == case["id"]
            and (event_boundary is None or annotation["event_id"] > event_boundary)
        ],
        baseline_state,
        event_boundary is not None or baseline_proposal_id is not None,
        annotations_available,
    )
    event_ids.update(responses)
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
    counts["client_consent_revocations"] = (
        len(revocations["consent_ids"]) if revocations["available"] else None
    )
    counts["client_consent_revocations_unknown_reason"] = (
        len(revocations["by_reason"]["unknown"]) if revocations["available"] else None
    )
    counts["consent_invalidations"] = (
        len(invalidations["consent_ids"]) if invalidations["available"] else None
    )
    counts["consent_invalidations_unattributed"] = (
        len(invalidations["unattributed_consent_ids"])
        if invalidations["available"]
        else None
    )
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
        "Отказ свободным текстом считается только по явной разметке оценщика; смысл текста не определяется.",
        "Клиентские отзывы считаются только по подтверждённым связям с сообщениями; это не полный охват всех причин утраты согласия.",
        "Флаг revoked включает использование согласия и другие причины; без аннотации причина не устанавливается.",
    ]
    if not revocations["available"]:
        limitations.append(
            "Нет разметки или сведений о согласиях; счётчик клиентских отзывов недоступен."
        )
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
        "client_response_sources": response_sources,
        "consent_revocations": revocations,
        "consent_invalidations": invalidations,
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
    versions = {row["version"] for row in rows}
    if len(versions) > 1:
        raise ValueError("Нельзя суммировать метрики разных версий")
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
        "version": next(iter(versions), METRICS_VERSION),
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
