"""Разобрать сохранённые 30 попыток без модели, исполнения и исправления исходных оценок."""

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from assessment import assess_attempt  # noqa: E402
from grading import GRADER_VERSION  # noqa: E402
from metrics import (  # noqa: E402
    METRICS_VERSION,
    evaluate_interventions,
    summarize_interventions,
)
from run_reapproval import PROJECT_ROOT, audit_state, write_json  # noqa: E402

SERIES = (
    "2026-10-31/pilot",
    "2026-10-31-revised/pilot",
    "2026-10-31-revised/main",
)


def error_category(original, assessment):
    if original["correct"] and assessment["complete"]:
        return None
    checks = assessment["checks"]
    if (
        not original["correct"]
        and checks["reply_content"]["status"] == "pass"
        and checks["delivery"]["status"] == "fail"
        and original.get("failure_reason")
        == "Нет законченного объяснения недопустимости обмена"
    ):
        return "evaluator"
    if original.get("violations"):
        return "application"
    if original.get("error_type") in {"BudgetExceeded", "AttemptLimit"}:
        return "limit"
    return "unclassified"


def review_saved(output):
    """Создать новый отчёт; источники только читаются."""
    output = Path(output).resolve()
    for series in SERIES:
        source = (HERE / series.split("/")[0]).resolve()
        if output == source or output.is_relative_to(source):
            raise ValueError("Отчёт нельзя записывать внутрь исторической серии")
    output.mkdir(parents=True, exist_ok=False)
    source_hashes = {}

    def read(path):
        raw = path.read_bytes()
        source_hashes[path.relative_to(PROJECT_ROOT).as_posix()] = hashlib.sha256(
            raw
        ).hexdigest()
        return json.loads(raw)

    attempts = []
    for series in SERIES:
        folder = HERE / series
        manifest = read(folder / "manifest.json")
        for original in manifest["attempts"]:
            scenario = next(
                c for c in manifest["scenarios"] if c["id"] == original["scenario_id"]
            )
            attempt_id = (
                f"{original['scenario_id']}-{original['trial']}-{original['mode']}"
            )
            path = folder / "attempts" / attempt_id
            saved = read(path / "result.json")
            state = read(path / "state.json")
            preparation = read(
                folder / "preparations" / scenario["id"] / "preparation.json"
            )
            if (path / "calls.json").is_file():
                read(path / "calls.json")
            assessment = assess_attempt(state, scenario, violations=audit_state(state))
            category = error_category(saved, assessment)
            metrics = evaluate_interventions(
                state,
                saved["events"],
                baseline_proposal_id=preparation["proposal_id"],
                event_boundary=preparation["event_boundary"],
                correct=None
                if any(c["status"] == "review" for c in assessment["checks"].values())
                else assessment["complete"],
                error_category=category,
            )
            attempts.append(
                {
                    "series": series,
                    "attempt_id": attempt_id,
                    "source": (path / "result.json")
                    .relative_to(PROJECT_ROOT)
                    .as_posix(),
                    "mode": saved["mode"],
                    "mutation": saved["mutation"],
                    "original_correct": saved["correct"],
                    "original_status": saved["status"],
                    "original_failure_reason": saved.get("failure_reason"),
                    "error_category": category,
                    "assessment": assessment,
                    "metrics": metrics,
                }
            )
    report = {
        "grader_version": GRADER_VERSION,
        "metrics_version": METRICS_VERSION,
        "kind": "read_only_review_of_saved_runs",
        "api_calls": 0,
        "new_attempts": 0,
        "human_measurement": False,
        "original_correct": {
            "numerator": sum(a["original_correct"] for a in attempts),
            "denominator": len(attempts),
        },
        "metrics": summarize_interventions(a["metrics"] for a in attempts),
        "by_series": {
            series: {
                "original_correct": {
                    "numerator": sum(
                        a["original_correct"] for a in attempts if a["series"] == series
                    ),
                    "denominator": sum(a["series"] == series for a in attempts),
                },
                "metrics": summarize_interventions(
                    a["metrics"] for a in attempts if a["series"] == series
                ),
            }
            for series in SERIES
        },
        "attempts": attempts,
        "source_sha256": source_hashes,
        "limits": [
            "Исходные оценки сохранены. Корректный недоставленный текст не является завершённой попыткой.",
            "Подготовительные согласования исключены по proposal_id/event_boundary контрольной точки.",
            "Доля принятия считает неизменные версии, включая устаревшие и отклонённые в знаменателе.",
            "Изменение условий не является само по себе ошибкой модели. Время человека не измерялось.",
            "Проверка русского текста ограничена описанными конструкциями; review требует ручного разбора.",
            "Серии нельзя объединять для вывода о преимуществе; общий итог служит проверке учёта.",
        ],
    }
    # Повторная сверка выявляет изменение источника во время чтения.
    for name, expected in source_hashes.items():
        if hashlib.sha256((PROJECT_ROOT / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError("Источник изменился во время разбора")
    write_json(output / "summary.json", report)
    with (output / "attempts.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as stream:
        fields = [
            "series",
            "attempt_id",
            "original_correct",
            "complete",
            "action_admissibility",
            "reply_content",
            "delivery",
            "final_state",
            "accepted",
            "offered",
            "mandatory_approvals",
            "additional_commands",
            "error_category",
        ]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for item in attempts:
            metric, assessment = item["metrics"], item["assessment"]
            writer.writerow(
                {
                    **{
                        key: item[key]
                        for key in (
                            "series",
                            "attempt_id",
                            "original_correct",
                            "error_category",
                        )
                    },
                    "complete": assessment["complete"],
                    **{
                        key: value["status"]
                        for key, value in assessment["checks"].items()
                    },
                    "accepted": metric["accepted_without_edits"]["numerator"],
                    "offered": metric["accepted_without_edits"]["denominator"],
                    "mandatory_approvals": metric["counts"][
                        "mandatory_staff_approvals"
                    ],
                    "additional_commands": metric["counts"][
                        "additional_staff_commands"
                    ],
                }
            )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="Новый каталог отчёта"
    )
    args = parser.parse_args()
    result = review_saved(args.output)
    print(
        f"Разобрано попыток: {len(result['attempts'])}. Новых запусков и вызовов API: 0."
    )


if __name__ == "__main__":
    main()
