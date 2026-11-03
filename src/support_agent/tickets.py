"""Регистрация локальной заявки в общей транзакции согласования."""

import json

REASON_CODE = "requested_order_result"


def existing_ticket_result(conn, case, proposal):
    """Прочитать результат исходной операции для повторного предложения."""
    request = proposal.action.arguments
    if (
        request["target"] not in {"local", "zammad"}
        or request["user_id"] != case["user_id"]
    ):
        raise ValueError("Некорректный получатель или клиент заявки")
    row = conn.execute(
        """SELECT o.result FROM support_tickets t
        JOIN operations o ON o.proposal_id=t.proposal_id
        JOIN proposals p ON p.id=t.proposal_id
        WHERE t.case_id=? AND p.case_id=? AND t.user_id=? AND t.order_id=? AND t.reason_code=?""",
        (case["id"], case["id"], case["user_id"], request["order_id"], REASON_CODE),
    ).fetchone()
    if row is None:
        row = conn.execute(
            """SELECT o.result FROM external_ticket_operations t
            JOIN operations o ON o.proposal_id=t.proposal_id
            WHERE t.case_id=? AND t.user_id=? AND t.order_id=? AND t.reason_code=?
            AND t.status='verified'""",
            (case["id"], case["user_id"], request["order_id"], REASON_CODE),
        ).fetchone()
    if row is None:
        raise ValueError("Не найден результат исходной регистрации заявки")
    return json.loads(row["result"])


def register_ticket(conn, case, proposal):
    request = proposal.action.arguments
    if request["target"] != "local" or request["user_id"] != case["user_id"]:
        raise ValueError("Некорректный получатель или клиент локальной заявки")
    existing = conn.execute(
        "SELECT * FROM support_tickets WHERE case_id=? AND order_id=? AND reason_code=?",
        (case["id"], request["order_id"], REASON_CODE),
    ).fetchone()
    duplicate = existing is not None
    if existing:
        ticket_id = existing["id"]
        original = conn.execute(
            "SELECT body FROM proposals WHERE id=? AND case_id=?",
            (existing["proposal_id"], case["id"]),
        ).fetchone()
        if original is None:
            raise ValueError("Не найдено согласованное основание заявки")
        approved = json.loads(original["body"])["action"]["arguments"]
    else:
        ticket_id = conn.execute(
            """INSERT INTO support_tickets
            (proposal_id,case_id,user_id,order_id,reason_code,title,body,status)
            VALUES (?,?,?,?,?,?,?,'open')""",
            (
                proposal.id,
                case["id"],
                case["user_id"],
                request["order_id"],
                REASON_CODE,
                request["title"],
                request["body"],
            ),
        ).lastrowid
        approved = request
    ticket = conn.execute(
        "SELECT * FROM support_tickets WHERE id=?", (ticket_id,)
    ).fetchone()
    if (
        ticket is None
        or ticket["id"] != ticket_id
        or ticket["id"] < 1
        or ticket["case_id"] != case["id"]
        or ticket["status"] != "open"
        or any(
            ticket[key] != approved[key]
            for key in ("user_id", "order_id", "title", "body")
        )
    ):
        raise ValueError("Сохранённая заявка не соответствует согласованным условиям")
    return {
        "status": "ticket created",
        "ticket_id": ticket["id"],
        "ticket_number": f"T-{ticket['id']}",
        "ticket_status": ticket["status"],
        "target": "local",
        "duplicate": duplicate,
    }
