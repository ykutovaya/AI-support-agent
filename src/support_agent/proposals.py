"""Предложения и точные условия, которые подтверждают клиент и сотрудник."""

import hashlib
import json
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field, model_serializer, model_validator

from support_agent.models import Contract


def digest(value: dict) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class Evidence(Contract):
    title: str
    source: str
    detail: str


class Action(Contract):
    name: str
    arguments: dict
    amount: Decimal
    currency: str = "USD"
    conditions: str
    result_status: Literal["exchange requested", "return requested"]
    description: str
    evidence: list[Evidence]
    condition_values: dict = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def preserve_legacy_signature(self, handler):
        """Пустое новое поле не меняет подписи ранее сохранённых предложений."""
        result = handler(self)
        if not self.condition_values:
            result.pop("condition_values", None)
        return result

    def fingerprint(self) -> str:
        return digest(self.model_dump(mode="json"))


class ConditionChange(Contract):
    field: str
    label: str
    before: Any
    after: Any


class ConditionCheck(Contract):
    status: Literal["unchanged", "changed", "forbidden"]
    reason: str
    changes: list[ConditionChange] = Field(default_factory=list)
    current: Action | None = None


class Proposal(Contract):
    id: str
    case_id: str
    version: int = Field(ge=1)
    kind: Literal["message", "action"]
    text: str
    action: Action | None = None
    consent_request: Action | None = None
    evidence: list[Evidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_proposal(self):
        if not self.text.strip():
            raise ValueError("Проект ответа не может быть пустым")
        if (self.kind == "action") != (self.action is not None):
            raise ValueError("Режим должен соответствовать наличию действия")
        if self.kind == "action" and self.consent_request is not None:
            raise ValueError("Запрос согласия — отдельное сообщение")
        return self

    def digest(self) -> str:
        return digest(self.model_dump(mode="json"))
