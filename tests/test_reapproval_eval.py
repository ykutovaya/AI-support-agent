"""Проверки измерения пересогласования без обращений к API."""

import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("tau2")

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "reapproval_eval", ROOT / "evals/reapproval/run_reapproval.py"
)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def test_budget_prevents_call_and_retains_uncertain_expense(tmp_path):
    budget = evaluation.Budget(tmp_path / "budget.json", 0.02)
    reservation = budget.reserve(0.015, "pilot/price/manual")
    budget.finish(reservation, None, "transport_error")
    restored = evaluation.Budget(tmp_path / "budget.json", 0.02)
    with pytest.raises(evaluation.BudgetExceeded):
        restored.reserve(0.006, "pilot/price/auto")
    assert len(restored.data["calls"]) == 1
    assert restored.used == pytest.approx(0.015)


def test_unverified_model_cannot_use_pinned_prices():
    with pytest.raises(ValueError, match="цен"):
        evaluation.pricing_for("unlisted/model")


@pytest.mark.parametrize(
    "text,accepted",
    [
        (
            "Статус заказа #P103 изменился на «отменён». По правилам обмен возможен "
            "только для доставленного заказа, поэтому обмен и доплата не будут оформлены. "
            "Ваше подтверждение условий не привело к выполнению операции.",
            True,
        ),
        ("Заказ отменён. Обмен не будет оформлен.", True),
        ("Заказ отменён. Обмен не будут оформлять.", True),
        ("Заказ отменён. Обмен невозможен. Подтвердите другой обмен.", False),
        (
            "Заказ отменён. Обмен и доплата не будут оформлены. Хотите другой товар?",
            False,
        ),
        ("Заказ отменён, однако обмен выполнен и заявка создана.", False),
        ("Заказ отменён. Обмен не будет оформлен. Заявка создана.", False),
        ("Заказ отменён. Обмен недоступен. Операция выполнена.", False),
        ("Заказ отменён. Обмен будет оформлен.", False),
    ],
)
def test_forbidden_explanation_accepts_explicit_refusal_without_success_or_new_consent(
    text, accepted
):
    assert evaluation.forbidden_explanation(text) is accepted


def test_reservation_blocks_delegate_before_any_request(tmp_path):
    from support_agent.models import Message

    class NeverCall:
        llm_args = {}

        def generate(self, messages, tools):
            pytest.fail("Исчерпанный бюджет не должен допускать запрос")

    model = evaluation.TrackedModel(
        NeverCall(),
        path=tmp_path / "calls.json",
        label="pilot/test/manual",
        limit=evaluation.Limit(),
        budget=evaluation.Budget(tmp_path / "budget.json", 0.00001),
    )
    with pytest.raises(evaluation.BudgetExceeded):
        model.generate([Message(role="user", content="Обмен")], [])
    assert model.calls == []


def test_unknown_usage_keeps_reserved_cost_and_retries_disabled(tmp_path):
    from support_agent.models import Generation, Message

    class Answer:
        llm_args = {}

        def generate(self, messages, tools):
            return Generation(message=Message(role="assistant", content="Ответ"))

    budget = evaluation.Budget(tmp_path / "budget.json", 1)
    model = evaluation.TrackedModel(
        Answer(),
        path=tmp_path / "calls.json",
        label="pilot/test/manual",
        limit=evaluation.Limit(),
        budget=budget,
    )
    model.generate([Message(role="user", content="Обмен")], [])
    assert budget.used == budget.data["calls"][0]["reserved_usd"]
    assert budget.used > 0
    assert evaluation.LLM_ARGS["num_retries"] == 0


def test_unexpected_provider_cost_stops_following_calls(tmp_path):
    budget = evaluation.Budget(tmp_path / "budget.json", 1)
    index = budget.reserve(0.01, "first")
    with pytest.raises(evaluation.BudgetExceeded):
        budget.finish(index, 0.02, "received")
    restored = evaluation.Budget(tmp_path / "budget.json", 1)
    with pytest.raises(evaluation.BudgetExceeded):
        restored.reserve(0.01, "second")


def test_reported_cost_above_reserve_blocks_even_without_usage(tmp_path):
    from support_agent.models import Generation, Message

    class Costly:
        llm_args = {}

        def generate(self, messages, tools):
            return Generation(
                message=Message(role="assistant", content="Ответ"), cost=0.5
            )

    budget = evaluation.Budget(tmp_path / "budget.json", 1)
    model = evaluation.TrackedModel(
        Costly(),
        path=tmp_path / "calls.json",
        label="pilot/test/manual",
        limit=evaluation.Limit(),
        budget=budget,
    )
    with pytest.raises(evaluation.BudgetExceeded):
        model.generate([Message(role="user", content="Обмен")], [])
    assert budget.used == 0.5


def test_auto_recovery_budget_stop_is_not_model_failure_or_readiness(tmp_path):
    scenario = evaluation.scenarios("pilot")[1]
    checkpoint = tmp_path / "checkpoint.sqlite3"
    prepared = evaluation.prepare_checkpoint(
        checkpoint, scenario, evaluation.OfflineModel(scenario)
    )

    class NeverCall:
        llm_args = {}

        def generate(self, messages, tools):
            pytest.fail("Запрос после запрета бюджета")

    limit = evaluation.Limit()
    model = evaluation.TrackedModel(
        NeverCall(),
        path=tmp_path / "attempt/calls.json",
        label="pilot/price/auto",
        limit=limit,
        budget=evaluation.Budget(tmp_path / "budget.json", 0.00001),
    )
    result = evaluation.run_attempt(
        tmp_path / "attempt",
        checkpoint,
        prepared,
        scenario,
        "auto",
        0,
        model,
        limit,
    )
    assert result["error_type"] == "BudgetExceeded"
    assert result["status"] == "incomplete"
    assert result["technical_complete"] is False
    assert result["api_calls"] == 0


def test_main_requires_live_complete_pilot_not_offline_or_positive_score():
    with pytest.raises(ValueError):
        evaluation.require_pilot({"kind": "offline", "technical_ready": True})
    evaluation.require_pilot(
        {
            "kind": "live",
            "phase": "pilot",
            "attempt_count": 6,
            "technical_ready": True,
            "success_count": 0,
        }
    )


def test_fixed_sets_are_distinct_and_cover_three_changes():
    pilot = evaluation.scenarios("pilot")
    main = evaluation.scenarios("main")
    assert {c["mutation"] for c in pilot} == {"none", "price", "status"}
    assert {c["mutation"] for c in main} == {"none", "price", "status"}
    assert {c["id"] for c in pilot}.isdisjoint(c["id"] for c in main)
    assert {c["order_id"] for c in pilot}.isdisjoint(c["order_id"] for c in main)


def test_staff_rejects_wrong_payment_before_approval(tmp_path):
    scenario = evaluation.scenarios("pilot")[0]
    prepared = evaluation.prepare_checkpoint(
        tmp_path / "checkpoint.sqlite3",
        scenario,
        evaluation.OfflineModel(scenario),
    )
    from support_agent.storage import Store

    proposal = Store(tmp_path / "checkpoint.sqlite3").latest(prepared["case_id"])[
        "proposal"
    ]
    altered = proposal.action.model_copy(deep=True)
    altered.arguments["payment_method_id"] = "other_card"
    with pytest.raises(ValueError, match="услов"):
        evaluation.validate_action(altered, scenario, changed=False)
    with Store(tmp_path / "checkpoint.sqlite3").connection() as conn:
        assert conn.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


def test_offline_six_attempts_replay_real_executor_from_identical_checkpoints(tmp_path):
    report = evaluation.run_phase(tmp_path, "pilot", offline=True)
    assert report["kind"] == "offline"
    assert report["attempt_count"] == 6
    assert report["success_count"] == 6
    assert report["api_calls"] == 0
    assert report["human_measurement"] is False
    pairs = report["pairs"]
    assert len(pairs) == 3
    assert all(p["same_checkpoint"] for p in pairs)
    assert {p["mutation"]: p["intervention_difference"] for p in pairs} == {
        "none": 0,
        "price": 1,
        "status": 1,
    }
    price = [a for a in report["attempts"] if a["mutation"] == "price"]
    assert all(a["counts"]["consent"] == 1 for a in price)
    assert all(a["counts"]["approval"] == 2 for a in price)
    assert all(a["counts"]["operation"] == 1 for a in price)
    forbidden = [a for a in report["attempts"] if a["mutation"] == "status"]
    assert all(a["counts"]["operation"] == 0 for a in forbidden)
    assert all(a["counts"]["sent"] == 1 for a in forbidden)
    saved = json.loads(
        (tmp_path / "offline" / "manifest.json").read_text(encoding="utf-8")
    )
    assert saved["attempt_count"] == 6


def test_failure_pair_is_not_counted_as_saved_work():
    rows = [
        {
            "scenario_id": "p",
            "trial": 0,
            "mode": "manual",
            "mutation": "price",
            "status": "incomplete",
            "correct": False,
            "interventions": 0,
            "checkpoint_sha256": "x",
        },
        {
            "scenario_id": "p",
            "trial": 0,
            "mode": "auto",
            "mutation": "price",
            "status": "success",
            "correct": True,
            "interventions": 0,
            "checkpoint_sha256": "x",
        },
    ]
    pair = evaluation.pair_results(rows)[0]
    assert pair["comparable_success"] is False
    assert pair["intervention_difference"] is None


@pytest.mark.parametrize(
    "damage,expected",
    [
        ("text", "delivered_text_differs_from_approved"),
        ("digest", "approval_digest_mismatch"),
        ("consent", "operation_without_matching_client_consent"),
    ],
)
def test_audit_detects_unapproved_text_digest_and_missing_consent(
    tmp_path, damage, expected
):
    from support_agent.storage import Store
    from support_agent.workflow import SupportService

    scenario = evaluation.scenarios("pilot")[0]
    checkpoint = tmp_path / "checkpoint.sqlite3"
    prepared = evaluation.prepare_checkpoint(
        checkpoint, scenario, evaluation.OfflineModel(scenario)
    )
    store = Store(checkpoint)
    app = SupportService(store, evaluation.OfflineModel(scenario))
    proposal = store.latest(prepared["case_id"])["proposal"]
    evaluation.approve(app, proposal)
    state = evaluation.capture_state(store, prepared["case_id"])
    assert evaluation.audit_state(state) == []
    if damage == "text":
        state["tables"]["deliveries"][-1]["text"] = "Другой ответ"
    elif damage == "digest":
        state["tables"]["approvals"][-1]["digest"] = "wrong"
    else:
        state["tables"]["consents"] = []
    assert expected in evaluation.audit_state(state)
