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


def test_reservation_is_durable_and_blocks_overspending(tmp_path):
    ledger = tmp_path / "budget.json"
    budget = runtime.Budget(ledger, 0.01)
    first = budget.reserve(0.008, "first")
    assert json.loads(ledger.read_text())["calls"][first]["status"] == "reserved"
    with pytest.raises(runtime.BudgetLimit):
        budget.reserve(0.003, "second")
    budget.finish(first, None, "unknown")
    assert runtime.Budget(ledger, 0.01).used == pytest.approx(0.008)


def test_lost_response_is_saved_and_not_retried(tmp_path):
    from support_agent.models import Message

    class Failing:
        llm_args = {}
        calls = 0

        def generate(self, messages, tools):
            self.calls += 1
            raise ConnectionError("fake credential must not enter report")

    inner = Failing()
    budget = runtime.Budget(tmp_path / "budget.json", 1)
    model = runtime.TrackedModel(
        inner,
        path=tmp_path / "calls.json",
        label="attempt",
        budget=budget,
        limit=runtime.Limit(),
    )
    with pytest.raises(ConnectionError):
        model.generate([Message(role="user", content="hello")], [])
    report = (tmp_path / "calls.json").read_text()
    assert "ConnectionError" in report
    assert "fake credential" not in report
    assert inner.calls == 1 and budget.used > 0


def test_attempt_allocation_survives_restart_and_limits_repeats(tmp_path):
    registry = runtime.AttemptRegistry(tmp_path / "attempts.json")
    for case in range(4):
        registry.reserve("development", str(case))
    with pytest.raises(runtime.RuntimeLimit):
        registry.reserve("development", "extra")
    for case in range(6):
        registry.reserve("control", str(case))
    registry.reserve("connection", "connection")
    for repeat in range(4):
        registry.reserve("repeat", str(repeat))
    restored = runtime.AttemptRegistry(tmp_path / "attempts.json")
    with pytest.raises(runtime.RuntimeLimit):
        restored.reserve("repeat", "fifth")
    assert len(restored.rows) == 15


def test_usage_above_reservation_stops_new_calls(tmp_path):
    budget = runtime.Budget(tmp_path / "budget.json", 1)
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
        budget=runtime.Budget(tmp_path / "budget.json", 1),
        limit=Expired(),
    )
    with pytest.raises(runtime.RuntimeLimit):
        model.generate([Message(role="user", content="hello")], [])
