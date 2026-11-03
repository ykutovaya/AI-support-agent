"""Пределы оплачиваемых вызовов и сохранение неопределённого расхода."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "investigation_runtime", ROOT / "evals/investigation/runtime.py"
)
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


def authorization(max_usd=1, *, max_attempts=4, phase_caps=None):
    return {
        "scope": "known_case_regression",
        "max_usd": max_usd,
        "max_attempts": max_attempts,
        "phase_caps": phase_caps or {"connection": 1, "regression": 3, "repeat": 0},
        "case_ids": ["control-identity", "control-intent", "control-price"],
    }


def test_reservation_is_durable_and_blocks_overspending(tmp_path):
    ledger = tmp_path / "budget.json"
    allowed = authorization(0.01)
    budget = runtime.Budget(ledger, 0.01, authorization=allowed)
    first = budget.reserve(0.008, "first")
    assert (
        json.loads(ledger.read_text(encoding="utf-8"))["calls"][first]["status"]
        == "reserved"
    )
    with pytest.raises(runtime.BudgetLimit):
        budget.reserve(0.003, "second")
    budget.finish(first, None, "unknown")
    assert runtime.Budget(ledger, 0.01, authorization=allowed).used == pytest.approx(
        0.008
    )


def test_lost_response_is_saved_and_not_retried(tmp_path):
    from support_agent.models import Message

    class Failing:
        llm_args = {}
        calls = 0

        def generate(self, messages, tools):
            self.calls += 1
            raise ConnectionError("fake credential must not enter report")

    inner = Failing()
    budget = runtime.Budget(tmp_path / "budget.json", 1, authorization=authorization())
    model = runtime.TrackedModel(
        inner,
        path=tmp_path / "calls.json",
        label="attempt",
        budget=budget,
        limit=runtime.Limit(),
    )
    with pytest.raises(ConnectionError):
        model.generate([Message(role="user", content="hello")], [])
    report = (tmp_path / "calls.json").read_text(encoding="utf-8")
    assert "ConnectionError" in report
    assert "fake credential" not in report
    assert inner.calls == 1 and budget.used > 0


def test_attempt_allocation_survives_restart_and_limits_repeats(tmp_path):
    path = tmp_path / "attempts.json"
    allowed = authorization()
    registry = runtime.AttemptRegistry(path, authorization=allowed)
    for case_id in allowed["case_ids"]:
        registry.reserve("regression", case_id)
    registry.reserve("connection", "connection")
    restored = runtime.AttemptRegistry(path, authorization=allowed)
    with pytest.raises(runtime.RuntimeLimit):
        restored.reserve("repeat", "control-price")
    assert len(restored.rows) == 4
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["authorization"] == allowed
    assert [row["case_id"] for row in restored.rows] == [
        *allowed["case_ids"],
        "connection",
    ]


@pytest.mark.parametrize(
    ("phase", "case_id"),
    [
        ("development", "control-price"),
        ("control", "control-price"),
        ("regression", "control-return"),
        ("connection", "control-price"),
        ("regression", "connection"),
    ],
)
def test_attempt_rejects_unapproved_phase_or_case(tmp_path, phase, case_id):
    registry = runtime.AttemptRegistry(
        tmp_path / "attempts.json", authorization=authorization()
    )
    with pytest.raises(ValueError):
        registry.reserve(phase, case_id)
    assert registry.rows == []


@pytest.mark.parametrize(
    "allowed",
    [
        authorization(max_attempts=1),
        authorization(phase_caps={"regression": 1, "repeat": 0}),
    ],
    ids=["total_limit", "phase_limit"],
)
def test_attempt_applies_total_and_phase_limits(tmp_path, allowed):
    registry = runtime.AttemptRegistry(
        tmp_path / "attempts.json", authorization=allowed
    )
    registry.reserve("regression", "control-identity")
    with pytest.raises(runtime.RuntimeLimit):
        registry.reserve("regression", "control-price")
    assert len(registry.rows) == 1


def test_attempt_needs_explicit_repeat_and_repeat_allowance(tmp_path):
    allowed = authorization(
        phase_caps={"connection": 1, "regression": 3, "repeat": 1}, max_attempts=5
    )
    registry = runtime.AttemptRegistry(
        tmp_path / "attempts.json", authorization=allowed
    )
    registry.reserve("regression", "control-price")
    with pytest.raises(runtime.RuntimeLimit):
        registry.reserve("regression", "control-price")
    registry.reserve("repeat", "control-price")
    with pytest.raises(runtime.RuntimeLimit):
        registry.reserve("repeat", "control-price")
    assert [row["phase"] for row in registry.rows] == ["regression", "repeat"]


@pytest.mark.parametrize("resource", ["attempts", "budget"])
def test_saved_authorization_cannot_be_replaced(tmp_path, resource):
    path = tmp_path / f"{resource}.json"
    allowed = authorization()
    if resource == "attempts":
        registry = runtime.AttemptRegistry(path, authorization=allowed)
        registry.reserve("regression", "control-price")
    else:
        budget = runtime.Budget(path, 1, authorization=allowed)
        budget.reserve(0.01, "first")
    saved = path.read_bytes()
    changed = authorization(max_attempts=5)
    with pytest.raises(ValueError):
        if resource == "attempts":
            runtime.AttemptRegistry(path, authorization=changed)
        else:
            runtime.Budget(path, 1, authorization=changed)
    assert path.read_bytes() == saved


def test_budget_uses_explicit_limit_instead_of_historical_five_dollars(tmp_path):
    path = tmp_path / "budget.json"
    allowed = authorization(10)
    budget = runtime.Budget(path, 10, authorization=allowed)
    budget.reserve(6, "first")
    with pytest.raises(runtime.BudgetLimit):
        budget.reserve(5, "second")
    assert json.loads(path.read_text(encoding="utf-8"))["authorization"] == allowed


@pytest.mark.parametrize("limit", [0, -1, float("nan"), float("inf"), True])
def test_budget_rejects_invalid_authorized_limit(tmp_path, limit):
    with pytest.raises(ValueError):
        runtime.Budget(
            tmp_path / "budget.json", limit, authorization=authorization(limit)
        )


def test_budget_rejects_limit_different_from_authorization(tmp_path):
    with pytest.raises(ValueError):
        runtime.Budget(tmp_path / "budget.json", 2, authorization=authorization(1))


def test_usage_above_reservation_stops_new_calls(tmp_path):
    budget = runtime.Budget(tmp_path / "budget.json", 1, authorization=authorization())
    index = budget.reserve(0.01, "attempt")
    with pytest.raises(runtime.BudgetLimit):
        budget.finish(index, 0.02, "received")
    with pytest.raises(runtime.BudgetLimit):
        budget.reserve(0.001, "next")


def test_time_expires_after_reservation_without_starting_request(tmp_path):
    from support_agent.models import Message

    class Expired:
        remaining = 0

        def take(self, count=1):
            pass

    class NeverCalled:
        llm_args = {}

        def generate(self, messages, tools):
            pytest.fail("Вызов после исчерпания времени")

    model = runtime.TrackedModel(
        NeverCalled(),
        path=tmp_path / "calls.json",
        label="expired",
        budget=runtime.Budget(
            tmp_path / "budget.json", 1, authorization=authorization()
        ),
        limit=Expired(),
    )
    with pytest.raises(runtime.RuntimeLimit):
        model.generate([Message(role="user", content="hello")], [])
