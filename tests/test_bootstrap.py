"""Загрузка рабочего снимка и явное включение API без обращений к модели."""

import json
from pathlib import Path

import pytest

pytest.importorskip("tau2")

from support_agent.bootstrap import create_service


@pytest.fixture
def project(tmp_path, snapshot, monkeypatch):
    root = tmp_path / "application"
    root.mkdir()
    data = tmp_path / "tau2-bench/data/tau2/domains/retail"
    data.mkdir(parents=True)
    (data / "db.json").write_text(json.dumps(snapshot), encoding="utf-8")
    (data / "policy.md").write_text("Учебные правила", encoding="utf-8")
    for name in (
        "SUPPORT_AGENT_MODE",
        "SUPPORT_AGENT_DB",
        "SUPPORT_AGENT_RETAIL_DB",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    return root, data


def test_default_service_uses_demo_without_reading_credentials(project, monkeypatch):
    root, _ = project
    (root / ".env").write_text("NOT_A_CONFIGURATION", encoding="utf-8")
    import dotenv

    def forbidden(*args, **kwargs):
        pytest.fail("Режим демонстрации не читает .env и не вызывает API")

    monkeypatch.setattr(dotenv, "dotenv_values", forbidden)
    monkeypatch.setattr("support_agent.adapters.llm.LiteLLMClient.generate", forbidden)
    app = create_service(root)
    from support_agent.adapters.demo_model import DemoModel

    assert isinstance(app.model, DemoModel)
    assert app.mode == "demo"
    assert app.store.path == root / "var/support.sqlite3"
    assert app.store.snapshot()[1] == "Учебные правила"


def test_existing_working_snapshot_does_not_reread_source_files(project, monkeypatch):
    root, data = project
    first = create_service(root)
    with first.store.transaction() as conn:
        snapshot, _ = first.store.retail(conn)
        snapshot["orders"]["#O1"]["status"] = "return requested"
        first.store.save_retail(conn, snapshot)
    original = Path.read_text

    def guarded(path, *args, **kwargs):
        if path in {data / "db.json", data / "policy.md"}:
            pytest.fail(
                "Повторный запуск должен читать рабочую SQLite, а не исходные файлы"
            )
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    second = create_service(root)
    assert second.store.snapshot()[0]["orders"]["#O1"]["status"] == "return requested"


def test_paths_can_be_configured_without_changing_original_source(
    project, tmp_path, monkeypatch
):
    root, data = project
    source = data / "db.json"
    before = source.read_bytes()
    target = tmp_path / "custom/work.sqlite3"
    monkeypatch.setenv("SUPPORT_AGENT_DB", str(target))
    monkeypatch.setenv("SUPPORT_AGENT_RETAIL_DB", str(source))
    app = create_service(root)
    assert app.store.path == target
    assert source.read_bytes() == before


def test_api_mode_explicitly_loads_root_env_but_does_not_call_model(
    project, monkeypatch
):
    root, _ = project
    (root / ".env").write_text(
        "OPENAI_API_KEY=synthetic-test-key\nOPENAI_MODEL=gpt-6-luna\n", encoding="utf-8"
    )
    monkeypatch.setenv("SUPPORT_AGENT_MODE", "api")

    def forbidden(*args, **kwargs):
        pytest.fail("Создание сервиса не должно обращаться к модели")

    monkeypatch.setattr("support_agent.adapters.llm.LiteLLMClient.generate", forbidden)
    app = create_service(root)
    assert app.mode == "api"
    assert app.model.model == "openai/responses/gpt-6-luna"
    assert app.model.llm_args == {
        "reasoning_effort": "low",
        "max_tokens": 8192,
        "timeout": 90,
        "num_retries": 1,
    }
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)


@pytest.mark.parametrize("mode", ["wrong", "API"])
def test_unknown_mode_is_not_silently_enabled(project, monkeypatch, mode):
    root, _ = project
    monkeypatch.setenv("SUPPORT_AGENT_MODE", mode)
    with pytest.raises(ValueError):
        create_service(root)


def test_runtime_disables_implicit_env_and_remote_price_download(project, monkeypatch):
    root, _ = project
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.delenv("LITELLM_LOCAL_MODEL_COST_MAP", raising=False)
    create_service(root)
    import os

    assert os.environ["PYTHON_DOTENV_DISABLED"] == "1"
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
