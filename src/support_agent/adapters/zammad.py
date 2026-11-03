"""Zammad: одна попытка записи и обязательная сверка сохранённой заявки."""

import json
import re
import ssl
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

TIMEOUT_SECONDS = 15.0
MAX_RESPONSE_BYTES = 2_000_000
REJECTION_STATUSES = frozenset({400, 401, 403, 404, 422})


class TicketRejected(Exception):
    """Запись не выполнялась или Zammad однозначно отклонил создание."""


class TicketOutcomeUnknown(Exception):
    """Результат записи нельзя подтвердить; повторное создание запрещено."""

    def __init__(self, message="Результат операции Zammad не подтверждён", *, ticket_id=None):
        super().__init__(message)
        self.ticket_id = ticket_id


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Токен и тело не должны уходить по адресу из ответа сервера.
        return None


class UrllibTransport:
    """Транспорт без повторов; callable можно заменить в автономных проверках."""

    def __call__(self, method, url, *, headers, body, timeout):
        # create_default_context загружает и системные доверенные сертификаты Windows.
        context = ssl.create_default_context()
        opener = build_opener(HTTPSHandler(context=context), _NoRedirect())
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            # Ответ ошибки может содержать секреты и данные клиентов.
            status = error.code
            error.close()
            return status, b""


def normalize_base_url(value: str) -> str:
    """Разрешён только HTTPS-адрес сервера без учётных данных и пути API."""
    try:
        if not isinstance(value, str) or any(char.isspace() for char in value):
            raise ValueError
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "\\" in value
        ):
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
        if port is not None and not 0 < port <= 65535:
            raise ValueError
        if ":" in host:
            host = f"[{host}]"
        authority = host if port in {None, 443} else f"{host}:{port}"
        return f"https://{authority}"
    except (ValueError, UnicodeError):
        raise TicketRejected("Некорректный адрес сервера Zammad") from None


def _positive_id(value):
    return type(value) is int and value > 0


class ZammadBackend:
    """Отправляет согласованный текст и сверяет результат по постоянному тегу."""

    def __init__(self, base_url: str, token: str, *, transport=None):
        self.base_url = normalize_base_url(base_url)
        if (
            not isinstance(token, str)
            or not token
            or any(char.isspace() or ord(char) < 32 for char in token)
        ):
            raise TicketRejected("Не задан корректный токен Zammad")
        self._token = token
        self._transport = transport if transport is not None else UrllibTransport()

    def _validate(self, operation_id, request):
        if (
            not isinstance(operation_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", operation_id) is None
        ):
            raise TicketRejected("Некорректный идентификатор операции")
        if not isinstance(request, dict) or request.get("target") != "zammad":
            raise TicketRejected("Некорректные параметры обращения Zammad")
        for key in ("title", "body", "order_id", "user_id", "question"):
            if not isinstance(request.get(key), str) or not request[key].strip():
                raise TicketRejected("Некорректные параметры обращения Zammad")
        destination = request.get("destination")
        if (
            not isinstance(destination, dict)
            or normalize_base_url(destination.get("base_url")) != self.base_url
            or not _positive_id(destination.get("group_id"))
            or not _positive_id(destination.get("customer_id"))
            or not isinstance(destination.get("group_name"), str)
            or not destination["group_name"].strip()
        ):
            raise TicketRejected("Получатель обращения не соответствует настройкам Zammad")
        return "asa-op-" + operation_id

    def _json(self, method, path, *, payload=None, ticket_id=None):
        headers = {
            "Authorization": "Token token=" + self._token,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Zammad-Suppress-Notifications": "true",
        }
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            status, raw = self._transport(
                method,
                self.base_url + "/api/v1/" + path,
                headers=headers,
                body=body,
                timeout=TIMEOUT_SECONDS,
            )
        except Exception:
            raise TicketOutcomeUnknown(
                "Ответ Zammad не получен; требуется проверка результата",
                ticket_id=ticket_id,
            ) from None
        if method == "POST" and status in REJECTION_STATUSES:
            raise TicketRejected("Zammad отклонил создание обращения")
        expected_status = 201 if method == "POST" else 200
        if status != expected_status:
            raise TicketOutcomeUnknown(
                "Zammad не подтвердил результат операции", ticket_id=ticket_id
            )
        try:
            if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError
            return json.loads(raw)
        except (ValueError, TypeError, UnicodeError):
            raise TicketOutcomeUnknown(
                "Получен непроверяемый ответ Zammad", ticket_id=ticket_id
            ) from None

    def create(self, operation_id: str, request: dict) -> dict:
        tag = self._validate(operation_id, request)
        destination = request["destination"]
        payload = {
            "title": request["title"],
            "group_id": destination["group_id"],
            "customer_id": destination["customer_id"],
            "state": "new",
            # API принимает строку тегов через запятую в транзакции создания.
            "tags": tag,
            "article": {
                "subject": request["title"],
                "body": request["body"],
                "type": "note",
                "sender": "Agent",
                "internal": True,
                "content_type": "text/plain",
            },
        }
        created = self._json("POST", "tickets", payload=payload)
        ticket_id = created.get("id") if isinstance(created, dict) else None
        if not _positive_id(ticket_id):
            raise TicketOutcomeUnknown("Zammad не вернул идентификатор обращения")
        return self._verify(ticket_id, tag, request)

    def find(self, operation_id: str, request: dict, ticket_id: int | None = None) -> dict | None:
        """Пустой поиск не доказывает отсутствие записи: индекс обновляется с задержкой."""
        tag = self._validate(operation_id, request)
        if ticket_id is not None:
            if not _positive_id(ticket_id):
                raise TicketOutcomeUnknown("Повреждён идентификатор обращения Zammad")
            return self._verify(ticket_id, tag, request)
        query = urlencode({
            "query": f'tags:"{tag}"',
            "expand": "true",
            "limit": 2,
            "per_page": 2,
            "page": 1,
        })
        matches = self._json("GET", "tickets/search?" + query)
        if not isinstance(matches, list):
            raise TicketOutcomeUnknown("Не удалось проверить результаты поиска Zammad")
        if not matches:
            return None
        if len(matches) != 1 or not isinstance(matches[0], dict):
            raise TicketOutcomeUnknown("Найдено неоднозначное соответствие операции Zammad")
        ticket_id = matches[0].get("id")
        if not _positive_id(ticket_id):
            raise TicketOutcomeUnknown("Не удалось проверить идентификатор найденной заявки")
        return self._verify(ticket_id, tag, request)

    def _verify(self, ticket_id, tag, request):
        ticket = self._json("GET", f"tickets/{ticket_id}?expand=true", ticket_id=ticket_id)
        destination = request["destination"]
        if (
            not isinstance(ticket, dict)
            or ticket.get("id") != ticket_id
            or not _positive_id(ticket.get("id"))
            or ticket.get("title") != request["title"]
            or not _positive_id(ticket.get("group_id"))
            or ticket.get("group_id") != destination["group_id"]
            or ticket.get("group") != destination["group_name"]
            or not _positive_id(ticket.get("customer_id"))
            or ticket.get("customer_id") != destination["customer_id"]
            or not isinstance(ticket.get("state"), str)
            or ticket.get("state") not in {"new", "open"}
            or not isinstance(ticket.get("number"), str)
            or not ticket["number"].strip()
        ):
            raise TicketOutcomeUnknown(
                "Сохранённое обращение не соответствует согласованным параметрам",
                ticket_id=ticket_id,
            )
        tags = self._json("GET", f"tags?object=Ticket&o_id={ticket_id}", ticket_id=ticket_id)
        if not isinstance(tags, dict) or not isinstance(tags.get("tags"), list) or tag not in tags["tags"]:
            raise TicketOutcomeUnknown(
                "Не подтверждена связь обращения с операцией", ticket_id=ticket_id
            )
        articles = self._json("GET", f"ticket_articles/by_ticket/{ticket_id}", ticket_id=ticket_id)
        if not isinstance(articles, list) or not any(
            isinstance(article, dict)
            and article.get("ticket_id") == ticket_id
            and article.get("body") == request["body"]
            and article.get("type") == "note"
            and article.get("sender") == "Agent"
            and article.get("internal") is True
            and article.get("content_type") == "text/plain"
            for article in articles
        ):
            raise TicketOutcomeUnknown(
                "Не подтверждён согласованный текст внутренней заметки",
                ticket_id=ticket_id,
            )
        return {
            "status": "ticket created",
            "target": "zammad",
            "ticket_id": ticket_id,
            "ticket_number": ticket["number"],
            "ticket_status": ticket["state"],
            "ticket_url": f"{self.base_url}/#ticket/zoom/{ticket_id}",
            "group_id": destination["group_id"],
            "customer_id": destination["customer_id"],
            "duplicate": False,
        }
