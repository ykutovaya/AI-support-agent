"""Ограниченный доступ к рабочему снимку магазина и проверяемые заявки."""

from collections import Counter
from copy import deepcopy
from decimal import Decimal, InvalidOperation

from jsonschema import ValidationError, validate
from tau2.domains.retail.data_model import GiftCard, RetailDB
from tau2.domains.retail.tools import RetailTools
from tau2.utils.text_match import fold_for_match

from support_agent.models import ToolSpec
from support_agent.proposals import (
    Action,
    ConditionChange,
    ConditionCheck,
    Evidence,
    digest,
)

READS = frozenset(
    {
        "find_user_id_by_email",
        "find_user_id_by_name_zip",
        "get_user_details",
        "get_order_details",
        "get_product_details",
    }
)
PROPOSALS = {
    "propose_exchange": "exchange_delivered_order_items",
    "propose_return": "return_delivered_order_items",
}
DESCRIPTIONS = {
    "find_user_id_by_email": "Проверить email клиента текущего обращения.",
    "find_user_id_by_name_zip": "Проверить имя и почтовый индекс клиента текущего обращения.",
    "get_user_details": "Прочитать профиль клиента текущего обращения.",
    "get_order_details": "Прочитать собственный заказ клиента.",
    "get_product_details": "Прочитать товар и варианты из собственных заказов клиента.",
    "propose_exchange": "Подготовить предложение обмена. Запись и отправка не выполняются.",
    "propose_return": "Подготовить предложение возврата. Запись и отправка не выполняются.",
}
CENT = Decimal("0.01")


def _money(value) -> Decimal:
    """Преобразовать цену без двоичной арифметики и недопустимых чисел."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("Некорректная денежная сумма") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError("Цена и баланс должны быть конечными неотрицательными числами")
    return amount


def _condition_label(field: str) -> str:
    """Подписи различий предназначены сотруднику, пути остаются стабильными."""
    if field == "order.status":
        return "Статус заказа"
    if field == "amount":
        return "Сумма доплаты / возврата, USD"
    if field == "policy":
        return "Правила обслуживания"
    parts = field.split(".")
    if len(parts) >= 5 and parts[0] == "products" and parts[2] == "variants":
        name = {"price": "Цена", "available": "Наличие", "options": "Параметры"}.get(
            parts[4], "Данные"
        )
        return f"{name} варианта {parts[3]} товара {parts[1]}"
    if field.startswith("payment_methods."):
        name = "Баланс" if parts[-1] == "balance" else "Данные"
        return f"{name} способа оплаты {parts[1]}"
    if field.startswith("order.items."):
        return f"Позиция заказа: {field.removeprefix('order.items.')}"
    return f"Условие: {field}"


def _condition_changes(before: dict, after: dict) -> list[ConditionChange]:
    changes = []

    def visit(old, new, field):
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(old.keys() | new.keys()):
                visit(old.get(key), new.get(key), f"{field}.{key}" if field else key)
        elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
            for index, (old_item, new_item) in enumerate(zip(old, new)):
                visit(old_item, new_item, f"{field}.{index}")
        elif old != new:
            changes.append(
                ConditionChange(
                    field=field, label=_condition_label(field), before=old, after=new
                )
            )

    visit(before, after, "")
    return changes


class RetailAdapter:
    """Работает с отдельной копией; вызывающая сторона сохраняет её в транзакции."""

    def __init__(self, snapshot: dict, policy: str):
        self._db = RetailDB.model_validate(deepcopy(snapshot))
        self.policy = policy
        native = RetailTools(self._db).get_tools(
            include=sorted(READS | set(PROPOSALS.values()))
        )
        self._schemas = {}
        for name, tool in native.items():
            schema = deepcopy(tool.openai_schema["function"]["parameters"])
            schema["additionalProperties"] = False
            self._schemas[name] = schema

    def specs(self) -> list[ToolSpec]:
        """Показать модели только разрешённые чтения и подготовку предложений."""
        return [
            ToolSpec(
                name=name,
                description=DESCRIPTIONS[name],
                parameters=deepcopy(self._schemas[PROPOSALS.get(name, name)]),
            )
            for name in [*sorted(READS), *PROPOSALS]
        ]

    def snapshot(self) -> dict:
        """Вернуть независимый снимок, пригодный для JSON и SQLite."""
        return self._db.model_dump(mode="json")

    def _arguments(self, name: str, args: dict) -> dict:
        if name not in self._schemas:
            raise ValueError("Инструмент не разрешён")
        try:
            validate(args, self._schemas[name])
        except ValidationError as error:
            raise ValueError("Некорректные параметры инструмента") from error
        return deepcopy(args)

    def _user(self, user_id: str):
        if user_id not in self._db.users:
            raise ValueError("Клиент обращения не найден")
        return self._db.users[user_id]

    def _order(self, order_id: str, user_id: str):
        user = self._user(user_id)
        order = self._db.orders.get(order_id)
        if order is None or order.user_id != user_id or order_id not in user.orders:
            raise ValueError("Заказ недоступен в этом обращении")
        return order

    def _condition_data(self, order, user, product_ids) -> dict:
        """Снимок только доступного заказа, способов оплаты и связанных товаров."""
        return {
            "order": order.model_dump(mode="json"),
            "payment_methods": {
                key: value.model_dump(mode="json")
                for key, value in user.payment_methods.items()
            },
            "products": {
                pid: self._db.products[pid].model_dump(mode="json")
                for pid in sorted(product_ids)
                if pid in self._db.products
            },
            "policy": self.policy,
        }

    def read(self, name: str, args: dict, user_id: str):
        """Проверить принадлежность до вызова чтения в RetailTools."""
        if name not in READS:
            raise ValueError("Чтение не разрешено")
        args = self._arguments(name, args)
        user = self._user(user_id)
        if name == "get_user_details" and args["user_id"] != user_id:
            raise ValueError("Профиль недоступен в этом обращении")
        if name == "get_order_details":
            self._order(args["order_id"], user_id)
        if name == "get_product_details":
            related = {
                item.product_id
                for oid in user.orders
                if oid in self._db.orders and self._db.orders[oid].user_id == user_id
                for item in self._db.orders[oid].items
            }
            if args["product_id"] not in related:
                raise ValueError("Товар не относится к заказам клиента")
        if name == "find_user_id_by_email" and fold_for_match(
            args["email"]
        ) != fold_for_match(user.email):
            raise ValueError("Email не соответствует клиенту обращения")
        if name == "find_user_id_by_name_zip" and (
            fold_for_match(args["first_name"]) != fold_for_match(user.name.first_name)
            or fold_for_match(args["last_name"]) != fold_for_match(user.name.last_name)
            or args["zip"] != user.address.zip
        ):
            raise ValueError("Имя и индекс не соответствуют клиенту обращения")
        # Даже ошибочно изменяющий инструмент чтения не затронет рабочий снимок.
        tools = RetailTools(self._db.model_copy(deep=True))
        result = getattr(tools, name)(**args)
        return (
            result.model_dump(mode="json")
            if hasattr(result, "model_dump")
            else deepcopy(result)
        )

    def plan(self, name: str, args: dict, user_id: str) -> Action:
        """Пересчитать условия без вызова изменяющего инструмента."""
        native = PROPOSALS.get(name, name)
        if native not in PROPOSALS.values():
            raise ValueError("Действие не разрешено")
        args = self._arguments(native, args)
        order = self._order(args["order_id"], user_id)
        user = self._user(user_id)
        if order.status != "delivered":
            raise ValueError("Заявка возможна только для доставленного заказа")
        requested = Counter(args["item_ids"])
        available = Counter(item.item_id for item in order.items)
        if not requested or any(
            count > available[item] for item, count in requested.items()
        ):
            raise ValueError(
                "Укажите непустой список позиций в пределах количества заказа"
            )
        selected = []
        for item_id in args["item_ids"]:
            matches = [item for item in order.items if item.item_id == item_id]
            # Native exchange выбирает первое совпадение. Не допускать спорный расчёт.
            if len({(item.product_id, _money(item.price)) for item in matches}) != 1:
                raise ValueError("Неоднозначные цены одинаковых позиций заказа")
            selected.append(matches[0])
        payment_id = args["payment_method_id"]
        payment = user.payment_methods.get(payment_id)
        if payment is None:
            raise ValueError("Способ оплаты не принадлежит клиенту")
        if isinstance(payment, GiftCard):
            _money(payment.balance)
        exchange = native == "exchange_delivered_order_items"
        if exchange:
            if len(args["item_ids"]) != len(args["new_item_ids"]):
                raise ValueError("Количество старых и новых позиций должно совпадать")
            amount = Decimal("0")
            for old, new_id in zip(selected, args["new_item_ids"]):
                product = self._db.products.get(old.product_id)
                new = product.variants.get(new_id) if product is not None else None
                if (
                    new is None
                    or not new.available
                    or new_id == old.item_id
                    or new.options == old.options
                ):
                    raise ValueError("Нужен доступный другой вариант того же товара")
                amount += _money(new.price) - _money(old.price)
            amount = amount.quantize(CENT)
            if isinstance(payment, GiftCard) and _money(payment.balance) < amount:
                raise ValueError("Недостаточно средств на подарочной карте")
        else:
            if not order.payment_history:
                raise ValueError("Нет первоначального способа оплаты")
            if (
                not isinstance(payment, GiftCard)
                and payment_id != order.payment_history[0].payment_method_id
            ):
                raise ValueError(
                    "Возврат возможен на первоначальный способ оплаты или подарочную карту"
                )
            amount = -sum(
                (_money(item.price) for item in selected), Decimal("0")
            ).quantize(CENT)
        product_ids = sorted({item.product_id for item in selected})
        if any(pid not in self._db.products for pid in product_ids):
            raise ValueError("Товар заказа отсутствует в снимке")
        condition_values = self._condition_data(order, user, product_ids)
        conditions = digest(condition_values)
        condition_values["amount"] = str(amount)
        evidence = [
            Evidence(
                title="Заказ клиента",
                source=f"orders/{order.order_id}",
                detail=f"Заказ {order.order_id}, статус {order.status}, клиент {user_id}.",
            )
        ]
        for pid in product_ids:
            product = self._db.products[pid]
            if exchange:
                relevant = sorted(
                    {
                        new_id
                        for old, new_id in zip(selected, args["new_item_ids"])
                        if old.product_id == pid
                    }
                )
                detail = "; ".join(
                    f"{variant_id}: {product.variants[variant_id].options}, "
                    f"{_money(product.variants[variant_id].price):.2f} USD, доступен"
                    for variant_id in relevant
                )
            else:
                detail = "; ".join(
                    f"{item.item_id}: цена покупки {_money(item.price):.2f} USD"
                    for item in selected
                    if item.product_id == pid
                )
            evidence.append(
                Evidence(
                    title=product.name,
                    source=f"products/{pid}",
                    detail=detail,
                )
            )
        section = "exchange-delivered-order" if exchange else "return-delivered-order"
        evidence.append(
            Evidence(
                title="Правила обмена" if exchange else "Правила возврата",
                source=f"policy.md#{section}",
                detail="Нужны доставленный заказ, явное согласие клиента и допустимый способ оплаты.",
            )
        )
        return Action(
            name=native,
            arguments=args,
            amount=amount,
            conditions=conditions,
            condition_values=condition_values,
            result_status="exchange requested" if exchange else "return requested",
            description=f"Заявка на {'обмен' if exchange else 'возврат'} {len(selected)} позиций заказа {order.order_id}",
            evidence=evidence,
        )

    def check(self, action: Action, user_id: str) -> ConditionCheck:
        """Показать прежние и текущие условия, не исполняя действие."""
        try:
            native = PROPOSALS.get(action.name, action.name)
            if native not in PROPOSALS.values():
                raise ValueError("Действие не разрешено")
            args = self._arguments(native, action.arguments)
            # Проверка доступа предшествует чтению значений для сравнения.
            order = self._order(args["order_id"], user_id)
            user = self._user(user_id)
        except ValueError as error:
            return ConditionCheck(status="forbidden", reason=str(error))

        before = deepcopy(action.condition_values)
        try:
            if set(before) != {
                "order",
                "payment_methods",
                "products",
                "policy",
                "amount",
            }:
                raise ValueError
            previous_amount = before.pop("amount")
            if (
                before["order"]["order_id"] != args["order_id"]
                or before["order"]["user_id"] != user_id
                or digest(before) != action.conditions
                or Decimal(str(previous_amount)) != action.amount
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError, InvalidOperation):
            return ConditionCheck(
                status="forbidden",
                reason="Прежние значения условий отсутствуют или повреждены; сформируйте новое предложение",
            )

        try:
            current = self.plan(native, args, user_id)
        except ValueError as error:
            product_ids = {
                item.product_id
                for item in order.items
                if item.item_id in args["item_ids"]
            }
            after = self._condition_data(order, user, product_ids)
            return ConditionCheck(
                status="forbidden",
                reason=str(error),
                changes=_condition_changes(before, after),
            )

        if current.fingerprint() == action.fingerprint():
            return ConditionCheck(
                status="unchanged", reason="Условия не изменились", current=current
            )
        before["amount"] = previous_amount
        changes = _condition_changes(before, current.condition_values)
        if not changes:
            return ConditionCheck(
                status="forbidden",
                reason="Сохранённое действие не соответствует условиям; сформируйте новое предложение",
            )
        return ConditionCheck(
            status="changed",
            reason="Условия изменились; требуется новое согласование",
            changes=changes,
            current=current,
        )

    def execute(self, action: Action, user_id: str) -> dict:
        """Исполнить подтверждённые условия на копии и проверить полный результат.

        Проверку согласий и транзакцию SQLite обеспечивает вызывающий исполнитель.
        До успешной проверки этот экземпляр не меняется.
        """
        current = self.plan(action.name, action.arguments, user_id)
        if current.fingerprint() != action.fingerprint():
            raise ValueError(
                "Условия действия изменились; требуется новое согласование"
            )
        candidate = self._db.model_copy(deep=True)
        tools = RetailTools(candidate)
        result = getattr(tools, current.name)(**current.arguments)
        expected = self.snapshot()
        oid = current.arguments["order_id"]
        expected_order = expected["orders"][oid]
        expected_order["status"] = current.result_status
        if current.name == "exchange_delivered_order_items":
            difference = candidate.orders[oid].exchange_price_difference
            if (
                difference is None
                or not Decimal(str(difference)).is_finite()
                or Decimal(str(difference)) != current.amount
            ):
                raise ValueError("Фактическая сумма обмена отличается от согласованной")
            expected_order.update(
                exchange_items=sorted(current.arguments["item_ids"]),
                exchange_new_items=sorted(current.arguments["new_item_ids"]),
                exchange_payment_method_id=current.arguments["payment_method_id"],
                exchange_price_difference=difference,
            )
        else:
            expected_order.update(
                return_items=sorted(current.arguments["item_ids"]),
                return_payment_method_id=current.arguments["payment_method_id"],
            )
        actual = candidate.model_dump(mode="json")
        if (
            actual != expected
            or result.model_dump(mode="json") != actual["orders"][oid]
        ):
            raise ValueError(
                "Результат операции отличается от заявки или изменены платежи"
            )
        self._db = candidate
        return deepcopy(actual["orders"][oid])
