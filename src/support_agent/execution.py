"""Подтверждение сотрудника, атомарное действие и отдельная тестовая доставка."""

import json
from uuid import uuid4

from support_agent.external_tickets import (
    ExternalTicketExecution,
    check_saved_approval,
    external_operation,
    unknown_outcome,
)
from support_agent.models import Message
from support_agent.proposals import Proposal
from support_agent.storage import encode
from support_agent.tickets import existing_ticket_result, register_ticket


def check_employee(case, employee_id):
    if not employee_id or case["employee_id"] != employee_id:
        raise ValueError("Нет доступа к этому обращению")


def has_consent(conn, case_id, action):
    return (
        conn.execute(
            """
        SELECT 1 FROM consents c JOIN outbox o ON o.proposal_id=c.question_id
        WHERE c.case_id=? AND c.action_hash=? AND c.accepted=1 AND c.revoked=0
        AND o.status='delivered' LIMIT 1
    """,
            (case_id, action.fingerprint()),
        ).fetchone()
        is not None
    )


class TestChannel:
    """Локальный получатель: сохраняет сообщение в SQLite без внешней отправки."""

    __test__ = False

    def __init__(self, store):
        self.store = store

    def send(self, delivery_id, case_id, text):
        with self.store.transaction() as conn:
            previous = conn.execute(
                "SELECT case_id,text FROM deliveries WHERE id=?", (delivery_id,)
            ).fetchone()
            if previous:
                if previous["case_id"] != case_id or previous["text"] != text:
                    raise ValueError(
                        "Идентификатор доставки связан с другим сообщением"
                    )
                return
            conn.execute(
                "INSERT INTO deliveries VALUES (?,?,?)", (delivery_id, case_id, text)
            )


class Executor(ExternalTicketExecution):
    def __init__(self, store, adapter_factory, channel=None, *, ticket_backend=None):
        self.store = store
        self.adapter_factory = adapter_factory
        self.channel = channel if channel is not None else TestChannel(store)
        self.ticket_backend = ticket_backend

    def approve(self, case_id, proposal_id, version, expected_digest, employee_id):
        """Подтвердить показанную версию; повтор не выполняет действие второй раз."""
        outcome = self._approve(
            case_id, proposal_id, version, expected_digest, employee_id
        )
        if outcome.pop("_submit", False):
            outcome = self._submit_external(proposal_id)
        with self.store.connection() as conn:
            operation = conn.execute(
                "SELECT result FROM operations WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        if operation:
            outcome["operation_result"] = json.loads(operation["result"])
        return outcome

    def _approve(self, case_id, proposal_id, version, expected_digest, employee_id):
        with self.store.transaction() as conn:
            case = self.store.load_case(conn, case_id)
            check_employee(case, employee_id)
            record = self.store.proposal(conn, proposal_id)
            proposal = record["proposal"]
            if (
                proposal.case_id != case_id
                or proposal.version != version
                or proposal.digest() != expected_digest
            ):
                raise ValueError("Предложение изменилось; ознакомьтесь с новой версией")
            delivery = conn.execute(
                "SELECT * FROM outbox WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if delivery:
                approval = conn.execute(
                    "SELECT digest FROM approvals WHERE proposal_id=?", (proposal_id,)
                ).fetchone()
                if approval is None or approval["digest"] != expected_digest:
                    raise ValueError("Согласованный текст изменился")
                if delivery["status"] == "delivered":
                    return {"status": "delivered", "proposal_id": proposal_id}
                self.store.event(
                    conn,
                    case_id,
                    "delivery_retry",
                    "Сотрудник продолжил доставку ранее согласованного текста",
                )
                delivery_id = delivery["id"]
            else:
                external = external_operation(conn, proposal_id=proposal_id)
                if external is not None:
                    check_saved_approval(conn, proposal, external)
                    if external["status"] in {"submitting", "unknown"}:
                        return unknown_outcome(external)
                    if external["status"] == "failed":
                        return {"status": "failed", "reason": external["reason"]}
                if record["status"] == "duplicate":
                    approval = conn.execute(
                        "SELECT digest FROM approvals WHERE proposal_id=?",
                        (proposal_id,),
                    ).fetchone()
                    if approval is None or approval["digest"] != expected_digest:
                        raise ValueError("Согласованные условия изменились")
                    return {
                        "status": "duplicate",
                        "proposal_id": proposal_id,
                        "reason": "Заявка по этому вопросу уже зарегистрирована",
                        "operation_result": existing_ticket_result(
                            conn, case, proposal
                        ),
                    }
                recovery = conn.execute(
                    "SELECT body FROM recoveries WHERE source_proposal_id=?",
                    (proposal_id,),
                ).fetchone()
                if record["status"] == "stale" and recovery:
                    check = json.loads(recovery["body"])
                    return {
                        "status": "stale",
                        "reason": check["reason"],
                        "recovery_id": proposal_id,
                        "check": check,
                    }
                if record["status"] != "pending":
                    raise ValueError(
                        "Нужно подготовить новое предложение и согласовать его"
                    )
                action = proposal.action or proposal.consent_request
                is_ticket = bool(
                    proposal.action and action.name == "create_support_ticket"
                )
                adapter = None
                if action:
                    snapshot, policy = self.store.retail(conn)
                    adapter = self.adapter_factory(snapshot, policy)
                    try:
                        if is_ticket and action.arguments.get("target") not in {
                            "local",
                            "zammad",
                        }:
                            raise ValueError("Неизвестный получатель заявки")
                        check = adapter.check(action, case["user_id"])
                        if check.status != "unchanged":
                            return self.mark_changed(conn, case, proposal, check)
                        if (
                            proposal.action
                            and not is_ticket
                            and not has_consent(conn, case_id, action)
                        ):
                            raise ValueError("Нет согласия клиента на текущие условия")
                    except ValueError as error:
                        conn.execute(
                            "UPDATE proposals SET status='stale',reason=? WHERE id=?",
                            (str(error), proposal_id),
                        )
                        self.store.event(conn, case_id, "blocked", str(error))
                        return {"status": "stale", "reason": str(error)}
                if is_ticket:
                    prepared = self._ticket_preflight(
                        conn, case, proposal, expected_digest, employee_id
                    )
                    if prepared is not None:
                        return prepared
                # Откат действия не освобождает блокировку до фиксации состояния ошибки.
                conn.execute("SAVEPOINT operation_attempt")
                try:
                    conn.execute(
                        "INSERT INTO approvals VALUES (?,?,?)",
                        (proposal_id, expected_digest, employee_id),
                    )
                    self.store.event(
                        conn,
                        case_id,
                        "approval",
                        f"Сотрудник подтвердил предложение версии {version}",
                    )
                    if proposal.action:
                        if is_ticket:
                            result = register_ticket(conn, case, proposal)
                        else:
                            result = adapter.execute(proposal.action, case["user_id"])
                            self.store.save_retail(conn, adapter.snapshot())
                        if is_ticket and result["duplicate"]:
                            result = existing_ticket_result(conn, case, proposal)
                            conn.execute(
                                "UPDATE proposals SET status='duplicate',reason=? WHERE id=?",
                                (
                                    "Заявка по этому вопросу уже зарегистрирована",
                                    proposal_id,
                                ),
                            )
                            self.store.event(conn, case_id, "duplicate", encode(result))
                            conn.execute("RELEASE operation_attempt")
                            return {
                                "status": "duplicate",
                                "proposal_id": proposal_id,
                                "reason": "Заявка по этому вопросу уже зарегистрирована",
                                "operation_result": result,
                            }
                        self.store.save_operation(conn, proposal_id, result)
                        if not is_ticket:
                            conn.execute(
                                "UPDATE consents SET revoked=1 WHERE case_id=?",
                                (case_id,),
                            )
                        self.store.event(conn, case_id, "operation", encode(result))
                        case["state"].messages.append(
                            Message(
                                role="assistant",
                                content="Служебный результат исполнителя (не сообщение клиенту): "
                                + encode(result),
                            )
                        )
                    delivery_id = uuid4().hex
                    conn.execute(
                        "INSERT INTO outbox(id,proposal_id,case_id,text) VALUES (?,?,?,?)",
                        (delivery_id, proposal_id, case_id, proposal.text),
                    )
                    conn.execute(
                        "UPDATE proposals SET status=? WHERE id=?",
                        ("executed" if proposal.action else "approved", proposal_id),
                    )
                    self.store.save_case(conn, case, case["revision"])
                except Exception as error:
                    conn.execute("ROLLBACK TO operation_attempt")
                    conn.execute("RELEASE operation_attempt")
                    conn.execute(
                        "UPDATE proposals SET status='failed',reason=? WHERE id=?",
                        (type(error).__name__, proposal_id),
                    )
                    conn.execute(
                        "UPDATE consents SET revoked=1 WHERE case_id=?", (case_id,)
                    )
                    failure = Proposal(
                        id=uuid4().hex,
                        case_id=case_id,
                        version=self.store.next_version(conn, case_id),
                        kind="message",
                        text="Операция не выполнена. Потребуется повторная проверка обращения.",
                    )
                    self.store.add_proposal(conn, failure)
                    self.store.event(
                        conn,
                        case_id,
                        "error",
                        f"Исполнение отменено: {type(error).__name__}",
                    )
                    case = self.store.load_case(conn, case_id)
                    case["state"].messages.append(
                        Message(
                            role="assistant",
                            content="Служебный результат исполнителя: операция не выполнена, изменения отменены.",
                        )
                    )
                    self.store.save_case(conn, case, case["revision"])
                    return {
                        "status": "failed",
                        "reason": "Изменения отменены; новый ответ ожидает согласования",
                    }
                else:
                    conn.execute("RELEASE operation_attempt")
        # Отправка выполняется после фиксации результата, с постоянным ключом доставки.
        return self._deliver(delivery_id)

    def mark_changed(self, conn, case, proposal, check):
        """Зафиксировать изменение и отозвать согласие в текущей транзакции."""
        body = check.model_dump(mode="json")
        conn.execute(
            "UPDATE proposals SET status='stale',reason=? WHERE id=?",
            (check.reason, proposal.id),
        )
        conn.execute("UPDATE consents SET revoked=1 WHERE case_id=?", (case["id"],))
        conn.execute(
            "INSERT INTO recoveries(source_proposal_id,case_id,body) VALUES (?,?,?)",
            (proposal.id, case["id"], encode(body)),
        )
        self.store.event(conn, case["id"], "conditions_changed", encode(body))
        self.store.save_case(conn, case, case["revision"])
        return {
            "status": "stale",
            "reason": check.reason,
            "recovery_id": proposal.id,
            "check": body,
        }

    def _deliver(self, delivery_id):
        with self.store.connection() as conn:
            delivery = dict(
                conn.execute(
                    "SELECT * FROM outbox WHERE id=?", (delivery_id,)
                ).fetchone()
            )
        try:
            self.channel.send(delivery_id, delivery["case_id"], delivery["text"])
        except Exception as error:
            with self.store.transaction() as conn:
                current = conn.execute(
                    "SELECT status FROM outbox WHERE id=?", (delivery_id,)
                ).fetchone()
                if current["status"] == "delivered":
                    return {
                        "status": "delivered",
                        "proposal_id": delivery["proposal_id"],
                    }
                conn.execute(
                    "UPDATE outbox SET status='failed',error=? WHERE id=?",
                    (type(error).__name__, delivery_id),
                )
                conn.execute(
                    "UPDATE proposals SET status='delivery_failed',reason='Не подтверждена доставка ответа' WHERE id=?",
                    (delivery["proposal_id"],),
                )
                self.store.event(
                    conn,
                    delivery["case_id"],
                    "error",
                    "Доставка не подтверждена; повтор возможен после нажатия сотрудника",
                )
            return {
                "status": "delivery_failed",
                "reason": "Действие не повторяется. Подтвердите повторную отправку того же текста.",
            }
        with self.store.transaction() as conn:
            changed = conn.execute(
                "UPDATE outbox SET status='delivered',error='' WHERE id=? AND status!='delivered'",
                (delivery_id,),
            ).rowcount
            if changed:
                conn.execute(
                    "UPDATE proposals SET status='delivered',reason='' WHERE id=?",
                    (delivery["proposal_id"],),
                )
                self.store.event(conn, delivery["case_id"], "sent", delivery["text"])
                case = self.store.load_case(conn, delivery["case_id"])
                case["state"].messages.append(
                    Message(role="assistant", content=delivery["text"])
                )
                self.store.save_case(conn, case, case["revision"])
        return {"status": "delivered", "proposal_id": delivery["proposal_id"]}
