"""Локальные заявки доступны при обычном запуске без ключей и внешних сервисов."""

import json

from support_agent.bootstrap import create_service


def test_local_ticket_is_available_without_connection_settings_or_model_keys(
    tmp_path, snapshot, monkeypatch
):
    source = tmp_path / "db.json"
    source.write_text(json.dumps(snapshot), encoding="utf-8")
    source.with_name("policy.md").write_text("Учебные правила", encoding="utf-8")
    monkeypatch.setenv("SUPPORT_AGENT_MODE", "demo")
    monkeypatch.setenv("SUPPORT_AGENT_RETAIL_DB", str(source))
    monkeypatch.setenv("SUPPORT_AGENT_DB", str(tmp_path / "tickets.sqlite3"))
    import dotenv

    def forbidden(*args, **kwargs):
        raise AssertionError("Учебная модель не читает ключи API модели")

    monkeypatch.setattr(dotenv, "dotenv_values", forbidden)
    service = create_service(tmp_path)
    data, policy = service.store.snapshot()
    specs = service.adapter_factory(data, policy).specs()
    assert "propose_support_ticket" in {tool.name for tool in specs}
    assert source.read_text(encoding="utf-8") == json.dumps(snapshot)
