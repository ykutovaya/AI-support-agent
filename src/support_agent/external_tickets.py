"""Сохранённое внешнее исполнение: один POST, проверка GET и отдельная доставка."""

import json
from uuid import uuid4

from support_agent.adapters.zammad import TicketRejected
from support_agent.models import Message
from support_agent.proposals import Proposal
from support_agent.storage import encode
from support_agent.tickets import REASON_CODE, existing_ticket_result

UNKNOWN_REASON = (
    "Исход регистрации неизвестен; проверьте результат в Zammad без повторного создания"
)
REJECTED_REASON = "Zammad отклонил регистрацию; новый ответ ожидает согласования"


def external_operation(conn, *, proposal_id=None, case=None, request=None):
    if proposal_id is not None:
        return conn.execute(
            "SELECT * FROM external_ticket_operations WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
    return conn.execute(
        """SELECT * FROM external_ticket_operations
        WHERE case_id=? AND user_id=? AND order_id=? AND reason_code=?""",
        (case["id"], case["user_id"], request["order_id"], REASON_CODE),
    ).fetchone()


def check_saved_approval(conn, proposal, row):
    approval = conn.execute(
        "SELECT digest FROM approvals WHERE proposal_id=?", (proposal.id,)
    ).fetchone()
    if (
        approval is None
        or approval["digest"] != row["digest"]
        or row["digest"] != proposal.digest()
        or proposal.action is None
        or proposal.action.arguments != json.loads(row["request"])
        or proposal.text != row["text"]
    ):
        raise ValueError("Сохранённые условия внешней регистрации изменились")


def unknown_outcome(row, reason=UNKNOWN_REASON):
    return {"status": "unknown", "proposal_id": row["proposal_id"], "reason": reason}


class ExternalTicketExecution:
    """Часть общего исполнителя; сетевые вызовы выполняются после фиксации SQLite."""

    def _ticket_preflight(self, conn, case, proposal, expected_digest, employee_id):
        request = proposal.action.arguments
        previous = external_operation(conn, case=case, request=request)
        if previous is not None and previous["status"] in {"submitting", "unknown"}:
            return unknown_outcome(previous)
        local = conn.execute(
            "SELECT 1 FROM support_tickets WHERE case_id=? AND order_id=? AND reason_code=?",
            (case["id"], request["order_id"], REASON_CODE),
        ).fetchone()
        if (previous is not None and previous["status"] == "verified") or (
            local and request["target"] == "zammad"
        ):
            result = existing_ticket_result(conn, case, proposal)
            conn.execute(
                "INSERT INTO approvals VALUES (?,?,?)",
                (proposal.id, expected_digest, employee_id),
            )
            conn.execute(
                "UPDATE proposals SET status='duplicate',reason=? WHERE id=?",
                ("Заявка по этому вопросу уже зарегистрирована", proposal.id),
            )
            self.store.event(conn, case["id"], "duplicate", encode(result))
            return {
                "status": "duplicate",
                "proposal_id": proposal.id,
                "reason": "Заявка по этому вопросу уже зарегистрирована",
                "operation_result": result,
            }
        if request["target"] != "zammad":
            return None
        if previous is not None:
            return {
                "status": "failed",
                "reason": REJECTED_REASON,
                "proposal_id": previous["proposal_id"],
            }
        destination = request.get("destination", {})
        if (
            self.ticket_backend is None
            or self.ticket_backend.base_url != destination.get("base_url")
        ):
            reason = "Получатель Zammad недоступен или изменился; требуется новое согласование"
            conn.execute(
                "UPDATE proposals SET status='stale',reason=? WHERE id=?",
                (reason, proposal.id),
            )
            self.store.event(conn, case["id"], "blocked", reason)
            return {"status": "stale", "reason": reason}
        operation_id = uuid4().hex
        conn.execute(
            "INSERT INTO approvals VALUES (?,?,?)",
            (proposal.id, expected_digest, employee_id),
        )
        conn.execute(
            """INSERT INTO external_ticket_operations
            (operation_id,proposal_id,case_id,user_id,order_id,reason_code,digest,request,text,status)
            VALUES (?,?,?,?,?,?,?,?,?,'submitting')""",
            (
                operation_id,
                proposal.id,
                case["id"],
                case["user_id"],
                request["order_id"],
                REASON_CODE,
                expected_digest,
                encode(request),
                proposal.text,
            ),
        )
        conn.execute(
            "UPDATE proposals SET status='submitting',reason='' WHERE id=?",
            (proposal.id,),
        )
        self.store.event(
            conn,
            case["id"],
            "approval",
            f"Сотрудник подтвердил предложение версии {proposal.version}",
        )
        return {"status": "submitting", "proposal_id": proposal.id, "_submit": True}

    def _submit_external(self, proposal_id):
        with self.store.transaction() as conn:
            row = external_operation(conn, proposal_id=proposal_id)
            proposal = self.store.proposal(conn, proposal_id)["proposal"]
            check_saved_approval(conn, proposal, row)
            # После фиксации отправляем отдельную копию только что проверенных данных.
            request = json.loads(row["request"])
            backend = self.ticket_backend
        if backend is None or backend.base_url != request["destination"]["base_url"]:
            return self._unknown_external(proposal_id)
        try:
            result = backend.create(row["operation_id"], request)
        except TicketRejected:
            return self._reject_external(proposal_id)
        except Exception as error:
            return self._unknown_external(
                proposal_id, getattr(error, "ticket_id", None)
            )
        return self._complete_external(proposal_id, result)

    def _unknown_external(self, proposal_id, ticket_id=None):
        with self.store.transaction() as conn:
            row = external_operation(conn, proposal_id=proposal_id)
            if row["status"] == "verified":
                return {"status": "executed", "proposal_id": proposal_id}
            # Не сохраняем сообщения исключений: они могут содержать токен или тело ответа.
            conn.execute(
                """UPDATE external_ticket_operations SET status='unknown',
                ticket_id=COALESCE(ticket_id,?),reason=? WHERE proposal_id=?""",
                (
                    ticket_id if type(ticket_id) is int and ticket_id > 0 else None,
                    UNKNOWN_REASON,
                    proposal_id,
                ),
            )
            conn.execute(
                "UPDATE proposals SET status='unknown',reason=? WHERE id=?",
                (UNKNOWN_REASON, proposal_id),
            )
            self.store.event(conn, row["case_id"], "ticket_unknown", UNKNOWN_REASON)
            return unknown_outcome(row)

    def _reject_external(self, proposal_id):
        with self.store.transaction() as conn:
            row = external_operation(conn, proposal_id=proposal_id)
            conn.execute(
                "UPDATE external_ticket_operations SET status='failed',reason=? WHERE proposal_id=?",
                (REJECTED_REASON, proposal_id),
            )
            conn.execute(
                "UPDATE proposals SET status='failed',reason=? WHERE id=?",
                (REJECTED_REASON, proposal_id),
            )
            failure = Proposal(
                id=uuid4().hex,
                case_id=row["case_id"],
                version=self.store.next_version(conn, row["case_id"]),
                kind="message",
                text="Обращение не зарегистрировано. Потребуется повторная проверка обращения.",
            )
            self.store.add_proposal(conn, failure)
            self.store.event(conn, row["case_id"], "error", REJECTED_REASON)
            return {"status": "failed", "reason": REJECTED_REASON}

    def _complete_external(self, proposal_id, result):
        saved = self.store.external_ticket(proposal_id)
        destination = saved["request"]["destination"]
        ticket_id = result.get("ticket_id") if isinstance(result, dict) else None
        if (
            not isinstance(result, dict)
            or type(ticket_id) is not int
            or ticket_id < 1
            or result.get("status") != "ticket created"
            or result.get("target") != "zammad"
            or result.get("group_id") != destination["group_id"]
            or result.get("customer_id") != destination["customer_id"]
            or result.get("ticket_url")
            != f"{destination['base_url']}/#ticket/zoom/{ticket_id}"
            or not result.get("ticket_number")
            or not result.get("ticket_status")
            or result.get("duplicate") is not False
            or (saved["ticket_id"] is not None and saved["ticket_id"] != ticket_id)
        ):
            return self._unknown_external(proposal_id, ticket_id)
        with self.store.transaction() as conn:
            row = external_operation(conn, proposal_id=proposal_id)
            proposal = self.store.proposal(conn, proposal_id)["proposal"]
            check_saved_approval(conn, proposal, row)
            delivery = conn.execute(
                "SELECT * FROM outbox WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if delivery:
                if delivery["status"] == "delivered":
                    return {"status": "delivered", "proposal_id": proposal_id}
                delivery_id = delivery["id"]
            else:
                case = self.store.load_case(conn, row["case_id"])
                self.store.save_operation(conn, proposal_id, result)
                conn.execute(
                    """UPDATE external_ticket_operations SET status='verified',ticket_id=?,result=?,reason=''
                    WHERE proposal_id=?""",
                    (ticket_id, encode(result), proposal_id),
                )
                delivery_id = uuid4().hex
                conn.execute(
                    "INSERT INTO outbox(id,proposal_id,case_id,text) VALUES (?,?,?,?)",
                    (delivery_id, proposal_id, case["id"], row["text"]),
                )
                conn.execute(
                    "UPDATE proposals SET status='executed',reason='' WHERE id=?",
                    (proposal_id,),
                )
                self.store.event(conn, case["id"], "operation", encode(result))
                case["state"].messages.append(
                    Message(
                        role="assistant",
                        content="Служебный результат исполнителя (не сообщение клиенту): "
                        + encode(result),
                    )
                )
                self.store.save_case(conn, case, case["revision"])
        return self._deliver(delivery_id)

    def reconcile(self, case_id, proposal_id, version, expected_digest, employee_id):
        """Явная проверка неизвестного исхода; никогда не повторяет POST."""
        from support_agent.execution import check_employee

        with self.store.connection() as conn:
            case = self.store.load_case(conn, case_id)
            check_employee(case, employee_id)
            proposal = self.store.proposal(conn, proposal_id)["proposal"]
            if (
                proposal.case_id != case_id
                or proposal.version != version
                or proposal.digest() != expected_digest
            ):
                raise ValueError("Предложение изменилось; ознакомьтесь с новой версией")
            row = external_operation(conn, proposal_id=proposal_id)
            if row is None:
                raise ValueError("Внешняя регистрация не начиналась")
            check_saved_approval(conn, proposal, row)
            status = row["status"]
            request = json.loads(row["request"])
        if status == "verified":
            return self.approve(
                case_id, proposal_id, version, expected_digest, employee_id
            )
        if status == "failed":
            return {"status": "failed", "reason": REJECTED_REASON}
        if (
            self.ticket_backend is None
            or self.ticket_backend.base_url != request["destination"]["base_url"]
        ):
            return unknown_outcome(
                row, "Восстановите подключение к согласованному экземпляру Zammad"
            )
        try:
            result = self.ticket_backend.find(
                row["operation_id"], request, ticket_id=row["ticket_id"]
            )
        except Exception as error:
            return self._unknown_external(
                proposal_id, getattr(error, "ticket_id", None)
            )
        if result is None:
            return self._unknown_external(proposal_id)
        outcome = self._complete_external(proposal_id, result)
        with self.store.connection() as conn:
            operation = conn.execute(
                "SELECT result FROM operations WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
            if operation:
                outcome["operation_result"] = json.loads(operation["result"])
        return outcome
