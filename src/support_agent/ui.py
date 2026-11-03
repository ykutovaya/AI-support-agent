"""Рабочее место сотрудника: история, точное предложение и отдельные согласования."""

from datetime import datetime, timezone
from urllib.parse import urlsplit

import streamlit as st

EMPLOYEE = "demo-employee"
STATUS_LABELS = {
    "pending": "Ожидает согласования",
    "approved": "Согласовано, ожидает отправки",
    "executed": "Действие выполнено",
    "delivered": "Ответ доставлен",
    "sent": "Ответ доставлен",
    "delivery_failed": "Доставка не подтверждена",
    "stale": "Условия изменились — нужно новое предложение",
    "rejected": "Отклонено сотрудником",
    "failed": "Исполнение отменено",
    "duplicate": "Заявка по этому вопросу уже существует",
    "submitting": "Создание в Zammad начато — результат проверяется",
    "unknown": "Исход создания неизвестен",
}
EVENT_LABELS = {
    "employee": "Комментарий сотрудника",
    "tool": "Проверка данных",
    "rejected": "Причина отклонения",
    "rejection": "Причина отклонения",
    "approval": "Согласование сотрудника",
    "delivery_retry": "Продолжение доставки",
    "consent": "Согласие клиента",
    "consent_refused": "Отказ клиента",
    "consent_outdated": "Устаревшее согласие",
    "blocked": "Исполнение остановлено",
    "error": "Ошибка",
    "operation": "Фактический результат действия",
}


def _clear_case_fields():
    for key in ("request", "rejection_reason", "client_response"):
        st.session_state[key] = ""
    for key in ("_ui_error", "_last_result"):
        st.session_state.pop(key, None)


def _select_case():
    st.session_state["case_id"] = st.session_state["case_selector"]
    st.query_params["case"] = st.session_state["case_id"]
    _clear_case_fields()


def _invoke(method, *args):
    st.session_state.pop("_ui_error", None)
    try:
        return True, method(*args)
    except ValueError as error:
        st.session_state["_ui_error"] = str(error)
    except Exception:
        st.session_state["_ui_error"] = (
            "Не удалось завершить запрос. Проверьте состояние обращения перед повтором."
        )
    return False, None


def _new_case():
    ok, case_id = _invoke(
        st.session_state["_service"].create_case,
        st.session_state["user_id"],
        EMPLOYEE,
    )
    if ok:
        _clear_case_fields()
        st.session_state["case_id"] = case_id
        st.session_state["case_selector"] = case_id
        st.query_params["case"] = case_id


def _investigate(case_id):
    ok, _ = _invoke(
        st.session_state["_service"].investigate,
        case_id,
        st.session_state["request"],
        EMPLOYEE,
    )
    if ok:
        _clear_case_fields()


def _approve(case_id, proposal_id, version, digest):
    ok, result = _invoke(
        st.session_state["_service"].approve,
        case_id,
        proposal_id,
        version,
        digest,
        EMPLOYEE,
    )
    if ok:
        st.session_state["_last_result"] = result


def _reconcile(case_id, proposal_id, version, digest):
    ok, result = _invoke(
        st.session_state["_service"].reconcile,
        case_id,
        proposal_id,
        version,
        digest,
        EMPLOYEE,
    )
    if ok:
        st.session_state["_last_result"] = result


def _resume(case_id, source_proposal_id):
    ok, _ = _invoke(
        st.session_state["_service"].resume,
        case_id,
        source_proposal_id,
        EMPLOYEE,
    )
    if ok:
        st.session_state.pop("_last_result", None)


def _reject(case_id, proposal_id, version, digest):
    if not st.session_state["rejection_reason"].strip():
        st.session_state["_ui_error"] = "Укажите причину отклонения."
        return
    ok, _ = _invoke(
        st.session_state["_service"].reject,
        case_id,
        proposal_id,
        version,
        digest,
        st.session_state["rejection_reason"],
        EMPLOYEE,
    )
    if ok:
        _clear_case_fields()


def _client_response(case_id, question_id, accepted):
    ok, _ = _invoke(
        st.session_state["_service"].record_client_response,
        case_id,
        question_id,
        accepted,
        st.session_state["client_response"],
        EMPLOYEE,
    )
    if ok:
        _clear_case_fields()


def _user_label(user_id, users):
    user = users[user_id]
    name = user.get("name", {})
    if isinstance(name, dict):
        name = " ".join(
            str(name.get(part, "")) for part in ("first_name", "last_name")
        ).strip()
    return f"{name or user_id} · {user.get('email', user_id)}"


def _history(view):
    st.subheader("История обращения")
    if not view["events"]:
        st.caption("Сообщений пока нет.")
    for event in view["events"]:
        kind, text = event["kind"], event["text"]
        if kind in ("customer", "client", "user", "sent"):
            with st.chat_message("assistant" if kind == "sent" else "user"):
                st.caption("Ответ в тестовом канале" if kind == "sent" else "Клиент")
                st.text(text)


def _details(view):
    with st.expander("Подробности"):
        st.text(f"Идентификатор обращения: {view['case']['id']}")
        proposal = view["proposal"]
        if proposal:
            st.text(f"Идентификатор предложения: {proposal.id}")
            st.text(f"Версия: {proposal.version}")
            action = proposal.action or proposal.consent_request
            if action:
                st.caption("Точные параметры предложения")
                st.json(action.arguments)
        st.caption("Журнал действий")
        for event in view["events"]:
            if event["kind"] not in ("customer", "client", "user", "sent"):
                st.caption(EVENT_LABELS.get(event["kind"], "Событие обращения"))
                st.text(event["text"])
        if view.get("operation_result"):
            st.json(view["operation_result"])
        if view.get("current_delivery"):
            st.json(view["current_delivery"])


def _item_label(item_id, action):
    values = action.condition_values or {}
    products = values.get("products", {})
    item = next(
        (
            {"name": product.get("name", item_id), **product["variants"][item_id]}
            for product in products.values()
            if item_id in product.get("variants", {})
        ),
        None,
    )
    if item is None:
        item = next(
            (
                entry
                for entry in values.get("order", {}).get("items", [])
                if entry.get("item_id") == item_id
            ),
            None,
        )
    if item is None:
        return item_id
    labels = {"color": "цвет", "size": "размер", "material": "материал"}
    colors = {
        "black": "чёрный",
        "white": "белый",
        "blue": "синий",
        "red": "красный",
        "green": "зелёный",
        "gray": "серый",
    }
    options = ", ".join(
        f"{labels.get(key, key)}: {colors.get(value, value) if key == 'color' else value}"
        for key, value in item.get("options", {}).items()
    )
    return str(item.get("name", item_id)) + (f" ({options})" if options else "")


def _payment_label(payment_id, action):
    payment = (
        (action.condition_values or {}).get("payment_methods", {}).get(payment_id, {})
    )
    if payment.get("source") == "credit_card":
        return (
            f"Карта {payment.get('brand', '')} ··{payment.get('last_four', '')}".strip()
        )
    return {"paypal": "PayPal", "gift_card": "Подарочная карта"}.get(
        payment.get("source"), payment_id
    )


def _action_details(action):
    if action.name == "create_support_ticket":
        st.info(
            "Заявка в Zammad"
            if action.arguments.get("target") == "zammad"
            else "Локальная учебная заявка"
        )
    st.text(action.description)
    labels = {
        "order_id": "Заказ",
        "item_ids": "Позиции",
        "new_item_ids": "Замена на",
        "payment_method_id": "Способ оплаты",
        "user_id": "Клиент",
        "target": "Место регистрации",
        "title": "Тема",
        "body": "Текст заявки",
        "question": "Нерешённый вопрос",
    }
    for name, value in action.arguments.items():
        if name == "destination":
            st.text(f"Сервер Zammad: {urlsplit(value['base_url']).netloc}")
            st.text(f"Группа: {value['group_name']}")
            continue
        if name not in labels or name == "user_id":
            continue
        if name == "target" and value == "local":
            value = "Локальная база приложения"
        if name in ("item_ids", "new_item_ids"):
            value = [_item_label(item, action) for item in value]
        if name == "payment_method_id":
            value = _payment_label(value, action)
        shown = (
            ", ".join(str(item) for item in value)
            if isinstance(value, list)
            else str(value)
        )
        st.text(f"{labels.get(name, name)}: {shown}")
    if action.amount is None:
        return
    label = (
        "Доплата"
        if action.amount > 0
        else "Предполагаемый возврат"
        if action.amount < 0
        else "Изменение суммы"
    )
    st.metric(label, f"{abs(action.amount):.2f} {action.currency}")


def _evidence(proposal):
    action = proposal.action or proposal.consent_request
    entries = list(proposal.evidence) + (list(action.evidence) if action else [])
    seen = set()
    for item in entries:
        identity = (item.title, item.source, item.detail)
        if identity in seen:
            continue
        seen.add(identity)
        number = len(seen)
        st.markdown(f"[Основание {number}](#evidence-{number})")
        st.subheader(f"Данные основания {number}", anchor=f"evidence-{number}")
        st.text(item.title)
        st.text(f"Источник: {item.source}")
        st.text(item.detail)


def _recovery(view):
    recovery = view.get("recovery")
    proposal = view["proposal"]
    if not recovery or not proposal:
        return
    if proposal.id not in (
        recovery.get("source_proposal_id"),
        recovery.get("proposal_id"),
    ):
        return
    if view["proposal_status"] in (
        "delivered",
        "executed",
        "approved",
        "delivery_failed",
    ):
        return
    check = recovery.get("check") or {}
    if check.get("reason"):
        (st.caption if recovery.get("status") == "prepared" else st.warning)(
            check["reason"]
        )
    mode = view.get("recovery_mode", "manual")
    for change in check.get("changes", []):
        st.text(
            f"{change['label']}\nБыло: {change['before']}\nСтало: {change['after']}"
        )
    status = recovery.get("status")
    if status == "failed":
        st.error("Не удалось подготовить новую версию.")
        st.caption("Проверьте историю обращения перед новой попыткой расследования.")
    elif status == "running":
        st.info("Готовится новая версия предложения.")
    elif status == "prepared":
        st.caption("Новая версия подготовлена. Проверьте её перед согласованием.")
    elif status == "cancelled":
        st.caption("Подготовка новой версии отменена.")
    elif status == "pending" and mode == "auto":
        st.info("Ожидает автоматической подготовки новой версии.")
    if mode == "manual":
        st.button(
            "Продолжить расследование",
            key="resume_recovery",
            disabled=status != "pending",
            on_click=_resume,
            args=(view["case"]["id"], recovery["source_proposal_id"]),
        )


def _card(view):
    st.subheader("Предложение сотруднику")
    st.caption(
        "Автоматическая подготовка новой версии"
        if view.get("recovery_mode", "manual") == "auto"
        else "Подготовка новой версии по команде сотрудника"
    )
    proposal = view["proposal"]
    if proposal is None:
        st.info("Введите сообщение клиента и подготовьте решение.")
        return
    status = view["proposal_status"]
    with st.container(border=True):
        st.caption(
            f"Версия {proposal.version} · {STATUS_LABELS.get(status, 'Состояние уточняется')}"
        )
        _recovery(view)
        st.write("Действие и ответ" if proposal.action else "Только сообщение")
        action = proposal.action or proposal.consent_request
        if action:
            _action_details(action)
        if proposal.consent_request and status == "pending":
            st.info(
                "Сначала отправьте клиенту эти условия. Его согласие не заменяет подтверждение сотрудника."
            )
        with st.expander("Основания"):
            _evidence(proposal)
        _approvals(view)
        st.subheader("Точный текст ответа")
        st.text(proposal.text)
        continuing = status in ("approved", "executed")
        if continuing:
            st.caption(
                "Согласование уже сохранено. Продолжится отправка прежнего текста; действие не повторится."
            )
        elif status == "pending":
            st.caption(
                "Текущий ответ ещё не отправлен. После подтверждения текст будет передан без изменений в тестовый канал."
            )
        if view["reason"]:
            st.warning(view["reason"])
        args = (proposal.case_id, proposal.id, proposal.version, proposal.digest())
        retry = status == "delivery_failed"
        label = (
            "Продолжить отправку"
            if continuing
            else "Повторить отправку"
            if retry
            else "Подтвердить действие и ответ"
            if proposal.action
            else "Подтвердить отправку"
        )
        st.button(
            label,
            key="approve",
            type="primary",
            disabled=status
            not in ("pending", "approved", "executed", "delivery_failed"),
            on_click=_approve,
            args=args,
        )
        if status in ("submitting", "unknown"):
            st.warning(
                "Повторное создание заблокировано. Проверка только читает результат в Zammad; ответ об успехе ожидает подтверждённую заявку."
            )
            st.button(
                "Проверить результат в Zammad",
                key="reconcile",
                on_click=_reconcile,
                args=args,
            )
        st.text_area(
            "Причина отклонения", key="rejection_reason", disabled=status != "pending"
        )
        st.button(
            "Отклонить и пересмотреть",
            key="reject",
            disabled=status != "pending",
            on_click=_reject,
            args=args,
        )


def _client_channel(view):
    question = view["waiting_consent"]
    if question is None:
        return
    st.subheader("Ответ клиента — тестовый канал")
    st.caption(
        "Учебная имитация: ответ вводит сотрудник. Реальный клиент и внешняя отправка не подключены."
    )
    st.text(question.text)
    st.text_area("Текст ответа клиента", key="client_response")
    st.caption(
        "Введите ответ, затем выберите «Клиент согласен» или «Клиент отказался»."
    )
    ready = bool(st.session_state["client_response"].strip())
    left, right = st.columns(2)
    left.button(
        "Клиент согласен",
        key="client_accept",
        disabled=not ready,
        on_click=_client_response,
        args=(question.case_id, question.id, True),
    )
    right.button(
        "Клиент отказался",
        key="client_reject",
        disabled=not ready,
        on_click=_client_response,
        args=(question.case_id, question.id, False),
    )


def _approvals(view):
    st.write("Согласования")
    approval = view.get("approval") or {}
    st.caption(
        "Сотрудник: подтверждено"
        if approval.get("valid")
        else "Сотрудник: текущее предложение не подтверждено"
    )
    consent = view.get("client_consent") or {}
    labels = {
        "not_required": "Клиент: согласие на это действие не требуется",
        "waiting": "Клиент: ожидается согласие на текущие условия",
        "confirmed": "Клиент: текущие условия подтверждены",
        "used": "Клиент: согласие использовано для выполненного действия",
        "rejected": "Клиент: отказался от действия",
        "outdated": "Клиент: прежнее согласие больше не действует",
    }
    st.caption(
        labels.get(
            consent.get("status"), "Клиент: согласие на текущие условия не подтверждено"
        )
    )


def _next_step(view):
    proposal = view["proposal"]
    status = view["proposal_status"]
    if proposal is None:
        return (
            "Новое обращение",
            "Введите сообщение клиента и нажмите «Подготовить решение».",
        )
    if view.get("waiting_consent"):
        return (
            "Ожидается ответ клиента",
            "В тестовом канале введите ответ клиента и отметьте согласие или отказ.",
        )
    if status == "pending":
        if proposal.consent_request:
            return (
                "Согласование условий",
                "Проверьте условия и подтвердите отправку запроса клиенту.",
            )
        if proposal.action:
            return (
                "Согласование действия",
                "Проверьте действие и точный текст, затем подтвердите или отклоните предложение.",
            )
        return (
            "Согласование сообщения",
            "Проверьте точный текст и подтвердите отправку клиенту.",
        )
    if status in ("unknown", "submitting"):
        return (
            "Результат создания пока неизвестен",
            "Проверьте результат в Zammad. Повторное создание заблокировано.",
        )
    if status == "stale":
        recovery = view.get("recovery") or {}
        if recovery.get("source_proposal_id") != proposal.id:
            next_action = "Введите уточнение клиента и нажмите «Подготовить решение»."
        elif recovery.get("status") == "failed":
            next_action = "Подготовьте решение заново по сообщению клиента; причина ошибки сохранена в подробностях."
        elif recovery.get("status") == "running":
            next_action = "Дождитесь новой версии. Не повторяйте действие."
        elif recovery.get("status") == "cancelled":
            next_action = "Введите уточнение клиента и подготовьте новое решение."
        elif (
            recovery.get("status") == "pending" and view.get("recovery_mode") == "auto"
        ):
            next_action = "Ожидается подготовка новой версии."
        elif recovery.get("status") == "pending":
            next_action = "Продолжите расследование и согласуйте новые условия."
        else:
            next_action = "Введите уточнение клиента и нажмите «Подготовить решение»."
        return (
            "Условия изменились",
            "Прежнее подтверждение недействительно. " + next_action,
        )
    if status == "delivery_failed":
        return (
            "Ответ ожидает доставки",
            "Повторите доставку согласованного текста. Действие не повторится.",
        )
    if status in ("approved", "executed"):
        return (
            "Ответ ожидает отправки",
            "Нажмите «Продолжить отправку». Действие не повторится.",
        )
    if status in ("delivered", "sent"):
        return (
            "Ответ доставлен",
            "Проверьте итог ниже. Если есть новый вопрос клиента, введите его в этом обращении.",
        )
    if status == "duplicate":
        return (
            "Заявка уже существует",
            "Проверьте сохранённый номер заявки. Новая заявка и ответ не создавались.",
        )
    return (
        "Исполнение остановлено",
        "Проверьте причину и подготовьте новое предложение.",
    )


def _case_heading(view, users):
    st.subheader(f"Выбрано обращение · {view['case']['id'][:8]}")
    st.text(f"Клиент: {_user_label(view['case']['user_id'], users)}")
    proposal = view["proposal"]
    action = (proposal.action or proposal.consent_request) if proposal else None
    order_id = action.arguments.get("order_id") if action else None
    st.text(f"Заказ: {order_id}" if order_id else "Заказ: ещё не определён")
    step, instruction = _next_step(view)
    st.write(f"Текущий шаг: {step}")
    st.info(f"Следующий шаг: {instruction}")


def _results(view):
    proposal = view["proposal"]
    if proposal is None or view["proposal_status"] == "pending":
        return
    result = view.get("operation_result") or {}
    delivery = view.get("current_delivery")
    verification = view.get("verification") or {}
    ticket_action = bool(
        proposal.action and proposal.action.name == "create_support_ticket"
    )
    unknown = view["proposal_status"] in ("unknown", "submitting")
    st.subheader("Итог текущего предложения")
    st.write("Действие в магазине")
    if ticket_action:
        st.text("Заказ не изменялся: предложение относится к передаче специалисту.")
    elif result:
        st.text(
            f"{proposal.action.description if proposal.action else 'Действие'}. Результат: {result.get('status', 'не указан')}."
        )
        st.caption(
            "Это сохранённый результат. Реальные выплаты и письма не выполнялись."
        )
    elif proposal.action:
        st.text("Действие не выполнено.")
    else:
        st.text("Не применимо: предложение содержит только сообщение.")

    st.write("Заявка специалисту")
    if result.get("status") == "ticket created":
        st.text(
            "Заявка в Zammad"
            if result.get("target") == "zammad"
            else "Локальная учебная заявка"
        )
        st.text(f"Номер заявки: {result['ticket_number']}")
        st.text(f"Статус заявки: {result['ticket_status']}")
        if result.get("ticket_url"):
            st.markdown(f"[Ссылка на заявку]({result['ticket_url']})")
        if view["proposal_status"] == "duplicate":
            st.caption(
                "Показана существующая заявка. Повторная регистрация не выполнялась."
            )
        if result.get("target") == "zammad":
            st.caption(
                "Сохранённый результат Zammad; облако при открытии экрана не опрашивается."
            )
    elif unknown:
        st.text("Исход создания неизвестен. Сначала требуется проверка в Zammad.")
    elif ticket_action:
        st.text("Создание заявки не подтверждено.")
    else:
        st.text("Не создавалась: для этого действия не требуется.")

    st.write("Ответ клиенту")
    if delivery:
        st.text(delivery["text"])
        st.caption(
            "Доставлено в тестовый канал"
            if delivery.get("delivered")
            else "Доставка не подтверждена"
        )
    else:
        st.text("Текущий ответ ещё не отправлен.")

    st.write("Проверка результата")
    verification_status = verification.get(
        "status", "unknown" if unknown else "not_checked"
    )
    if verification_status == "confirmed":
        st.success("Результат подтверждён сохранённой проверкой.")
        if verification.get("source") == "saved_execution":
            when = verification.get("verified_at")
            if when:
                shown = (
                    datetime.fromisoformat(when)
                    .astimezone(timezone.utc)
                    .strftime("%d.%m.%Y %H:%M:%S UTC")
                )
                st.caption(f"Проверено: {shown}")
            else:
                st.caption("Время прежней проверки не сохранено.")
    elif verification_status == "mismatch":
        st.error("Обнаружено расхождение. Сохранённый результат требует проверки.")
    elif verification_status == "unknown":
        st.warning("Исход неизвестен. Сообщение об успехе не отправляется.")
    else:
        st.caption("Результат текущего предложения ещё не подтверждён.")
    if verification.get("reason"):
        st.text(verification["reason"])
    last = st.session_state.get("_last_result") or {}
    if unknown and last.get("reason"):
        st.text(last["reason"])


def main():
    st.set_page_config(page_title="Рабочее место поддержки", layout="wide")
    st.title("Рабочее место поддержки")
    st.caption(
        "Учебный магазин · локальный сотрудник · сообщения передаются только в тестовый канал"
    )
    if "_service" not in st.session_state:
        from support_agent.bootstrap import create_service

        try:
            st.session_state["_service"] = create_service()
        except ValueError as error:
            st.error(f"Не удалось открыть рабочее место. {error}")
            st.stop()
    service = st.session_state["_service"]
    if getattr(service, "mode", "demo") == "api":
        st.info("Режим API: подготовка обращается к настроенной модели")
    else:
        from support_agent.adapters.demo_model import INSTRUCTION

        st.info("Учебный режим без API: ответы задаёт заглушка")
        with st.expander("Как написать учебное обращение"):
            st.text(
                INSTRUCTION.replace(
                    "Для локальной учебной заявки", "Для заявки в Zammad"
                )
                if getattr(service, "ticket_target", "local") == "zammad"
                else INSTRUCTION
            )
    snapshot, _ = service.store.snapshot()
    users = snapshot["users"]
    with st.sidebar:
        st.subheader("Обращения")
        if not users:
            st.warning("В рабочем магазине нет клиентов.")
            return
        cases = service.store.list_cases(EMPLOYEE)
        case_map = {case["id"]: case for case in cases}
        selected = st.session_state.get("case_id")
        requested = st.query_params.get("case")
        if not selected and requested in case_map:
            selected = requested
        if requested and requested not in case_map:
            st.warning("Недоступное обращение. Выберите одно из сохранённых.")
        if selected not in case_map and cases:
            selected = cases[0]["id"]
            _clear_case_fields()
        if cases:
            st.session_state["case_id"] = selected
            st.query_params["case"] = selected
            st.session_state["case_selector"] = selected
            st.selectbox(
                "Открыть обращение",
                list(case_map),
                key="case_selector",
                format_func=lambda case_id: f"{_user_label(case_map[case_id]['user_id'], users)} · {case_id[:8]}",
                on_change=_select_case,
            )
        with st.expander("Создать новое обращение", expanded=not cases):
            st.selectbox(
                "Клиент для нового обращения",
                list(users),
                key="user_id",
                format_func=lambda user_id: _user_label(user_id, users),
            )
            st.button("Новое обращение", key="new_case", on_click=_new_case)
        if not cases:
            st.info("Создайте обращение для выбранного клиента.")
            return
    if st.session_state.get("_ui_error"):
        st.error(st.session_state["_ui_error"])
    view = service.view(selected, EMPLOYEE)
    _case_heading(view, users)
    history, proposal = st.columns([1, 1.2])
    with history:
        _history(view)
        st.text_area("Новое сообщение клиента", key="request")
        st.button(
            "Подготовить решение",
            key="investigate",
            type="primary",
            disabled=not st.session_state["request"].strip(),
            on_click=_investigate,
            args=(selected,),
        )
        _client_channel(view)
    with proposal:
        _card(view)
        _results(view)
    _details(view)


if __name__ == "__main__":
    main()
