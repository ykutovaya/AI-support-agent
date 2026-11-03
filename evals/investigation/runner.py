"""Полное обращение через общий процесс; клиент и сотрудник явно имитируются."""

import json
import time
from copy import deepcopy
from functools import partial
from pathlib import Path

from evals.investigation.assessment import approval_review, assess_case
from evals.investigation.cases import snapshot_for
from evals.reapproval.metrics import evaluate_interventions
from support_agent.adapters.ticket_retail import TicketRetailAdapter
from support_agent.storage import Store
from support_agent.workflow import SupportService

EMPLOYEE = "investigation-evaluation-employee"
TABLES = (
    "proposals",
    "consents",
    "approvals",
    "operations",
    "operation_verifications",
    "support_tickets",
    "external_ticket_operations",
    "outbox",
    "deliveries",
    "recoveries",
)


class RunLimit(RuntimeError):
    """Обращение достигло ограничения шагов или времени."""


class Limit:
    def __init__(self, max_steps=60, max_seconds=600):
        self.max_steps = max_steps
        self.max_seconds = max_seconds
        self.started = time.monotonic()
        self.steps = 0

    @property
    def remaining(self):
        return self.max_seconds - (time.monotonic() - self.started)

    def take(self, count=1):
        if self.steps + count > self.max_steps or self.remaining <= 0:
            raise RunLimit("Превышен предел обращения")
        self.steps += count


def _write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def capture_state(store, case_id, initial_retail):
    """Сохранить наблюдаемые результаты до запуска оценщика."""
    with store.connection() as conn:
        tables = {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in TABLES
        }
    case = store.case(case_id)
    return {
        "case": {key: case[key] for key in ("id", "user_id", "employee_id")},
        "tables": tables,
        "retail": store.snapshot()[0],
        "initial_retail": initial_retail,
        "messages": [
            message.model_dump(mode="json") for message in case["state"].messages
        ],
    }


def _failure(error, *, evaluator=False):
    cause = error
    while cause.__cause__ is not None:
        cause = cause.__cause__
    name = type(cause).__name__
    if evaluator:
        category = "evaluator"
    elif isinstance(cause, (RunLimit,)) or name in {
        "RuntimeLimit",
        "BudgetLimit",
        "BudgetExceeded",
        "AttemptLimit",
    }:
        category = "limit"
    elif isinstance(cause, (TimeoutError, ConnectionError)) or any(
        part in name.lower()
        for part in ("timeout", "connection", "ratelimit", "authentication", "apierror")
    ):
        category = "external"
    else:
        category = "application"
    # Сообщение исключения провайдера может содержать заголовки или ключ; его не копируем.
    return {"category": category, "type": type(error).__name__, "cause_type": name}


def _approve(service, proposal, limit):
    limit.take()
    return service.approve(
        proposal.case_id, proposal.id, proposal.version, proposal.digest(), EMPLOYEE
    )


def _customer(service, case_id, text, limit):
    limit.take()
    return service.investigate(case_id, text, EMPLOYEE)


def _apply_price(service, event):
    with service.store.transaction() as conn:
        shop, _ = service.store.retail(conn)
        shop["products"][event["product_id"]]["variants"][event["variant_id"]][
            "price"
        ] = event["price"]
        service.store.save_retail(conn, shop)


def run_case(folder, case, model, *, max_steps=60, max_seconds=600, limit=None):
    """Пройти одно новое обращение; hidden-критерии используются только оценщиком.

    Принимающий решение сотрудник имитируется проверкой предложения. Неизвестный
    смысл останавливает попытку с review; неверное предложение отклоняется с общей
    просьбой перепроверить обращение. Эталонные параметры в модель не передаются.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    limit = limit or Limit(max_steps, max_seconds)
    started = time.monotonic()
    store = Store(folder / "application.sqlite3")
    policy = Path(__file__).with_name("policy.md").read_text(encoding="utf-8")
    adapter_factory = partial(TicketRetailAdapter, target="local")
    # Как при штатном запуске: исходный снимок сначала приводится к RetailDB.
    # Иначе добавление необязательных null-полей при первой операции выглядит
    # в оценке как изменение чужих записей.
    store.initialize(adapter_factory(snapshot_for(case), policy).snapshot(), policy)
    initial_retail = store.snapshot()[0]
    service = SupportService(store, model, adapter_factory, recovery_mode="manual")
    case_id = service.create_case(case["user_id"], EMPLOYEE)
    interactions = deepcopy(case.get("interactions", []))
    consumed = []
    reviews = []
    decisions = []
    failure = None
    evaluator_failure = None
    review_required = False
    corrections = 0
    stage = "initial"
    stop_reason = "not_started"
    try:
        proposal = _customer(service, case_id, case["initial_message"], limit)
        while True:
            limit.take()
            try:
                review = approval_review(proposal, case, stage=stage)
            except Exception as error:
                evaluator_failure = _failure(error, evaluator=True)
                stop_reason = "approval_evaluator_error"
                break
            reviews.append(
                {"proposal_id": proposal.id, "version": proposal.version, **review}
            )
            if review["status"] == "review":
                review_required = True
                stop_reason = "staff_review_required"
                break
            if review["status"] != "pass":
                if corrections >= 1:
                    decisions.append(
                        {
                            "proposal_id": proposal.id,
                            "decision": "withhold",
                            "reason": review["reason"],
                        }
                    )
                    stop_reason = "repeated_incorrect_proposal"
                    break
                decisions.append(
                    {
                        "proposal_id": proposal.id,
                        "decision": "reject",
                        "reason": review["reason"],
                    }
                )
                corrections += 1
                limit.take()
                proposal = service.reject(
                    case_id,
                    proposal.id,
                    proposal.version,
                    proposal.digest(),
                    (
                        "Для согласования операции подготовьте предложение доступным инструментом; согласие клиента регистрирует приложение отдельно. Не заменяйте подготовку операции текстовым запросом подтверждения."
                        if review["reason"] == "structured_action_required"
                        else "Предложение не соответствует запросу клиента или проверенным данным. Перечитайте историю и источники, уточните недостающие сведения; не выполняйте неподтверждённые действия."
                    ),
                    EMPLOYEE,
                )
                continue
            decisions.append({"proposal_id": proposal.id, "decision": "approve"})
            event = (
                next(
                    (
                        e
                        for i, e in enumerate(interactions)
                        if i not in consumed and e["trigger"] == "before_action"
                    ),
                    None,
                )
                if proposal.action
                else None
            )
            if event is not None:
                index = next(
                    i
                    for i, e in enumerate(interactions)
                    if i not in consumed and e is event
                )
                consumed.append(index)
                if event["kind"] == "customer":
                    proposal = _customer(service, case_id, event["text"], limit)
                    stage = "changed"
                    continue
                if event["kind"] != "price_change":
                    raise ValueError("Неизвестное событие клиента")
                _apply_price(service, event)
                stage = "changed"
            outcome = _approve(service, proposal, limit)
            if outcome.get("recovery_id"):
                limit.take()
                proposal = service.resume(case_id, outcome["recovery_id"], EMPLOYEE)
                continue
            if outcome.get("status") != "delivered":
                stop_reason = f"execution_{outcome.get('status', 'unknown')}"
                break
            if proposal.consent_request is not None:
                limit.take()
                action = proposal.consent_request
                response = f"Подтверждаю указанные позиции, замену, способ оплаты и сумму {action.amount} {action.currency}."
                proposal = service.record_client_response(
                    case_id, proposal.id, True, response, EMPLOYEE
                )
                continue
            if proposal.action is not None:
                stop_reason = "action_delivered"
                break
            event = next(
                (
                    e
                    for i, e in enumerate(interactions)
                    if i not in consumed and e["trigger"] == "message"
                ),
                None,
            )
            if event is not None:
                index = next(
                    i
                    for i, e in enumerate(interactions)
                    if i not in consumed and e is event
                )
                consumed.append(index)
                if event["kind"] != "customer":
                    raise ValueError("Неизвестный ответ клиента")
                proposal = _customer(service, case_id, event["text"], limit)
                continue
            stop_reason = "message_delivered"
            break
    except Exception as error:
        failure = _failure(error)
        stop_reason = "execution_error"
    state = capture_state(store, case_id, initial_retail)
    events = store.events(case_id)
    state["events"] = events
    _write(folder / "state.json", state)
    _write(folder / "events.json", events)
    _write(folder / "staff-reviews.json", reviews)
    _write(folder / "staff-decisions.json", decisions)
    assessment = None
    try:
        assessment = assess_case(state, case)
    except Exception as error:
        evaluator_failure = _failure(error, evaluator=True)
    correct = (
        None
        if assessment is None or review_required or failure or evaluator_failure
        else assessment["complete"]
    )
    if assessment and any(
        check.get("status") == "review"
        for check in assessment.get("checks", {}).values()
    ):
        correct = None
    metrics = None
    try:
        metrics = evaluate_interventions(
            state,
            events,
            correct=correct,
            error_category=(failure or evaluator_failure or {}).get("category"),
        )
    except Exception as error:
        evaluator_failure = _failure(error, evaluator=True)
    needs_review = review_required or bool(
        assessment
        and any(
            check.get("status") == "review"
            for check in assessment.get("checks", {}).values()
        )
    )
    status = (
        "failed"
        if failure or evaluator_failure
        else "review"
        if needs_review
        else "complete"
        if assessment and assessment["complete"]
        else "failed"
    )
    result = {
        "status": status,
        "case_id": case["id"],
        "split": case["split"],
        "application_case_id": case_id,
        "model_source": "injected",
        "ticket_backend": "local",
        "recovery_mode": "manual",
        "client": "scripted",
        "employee": "scripted_review",
        "stop_reason": stop_reason,
        "review_required": review_required,
        "failure": failure,
        "evaluator_failure": evaluator_failure,
        "assessment": assessment,
        "metrics": metrics,
        "steps": limit.steps,
        "elapsed_seconds": time.monotonic() - started,
        "interactions_consumed": consumed,
        "interactions_total": len(interactions),
    }
    _write(folder / "result.json", result)
    return result
