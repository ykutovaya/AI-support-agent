"""Явное подключение Zammad независимо от режима модели, без сетевых вызовов."""

import json
import os
from urllib.request import OpenerDirector

import pytest
import test_bootstrap

from support_agent.adapters.demo_model import DemoModel
from support_agent.bootstrap import create_service

project = test_bootstrap.project


@pytest.fixture
def zammad_project(project, monkeypatch):
    root, data = project
    for name in (
        "SUPPORT_AGENT_TICKET_BACKEND",
        "ZAMMAD_URL",
        "ZAMMAD_TOKEN",
        "ZAMMAD_GROUP_ID",
        "ZAMMAD_GROUP_NAME",
        "ZAMMAD_CUSTOMER_MAP",
    ):
        monkeypatch.delenv(name, raising=False)
    values = {
        "ZAMMAD_URL": "https://support.example.test/",
        "ZAMMAD_TOKEN": "synthetic-zammad-token",
        "ZAMMAD_GROUP_ID": "2",
        "ZAMMAD_GROUP_NAME": "Retail Demo",
        "ZAMMAD_CUSTOMER_MAP": '{"u1":4}',
    }
    (root / ".env").write_text(
        "\n".join(f"{name}={value}" for name, value in values.items())
        + "\nOPENAI_API_KEY=must-not-load-model-key\nSUPPORT_AGENT_MODE=api\n",
        encoding="utf-8",
    )
    snapshot = json.loads((data / "db.json").read_text(encoding="utf-8"))
    snapshot["orders"]["#O1"]["status"] = "exchange requested"
    (data / "db.json").write_text(json.dumps(snapshot), encoding="utf-8")
    monkeypatch.setenv("SUPPORT_AGENT_TICKET_BACKEND", "zammad")

    def forbidden(*args, **kwargs):
        pytest.fail("Настройка сервиса и подготовка предложения не вызывают API")

    monkeypatch.setattr(OpenerDirector, "open", forbidden)
    monkeypatch.setattr("support_agent.adapters.llm.LiteLLMClient.generate", forbidden)
    return root


def plan(app):
    data, policy = app.store.snapshot()
    return app.adapter_factory(data, policy).plan(
        "propose_support_ticket",
        {"order_id": "#O1", "question": "Каков результат обмена?"},
        "u1",
    )


def test_explicit_zammad_loads_only_ticket_settings_and_keeps_demo(zammad_project):
    app = create_service(zammad_project)
    action = plan(app)
    assert isinstance(app.model, DemoModel) and app.mode == "demo"
    assert action.arguments["target"] == "zammad"
    assert action.arguments["destination"] == {
        "base_url": "https://support.example.test",
        "group_id": 2,
        "group_name": "Retail Demo",
        "customer_id": 4,
    }
    assert os.environ.get("OPENAI_API_KEY") is None
    assert "synthetic-zammad-token" not in action.model_dump_json()
    assert "must-not-load-model-key" not in action.model_dump_json()


def test_ticket_backend_is_enabled_only_in_process_environment(
    zammad_project, monkeypatch
):
    monkeypatch.delenv("SUPPORT_AGENT_TICKET_BACKEND")
    import dotenv

    def forbidden(*args, **kwargs):
        pytest.fail("Локальный режим не читает файл ключей")

    monkeypatch.setattr(dotenv, "dotenv_values", forbidden)
    assert plan(create_service(zammad_project)).arguments["target"] == "local"


def test_environment_destination_overrides_file(zammad_project, monkeypatch):
    monkeypatch.setenv("ZAMMAD_GROUP_ID", "7")
    monkeypatch.setenv("ZAMMAD_GROUP_NAME", "Another Demo")
    monkeypatch.setenv("ZAMMAD_CUSTOMER_MAP", '{"u1":9}')
    destination = plan(create_service(zammad_project)).arguments["destination"]
    assert destination["group_id"] == 7
    assert destination["group_name"] == "Another Demo"
    assert destination["customer_id"] == 9


@pytest.mark.parametrize(
    "name,value",
    [
        ("SUPPORT_AGENT_TICKET_BACKEND", "wrong"),
        ("ZAMMAD_TOKEN", ""),
        ("ZAMMAD_GROUP_ID", "0"),
        ("ZAMMAD_GROUP_NAME", ""),
        ("ZAMMAD_CUSTOMER_MAP", "[]"),
        ("ZAMMAD_CUSTOMER_MAP", '{"u1":true}'),
        ("ZAMMAD_CUSTOMER_MAP", '{"u1":0}'),
        ("ZAMMAD_CUSTOMER_MAP", "not-json"),
    ],
)
def test_invalid_settings_fail_without_local_fallback(
    zammad_project, monkeypatch, name, value
):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError) as error:
        create_service(zammad_project)
    assert "synthetic-zammad-token" not in str(error.value)
    assert "must-not-load-model-key" not in str(error.value)
