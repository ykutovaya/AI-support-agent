"""Начало серии не разрешает сеть; контроль требует неизменной версии."""

import hashlib
import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "investigation_cli", ROOT / "evals/investigation/run_investigation.py"
)
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


def test_freeze_detects_source_drift(tmp_path):
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src/app.py").write_text("original")
    series = tmp_path / "series"
    series.mkdir()
    cli.freeze_sources(series, root=source)
    cli.verify_freeze(series, root=source)
    (source / "src/app.py").write_text("changed")
    with pytest.raises(ValueError, match="измен"):
        cli.verify_freeze(series, root=source)


def test_default_command_does_not_read_credentials_or_call_api(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("credentials/API accessed")

    monkeypatch.setattr(cli, "load_credentials", forbidden)
    assert cli.main([]) == 0
    assert "--run" in capsys.readouterr().out


def test_second_process_cannot_open_same_series(tmp_path):
    with cli.exclusive_run(tmp_path):
        with pytest.raises(ValueError, match="выполняется"):
            with cli.exclusive_run(tmp_path):
                pytest.fail("параллельный запуск разрешён")
    assert not (tmp_path / ".running").exists()


def test_resume_only_selects_cases_never_started():
    selection = [{"id": f"dev-{i}"} for i in range(4)]
    rows = [
        {"phase": "development", "case_id": "dev-0", "status": "complete"},
        {"phase": "development", "case_id": "dev-1", "status": "reserved"},
    ]
    assert cli.unstarted_cases(selection, rows, "development") == selection[2:]


KNOWN = ["control-identity", "control-intent", "control-price"]


def init_known(path, *extra):
    return cli.main(
        [
            "--init",
            "--output",
            str(path),
            "--budget-usd",
            "5",
            "--cases",
            *KNOWN,
            *extra,
        ]
    )


def test_selected_series_freezes_before_any_paid_attempt(tmp_path):
    series = tmp_path / "known"
    assert init_known(series) == 0
    data = json.loads((series / "series.json").read_text(encoding="utf-8"))
    assert data["authorization"] == {
        "scope": "known_case_regression",
        "max_usd": 5.0,
        "max_attempts": 4,
        "phase_caps": {"connection": 1, "regression": 3, "repeat": 0},
        "case_ids": KNOWN,
    }
    assert [c["id"] for c in data["cases"]] == KNOWN
    assert cli.main(["--freeze", "--output", str(series)]) == 0
    cli.verify_freeze(series)


@pytest.mark.parametrize("selection", [["absent"], [KNOWN[0], KNOWN[0]]])
def test_invalid_selection_creates_no_series(tmp_path, selection):
    series = tmp_path / "invalid"
    with pytest.raises((ValueError, SystemExit)):
        cli.main(
            [
                "--init",
                "--output",
                str(series),
                "--budget-usd",
                "5",
                "--cases",
                *selection,
            ]
        )
    assert not series.exists()


def test_init_requires_explicit_budget(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["--init", "--output", str(tmp_path / "missing")])
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize(
    "run_args",
    [
        ["--phase", "development", "--budget-usd", "5"],
        ["--phase", "regression", "--budget-usd", "6"],
        ["--repeat", "control-price", "--budget-usd", "5"],
    ],
)
def test_out_of_scope_run_cannot_read_credentials(tmp_path, monkeypatch, run_args):
    series = tmp_path / "known"
    init_known(series)
    cli.main(["--freeze", "--output", str(series)])

    def forbidden(*args, **kwargs):
        pytest.fail("Ключ прочитан для несогласованного запуска")

    monkeypatch.setattr(cli, "load_credentials", forbidden)
    with pytest.raises((ValueError, SystemExit)):
        cli.main(
            ["--run", "--output", str(series), "--key-env", "unused.env", *run_args]
        )


def test_selected_run_calls_only_allowed_cases_once(tmp_path, monkeypatch):
    from evals.investigation import runner
    from support_agent.adapters import llm

    series = tmp_path / "known"
    init_known(series)
    cli.main(["--freeze", "--output", str(series)])
    called = []
    credentials = []
    monkeypatch.setattr(cli, "load_credentials", lambda path: credentials.append(path))
    monkeypatch.setattr(llm, "LiteLLMClient", lambda *args: object())

    def fake_run(folder, case, model, **kwargs):
        called.append(case["id"])
        return {"status": "review"}

    monkeypatch.setattr(runner, "run_case", fake_run)
    command = [
        "--run",
        "--phase",
        "regression",
        "--output",
        str(series),
        "--key-env",
        "unused.env",
        "--budget-usd",
        "5",
    ]
    assert cli.main(command) == 0
    assert cli.main(command) == 0
    assert called == KNOWN
    assert len(credentials) == 1
    rows = json.loads((series / "attempts.json").read_text(encoding="utf-8"))[
        "attempts"
    ]
    assert [r["case_id"] for r in rows] == KNOWN
    assert all(r["status"] == "review" for r in rows)


def control_file(path):
    """Тестовые копии разработочного примера; будущий контроль здесь не читается."""
    from evals.investigation.cases import load_cases

    example = load_cases("development")[0]
    cases = []
    for index in range(6):
        case = deepcopy(example)
        case.update(id=f"cli-fixture-{index}", split="control")
        cases.append(case)
    path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    return cases


def init_control(series, dataset, *extra):
    return cli.main(
        [
            "--init",
            "--output",
            str(series),
            "--budget-usd",
            "5",
            "--control-file",
            str(dataset),
            *extra,
        ]
    )


def test_new_control_freezes_six_cases_without_development(tmp_path):
    dataset = tmp_path / "development-fixtures.json"
    cases = control_file(dataset)
    raw = dataset.read_bytes()
    series = tmp_path / "series"
    assert init_control(series, dataset) == 0
    saved = json.loads((series / "series.json").read_text(encoding="utf-8"))
    assert saved["authorization"] == {
        "scope": "new_control",
        "max_usd": 5.0,
        "max_attempts": 7,
        "phase_caps": {"connection": 1, "control": 6, "repeat": 0},
        "case_ids": [case["id"] for case in cases],
    }
    assert (series / "control-cases.json").read_bytes() == raw
    assert saved["control_dataset_sha256"] == hashlib.sha256(raw).hexdigest()
    assert cli.main(["--freeze", "--output", str(series)]) == 0
    cli.verify_freeze(series)


@pytest.mark.parametrize(
    "damage", ["five", "duplicate", "old_id", "development", "mix", "repeats"]
)
def test_invalid_new_control_is_rejected_before_series_creation(tmp_path, damage):
    dataset = tmp_path / "fixtures.json"
    cases = control_file(dataset)
    extra = []
    if damage == "five":
        cases.pop()
    elif damage == "duplicate":
        cases[-1]["id"] = cases[0]["id"]
    elif damage == "old_id":
        cases[0]["id"] = "control-identity"
    elif damage == "development":
        cases[0]["split"] = "development"
    elif damage == "mix":
        extra = ["--cases", "control-identity"]
    else:
        extra = ["--max-repeats", "1"]
    dataset.write_text(json.dumps(cases), encoding="utf-8")
    series = tmp_path / "series"
    with pytest.raises((ValueError, SystemExit)):
        init_control(series, dataset, *extra)
    assert not series.exists()


@pytest.mark.parametrize("phase", ["connection", "control"])
@pytest.mark.parametrize("damage", ["not_frozen", "changed_copy"])
def test_new_control_requires_unchanged_freeze_before_credentials(
    tmp_path, monkeypatch, phase, damage
):
    dataset = tmp_path / "fixtures.json"
    control_file(dataset)
    series = tmp_path / "series"
    init_control(series, dataset)
    if damage == "changed_copy":
        cli.main(["--freeze", "--output", str(series)])
        with (series / "control-cases.json").open("a", encoding="utf-8") as handle:
            handle.write(" ")

    def forbidden(*args, **kwargs):
        pytest.fail("Ключ прочитан до проверки нового контрольного набора")

    monkeypatch.setattr(cli, "load_credentials", forbidden)
    with pytest.raises(ValueError):
        cli.main(
            [
                "--run",
                "--phase",
                phase,
                "--output",
                str(series),
                "--budget-usd",
                "5",
                "--key-env",
                "unused.env",
            ]
        )


def test_new_control_runs_frozen_copy_only_once(tmp_path, monkeypatch):
    from evals.investigation import runner
    from support_agent.adapters import llm

    dataset = tmp_path / "fixtures.json"
    cases = control_file(dataset)
    series = tmp_path / "series"
    init_control(series, dataset)
    cli.main(["--freeze", "--output", str(series)])
    # Исходный файл не является рабочими данными после сохранения копии.
    dataset.write_text("[]", encoding="utf-8")
    called, credentials = [], []
    monkeypatch.setattr(cli, "load_credentials", lambda path: credentials.append(path))
    monkeypatch.setattr(llm, "LiteLLMClient", lambda *args: object())

    def fake_run(folder, case, model, **kwargs):
        called.append(case)
        return {"status": "review"}

    monkeypatch.setattr(runner, "run_case", fake_run)
    command = [
        "--run",
        "--phase",
        "control",
        "--output",
        str(series),
        "--budget-usd",
        "5",
        "--key-env",
        "unused.env",
    ]
    assert cli.main(command) == 0
    assert cli.main(command) == 0
    assert called == cases
    assert len(credentials) == 1
    rows = json.loads((series / "attempts.json").read_text(encoding="utf-8"))[
        "attempts"
    ]
    assert len(rows) == 6 and all(row["phase"] == "control" for row in rows)
    for folder in series.glob("*-control-*"):
        provenance = json.loads(
            (folder / "provenance.json").read_text(encoding="utf-8")
        )
        assert provenance["new_control"] is True
        assert provenance["known_case_regression"] is False
