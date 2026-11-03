"""Создать отдельную учебную базу для трёх проходов через экран, без API."""

import argparse
import copy
import json
import os
from functools import partial
from pathlib import Path

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"

from support_agent.adapters.demo_model import DemoModel
from support_agent.adapters.ticket_retail import TicketRetailAdapter
from support_agent.storage import Store
from support_agent.workflow import SupportService

ROOT = Path(__file__).resolve().parents[2]
EMPLOYEE = "demo-employee"


def prepare(path: Path):
    """Подготовить новые обращения; существующий файл никогда не менять.

    Для изменения цены заранее сохранены запрос условий и согласие клиента.
    Обычный обмен и передача специалисту начинаются с пустого обращения.
    """
    source = ROOT / "evals/reapproval/base_shop.json"
    base = json.loads(source.read_text(encoding="utf-8"))
    policy = source.with_name("policy.md").read_text(encoding="utf-8")
    snapshot = {"users": {}, "products": {}, "orders": {}}
    labels = {
        "exchange": "Обычный обмен",
        "price": "Изменение цены",
        "ticket": "Передача специалисту",
    }
    for key, label in labels.items():
        user = copy.deepcopy(base["users"]["u1"])
        order = copy.deepcopy(base["orders"]["#O1"])
        product = copy.deepcopy(base["products"]["p1"])
        order_id, product_id = f"#{key.upper()}", f"product_{key}"
        user.update(
            user_id=key,
            email=f"{key}@example.test",
            name={"first_name": label, "last_name": ""},
            orders=[order_id],
        )
        order.update(order_id=order_id, user_id=key)
        for item in order["items"]:
            item.update(product_id=product_id, item_id=f"{key}_{item['item_id']}")
        product["product_id"] = product_id
        product["variants"] = {
            f"{key}_{item_id}": {**variant, "item_id": f"{key}_{item_id}"}
            for item_id, variant in product["variants"].items()
        }
        if key == "ticket":
            order.update(
                status="exchange requested",
                exchange_items=["ticket_old"],
                exchange_new_items=["ticket_new"],
                exchange_payment_method_id="card",
                exchange_price_difference=2.35,
            )
        snapshot["users"][key] = user
        snapshot["orders"][order_id] = order
        snapshot["products"][product_id] = product

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Исключительное создание защищает историю и при одновременном запуске.
    with path.open("xb"):
        pass
    store = Store(path)
    store.initialize(snapshot, policy)
    service = SupportService(
        store,
        DemoModel(),
        partial(TicketRetailAdapter, target="local"),
        recovery_mode="manual",
    )
    cases = {key: service.create_case(key, EMPLOYEE) for key in labels}
    case = cases["price"]
    question = service.investigate(
        case, "Обменяйте товар. Email price@example.test", EMPLOYEE
    )
    service.approve(case, question.id, question.version, question.digest(), EMPLOYEE)
    service.record_client_response(
        case, question.id, True, "Согласен с заменой и доплатой 2.35 USD.", EMPLOYEE
    )
    with store.transaction() as conn:
        current, _ = store.retail(conn)
        current["products"]["product_price"]["variants"]["price_new"]["price"] = 19.99
        store.save_retail(conn, current)
    return service, cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Новый файл SQLite")
    args = parser.parse_args()
    try:
        service, _ = prepare(args.db)
    except FileExistsError:
        parser.error("Файл уже существует. Выберите новое имя, сохранив прежнюю базу.")
    print(f"Подготовлена учебная база: {service.store.path}")
    print(
        "Режимы запуска: demo / manual / local. Инструкция — documents/evaluation/acceptance.md."
    )


if __name__ == "__main__":
    main()
