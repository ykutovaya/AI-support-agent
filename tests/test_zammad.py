"""Проверка Zammad без сети: создание, сверка результата и безопасное восстановление."""

import json
import ssl
import traceback
from copy import deepcopy
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from support_agent.adapters.zammad import (
    TicketOutcomeUnknown,
    TicketRejected,
    UrllibTransport,
    ZammadBackend,
)

BASE_URL = "https://support.example.test"
OPERATION_ID = "a07e19b3dd524e6bbbc4ba5df5971cb6"
TAG = "asa-op-" + OPERATION_ID
TOKEN = "secret-token-must-not-appear"
REQUEST = {
    "order_id": "#O1",
    "user_id": "u1",
    "target": "zammad",
    "title": "Уточнение результата заявки по заказу #O1",
    "body": "Согласованные факты.\nНерешённый вопрос: когда завершится обмен?",
    "question": "Когда завершится обмен?",
    "destination": {
        "base_url": BASE_URL,
        "group_id": 2,
        "group_name": "Retail Demo",
        "customer_id": 4,
    },
}
TICKET = {
    "id": 27,
    "number": "92027",
    "title": REQUEST["title"],
    "group_id": 2,
    "group": "Retail Demo",
    "customer_id": 4,
    "state": "new",
}
ARTICLE = {
    "id": 32,
    "ticket_id": 27,
    "subject": REQUEST["title"],
    "body": REQUEST["body"],
    "type": "note",
    "sender": "Agent",
    "internal": True,
    "content_type": "text/plain",
}
RESULT = {
    "status": "ticket created",
    "target": "zammad",
    "ticket_id": 27,
    "ticket_number": "92027",
    "ticket_status": "new",
    "ticket_url": BASE_URL + "/#ticket/zoom/27",
    "group_id": 2,
    "customer_id": 4,
    "duplicate": False,
}


def response(value, status=200):
    return status, json.dumps(value, ensure_ascii=False).encode("utf-8")


class FakeTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, *, headers, body, timeout):
        self.calls.append((method, url, headers, body, timeout))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def readback(ticket=None, article=None, tags=None):
    return [
        response(TICKET if ticket is None else ticket),
        response({"tags": [TAG] if tags is None else tags}),
        response([ARTICLE if article is None else article]),
    ]


def backend(replies):
    transport = FakeTransport(replies)
    return ZammadBackend(BASE_URL, TOKEN, transport=transport), transport


def test_create_preserves_approved_text_and_requires_verified_readback():
    service, transport = backend([response({"id": 27}, 201), *readback()])
    assert service.create(OPERATION_ID, REQUEST) == RESULT
    method, url, headers, body, timeout = transport.calls[0]
    assert (method, url) == ("POST", BASE_URL + "/api/v1/tickets")
    assert json.loads(body) == {
        "title": REQUEST["title"],
        "group_id": 2,
        "customer_id": 4,
        "state": "new",
        "tags": TAG,
        "article": {
            "subject": REQUEST["title"],
            "body": REQUEST["body"],
            "type": "note",
            "sender": "Agent",
            "internal": True,
            "content_type": "text/plain",
        },
    }
    assert headers["X-Zammad-Suppress-Notifications"] == "true"
    assert headers["Authorization"] == "Token token=" + TOKEN
    assert headers["Content-Type"] == "application/json"
    assert 0 < timeout <= 30
    assert [(call[0], urlsplit(call[1]).path) for call in transport.calls[1:]] == [
        ("GET", "/api/v1/tickets/27"),
        ("GET", "/api/v1/tags"),
        ("GET", "/api/v1/ticket_articles/by_ticket/27"),
    ]
    assert parse_qs(urlsplit(transport.calls[1][1]).query) == {"expand": ["true"]}
    assert parse_qs(urlsplit(transport.calls[2][1]).query) == {
        "object": ["Ticket"], "o_id": ["27"]
    }


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_definite_http_rejection_never_becomes_success_or_retries(status):
    service, transport = backend([response({"error": TOKEN}, status)])
    with pytest.raises(TicketRejected) as caught:
        service.create(OPERATION_ID, REQUEST)
    assert len(transport.calls) == 1
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "failure",
    [TimeoutError(TOKEN), OSError(TOKEN), response({}, 503), response({}, 429),
     response({}, 302), response({}, 307)],
)
def test_uncertain_post_never_retries_or_exposes_raw_error(failure):
    service, transport = backend([failure])
    with pytest.raises(TicketOutcomeUnknown) as caught:
        service.create(OPERATION_ID, REQUEST)
    assert len(transport.calls) == 1
    assert caught.value.ticket_id is None
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize(
    ("field", "value"),
    [("title", "Изменённый заголовок"), ("group_id", 3), ("customer_id", 8),
     ("state", "closed"), ("id", 28), ("number", ""), ("group", "Другая группа")],
)
def test_changed_ticket_readback_remains_unknown_with_known_id(field, value):
    ticket = {**TICKET, field: value}
    service, _ = backend([response({"id": 27}, 201), *readback(ticket=ticket)])
    with pytest.raises(TicketOutcomeUnknown) as caught:
        service.create(OPERATION_ID, REQUEST)
    assert caught.value.ticket_id == 27


@pytest.mark.parametrize("state", [[], {}])
def test_malformed_state_is_unknown_with_known_id(state):
    service, _ = backend([
        response({"id": 27}, 201), *readback(ticket={**TICKET, "state": state})
    ])
    with pytest.raises(TicketOutcomeUnknown) as caught:
        service.create(OPERATION_ID, REQUEST)
    assert caught.value.ticket_id == 27


@pytest.mark.parametrize("field", ["group_id", "customer_id"])
def test_boolean_destination_id_in_readback_cannot_confirm_integer_id(field):
    request = deepcopy(REQUEST)
    request["destination"][field] = 1
    service, _ = backend([
        response({"id": 27}, 201), *readback(ticket={**TICKET, field: True})
    ])
    with pytest.raises(TicketOutcomeUnknown):
        service.create(OPERATION_ID, request)


@pytest.mark.parametrize(
    ("field", "value"),
    [("body", "Другие факты"), ("type", "email"), ("sender", "Customer"),
     ("internal", False), ("content_type", "text/html"), ("ticket_id", 29)],
)
def test_wrong_article_is_not_evidence_of_approved_internal_note(field, value):
    article = {**ARTICLE, field: value}
    service, _ = backend([response({"id": 27}, 201), *readback(article=article)])
    with pytest.raises(TicketOutcomeUnknown) as caught:
        service.create(OPERATION_ID, REQUEST)
    assert caught.value.ticket_id == 27


@pytest.mark.parametrize("tags", [[], [TAG + "extra"], ["другая-операция"]])
def test_success_requires_exact_persisted_operation_tag(tags):
    service, _ = backend([response({"id": 27}, 201), *readback(tags=tags)])
    with pytest.raises(TicketOutcomeUnknown) as caught:
        service.create(OPERATION_ID, REQUEST)
    assert caught.value.ticket_id == 27


@pytest.mark.parametrize("failure", [response({}, 403), response({}, 404), TimeoutError()])
def test_read_failure_after_post_is_unknown_even_for_http_rejection(failure):
    service, _ = backend([response({"id": 27}, 201), failure])
    with pytest.raises(TicketOutcomeUnknown) as caught:
        service.create(OPERATION_ID, REQUEST)
    assert caught.value.ticket_id == 27


def test_find_known_ticket_reads_and_verifies_without_post_or_search():
    service, transport = backend(readback())
    assert service.find(OPERATION_ID, REQUEST, ticket_id=27) == RESULT
    assert all(call[0] == "GET" for call in transport.calls)
    assert urlsplit(transport.calls[0][1]).path == "/api/v1/tickets/27"


def test_find_by_tag_reads_current_values_and_does_not_trust_search_copy():
    service, transport = backend([response([{"id": 27}]), *readback()])
    assert service.find(OPERATION_ID, REQUEST) == RESULT
    search = transport.calls[0]
    assert search[0] == "GET"
    assert urlsplit(search[1]).path == "/api/v1/tickets/search"
    assert parse_qs(urlsplit(search[1]).query)["query"] == [f'tags:"{TAG}"']
    assert all(call[0] == "GET" for call in transport.calls)


def test_empty_search_is_inconclusive_and_never_creates_ticket():
    service, transport = backend([response([])])
    assert service.find(OPERATION_ID, REQUEST) is None
    assert len(transport.calls) == 1 and transport.calls[0][0] == "GET"


def test_multiple_search_matches_require_manual_reconciliation():
    service, transport = backend([response([{"id": 27}, {"id": 28}])])
    with pytest.raises(TicketOutcomeUnknown):
        service.find(OPERATION_ID, REQUEST)
    assert all(call[0] == "GET" for call in transport.calls)


def test_known_ticket_id_does_not_bypass_operation_tag_validation():
    service, _ = backend(readback(tags=[]))
    with pytest.raises(TicketOutcomeUnknown) as caught:
        service.find(OPERATION_ID, REQUEST, ticket_id=27)
    assert caught.value.ticket_id == 27


def test_constructor_has_no_io_and_normalizes_same_destination():
    transport = FakeTransport([response({"id": 27}, 201), *readback()])
    service = ZammadBackend("HTTPS://SUPPORT.EXAMPLE.TEST:443/", TOKEN, transport=transport)
    assert service.base_url == BASE_URL
    assert transport.calls == []
    request = deepcopy(REQUEST)
    request["destination"]["base_url"] = "https://SUPPORT.EXAMPLE.TEST/"
    assert service.create(OPERATION_ID, request) == RESULT


@pytest.mark.parametrize(
    ("field", "value"),
    [("base_url", "https://other.example.test"), ("group_id", 0),
     ("group_id", True), ("customer_id", "4"), ("group_name", "")],
)
def test_invalid_destination_is_rejected_before_network(field, value):
    service, transport = backend([])
    request = deepcopy(REQUEST)
    request["destination"][field] = value
    with pytest.raises(TicketRejected):
        service.create(OPERATION_ID, request)
    assert transport.calls == []


@pytest.mark.parametrize(
    "url",
    ["http://support.example.test", "https://user:secret@support.example.test",
     "https://support.example.test/?token=secret", "https://support.example.test/#a",
     "https://support.example.test/api/v1", "https://support.example.test/\n"],
)
def test_unsafe_backend_url_is_rejected_without_io(url):
    with pytest.raises(TicketRejected):
        ZammadBackend(url, TOKEN, transport=FakeTransport([]))


@pytest.mark.parametrize("operation_id", ["", "one,two", 'one" OR *', "a\nb"])
def test_unsafe_operation_id_cannot_inject_tag_or_search(operation_id):
    service, transport = backend([])
    with pytest.raises(TicketRejected):
        service.create(operation_id, REQUEST)
    assert transport.calls == []


@pytest.mark.parametrize("payload", [b"not-json", b"[]", b'{"id":true}'])
def test_created_http_status_without_valid_id_is_not_success(payload):
    service, _ = backend([(201, payload)])
    with pytest.raises(TicketOutcomeUnknown):
        service.create(OPERATION_ID, REQUEST)


def test_urllib_transport_verifies_tls_bounds_reads_and_disables_redirects(monkeypatch):
    import support_agent.adapters.zammad as module

    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            captured["read_size"] = size
            return b"{}"

    class Opener:
        def open(self, request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

    def build(*handlers):
        captured["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(module, "build_opener", build)
    transport = UrllibTransport()
    assert transport("GET", BASE_URL, headers={}, body=None, timeout=7) == (200, b"{}")
    https = next(item for item in captured["handlers"] if hasattr(item, "_context"))
    assert https._context.verify_mode == ssl.CERT_REQUIRED
    assert https._context.check_hostname is True
    assert 0 < captured["read_size"] <= 2_000_001
    redirect = next(item for item in captured["handlers"] if hasattr(item, "redirect_request"))
    assert redirect.redirect_request(None, None, 302, "move", {}, "https://other.test") is None
    assert captured["timeout"] == 7


def test_urllib_http_error_does_not_read_or_expose_response_body(monkeypatch):
    import support_agent.adapters.zammad as module

    class Opener:
        def open(self, request, timeout):
            raise HTTPError(BASE_URL, 403, TOKEN, {}, None)

    monkeypatch.setattr(module, "build_opener", lambda *handlers: Opener())
    service = ZammadBackend(BASE_URL, TOKEN)
    with pytest.raises(TicketRejected) as caught:
        service.create(OPERATION_ID, REQUEST)
    assert TOKEN not in "".join(traceback.format_exception(caught.value))


def test_oversized_response_is_unknown_and_never_retried():
    service, transport = backend([(201, b" " * 2_000_001)])
    with pytest.raises(TicketOutcomeUnknown):
        service.create(OPERATION_ID, REQUEST)
    assert len(transport.calls) == 1
