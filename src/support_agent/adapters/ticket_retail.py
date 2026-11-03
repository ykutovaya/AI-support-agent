"""Планирование обращения специалисту по зарегистрированной заявке магазина."""

from copy import deepcopy
from urllib.parse import urlsplit

from support_agent.adapters.retail import RetailAdapter, _condition_changes
from support_agent.models import ToolSpec
from support_agent.proposals import Action, ConditionCheck, Evidence, digest

PROPOSE_TICKET = "propose_support_ticket"
CREATE_TICKET = "create_support_ticket"
REQUESTED_STATUSES = frozenset({"exchange requested", "return requested"})
RULE_SOURCE = "policy.md#requested-order-result"
RULE_VERSION = "1"
TICKET_ARGUMENTS = {"order_id", "user_id", "target", "title", "body", "question"}
TICKET_CONDITIONS = {"order", "user_id", "policy", "target", "question"}
QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "minLength": 1},
        "question": {
            "type": "string",
            "minLength": 1,
            "maxLength": 500,
            "pattern": r"\S",
        },
    },
    "required": ["order_id", "question"],
    "additionalProperties": False,
}


class TicketRetailAdapter(RetailAdapter):
    """Готовит обращение в выбранной конфигурацией системе поддержки."""

    def __init__(
        self,
        snapshot: dict,
        policy: str,
        *,
        target: str = "local",
        destination: dict | None = None,
        customer_mapping: dict | None = None,
    ):
        super().__init__(snapshot, policy)
        if not isinstance(target, str) or not target.strip():
            raise ValueError("Укажите получателя обращения")
        self.target = target
        self.destination = deepcopy(destination)
        self.customer_mapping = deepcopy(customer_mapping or {})
        self._schemas[PROPOSE_TICKET] = deepcopy(QUESTION_SCHEMA)

    def specs(self) -> list[ToolSpec]:
        return [
            *super().specs(),
            ToolSpec(
                name=PROPOSE_TICKET,
                description=(
                    "Подготовить обращение специалисту в настроенной системе поддержки, когда клиент спрашивает текущий "
                    "результат обмена или возврата, а заказ имеет статус exchange requested "
                    "или return requested. Эти статусы подтверждают регистрацию заявки, "
                    "но не фактическое исполнение. Передайте только нерешённый вопрос "
                    "без адреса и платёжных данных. Регистрация выполняется после "
                    "подтверждения сотрудником."
                ),
                parameters=deepcopy(QUESTION_SCHEMA),
            ),
        ]

    def _ticket_conditions(self, order, user_id: str, question: str) -> dict:
        values = {
            "order": {
                "order_id": order.order_id,
                "user_id": order.user_id,
                "status": order.status,
            },
            "user_id": user_id,
            "policy": self.policy,
            "target": self.target,
            "question": question,
        }
        if self.target == "zammad":
            customer_id = self.customer_mapping.get(user_id)
            if type(customer_id) is not int or customer_id <= 0:
                raise ValueError(
                    "Не настроено соответствие клиента тестовому клиенту Zammad"
                )
            if not isinstance(self.destination, dict) or set(self.destination) != {
                "base_url",
                "group_id",
                "group_name",
            }:
                raise ValueError("Не настроено назначение Zammad")
            destination = {**self.destination, "customer_id": customer_id}
            self._validate_destination(destination)
            values["destination"] = destination
        return values

    @staticmethod
    def _validate_destination(destination):
        if not isinstance(destination, dict) or set(destination) != {
            "base_url",
            "group_id",
            "group_name",
            "customer_id",
        }:
            raise ValueError("Некорректное назначение Zammad")
        if any(
            type(destination[key]) is not int or destination[key] <= 0
            for key in ("group_id", "customer_id")
        ):
            raise ValueError("Некорректное назначение Zammad")
        if (
            not isinstance(destination["group_name"], str)
            or not destination["group_name"].strip()
        ):
            raise ValueError("Некорректное назначение Zammad")
        if not isinstance(destination["base_url"], str):
            raise ValueError("Некорректное назначение Zammad")
        url = urlsplit(destination["base_url"])
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Некорректное назначение Zammad")

    @staticmethod
    def _keys(base, target):
        return base | {"destination"} if target == "zammad" else base

    @staticmethod
    def _ticket_action(values: dict) -> Action:
        if values["target"] == "zammad":
            TicketRetailAdapter._validate_destination(values["destination"])
        order = values["order"]
        if order["status"] not in REQUESTED_STATUSES:
            raise ValueError(
                "Обращение возможно только по зарегистрированной заявке на обмен или возврат"
            )
        order_id, user_id = order["order_id"], values["user_id"]
        policy_version = digest({"policy": values["policy"]})
        evidence = [
            Evidence(
                title="Заказ клиента",
                source=f"orders/{order_id}",
                detail=f"Заказ {order_id}, клиент {user_id}, статус {order['status']}.",
            ),
            Evidence(
                title="Применённое правило",
                source=RULE_SOURCE,
                detail=(
                    f"Версия правила {RULE_VERSION}; версия правил магазина {policy_version}. "
                    "Заявка на обмен или возврат зарегистрирована. Фактическое исполнение "
                    "не установлено по данным retail; для ответа о текущем результате "
                    "требуется проверка специалиста."
                ),
            ),
            Evidence(
                title="Выполненные проверки",
                source=f"orders/{order_id}",
                detail=(
                    "Проверены принадлежность заказа клиенту обращения, наличие заказа "
                    "в списке его заказов и статус зарегистрированной заявки."
                ),
            ),
        ]
        title = f"Уточнение результата заявки по заказу {order_id}"
        body = "\n".join(
            f"{item.title}: {item.detail} Источник: {item.source}." for item in evidence
        )
        body += f"\nНерешённый вопрос клиента: {values['question']}"
        return Action(
            name=CREATE_TICKET,
            arguments={
                "order_id": order_id,
                "user_id": user_id,
                "target": values["target"],
                "title": title,
                "body": body,
                "question": values["question"],
                **(
                    {"destination": deepcopy(values["destination"])}
                    if values["target"] == "zammad"
                    else {}
                ),
            },
            amount=None,
            currency="",
            conditions=digest(values),
            condition_values=deepcopy(values),
            result_status="ticket created",
            description=f"Создать обращение специалисту по заказу {order_id}",
            evidence=evidence,
        )

    def plan(self, name: str, args: dict, user_id: str) -> Action:
        if name not in {PROPOSE_TICKET, CREATE_TICKET}:
            return super().plan(name, args, user_id)
        request = args
        if name == CREATE_TICKET:
            if set(args) != self._keys(TICKET_ARGUMENTS, args.get("target")):
                raise ValueError("Некорректные параметры обращения")
            request = {"order_id": args["order_id"], "question": args["question"]}
        request = self._arguments(PROPOSE_TICKET, request)
        order = self._order(request["order_id"], user_id)
        action = self._ticket_action(
            self._ticket_conditions(order, user_id, request["question"])
        )
        if name == CREATE_TICKET and args != action.arguments:
            raise ValueError("Параметры обращения не соответствуют проверенным данным")
        return action

    def check(self, action: Action, user_id: str) -> ConditionCheck:
        if action.name != CREATE_TICKET:
            return super().check(action, user_id)
        try:
            args = action.arguments
            if set(args) != self._keys(TICKET_ARGUMENTS, args.get("target")):
                raise ValueError("Некорректные параметры обращения")
            request = self._arguments(
                PROPOSE_TICKET,
                {"order_id": args["order_id"], "question": args["question"]},
            )
            # Сначала проверяются права, затем читаются данные для сравнения.
            order = self._order(request["order_id"], user_id)
        except ValueError as error:
            return ConditionCheck(status="forbidden", reason=str(error))

        before = deepcopy(action.condition_values)
        try:
            if set(before) != self._keys(TICKET_CONDITIONS, before.get("target")):
                raise ValueError
            if set(before["order"]) != {"order_id", "user_id", "status"}:
                raise ValueError
            if (
                before["order"]["order_id"] != args["order_id"]
                or before["order"]["user_id"] != user_id
                or before["user_id"] != user_id
                or before["question"] != args["question"]
                or not isinstance(before["policy"], str)
                or not isinstance(before["target"], str)
                or not before["target"].strip()
                or self._ticket_action(before).fingerprint() != action.fingerprint()
            ):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return ConditionCheck(
                status="forbidden",
                reason="Сохранённое обращение или его условия повреждены; сформируйте новое предложение",
            )

        try:
            after = self._ticket_conditions(order, user_id, request["question"])
        except ValueError as error:
            return ConditionCheck(status="forbidden", reason=str(error))
        changes = _condition_changes(before, after)
        try:
            current = self._ticket_action(after)
        except ValueError as error:
            return ConditionCheck(
                status="forbidden", reason=str(error), changes=changes
            )
        if changes:
            return ConditionCheck(
                status="changed",
                reason="Условия изменились; требуется новое согласование",
                changes=changes,
                current=current,
            )
        return ConditionCheck(
            status="unchanged", reason="Условия не изменились", current=current
        )

    def execute(self, action: Action, user_id: str) -> dict:
        if action.name == CREATE_TICKET:
            raise ValueError(
                "Создание обращения выполняет отдельный исполнитель после подтверждения"
            )
        return super().execute(action, user_id)
