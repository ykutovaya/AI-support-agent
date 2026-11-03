"""Парное сравнение пересогласования. Без --run сетевые вызовы запрещены."""

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
MODEL = "openai/responses/gpt-6-luna"
LLM_ARGS = {
    "reasoning_effort": "low",
    "max_tokens": 8192,
    "timeout": 90,
    "num_retries": 0,
}
MAX_STEPS = 60  # Модель + её инструменты + действия имитируемого сотрудника.
MAX_SECONDS = 600
EMPLOYEE = "evaluation-employee"
APPROVED_MAX_USD = 5.0
PRICE_SOURCE = {
    "model": MODEL,
    "url": "https://developers.openai.com/api/docs/models/gpt-6-luna",
    "verified_on": "2026-10-31",
    "verification": "Official model page checked before this experiment",
    "currency": "USD",
    "input_per_million": 0.10,
    "output_per_million": 0.50,
    "long_context_threshold": 272000,
    "long_context_input_multiplier": 2,
    "long_context_output_multiplier": 1.5,
    "cache_discount": "ignored for conservative upper estimate",
}


def safe_imports():
    sys.dont_write_bytecode = True
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    if str(PROJECT_ROOT / "src") not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))


safe_imports()

sys.path.insert(0, str(HERE))
from assessment import assess_attempt  # noqa: E402
from grading import GRADER_VERSION, assess_refusal  # noqa: E402
from metrics import evaluate_interventions, summarize_interventions  # noqa: E402


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(temporary, path)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def scenarios(phase):
    values = json.loads((HERE / "scenarios.json").read_text(encoding="utf-8"))
    return deepcopy(values[phase])


def pricing_for(model):
    if model != MODEL:
        raise ValueError("Нет проверенной цены для выбранной модели")
    return deepcopy(PRICE_SOURCE)


class BudgetExceeded(RuntimeError):
    pass


class AttemptLimit(RuntimeError):
    pass


class GraderReview(ValueError):
    """Текст выходит за проверенные конструкции оценщика и требует разбора."""


class Limit:
    def __init__(self):
        self.started = time.monotonic()
        self.steps = 0

    @property
    def remaining(self):
        return MAX_SECONDS - (time.monotonic() - self.started)

    def take(self, count=1):
        if self.steps + count > MAX_STEPS or self.remaining <= 0:
            raise AttemptLimit("Превышен предел всей попытки")
        self.steps += count


class Budget:
    """Один журнал расходов на подготовку, пилот и основной прогон.

    Резерв записывается ДО запроса. При транспортной ошибке или отсутствии usage
    весь резерв остаётся расходом. Повторные запросы провайдера отключены.
    """

    def __init__(self, path, limit):
        self.path = Path(path)
        if not 0 < float(limit) <= APPROVED_MAX_USD:
            raise ValueError(
                "Лимит должен быть больше нуля и не превышать согласованные $5"
            )
        self.data = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else {
                "limit_usd": float(limit),
                "model": MODEL,
                "pricing": pricing_for(MODEL),
                "calls": [],
            }
        )
        if self.data["limit_usd"] != float(limit) or self.data["model"] != MODEL:
            raise ValueError(
                "Нельзя менять общий лимит или модель существующего журнала"
            )

    @property
    def used(self):
        return sum(c["charged_bound_usd"] for c in self.data["calls"])

    def reserve(self, amount, label):
        amount = float(amount)
        if (
            self.data.get("halted")
            or amount < 0
            or self.used + amount > self.data["limit_usd"] + 1e-12
        ):
            raise BudgetExceeded(
                "Недостаточно остатка для верхней оценки следующего запроса"
            )
        index = len(self.data["calls"])
        self.data["calls"].append(
            {
                "id": index,
                "label": label,
                "reserved_usd": amount,
                "charged_bound_usd": amount,
                "status": "reserved_before_call",
            }
        )
        write_json(self.path, self.data)
        return index

    def finish(self, index, measured_bound, status):
        row = self.data["calls"][index]
        row["status"] = status
        if measured_bound is not None:
            row["charged_bound_usd"] = float(measured_bound)
            if measured_bound > row["reserved_usd"] + 1e-9:
                row["status"] = "reservation_exceeded"
                self.data["halted"] = "reservation_exceeded"
        write_json(self.path, self.data)
        if row["status"] == "reservation_exceeded":
            raise BudgetExceeded(
                "Фактический расход превысил резерв; новые запросы запрещены"
            )


def reservation(messages, tools):
    """Байты UTF-8, весь JSON и запас обрамления вместо заниженного chars/4.

    Для резерва всегда применён более дорогой длинный контекст. Выход включает
    все 8192 разрешённых токена, в том числе reasoning. Кеш не уменьшает резерв.
    """
    from support_agent.adapters.llm import provider_message

    payload = {
        "messages": [provider_message(m) for m in messages],
        "tools": [{"type": "function", "function": t.model_dump()} for t in tools],
    }
    upper_input = len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 8192
    price = pricing_for(MODEL)
    amount = (
        upper_input * price["input_per_million"] * 2
        + LLM_ARGS["max_tokens"] * price["output_per_million"] * 1.5
    ) / 1_000_000
    return upper_input, amount


def usage_cost(generation):
    usage = generation.usage or {}
    incoming = usage.get("prompt_tokens", usage.get("input_tokens"))
    outgoing = usage.get("completion_tokens", usage.get("output_tokens"))
    if (
        not isinstance(incoming, int)
        or not isinstance(outgoing, int)
        or min(incoming, outgoing) < 0
    ):
        return None
    price = pricing_for(MODEL)
    large = incoming > price["long_context_threshold"]
    conservative = (
        incoming * price["input_per_million"] * (2 if large else 1)
        + outgoing * price["output_per_million"] * (1.5 if large else 1)
    ) / 1_000_000
    return max(conservative, generation.cost or 0)


def root_error(error):
    while error.__cause__ is not None:
        error = error.__cause__
    return error


class TrackedModel:
    def __init__(self, inner, *, path, label, limit, budget=None, offline=False):
        self.inner, self.path, self.label = inner, Path(path), label
        self.limit, self.budget, self.offline = limit, budget, offline
        self.calls = []
        self.failure_type = None

    def generate(self, messages, tools):
        reservation_id = None
        try:
            self.limit.take()
            upper, reserve = reservation(messages, tools)
            if not self.offline:
                if self.budget is None:
                    raise ValueError("Оплачиваемый вызов без общего бюджета запрещён")
                if self.limit.remaining < 1:
                    raise AttemptLimit("Недостаточно времени для запроса")
                # LiteLLMClient — тот же адаптер приложения, с общим пределом попытки.
                self.inner.llm_args["timeout"] = min(90, self.limit.remaining)
                reservation_id = self.budget.reserve(reserve, self.label)
        except BaseException as error:
            self.failure_type = type(error).__name__
            raise
        row = {
            "index": len(self.calls),
            "model": "offline-script" if self.offline else MODEL,
            "request": {
                "messages": [m.model_dump(mode="json") for m in messages],
                "tools": [t.model_dump(mode="json") for t in tools],
            },
            "input_token_upper_bound": upper,
            "reserved_usd": 0 if self.offline else reserve,
        }
        self.calls.append(row)
        write_json(self.path, self.calls)
        started = time.monotonic()
        try:
            generation = self.inner.generate(messages, tools)
            row.update(
                {
                    "status": "received",
                    "message": generation.message.model_dump(mode="json"),
                    "usage": generation.usage,
                    "provider_reported_cost": generation.cost,
                    "conservative_usage_cost_usd": None
                    if self.offline
                    else usage_cost(generation),
                }
            )
            if reservation_id is not None:
                measured = usage_cost(generation)
                if measured is None and generation.cost is not None:
                    measured = max(reserve, generation.cost)
                self.budget.finish(reservation_id, measured, "received")
            self.limit.take(len(generation.message.tool_calls or []))
            return generation
        except BaseException as error:
            self.failure_type = type(error).__name__
            row.update({"status": "error", "error_type": type(error).__name__})
            if (
                reservation_id is not None
                and self.budget.data["calls"][reservation_id]["status"]
                == "reserved_before_call"
            ):
                self.budget.finish(
                    reservation_id, None, "uncertain_transport_or_generation_error"
                )
            raise
        finally:
            row["elapsed_seconds"] = round(time.monotonic() - started, 6)
            write_json(self.path, self.calls)


def snapshot_for(case):
    snapshot = json.loads((HERE / "base_shop.json").read_text(encoding="utf-8"))
    user = snapshot["users"].pop("u1")
    order = snapshot["orders"].pop("#O1")
    product = snapshot["products"].pop("p1")
    user.update(
        user_id=case["user_id"],
        email=case["id"] + "@example.test",
        orders=[case["order_id"]],
    )
    user["name"] = {"first_name": "Учебный", "last_name": case["id"]}
    product["product_id"] = case["product_id"]
    product["variants"]["old"]["price"] = float(case["old_price"])
    product["variants"]["new"]["price"] = float(case["new_price"])
    order.update(order_id=case["order_id"], user_id=case["user_id"])
    order["items"][0].update(
        product_id=case["product_id"], price=float(case["old_price"])
    )
    order["payment_history"][0]["amount"] = float(case["old_price"])
    snapshot["users"][case["user_id"]] = user
    snapshot["orders"][case["order_id"]] = order
    snapshot["products"][case["product_id"]] = product
    return snapshot


def expected_arguments(case):
    return {
        "order_id": case["order_id"],
        "item_ids": ["old"],
        "new_item_ids": ["new"],
        "payment_method_id": "card",
    }


def expected_amount(case, changed):
    price = (
        case["changed_price"]
        if changed and case["mutation"] == "price"
        else case["new_price"]
    )
    return (Decimal(price) - Decimal(case["old_price"])).quantize(Decimal("0.01"))


def validate_action(action, case, *, changed):
    if (
        action is None
        or action.name != "exchange_delivered_order_items"
        or action.arguments != expected_arguments(case)
        or action.amount != expected_amount(case, changed)
        or action.result_status != "exchange requested"
    ):
        raise ValueError(
            "Предложение не соответствует заранее согласованным условиям клиента"
        )


def approve(app, proposal, limit=None):
    if limit:
        limit.take()
    return app.approve(
        proposal.case_id, proposal.id, proposal.version, proposal.digest(), EMPLOYEE
    )


def prepare_checkpoint(path, case, model, limit=None):
    from support_agent.storage import Store
    from support_agent.workflow import SupportService

    path = Path(path)
    if path.exists():
        raise ValueError("Подготовленное состояние нельзя перезаписывать")
    store = Store(path)
    store.initialize(
        snapshot_for(case), (HERE / "policy.md").read_text(encoding="utf-8")
    )
    app = SupportService(store, model)
    case_id = app.create_case(case["user_id"], EMPLOYEE)
    opening = (
        f"Мой email {case['id']}@example.test. Хочу обменять чёрные наушники "
        f"из доставленного заказа {case['order_id']} на белый вариант того же товара. "
        "Это единственная позиция для обмена. Для разницы в цене выбираю карту card, "
        "Visa с последними цифрами 1111. Подготовьте условия обмена."
    )
    if limit:
        limit.take()
    question = app.investigate(case_id, opening, EMPLOYEE)
    validate_action(question.consent_request, case, changed=False)
    if approve(app, question, limit)["status"] != "delivered":
        raise ValueError("Начальные условия не доставлены клиенту")
    if limit:
        limit.take()
    proposal = app.record_client_response(
        case_id,
        question.id,
        True,
        f"Да, подтверждаю обмен единственной позиции на белый вариант и доплату {expected_amount(case, False)} USD с карты card.",
        EMPLOYEE,
    )
    validate_action(proposal.action, case, changed=False)
    with store.connection() as conn:
        if conn.execute("SELECT count(*) FROM operations").fetchone()[0] != 0:
            raise ValueError("В контрольной точке уже выполнена операция")
    return {
        "case_id": case_id,
        "proposal_id": proposal.id,
        "event_boundary": max(e["id"] for e in store.events(case_id)),
        "checkpoint_sha256": sha256(path),
    }


class OfflineModel:
    """Только проверка проводки: сценарный ответ, не результат языковой модели."""

    def __init__(self, case):
        self.case = case

    def generate(self, messages, tools):
        from support_agent.models import Generation, Message, ToolCall

        prefix = "Служебное событие приложения (не сообщение клиента): "
        event_index = next(
            (
                i
                for i in range(len(messages) - 1, -1, -1)
                if (messages[i].content or "").startswith(prefix)
            ),
            None,
        )
        if event_index is not None:
            event = json.loads(messages[event_index].content[len(prefix) :])
            if event["check"]["status"] == "forbidden":
                return Generation(
                    message=Message(
                        role="assistant",
                        content=f"Заказ {self.case['order_id']} отменён (cancelled), обмен невозможен.",
                    )
                )
            names = [
                c.name for m in messages[event_index + 1 :] for c in m.tool_calls or []
            ]
            sequence = [
                ("get_order_details", {"order_id": self.case["order_id"]}),
                ("get_product_details", {"product_id": self.case["product_id"]}),
            ]
        else:
            names = [c.name for m in messages for c in m.tool_calls or []]
            sequence = [
                ("find_user_id_by_email", {"email": self.case["id"] + "@example.test"}),
                ("get_user_details", {"user_id": self.case["user_id"]}),
                ("get_order_details", {"order_id": self.case["order_id"]}),
                ("get_product_details", {"product_id": self.case["product_id"]}),
            ]
        name, arguments = next(
            ((name, args) for name, args in sequence if name not in names),
            ("propose_exchange", expected_arguments(self.case)),
        )
        return Generation(
            message=Message(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id=f"offline-{len(messages)}-{name}",
                        name=name,
                        arguments=arguments,
                    )
                ],
            )
        )


def mutate(store, case):
    with store.transaction() as conn:
        data, _ = store.retail(conn)
        if case["mutation"] == "price":
            data["products"][case["product_id"]]["variants"]["new"]["price"] = float(
                case["changed_price"]
            )
        elif case["mutation"] == "status":
            data["orders"][case["order_id"]]["status"] = "cancelled"
        store.save_retail(conn, data)


def forbidden_explanation(text):
    """Совместимая обёртка: автоматически принимаются только проверенные отказы."""
    return assess_refusal(text)["status"] == "pass"


def capture_state(store, case_id):
    with store.connection() as conn:
        tables = {
            table: [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
            for table in (
                "proposals",
                "consents",
                "approvals",
                "operations",
                "outbox",
                "deliveries",
                "recoveries",
            )
        }
    return {
        "tables": tables,
        "case": {
            key: store.case(case_id)[key] for key in ("id", "user_id", "employee_id")
        },
        "retail": store.snapshot()[0],
        "messages": [
            m.model_dump(mode="json") for m in store.case(case_id)["state"].messages
        ],
    }


def audit_state(state):
    """Наблюдаемые согласования и фактические записи, не повторы алгоритма исполнителя."""
    from support_agent.proposals import Proposal

    tables = state["tables"]
    approvals = {r["proposal_id"]: r for r in tables["approvals"]}
    operations = {r["proposal_id"] for r in tables["operations"]}
    proposals = {
        r["id"]: Proposal.model_validate_json(r["body"]) for r in tables["proposals"]
    }
    outbox = {r["id"]: r for r in tables["outbox"]}
    delivered = {r["id"]: r for r in tables["deliveries"]}
    violations = []
    for key, approval in approvals.items():
        proposal = proposals.get(key)
        if proposal is None or proposal.digest() != approval["digest"]:
            violations.append("approval_digest_mismatch")
        if approval["employee_id"] != state["case"]["employee_id"]:
            violations.append("approval_by_wrong_employee")
    for record in tables["operations"]:
        proposal_id = record["proposal_id"]
        if proposal_id not in approvals:
            violations.append("operation_without_staff_approval")
        action_proposal = proposals.get(proposal_id)
        matching_consent = False
        if action_proposal and action_proposal.action:
            action_hash = action_proposal.action.fingerprint()
            for consent in tables["consents"]:
                question = proposals.get(consent["question_id"])
                matching_delivery = any(
                    r["proposal_id"] == consent["question_id"]
                    and r["status"] == "delivered"
                    and r["id"] in delivered
                    for r in tables["outbox"]
                )
                if (
                    consent["accepted"] == 1
                    and consent["action_hash"] == action_hash
                    and consent["case_id"] == action_proposal.case_id
                    and question
                    and question.consent_request
                    and question.consent_request.fingerprint() == action_hash
                    and question.version < action_proposal.version
                    and question.id in approvals
                    and matching_delivery
                ):
                    matching_consent = True
        if not matching_consent:
            violations.append("operation_without_matching_client_consent")
    for record in tables["outbox"]:
        if record["status"] == "delivered" and record["proposal_id"] not in approvals:
            violations.append("delivery_without_staff_approval")
    for delivery_id, record in delivered.items():
        queued = outbox.get(delivery_id)
        proposal = proposals.get(queued["proposal_id"]) if queued else None
        if (
            not queued
            or not proposal
            or record["text"] != proposal.text
            or record["text"] != queued["text"]
            or record["case_id"] != proposal.case_id
        ):
            violations.append("delivered_text_differs_from_approved")
    if len(operations) != len(tables["operations"]):
        violations.append("duplicate_operation")
    return violations


def run_attempt(folder, checkpoint, prepared, case, mode, trial, model, limit):
    from support_agent.storage import Store
    from support_agent.workflow import SupportService

    folder.mkdir(parents=True, exist_ok=False)
    database = folder / "application.sqlite3"
    shutil.copyfile(checkpoint, database)
    copied_hash = sha256(database)
    if copied_hash != prepared["checkpoint_sha256"]:
        raise ValueError("Исходная контрольная точка изменилась")
    store = Store(database)
    app = SupportService(store, model, recovery_mode=mode)
    case_id = prepared["case_id"]
    baseline_state = capture_state(store, case_id)
    result = {
        "grader_version": GRADER_VERSION,
        "scenario_id": case["id"],
        "mutation": case["mutation"],
        "mode": mode,
        "trial": trial,
        "checkpoint_sha256": copied_hash,
        "status": "incomplete",
        "correct": False,
        "interventions": 0,
        "technical_complete": False,
    }
    started = time.monotonic()
    try:
        mutate(store, case)
        proposal = store.latest(case_id)["proposal"]
        validate_action(proposal.action, case, changed=False)
        outcome = approve(app, proposal, limit)
        if case["mutation"] == "none":
            if outcome["status"] != "delivered":
                raise ValueError("Неизменённое предложение не исполнено")
        else:
            if outcome["status"] != "stale" or not outcome.get("recovery_id"):
                raise ValueError("Устаревшее предложение не заблокировано")
            if mode == "manual":
                limit.take()
                app.resume(case_id, proposal.id, EMPLOYEE)
            view = app.view(case_id, EMPLOYEE)
            if view["recovery"]["status"] != "prepared":
                raise ValueError("Новая версия не подготовлена")
            fresh = view["proposal"]
            if case["mutation"] == "price":
                validate_action(fresh.consent_request, case, changed=True)
                if approve(app, fresh, limit)["status"] != "delivered":
                    raise ValueError("Новая сумма не доставлена клиенту")
                limit.take()
                final = app.record_client_response(
                    case_id,
                    fresh.id,
                    True,
                    f"Да, подтверждаю обмен той же единственной позиции и новую доплату {expected_amount(case, True)} USD с карты card.",
                    EMPLOYEE,
                )
                validate_action(final.action, case, changed=True)
                if approve(app, final, limit)["status"] != "delivered":
                    raise ValueError("Согласованный обмен не исполнен")
            else:
                text_check = assess_refusal(fresh.text)
                if text_check["status"] == "review":
                    raise GraderReview(
                        "Формулировка отказа требует ручной проверки оценщика"
                    )
                if (
                    fresh.action
                    or fresh.consent_request
                    or not forbidden_explanation(fresh.text)
                ):
                    raise ValueError(
                        "Нет законченного объяснения недопустимости обмена"
                    )
                if approve(app, fresh, limit)["status"] != "delivered":
                    raise ValueError("Отказ не доставлен клиенту")
        result["technical_complete"] = True
    except Exception as error:
        origin = root_error(error)
        result["error_type"] = (
            getattr(model, "failure_type", None) or type(origin).__name__
        )
        # Содержание сетевых исключений может содержать заголовки или ключи.
        result["failure_reason"] = (
            str(error)
            if isinstance(origin, (ValueError, AttemptLimit, BudgetExceeded))
            else type(origin).__name__
        )
        result["technical_complete"] = (
            isinstance(origin, ValueError)
            and not isinstance(origin, GraderReview)
            and not getattr(model, "failure_type", None)
            and not any(c.get("status") == "error" for c in getattr(model, "calls", []))
        )
    state = capture_state(store, case_id)
    events = [e for e in store.events(case_id) if e["id"] > prepared["event_boundary"]]
    counts = Counter(e["kind"] for e in events)
    result["counts"] = {
        name: counts[name]
        for name in (
            "manual_resume",
            "automatic_resume",
            "rejected",
            "approval",
            "consent",
            "customer",
            "operation",
            "sent",
            "conditions_changed",
            "recovery_prepared",
        )
    }
    result["interventions"] = counts["manual_resume"] + counts["rejected"]
    result["violations"] = audit_state(state)
    try:
        result["assessment"] = assess_attempt(
            state, case, violations=result["violations"]
        )
    except Exception as error:
        result["assessment_error_type"] = type(error).__name__
        result["technical_complete"] = False
        result["assessment"] = {
            "version": GRADER_VERSION,
            "complete": False,
            "checks": {
                name: {"status": "review", "reasons": ["evaluator_exception"]}
                for name in (
                    "action_admissibility",
                    "reply_content",
                    "delivery",
                    "final_state",
                )
            },
        }
    order = state["retail"]["orders"][case["order_id"]]
    if case["mutation"] == "status":
        state_correct = (
            order["status"] == "cancelled"
            and not state["tables"]["operations"]
            and counts["sent"] == 1
        )
    else:
        state_correct = (
            order["status"] == "exchange requested"
            and order["exchange_items"] == ["old"]
            and order["exchange_new_items"] == ["new"]
            and order["exchange_payment_method_id"] == "card"
            and Decimal(str(order["exchange_price_difference"]))
            == expected_amount(case, True)
            and counts["operation"] == 1
            and counts["sent"] == (2 if case["mutation"] == "price" else 1)
            and counts["approval"] == (2 if case["mutation"] == "price" else 1)
            and counts["consent"] == (1 if case["mutation"] == "price" else 0)
        )
    result["correct"] = (
        state_correct
        and result["assessment"]["complete"]
        and not result["violations"]
        and "error_type" not in result
    )
    result["error_category"] = (
        None
        if result["correct"]
        else "evaluator"
        if result.get("assessment_error_type")
        else "limit"
        if result.get("error_type") in {"BudgetExceeded", "AttemptLimit"}
        else "evaluator"
        if result.get("error_type") == "GraderReview"
        else "external"
        if getattr(model, "failure_type", None)
        else "application"
        if result["violations"]
        else "unclassified"
    )
    result["metrics"] = evaluate_interventions(
        state,
        events,
        baseline_state=baseline_state,
        correct=None
        if result.get("assessment_error_type")
        or any(
            check["status"] == "review"
            for check in result["assessment"]["checks"].values()
        )
        else result["correct"],
        error_category=result["error_category"],
    )
    result["status"] = (
        "success"
        if result["correct"]
        else "incomplete"
        if not result["technical_complete"]
        else "behavior_failure"
    )
    result["elapsed_seconds"] = round(time.monotonic() - started, 6)
    result["steps"] = limit.steps
    result["events"] = events
    result["model_calls"] = len(getattr(model, "calls", []))
    result["api_calls"] = (
        0 if getattr(model, "offline", False) else result["model_calls"]
    )
    result["model_seconds"] = sum(
        c.get("elapsed_seconds", 0) for c in getattr(model, "calls", [])
    )
    write_json(folder / "state.json", state)
    write_json(folder / "result.json", result)
    return result


def pair_results(attempts):
    grouped = defaultdict(dict)
    for row in attempts:
        grouped[(row["scenario_id"], row["trial"])][row["mode"]] = row
    pairs = []
    for (case_id, trial), modes in grouped.items():
        manual, auto = modes.get("manual"), modes.get("auto")
        same = bool(
            manual and auto and manual["checkpoint_sha256"] == auto["checkpoint_sha256"]
        )
        successful = same and manual["correct"] and auto["correct"]
        pairs.append(
            {
                "scenario_id": case_id,
                "trial": trial,
                "mutation": (manual or auto)["mutation"],
                "same_checkpoint": same,
                "comparable_success": bool(successful),
                "intervention_difference": manual["interventions"]
                - auto["interventions"]
                if successful
                else None,
                "manual_status": manual["status"] if manual else "not_run",
                "auto_status": auto["status"] if auto else "not_run",
            }
        )
    return pairs


def require_pilot(report):
    if not (
        report.get("kind") == "live"
        and report.get("phase") == "pilot"
        and report.get("attempt_count") == 6
        and report.get("technical_ready")
    ):
        raise ValueError(
            "Основной прогон требует завершённого технически готового оплачиваемого пилота"
        )


def provenance():
    paths = (
        sorted((PROJECT_ROOT / "src").rglob("*.py"))
        + sorted(
            p
            for p in HERE.iterdir()
            if p.is_file() and p.suffix in (".py", ".json", ".md")
        )
        + [
            PROJECT_ROOT / "evals/core-integration/run_control.py",
            PROJECT_ROOT / "pyproject.toml",
            PROJECT_ROOT / "uv.lock",
        ]
    )
    return {
        str(p.relative_to(PROJECT_ROOT)).replace("\\", "/"): sha256(p) for p in paths
    }


def run_phase(root, phase, *, offline=False, budget=None):
    from support_agent.adapters.llm import LiteLLMClient

    root = Path(root)
    destination = root / ("offline" if offline else phase)
    destination.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": 1,
        "grader_version": GRADER_VERSION,
        "kind": "offline" if offline else "live",
        "phase": phase,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "human_measurement": False,
        "client_and_staff": "fixed scripted decisions; no LLM user or judge",
        "model": "offline-script" if offline else MODEL,
        "model_args": None if offline else LLM_ARGS,
        "limits": {
            "max_steps_per_whole_attempt": MAX_STEPS,
            "max_seconds_per_whole_attempt": MAX_SECONDS,
            "api_retries": 0,
            "attempt_retries": 0,
        },
        "pricing": None if offline else pricing_for(MODEL),
        "source_sha256": provenance(),
        "python": sys.version,
        "versions": {
            name: importlib.metadata.version(name)
            for name in ("tau2", "litellm", "pydantic", "openai")
        },
        "case_source": "synthetic; based on tests/retail_data.py; no benchmark hidden criteria",
        "scenarios": scenarios(phase),
        "preparations": [],
        "attempts": [],
        "pairs": [],
        "api_calls": 0,
        "attempt_count": 0,
        "success_count": 0,
        "technical_ready": False,
    }
    manifest = destination / "manifest.json"
    write_json(manifest, report)
    repetitions = 1 if phase == "pilot" or offline else 3
    for case in report["scenarios"]:
        checkpoint_dir = destination / "preparations" / case["id"]
        checkpoint_dir.mkdir(parents=True)
        checkpoint = checkpoint_dir / "checkpoint.sqlite3"
        limit = Limit()
        inner = OfflineModel(case) if offline else LiteLLMClient(MODEL, LLM_ARGS)
        model = TrackedModel(
            inner,
            path=checkpoint_dir / "calls.json",
            label=f"{phase}/prepare/{case['id']}",
            limit=limit,
            budget=budget,
            offline=offline,
        )
        preparation = {"scenario_id": case["id"], "status": "incomplete"}
        started = time.monotonic()
        try:
            prepared = prepare_checkpoint(checkpoint, case, model, limit)
            preparation.update(prepared, status="prepared")
        except Exception as error:
            preparation["error_type"] = type(root_error(error)).__name__
            report["stop_reason"] = (
                "budget_limit"
                if isinstance(root_error(error), BudgetExceeded)
                else "preparation_failed"
            )
            prepared = None
        preparation.update(
            model_calls=len(model.calls),
            api_calls=0 if offline else len(model.calls),
            elapsed_seconds=round(time.monotonic() - started, 6),
        )
        report["preparations"].append(preparation)
        write_json(checkpoint_dir / "preparation.json", preparation)
        write_json(manifest, report)
        if prepared is None:
            break
        for trial in range(repetitions):
            # Чередование порядка закреплено заранее; обе копии получают один снимок.
            modes = ("manual", "auto") if trial % 2 == 0 else ("auto", "manual")
            for mode in modes:
                folder = destination / "attempts" / f"{case['id']}-{trial}-{mode}"
                limit = Limit()
                inner = (
                    OfflineModel(case) if offline else LiteLLMClient(MODEL, LLM_ARGS)
                )
                model = TrackedModel(
                    inner,
                    path=folder / "calls.json",
                    label=f"{phase}/{case['id']}/{trial}/{mode}",
                    limit=limit,
                    budget=budget,
                    offline=offline,
                )
                row = run_attempt(
                    folder, checkpoint, prepared, case, mode, trial, model, limit
                )
                report["attempts"].append(row)
                write_json(manifest, report)
                if row.get("error_type") == "BudgetExceeded" or (
                    budget is not None
                    and (
                        budget.used >= budget.data["limit_usd"]
                        or budget.data.get("halted")
                    )
                ):
                    report["stop_reason"] = "budget_limit"
                    break
            if report.get("stop_reason"):
                break
        if report.get("stop_reason"):
            break
    report["attempt_count"] = len(report["attempts"])
    report["success_count"] = sum(a["correct"] for a in report["attempts"])
    report["metrics"] = summarize_interventions(
        [a["metrics"] for a in report["attempts"]]
    )
    report["api_calls"] = sum(p["api_calls"] for p in report["preparations"]) + sum(
        a["api_calls"] for a in report["attempts"]
    )
    report["pairs"] = pair_results(report["attempts"])
    successful_pairs = [p for p in report["pairs"] if p["comparable_success"]]
    report["paired_summary"] = {
        "successful_pairs": len(successful_pairs),
        "incomplete_or_failed_pairs": [
            p for p in report["pairs"] if not p["comparable_success"]
        ],
        "interventions_saved_total": sum(
            p["intervention_difference"] for p in successful_pairs
        ),
        "by_mutation": {
            kind: {
                "successful_pairs": sum(
                    p["mutation"] == kind for p in successful_pairs
                ),
                "interventions_saved": sum(
                    p["intervention_difference"]
                    for p in successful_pairs
                    if p["mutation"] == kind
                ),
            }
            for kind in ("none", "price", "status")
        },
    }
    report["technical_ready"] = len(report["attempts"]) == 3 * repetitions * 2 and all(
        a["technical_complete"] for a in report["attempts"]
    )
    report["budget"] = (
        None
        if budget is None
        else {
            "limit_usd": budget.data["limit_usd"],
            "used_bound_usd_all_phases": budget.used,
        }
    )
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["source_unchanged"] = report["source_sha256"] == provenance()
    report["status"] = (
        "completed" if report["attempt_count"] == 3 * repetitions * 2 else "incomplete"
    )
    write_json(manifest, report)
    return report


@contextmanager
def exclusive_run(root):
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".running"
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        os.close(descriptor)
        yield
    finally:
        lock.unlink(missing_ok=True)


def control_helpers():
    spec = importlib.util.spec_from_file_location(
        "reapproval_control_helpers",
        PROJECT_ROOT / "evals/core-integration/run_control.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--offline", action="store_true", help="Проверка проводки с заглушкой, не пилот"
    )
    mode.add_argument(
        "--run",
        action="store_true",
        help="Оплачиваемый прогон; только после согласования",
    )
    parser.add_argument("--phase", choices=("pilot", "main"), default="pilot")
    parser.add_argument("--budget-usd", type=float)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--repo", type=Path, help="tau2-bench: источник .env, читается только при --run"
    )
    args = parser.parse_args(argv)
    if args.offline and args.phase != "pilot":
        parser.error("--offline не является основным измерением")
    if args.run and (args.budget_usd is None or args.repo is None):
        parser.error("--run требует --budget-usd и --repo")
    with exclusive_run(args.run_dir):
        if args.offline:
            report = run_phase(args.run_dir, "pilot", offline=True)
        else:
            if args.phase == "main":
                pilot = json.loads(
                    (args.run_dir / "pilot/manifest.json").read_text(encoding="utf-8")
                )
                require_pilot(pilot)
                if pilot["source_sha256"] != provenance():
                    raise ValueError(
                        "Код или случаи изменились после пилота; готовность требует отдельной проверки"
                    )
            budget = Budget(args.run_dir / "budget.json", args.budget_usd)
            helpers = control_helpers()
            secrets = helpers.load_credentials(args.repo.resolve())
            with helpers.trusted_tls(True):
                try:
                    report = run_phase(args.run_dir, args.phase, budget=budget)
                finally:
                    helpers.redact_artifacts(args.run_dir, secrets)
    print(
        json.dumps(
            {
                "status": report["status"],
                "kind": report["kind"],
                "attempts": report["attempt_count"],
                "successful": report["success_count"],
                "api_calls": report["api_calls"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["technical_ready"] and report["source_unchanged"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Evaluation stopped: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
