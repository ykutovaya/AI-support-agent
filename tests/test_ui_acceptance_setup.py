"""Приёмочные обращения независимы; подготовка не перезаписывает историю."""

import importlib.util
from pathlib import Path

import pytest


def setup_module_file():
    path = Path(__file__).parents[1] / "evals/ui-readability/prepare_acceptance.py"
    spec = importlib.util.spec_from_file_location("prepare_acceptance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_three_cases_keep_price_change_separate_and_do_not_execute(tmp_path):
    module = setup_module_file()
    service, cases = module.prepare(tmp_path / "acceptance.sqlite3")
    employee = "demo-employee"
    normal = service.investigate(
        cases["exchange"],
        "Обменяйте товар. Email exchange@example.test",
        employee,
    )
    assert str(normal.consent_request.amount) == "2.35"
    price = service.view(cases["price"], employee)["proposal"]
    assert str(price.action.amount) == "2.35"
    outcome = service.approve(
        cases["price"], price.id, price.version, price.digest(), employee
    )
    assert outcome["status"] == "stale"
    new = service.resume(cases["price"], price.id, employee)
    assert str(new.consent_request.amount) == "9.99"
    ticket = service.investigate(
        cases["ticket"],
        "Проверьте результат обмена. Email ticket@example.test",
        employee,
    )
    assert ticket.action.name == "create_support_ticket"
    assert ticket.action.arguments["target"] == "local"
    with service.store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 1


def test_setup_refuses_to_overwrite_an_existing_database(tmp_path):
    module = setup_module_file()
    path = tmp_path / "history.sqlite3"
    path.write_bytes(b"existing history")
    with pytest.raises(FileExistsError):
        module.prepare(path)
    assert path.read_bytes() == b"existing history"
