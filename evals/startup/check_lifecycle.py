"""Проверка запуска и восстановления в отдельных процессах без API и интерфейса."""

import argparse
import hashlib
import importlib.metadata
import json
import os
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

EMPLOYEE = "demo-employee"
PHASES = ("prepare", "restore_pending", "execute", "restore_complete", "new_snapshot")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def value_hash(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def database_state(path):
    """Прочитать все таблицы; соединение не может изменить базу."""
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        tables = {}
        for name in names:
            identifier = '"' + name.replace('"', '""') + '"'
            tables[name] = [
                dict(row)
                for row in conn.execute(f"SELECT * FROM {identifier} ORDER BY rowid")
            ]
    return tables


def configure(project, database):
    """Выбрать только учебную модель и явно заданный рабочий снимок."""
    os.environ.update(
        SUPPORT_AGENT_MODE="demo",
        SUPPORT_AGENT_RECOVERY_MODE="manual",
        SUPPORT_AGENT_DB=str(database),
        SUPPORT_AGENT_RETAIL_DB=str(project / "evals/reapproval/base_shop.json"),
        PYTHON_DOTENV_DISABLED="1",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
    )


def worker(args):
    project, database = args.project_root.resolve(), args.database.resolve()
    if args.worker in {"prepare", "new_snapshot"}:
        assert not database.exists(), "Для нового снимка нужен новый файл"
    else:
        assert database.is_file(), "Не найдена сохранённая рабочая база"
    configure(project, database)
    from support_agent.bootstrap import create_service

    app = create_service(project)
    assert app.mode == "demo" and app.recovery_mode == "manual"
    case_id = args.case_id
    if args.worker == "prepare":
        case_id = app.create_case("u1", EMPLOYEE)
        proposal = app.investigate(
            case_id,
            "Обменяйте товар на любой другой доступный вариант. Email user1@example.test",
            EMPLOYEE,
        )
        assert proposal.action is None
        assert proposal.consent_request.amount == Decimal("2.35")
        assert app.view(case_id, EMPLOYEE)["proposal_status"] == "pending"
    elif args.worker == "execute":
        view = app.view(case_id, EMPLOYEE)
        proposal = view["proposal"]
        assert proposal.id == args.proposal_id
        assert view["proposal_status"] == "pending"
        payments = app.store.snapshot()[0]["orders"]["#O1"]["payment_history"]
        app.approve(case_id, proposal.id, proposal.version, proposal.digest(), EMPLOYEE)
        after_question = database_state(database)
        assert len(after_question["deliveries"]) == 1
        assert not after_question["operations"]
        action = app.record_client_response(
            case_id,
            proposal.id,
            True,
            "Согласен на обмен и доплату 2.35 USD",
            EMPLOYEE,
        )
        assert not database_state(database)["operations"]
        app.approve(case_id, action.id, action.version, action.digest(), EMPLOYEE)
        view = app.view(case_id, EMPLOYEE)
        assert view["proposal_status"] == "delivered"
        assert view["deliveries"][-1]["text"] == action.text
        order = app.store.snapshot()[0]["orders"]["#O1"]
        assert order["status"] == "exchange requested"
        assert Decimal(str(order["exchange_price_difference"])) == Decimal("2.35")
        assert order["payment_history"] == payments
    elif args.worker in {"restore_pending", "restore_complete"}:
        view = app.view(case_id, EMPLOYEE)
        assert view["proposal"].id == args.proposal_id
        expected = "pending" if args.worker == "restore_pending" else "delivered"
        assert view["proposal_status"] == expected

    state = database_state(database)
    counts = {name: len(rows) for name, rows in state.items()}
    if args.worker in {"prepare", "restore_pending"}:
        assert counts["operations"] == counts["deliveries"] == 0
        assert counts["consents"] == counts["approvals"] == 0
    elif args.worker in {"execute", "restore_complete"}:
        assert counts["operations"] == 1 and counts["deliveries"] == 2
        assert counts["approvals"] == 2 and counts["consents"] == 1
    else:
        assert counts["retail_state"] == 1
        assert all(
            count == 0
            for name, count in counts.items()
            if name not in {"retail_state", "sqlite_sequence"}
        )
        assert app.store.snapshot()[0]["orders"]["#O1"]["status"] == "delivered"

    import tau2

    import support_agent

    versions = {
        name: importlib.metadata.version(name)
        for name in ("support-agent", "tau2", "streamlit", "pydantic", "litellm")
    }
    result = {
        "phase": args.worker,
        "pid": os.getpid(),
        "python": sys.version,
        "executable": sys.executable,
        "prefix": sys.prefix,
        "versions": versions,
        "module_paths": {
            "support_agent": support_agent.__file__,
            "tau2": tau2.__file__,
        },
        "database": str(database),
        "mode": app.mode,
        "recovery_mode": app.recovery_mode,
        "case_id": case_id,
        "proposal_id": app.view(case_id, EMPLOYEE)["proposal"].id if case_id else None,
        "counts": counts,
        "shop_sha256": value_hash(app.store.snapshot()),
        "state_sha256": value_hash(state),
        "database_sha256": sha256(database),
        "api_calls": 0,
        "ui_or_human_check": False,
        "state": state,
    }
    print(json.dumps(result, ensure_ascii=False))


def run(args):
    project, output = args.project_root.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    fixture = project / "evals/reapproval/base_shop.json"
    policy = fixture.with_name("policy.md")
    hashes_before = {str(path): sha256(path) for path in (fixture, policy)}
    database, fresh = output / "working.sqlite3", output / "fresh.sqlite3"
    phases = []

    def call(phase, db, case_id=None, proposal_id=None):
        command = [
            sys.executable,
            "-B",
            str(Path(__file__).resolve()),
            "--project-root",
            str(project),
            "--worker",
            phase,
            "--database",
            str(db),
        ]
        if case_id:
            command += ["--case-id", case_id, "--proposal-id", proposal_id]
        env = os.environ.copy()
        for key in ("PYTHONPATH", "PYTHONHOME"):
            env.pop(key, None)
        env.update(PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
        completed = subprocess.run(
            command,
            cwd=project,
            env=env,
            capture_output=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
        (output / f"{phase}.stderr.txt").write_text(completed.stderr, "utf-8")
        if completed.returncode:
            raise RuntimeError(
                f"Фаза {phase} завершилась с кодом {completed.returncode}; см. stderr"
            )
        result = json.loads(completed.stdout)
        (output / f"{phase}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8"
        )
        phases.append({key: value for key, value in result.items() if key != "state"})
        return result

    prepared = call("prepare", database)
    restored = call(
        "restore_pending", database, prepared["case_id"], prepared["proposal_id"]
    )
    assert restored["state"] == prepared["state"]
    assert restored["database_sha256"] == prepared["database_sha256"]
    executed = call("execute", database, prepared["case_id"], prepared["proposal_id"])
    completed = call(
        "restore_complete", database, executed["case_id"], executed["proposal_id"]
    )
    assert completed["state"] == executed["state"]
    assert completed["database_sha256"] == executed["database_sha256"]
    old_database_hash = sha256(database)
    new = call("new_snapshot", fresh)
    assert new["shop_sha256"] == prepared["shop_sha256"]
    assert sha256(database) == old_database_hash
    hashes_after = {str(path): sha256(path) for path in (fixture, policy)}
    assert hashes_before == hashes_after
    result = {
        "status": "passed",
        "kind": "separate_process_lifecycle",
        "api_calls": 0,
        "ui_or_human_check": False,
        "fixture_hashes_before": hashes_before,
        "fixture_hashes_after": hashes_after,
        "restores_unchanged": True,
        "fresh_snapshot_preserves_previous_database": True,
        "phases": phases,
    }
    (output / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    print(json.dumps({"status": "passed", "processes": len(phases), "api_calls": 0}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", choices=PHASES, help=argparse.SUPPRESS)
    parser.add_argument("--database", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--case-id", help=argparse.SUPPRESS)
    parser.add_argument("--proposal-id", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.worker:
        if arguments.database is None:
            parser.error("Для внутренней фазы нужна база")
        worker(arguments)
    else:
        if arguments.output is None:
            parser.error("Укажите новый каталог результатов через --output")
        run(arguments)
