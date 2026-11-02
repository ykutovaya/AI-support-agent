"""Учебная заглушка модели: одна позиция доставленного заказа, без API и доступа к БД.

Это заданные правила, а не языковая модель. Для обмена выбирается первый доступный
другой вариант; предпочтения по цвету и иные свободные требования не разбираются.
Все предложения проходят общее ядро, согласования и исполнитель приложения.
"""

import json
import re
from decimal import Decimal
from uuid import uuid4

from support_agent.models import Generation, Message, ToolCall, ToolSpec

INSTRUCTION = (
    "Учебный режим использует заданные правила, а не языковую модель: одна позиция "
    "первого найденного доставленного заказа и первый другой доступный вариант. "
    "Пример: «Обменяйте товар на любой другой доступный вариант. Email ваш@email». "
    "Для возврата напишите «Хочу вернуть товар. Email ваш@email». "
    "Для локальной учебной заявки: «Проверьте результат обмена. Email ваш@email» "
    "или «Проверьте результат возврата. Email ваш@email» — для уже зарегистрированной операции."
)


def _reply(text: str) -> Generation:
    return Generation(
        message=Message(role="assistant", content=text),
        cost=0,
        raw_data={"mode": "demo", "synthetic": True},
    )


class DemoModel:
    """Восстанавливает учебную последовательность только из переданного диалога."""

    def generate(self, messages: list[Message], tools: list[ToolSpec]) -> Generation:
        users = [
            (index, m.content or "")
            for index, m in enumerate(messages)
            if m.role == "user"
        ]
        if not users:
            return _reply(INSTRUCTION)
        start, latest = users[-1]
        if re.search(
            r"не\s+(?:надо|нужно|хочу)|передумал|отмен|отказ", latest.casefold()
        ):
            return _reply(
                "Новое действие не предложено. Проверьте фактический результат предыдущих операций в журнале; это сообщение их не отменяет."
            )
        intent = None
        checking_result = False
        question = ""
        for _, text in reversed(users):
            lower = text.casefold()
            if re.search(r"возврат|вернуть", lower):
                intent = "return"
            elif "обмен" in lower:
                intent = "exchange"
            if intent:
                checking_result = bool(re.search(r"провер\w*\s+результат", lower))
                question = re.split(r"\bemail\b", text, flags=re.IGNORECASE)[0].strip()[
                    :500
                ]
                break
        if intent is None:
            return _reply(INSTRUCTION)
        email = None
        for _, text in reversed(users):
            match = re.search(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", text)
            if match:
                email = match.group(0)
                break
        if email is None:
            return _reply("Уточните email клиента. " + INSTRUCTION)
        available = {tool.name for tool in tools}

        def call(name, arguments):
            if name not in available:
                return _reply(
                    "Для учебного примера не подключён нужный инструмент. Запись и отправка не выполнялись."
                )
            return Generation(
                message=Message(
                    role="assistant",
                    tool_calls=[
                        ToolCall(id=uuid4().hex, name=name, arguments=arguments),
                    ],
                ),
                cost=0,
                raw_data={"mode": "demo", "synthetic": True},
            )

        if checking_result and "propose_support_ticket" not in available:
            return _reply(
                "Инструмент заявок недоступен. Фактическое завершение обмена или "
                "возврата не подтверждено; нужна проверка сотрудника."
            )

        # После новой реплики сведения перечитываются; истории разных обращений
        # не сохраняются в экземпляре модели и не смешиваются.
        calls = {c.id: c for m in messages[start + 1 :] for c in m.tool_calls or []}
        results = []
        for message in messages[start + 1 :]:
            if message.role != "tool" or message.tool_call_id not in calls:
                continue
            if message.error:
                return _reply(
                    "Учебный пример остановлен после ошибки инструмента. Уточните данные или условия; запись и отправка не выполнялись."
                )
            try:
                value = json.loads(message.content or "null")
            except (TypeError, ValueError):
                return _reply(
                    "Не удалось прочитать результат инструмента. Нужна проверка сотрудника."
                )
            results.append((calls[message.tool_call_id], value))

        def values(name):
            return [value for request, value in results if request.name == name]

        identities = values("find_user_id_by_email")
        if not identities:
            return call("find_user_id_by_email", {"email": email})
        if not isinstance(identities[-1], str):
            return _reply("Не удалось определить клиента по результату поиска.")
        profiles = values("get_user_details")
        if not profiles:
            return call("get_user_details", {"user_id": identities[-1]})
        profile = profiles[-1]
        if not isinstance(profile, dict):
            return _reply("Не удалось прочитать профиль клиента.")
        orders = values("get_order_details")
        delivered = next(
            (
                o
                for o in orders
                if isinstance(o, dict)
                and o.get("status")
                == (f"{intent} requested" if checking_result else "delivered")
                and (checking_result or o.get("items"))
            ),
            None,
        )
        if delivered is None:
            tried = {
                request.arguments["order_id"]
                for request, _ in results
                if request.name == "get_order_details"
            }
            order_ids = profile.get("orders") or []
            next_id = next((oid for oid in order_ids[:6] if oid not in tried), None)
            if next_id is None:
                return _reply(
                    "Учебный пример не нашёл заказ с зарегистрированной операцией среди первых шести заказов. Нужна проверка сотрудника."
                    if checking_result
                    else "Учебный пример не нашёл доставленный заказ среди первых шести заказов. Нужна проверка сотрудника."
                )
            return call("get_order_details", {"order_id": next_id})
        if checking_result:
            return call(
                "propose_support_ticket",
                {"order_id": delivered["order_id"], "question": question},
            )
        item = delivered["items"][0]
        products = values("get_product_details")
        product = next(
            (
                p
                for p in products
                if isinstance(p, dict) and p.get("product_id") == item.get("product_id")
            ),
            None,
        )
        if product is None:
            return call("get_product_details", {"product_id": item["product_id"]})
        methods = profile.get("payment_methods") or {}
        history = delivered.get("payment_history") or []
        original = history[0].get("payment_method_id") if history else None
        if intent == "return":
            payment_id = (
                original
                if original in methods
                else next(
                    (
                        key
                        for key, value in methods.items()
                        if value.get("source") == "gift_card"
                    ),
                    None,
                )
            )
            new_id = None
        else:
            variants = product.get("variants") or {}
            new_id = next(
                (
                    key
                    for key, value in variants.items()
                    if value.get("available")
                    and key != item["item_id"]
                    and value.get("options") != item.get("options")
                ),
                None,
            )
            if new_id is None:
                return _reply(
                    "Другой доступный вариант этого товара не найден. Учебный пример не создаёт заявку."
                )
            difference = Decimal(str(variants[new_id]["price"])) - Decimal(
                str(item["price"])
            )

            def usable(key):
                method = methods[key]
                return (
                    method.get("source") != "gift_card"
                    or Decimal(str(method.get("balance", 0))) >= difference
                )

            payment_id = (
                original
                if original in methods and usable(original)
                else next((key for key in methods if usable(key)), None)
            )
        if payment_id is None:
            return _reply(
                "Не найден допустимый способ оплаты. Учебный пример не создаёт заявку."
            )
        arguments = {
            "order_id": delivered["order_id"],
            "item_ids": [item["item_id"]],
            "payment_method_id": payment_id,
        }
        if intent == "exchange":
            arguments["new_item_ids"] = [new_id]
        proposal_name = "propose_exchange" if intent == "exchange" else "propose_return"
        if values(proposal_name):
            return _reply(
                "Предложение подготовлено. Согласия клиента и сотрудника регистрируются отдельно в приложении."
            )
        return call(proposal_name, arguments)
