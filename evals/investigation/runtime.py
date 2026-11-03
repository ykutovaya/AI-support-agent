"""Ограничения новой серии: расходы, попытки и запись вызовов до их выполнения."""

import json
import math
import time
from copy import deepcopy
from pathlib import Path

MODEL = "openai/responses/gpt-6-luna"
LLM_ARGS = {
    "reasoning_effort": "low",
    "max_tokens": 8192,
    "timeout": 90,
    "num_retries": 0,
    "service_tier": "default",
}
PRICING = {
    "source": "https://developers.openai.com/api/docs/models/gpt-6-luna",
    "verified_on": "2026-11-03",
    "currency": "USD",
    "input_per_million": 0.10,
    "output_per_million": 0.50,
    "long_context_threshold": 272000,
    "long_input_multiplier": 2,
    "long_output_multiplier": 1.5,
    "cached_input": "скидка не вычитается из верхней оценки",
}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


class RuntimeLimit(RuntimeError):
    """Исчерпан предел шагов, времени или числа попыток."""


class BudgetLimit(RuntimeLimit):
    """Остатка бюджета недостаточно для верхней оценки следующего запроса."""


class Limit:
    def __init__(self, max_steps=60, max_seconds=600):
        self.started = time.monotonic()
        self.max_steps, self.max_seconds = max_steps, max_seconds
        self.steps = 0

    @property
    def remaining(self):
        return self.max_seconds - (time.monotonic() - self.started)

    def take(self, count=1):
        if self.remaining <= 0 or self.steps + count > self.max_steps:
            raise RuntimeLimit("Исчерпан предел попытки")
        self.steps += count


def _positive_finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _authorization(value):
    """Проверить явно переданные пределы, не подставляя прежнее разрешение."""
    if not isinstance(value, dict):
        raise ValueError("Нужно явное разрешение для текущей серии")
    caps = value.get("phase_caps")
    case_ids = value.get("case_ids")
    if (
        not _positive_finite(value.get("max_usd"))
        or type(value.get("max_attempts")) is not int
        or value["max_attempts"] <= 0
        or not isinstance(caps, dict)
        or not caps
        or any(
            not isinstance(phase, str) or not phase or type(cap) is not int or cap < 0
            for phase, cap in caps.items()
        )
        or not isinstance(case_ids, list)
        or not case_ids
        or any(not isinstance(case_id, str) or not case_id for case_id in case_ids)
        or len(set(case_ids)) != len(case_ids)
        or not isinstance(value.get("scope"), str)
        or not value["scope"]
    ):
        raise ValueError("Некорректные пределы или выборка текущей серии")
    return deepcopy(value)


class AttemptRegistry:
    def __init__(self, path, *, authorization):
        self.path = Path(path)
        self.authorization = _authorization(authorization)
        self.data = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else {"authorization": self.authorization, "attempts": []}
        )
        if (
            not isinstance(self.data, dict)
            or self.data.get("authorization") != self.authorization
            or not isinstance(self.data.get("attempts"), list)
        ):
            raise ValueError("Разрешение сохранённой серии менять нельзя")
        self.rows = self.data["attempts"]
        if not self.path.exists():
            write_json(self.path, self.data)

    def reserve(self, phase, case_id):
        caps = self.authorization["phase_caps"]
        if phase not in caps:
            raise ValueError("Этап не разрешён для текущей серии")
        allowed_case = (
            case_id == "connection"
            if phase == "connection"
            else case_id in self.authorization["case_ids"]
        )
        if not allowed_case:
            raise ValueError("Случай не входит в согласованную выборку")
        if (
            len(self.rows) >= self.authorization["max_attempts"]
            or sum(r["phase"] == phase for r in self.rows) >= caps[phase]
        ):
            raise RuntimeLimit("Исчерпано согласованное число попыток")
        if phase != "repeat" and any(r["case_id"] == case_id for r in self.rows):
            raise RuntimeLimit("Повтор нужно учитывать отдельно")
        row = {
            "number": len(self.rows) + 1,
            "phase": phase,
            "case_id": case_id,
            "status": "reserved",
        }
        self.rows.append(row)
        write_json(self.path, self.data)
        return row

    def finish(self, number, status):
        self.rows[number - 1]["status"] = status
        write_json(self.path, self.data)


class Budget:
    def __init__(self, path, limit, *, authorization):
        self.path = Path(path)
        allowed = _authorization(authorization)
        if not _positive_finite(limit) or limit != allowed["max_usd"]:
            raise ValueError("Лимит должен совпадать с разрешением текущей серии")
        self.data = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else {
                "authorization": allowed,
                "limit_usd": float(limit),
                "model": MODEL,
                "pricing": PRICING,
                "calls": [],
            }
        )
        if (
            not isinstance(self.data, dict)
            or self.data.get("authorization") != allowed
            or self.data.get("limit_usd") != float(limit)
            or self.data.get("model") != MODEL
        ):
            raise ValueError("Настройки сохранённого бюджета менять нельзя")
        if not self.path.exists():
            write_json(self.path, self.data)

    @property
    def used(self):
        return sum(c["charged_bound_usd"] for c in self.data["calls"])

    def reserve(self, amount, label):
        if (
            self.data.get("halted")
            or amount < 0
            or self.used + amount > self.data["limit_usd"]
        ):
            raise BudgetLimit("Недостаточно остатка для следующего запроса")
        index = len(self.data["calls"])
        self.data["calls"].append(
            {
                "id": index,
                "label": label,
                "reserved_usd": amount,
                "charged_bound_usd": amount,
                "status": "reserved",
            }
        )
        write_json(self.path, self.data)
        return index

    def finish(self, index, measured, status):
        row = self.data["calls"][index]
        row["status"] = status
        if measured is not None:
            row["charged_bound_usd"] = measured
            if measured > row["reserved_usd"] + 1e-9:
                self.data["halted"] = "reservation_exceeded"
        write_json(self.path, self.data)
        if self.data.get("halted"):
            raise BudgetLimit("Расход превысил резерв; следующие запросы остановлены")


def reservation(messages, tools):
    from support_agent.adapters.llm import provider_message

    payload = {
        "messages": [provider_message(m) for m in messages],
        "tools": [t.model_dump(mode="json") for t in tools],
    }
    upper = len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 8192
    return (
        upper * PRICING["input_per_million"] * 2
        + LLM_ARGS["max_tokens"] * PRICING["output_per_million"] * 1.5
    ) / 1_000_000


def usage_cost(generation):
    usage = generation.usage or {}
    incoming = usage.get("prompt_tokens", usage.get("input_tokens"))
    outgoing = usage.get("completion_tokens", usage.get("output_tokens"))
    if (
        type(incoming) is not int
        or type(outgoing) is not int
        or min(incoming, outgoing) < 0
    ):
        return None
    large = incoming > PRICING["long_context_threshold"]
    bound = (
        incoming * PRICING["input_per_million"] * (2 if large else 1)
        + outgoing * PRICING["output_per_million"] * (1.5 if large else 1)
    ) / 1_000_000
    return max(bound, generation.cost or 0)


class TrackedModel:
    def __init__(self, inner, *, path, label, budget, limit):
        self.inner, self.path, self.label = inner, Path(path), label
        if self.path.exists():
            raise ValueError("Журнал вызовов уже существует; нужна новая попытка")
        self.budget, self.limit = budget, limit
        self.calls = []
        self.failure_type = None

    def generate(self, messages, tools):
        self.limit.take()
        reserve = reservation(messages, tools)
        index = self.budget.reserve(reserve, self.label)
        row = {
            "index": len(self.calls),
            "model": MODEL,
            "reserved_usd": reserve,
            "request": {
                "messages": [m.model_dump(mode="json") for m in messages],
                "tools": [t.model_dump(mode="json") for t in tools],
            },
            "status": "started",
        }
        self.calls.append(row)
        write_json(self.path, self.calls)
        self.inner.llm_args["timeout"] = min(90, self.limit.remaining)
        started = time.monotonic()
        try:
            if self.limit.remaining < 1:
                self.budget.finish(index, 0, "not_sent_limit")
                raise RuntimeLimit("Время истекло до отправки запроса")
            generation = self.inner.generate(messages, tools)
            measured = usage_cost(generation)
            row.update(
                status="received",
                message=generation.message.model_dump(mode="json"),
                usage=generation.usage,
                raw_response=generation.raw_data,
                provider_reported_cost=generation.cost,
                conservative_usage_cost_usd=measured,
            )
            self.budget.finish(
                index, measured, "received" if measured is not None else "unknown_usage"
            )
            self.limit.take(len(generation.message.tool_calls or []))
            return generation
        except BaseException as error:
            self.failure_type = type(error).__name__
            row.update(status="error", error_type=type(error).__name__)
            if self.budget.data["calls"][index]["status"] == "reserved":
                self.budget.finish(index, None, "unknown")
            raise
        finally:
            row["elapsed_seconds"] = round(time.monotonic() - started, 6)
            write_json(self.path, self.calls)
