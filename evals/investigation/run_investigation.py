"""Проверка расследования. Сеть разрешается только явным --run."""

import argparse
import hashlib
import importlib.metadata
import json
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


def describe():
    print(
        "4 разработочных + 6 контрольных обращений; 1 подключение; до 4 повторов. API только с --run. Общий согласованный предел $5."
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
    parser.add_argument("--phase", choices=("connection", "development", "control"))
    parser.add_argument("--repeat")
    parser.add_argument("--budget-usd", type=float)
    parser.add_argument("--key-env", type=Path)
    args = parser.parse_args(argv)
    if not any((args.init, args.freeze, args.run)):
        describe()
        return 0
    if not args.output:
        parser.error("Укажите --output")
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
        args.output.mkdir(parents=True, exist_ok=False)
        write_json(
            args.output / "series.json",
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "authorization": {
                    "model": MODEL,
                    "max_usd": 5,
                    "max_attempts": 15,
                    "connection": 1,
                    "development": 4,
                    "control": 6,
                    "repeats": 4,
                },
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
                    (ROOT / "evals/investigation/cases.json").read_bytes()
                ).hexdigest(),
                "dataset_sha256": dataset_hashes(),
                "initial_source_sha256": source_hashes(),
                "cases": [{"id": c["id"], "split": c["split"]} for c in load_cases()],
            },
        )
        print("Серия подготовлена без API")
        return 0
    if not (args.output / "series.json").is_file():
        parser.error("Сначала --init в новом каталоге")
    with exclusive_run(args.output):
        if args.freeze:
            registry = AttemptRegistry(args.output / "attempts.json")
            if (
                sum(
                    r["phase"] == "development" and r["status"] != "reserved"
                    for r in registry.rows
                )
                != 4
            ):
                parser.error("Сначала завершите четыре разработочных попытки")
            freeze_sources(args.output)
            print("Версия закреплена")
            return 0
        if not args.run or not args.budget_usd or not args.key_env:
            parser.error("Нужны --run, --budget-usd и --key-env")
        if bool(args.phase) == bool(args.repeat):
            parser.error("Укажите --phase либо --repeat")
        if args.phase == "control":
            verify_freeze(args.output)
        series = json.loads((args.output / "series.json").read_text(encoding="utf-8"))
        if series["dataset_sha256"] != dataset_hashes():
            raise ValueError("Задания изменились после начала серии")
        from evals.investigation.runner import run_case
        from support_agent.adapters.llm import LiteLLMClient
        from support_agent.models import Message

        registry = AttemptRegistry(args.output / "attempts.json")
        budget = Budget(args.output / "budget.json", args.budget_usd)
        load_credentials(args.key_env)
        if args.phase == "connection" or args.repeat == "connection":
            selection = [{"id": "connection"}]
        else:
            selection = (
                [c for c in load_cases() if c["id"] == args.repeat]
                if args.repeat
                else load_cases(args.phase)
            )
            if not selection:
                parser.error("Задание не найдено")
        if not args.repeat:
            selection = unstarted_cases(selection, registry.rows, args.phase)
        if not selection:
            print("Неначатых заданий нет; повтор требует --repeat")
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
