"""Начало серии не разрешает сеть; контроль требует неизменной версии."""

import importlib.util
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
