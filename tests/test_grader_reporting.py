"""Версия оценщика и метрики сохраняются отдельно от исторических оценок."""

import hashlib
import importlib.util
import json
from pathlib import Path

from test_reapproval_eval import evaluation

ROOT = Path(__file__).parents[1]


def test_grader_exception_is_saved_as_evaluator_failure(tmp_path, monkeypatch):
    scenario = evaluation.scenarios("pilot")[0]
    checkpoint = tmp_path / "checkpoint.sqlite3"
    prepared = evaluation.prepare_checkpoint(
        checkpoint, scenario, evaluation.OfflineModel(scenario)
    )

    def broken(*args, **kwargs):
        raise RuntimeError("Ошибка оценщика")

    monkeypatch.setattr(evaluation, "assess_attempt", broken)
    result = evaluation.run_attempt(
        tmp_path / "attempt",
        checkpoint,
        prepared,
        scenario,
        "manual",
        0,
        evaluation.OfflineModel(scenario),
        evaluation.Limit(),
    )
    assert result["error_category"] == "evaluator"
    assert result["correct"] is False
    assert result["technical_complete"] is False
    assert result["assessment_error_type"] == "RuntimeError"
    assert (tmp_path / "attempt/state.json").is_file()
    assert (tmp_path / "attempt/result.json").is_file()


def test_new_offline_results_have_separate_checks_and_explained_metrics(tmp_path):
    report = evaluation.run_phase(tmp_path, "pilot", offline=True)
    assert report["grader_version"] == evaluation.GRADER_VERSION
    assert report["metrics"]["correct_results"]["denominator"] == 6
    for attempt in report["attempts"]:
        assert attempt["assessment"]["complete"]
        assert set(attempt["assessment"]["checks"]) == {
            "action_admissibility",
            "reply_content",
            "delivery",
            "final_state",
        }
        assert attempt["metrics"]["accepted_without_edits"]["denominator"] >= 1


def test_saved_review_keeps_unsent_refusals_in_all_attempts_without_overwriting(
    tmp_path,
):
    path = ROOT / "evals/reapproval/review_saved.py"
    spec = importlib.util.spec_from_file_location("review_saved", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sources = [
        ROOT / "evals/reapproval/2026-10-31",
        ROOT / "evals/reapproval/2026-10-31-revised",
    ]
    before = {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for source in sources
        for p in source.rglob("*.json")
    }
    report = module.review_saved(tmp_path / "review")
    assert len(report["attempts"]) == 30
    assert report["original_correct"] == {"numerator": 28, "denominator": 30}
    failed = next(
        row for row in report["attempts"] if row["attempt_id"] == "main-status-2-manual"
    )
    assert failed["original_correct"] is False
    assert failed["assessment"]["checks"]["reply_content"]["status"] == "pass"
    assert failed["assessment"]["checks"]["delivery"]["status"] == "fail"
    assert failed["assessment"]["complete"] is False
    assert failed["error_category"] == "evaluator"
    assert before == {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for source in sources
        for p in source.rglob("*.json")
    }
    assert (tmp_path / "review/attempts.csv").is_file()
    assert (
        json.loads((tmp_path / "review/summary.json").read_text(encoding="utf-8"))[
            "api_calls"
        ]
        == 0
    )
