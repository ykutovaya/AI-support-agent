"""Проверка расследования. Сеть разрешается только явным --run."""

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
sys.dont_write_bytecode = True


def source_hashes(root=ROOT):
    root = Path(root)
    # Отчёты внутри каталога серии в снимок исполняемого кода не входят.
    paths = list((root / "src").rglob("*.py")) if (root / "src").exists() else []
    directory = root / "evals/investigation"
    if directory.exists():
        paths.extend(
            p
            for p in directory.iterdir()
            if p.is_file() and p.suffix in {".py", ".json", ".md"}
        )
    paths.extend(
        p
        for p in (
            root / "pyproject.toml",
            root / "uv.lock",
            root / "evals/reapproval/metrics.py",
            root / "evals/reapproval/grading.py",
            root / "evals/reapproval/base_shop.json",
        )
        if p.is_file()
    )
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(paths)
    }


def freeze_sources(series, *, root=ROOT):
    from evals.investigation.runtime import write_json

    path = Path(series) / "freeze.json"
    if path.exists():
        raise ValueError("Версия уже закреплена")
    write_json(
        path,
        {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "sha256": source_hashes(root),
        },
    )


def verify_freeze(series, *, root=ROOT):
    path = Path(series) / "freeze.json"
    if not path.is_file():
        raise ValueError("Перед контролем закрепите версию командой --freeze")
    if json.loads(path.read_text(encoding="utf-8"))["sha256"] != source_hashes(root):
        raise ValueError(
            "Код, данные или настройки изменились после закрепления версии"
        )


@contextmanager
def exclusive_run(series):
    path = Path(series) / ".running"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise ValueError(
            "Серия уже выполняется или требует проверки после аварийной остановки"
        ) from None
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(str(os.getpid()))
        yield
    finally:
        path.unlink()


def load_credentials(path):
    from urllib.parse import urlparse

    from dotenv import dotenv_values

    values = dotenv_values(path)
    endpoint = (
        os.environ.get("OPENAI_BASE_URL")
        or values.get("OPENAI_BASE_URL")
        or "https://api.openai.com/v1"
    )
    if urlparse(endpoint).hostname != "api.openai.com":
        raise ValueError("Для другого провайдера нужна отдельная проверка тарифа")
    key = os.environ.get("OPENAI_API_KEY") or values.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("Локальный ключ OpenAI не найден")
    os.environ["OPENAI_API_KEY"] = key
    os.environ["OPENAI_BASE_URL"] = endpoint


def dataset_hashes():
    return {
        name: hashlib.sha256(
            (ROOT / "evals/investigation" / name).read_bytes()
        ).hexdigest()
        for name in ("cases.py", "cases.json", "policy.md")
    }


def saved_control_cases(directory, series):
    """Прочитать закреплённую копию нового набора, не исходный файл пользователя."""
    from evals.investigation.cases import load_cases

    path = Path(directory) / "control-cases.json"
    if not path.is_file() or hashlib.sha256(
        path.read_bytes()
    ).hexdigest() != series.get("control_dataset_sha256"):
        raise ValueError("Новый контрольный набор изменился после начала серии")
    cases = load_cases(path=path)
    if [case["id"] for case in cases] != series["authorization"]["case_ids"]:
        raise ValueError("Контрольный набор не совпадает с разрешённой выборкой")
    return cases


def describe():
    print(
        "API только с --run. При --init задайте --budget-usd; для известных случаев — --cases ID ..., для шести новых — --control-file PATH. Повторы по умолчанию запрещены."
    )


def unstarted_cases(selection, rows, phase):
    started = {r["case_id"] for r in rows if r["phase"] == phase}
    return [case for case in selection if case["id"] not in started]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument(
        "--phase", choices=("connection", "development", "control", "regression")
    )
    parser.add_argument("--repeat")
    parser.add_argument("--cases", nargs="+")
    parser.add_argument("--control-file", type=Path)
    parser.add_argument("--max-repeats", type=int, default=0)
    parser.add_argument("--budget-usd", type=float)
    parser.add_argument("--key-env", type=Path)
    args = parser.parse_args(argv)
    if not any((args.init, args.freeze, args.run)):
        describe()
        return 0
    if not args.output:
        parser.error("Укажите --output")
    if sum((args.init, args.freeze, args.run)) != 1:
        parser.error("Используйте только один из --init, --freeze, --run")
    if not args.init and (args.cases or args.control_file or args.max_repeats):
        parser.error("Набор и лимит повторов задаются только при --init")
    from evals.investigation.cases import load_cases
    from evals.investigation.runtime import (
        LLM_ARGS,
        MODEL,
        PRICING,
        AttemptRegistry,
        Budget,
        Limit,
        TrackedModel,
        write_json,
    )

    if args.init:
        if (
            args.budget_usd is None
            or not math.isfinite(args.budget_usd)
            or args.budget_usd <= 0
        ):
            parser.error("При --init нужен согласованный положительный --budget-usd")
        if args.max_repeats < 0:
            parser.error("--max-repeats не может быть отрицательным")
        all_cases = {c["id"]: c for c in load_cases()}
        control_bytes = None
        if args.control_file:
            if args.cases or args.max_repeats:
                parser.error(
                    "Новый контроль нельзя смешивать с выбором старых случаев или повторами"
                )
            control_bytes = args.control_file.read_bytes()
            control_cases = json.loads(control_bytes.decode("utf-8-sig"))
            if (
                not isinstance(control_cases, list)
                or len(control_cases) != 6
                or any(
                    not isinstance(case, dict)
                    or not isinstance(case.get("id"), str)
                    or not case["id"]
                    or case.get("split") != "control"
                    for case in control_cases
                )
                or len({case["id"] for case in control_cases}) != 6
                or any(case["id"] in all_cases for case in control_cases)
            ):
                parser.error(
                    "Нужны ровно шесть новых различных случаев со split=control"
                )
        if args.cases and (
            len(set(args.cases)) != len(args.cases)
            or any(c not in all_cases for c in args.cases)
        ):
            parser.error("Случаи должны существовать и не повторяться")
        selection = (
            control_cases
            if args.control_file
            else [all_cases[c] for c in args.cases]
            if args.cases
            else list(all_cases.values())
        )
        caps = {"connection": 1}
        if args.control_file:
            caps["control"] = len(selection)
        elif args.cases:
            caps["regression"] = len(selection)
        else:
            caps.update(
                {
                    phase: sum(c["split"] == phase for c in selection)
                    for phase in ("development", "control")
                }
            )
        caps["repeat"] = args.max_repeats
        authorization = {
            "scope": "new_control"
            if args.control_file
            else "known_case_regression"
            if args.cases
            else "development_and_control",
            "max_usd": args.budget_usd,
            "max_attempts": sum(caps.values()),
            "phase_caps": caps,
            "case_ids": [c["id"] for c in selection],
        }
        args.output.mkdir(parents=True, exist_ok=False)
        if control_bytes is not None:
            (args.output / "control-cases.json").write_bytes(control_bytes)
        write_json(
            args.output / "series.json",
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "authorization": authorization,
                **(
                    {
                        "control_dataset_sha256": hashlib.sha256(
                            control_bytes
                        ).hexdigest(),
                        "control_source_name": args.control_file.name,
                    }
                    if control_bytes is not None
                    else {}
                ),
                "model": MODEL,
                "llm_args": LLM_ARGS,
                "pricing": PRICING,
                "limits": {"steps": 60, "seconds": 600, "api_retries": 0},
                "ticket_backend": "local",
                "recovery_mode": "manual",
                "customer_and_employee": "scripted_simulation",
                "python": sys.version,
                "dependencies": {
                    n: importlib.metadata.version(n)
                    for n in ("litellm", "openai", "tau2", "pydantic")
                },
                "case_definition_sha256": hashlib.sha256(
                    control_bytes
                    if control_bytes is not None
                    else (ROOT / "evals/investigation/cases.json").read_bytes()
                ).hexdigest(),
                "dataset_sha256": dataset_hashes(),
                "initial_source_sha256": source_hashes(),
                "cases": [{"id": c["id"], "split": c["split"]} for c in selection],
            },
        )
        print("Серия подготовлена без API")
        return 0
    if not (args.output / "series.json").is_file():
        parser.error("Сначала --init в новом каталоге")
    with exclusive_run(args.output):
        series = json.loads((args.output / "series.json").read_text(encoding="utf-8"))
        authorization = series["authorization"]
        if "phase_caps" not in authorization:
            parser.error(
                "Историческая серия сохранена для чтения; подготовьте новую через --init"
            )
        registry = AttemptRegistry(
            args.output / "attempts.json", authorization=authorization
        )
        selected = authorization["scope"] == "known_case_regression"
        new_control = authorization["scope"] == "new_control"
        control_cases = (
            saved_control_cases(args.output, series) if new_control else None
        )
        if args.freeze:
            if (
                not selected
                and not new_control
                and (
                    sum(
                        r["phase"] == "development" and r["status"] != "reserved"
                        for r in registry.rows
                    )
                    != authorization["phase_caps"].get("development", 0)
                )
            ):
                parser.error("Сначала завершите четыре разработочных попытки")
            freeze_sources(args.output)
            print("Версия закреплена")
            return 0
        if not args.run or not args.budget_usd or not args.key_env:
            parser.error("Нужны --run, --budget-usd и --key-env")
        if bool(args.phase) == bool(args.repeat):
            parser.error("Укажите --phase либо --repeat")
        phase = "repeat" if args.repeat else args.phase
        if authorization["phase_caps"].get(phase, 0) <= 0:
            parser.error("Фаза или повторы не разрешены условиями этой серии")
        if selected or new_control or args.phase == "control" or args.repeat:
            verify_freeze(args.output)
        if series["dataset_sha256"] != dataset_hashes():
            raise ValueError("Задания изменились после начала серии")
        if (
            series["model"] != MODEL
            or series["llm_args"] != LLM_ARGS
            or series["pricing"] != PRICING
        ):
            raise ValueError("Настройки модели или тариф изменились после начала серии")
        from evals.investigation.runner import run_case
        from support_agent.adapters.llm import LiteLLMClient
        from support_agent.models import Message

        budget = Budget(
            args.output / "budget.json", args.budget_usd, authorization=authorization
        )
        if args.phase == "connection" or args.repeat == "connection":
            selection = [{"id": "connection"}]
        else:
            selection = (
                control_cases
                if new_control
                else [c for c in load_cases() if c["id"] == args.repeat]
                if args.repeat
                else (
                    [
                        c
                        for case_id in authorization["case_ids"]
                        for c in load_cases()
                        if c["id"] == case_id
                    ]
                    if args.phase == "regression"
                    else load_cases(args.phase)
                )
            )
            if not selection:
                parser.error("Задание не найдено")
        if not args.repeat:
            selection = unstarted_cases(selection, registry.rows, args.phase)
        if not selection:
            print("Неначатых заданий нет")
            return 0
        if any(
            c["id"] != "connection" and c["id"] not in authorization["case_ids"]
            for c in selection
        ):
            parser.error("Случай не включён в согласованный набор")
        if (
            len(registry.rows) + len(selection) > authorization["max_attempts"]
            or sum(r["phase"] == phase for r in registry.rows) + len(selection)
            > authorization["phase_caps"][phase]
        ):
            parser.error("Лимит попыток исчерпан")
        load_credentials(args.key_env)
        for case in selection:
            phase = "repeat" if args.repeat else args.phase
            row = registry.reserve(phase, case["id"])
            folder = args.output / f"{row['number']:02d}-{phase}-{case['id']}"
            limit = Limit()
            model = TrackedModel(
                LiteLLMClient(MODEL, LLM_ARGS),
                path=folder / "calls.json",
                label=folder.name,
                budget=budget,
                limit=limit,
            )
            if case["id"] == "connection":
                folder.mkdir(exist_ok=False)
                try:
                    answer = model.generate(
                        [Message(role="user", content="Ответь одним словом: готово")],
                        [],
                    )
                    status = "complete"
                    write_json(
                        folder / "result.json",
                        {"status": status, "reply": answer.message.content},
                    )
                except Exception as error:
                    status = "external_error"
                    write_json(
                        folder / "result.json",
                        {"status": status, "error_type": type(error).__name__},
                    )
            else:
                result = run_case(folder, case, model, limit=limit)
                status = result.get("status", "recorded")
            registry.finish(row["number"], status)
            write_json(
                folder / "provenance.json",
                {
                    "phase": phase,
                    "case_id": case["id"],
                    "sha256": source_hashes(),
                    "frozen_control": args.phase == "control",
                    "known_case_regression": selected,
                    "new_control": new_control,
                },
            )
            print(
                json.dumps(
                    {
                        "attempt": row["number"],
                        "case": case["id"],
                        "status": status,
                        "cost_bound_usd": budget.used,
                    },
                    ensure_ascii=False,
                )
            )
            if case["id"] == "connection" and status != "complete":
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
