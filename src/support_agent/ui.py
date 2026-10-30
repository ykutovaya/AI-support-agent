"""Рабочее место сотрудника: история, точное предложение и отдельные согласования."""

import json

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
        else:
            st.caption(EVENT_LABELS.get(kind, "Событие обращения"))
            st.text(text)


def _action_details(action):
    st.text(action.description)
    labels = {
        "order_id": "Заказ",
        "item_ids": "Позиции",
        "new_item_ids": "Замена на",
        "payment_method_id": "Способ оплаты",
    }
    for name, value in action.arguments.items():
        shown = (
            ", ".join(str(item) for item in value)
            if isinstance(value, list)
            else str(value)
        )
        st.text(f"{labels.get(name, name)}: {shown}")
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
    if not recovery:
        return
    check = recovery.get("check") or {}
    if check.get("reason"):
        st.warning(check["reason"])
    mode = view.get("recovery_mode", "manual")
    if mode == "auto":
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
        if proposal.consent_request:
            st.info(
                "Сначала отправьте клиенту эти условия. Его согласие не заменяет подтверждение сотрудника."
            )
        _evidence(proposal)
        st.subheader("Точный текст ответа")
        st.text(proposal.text)
        continuing = status in ("approved", "executed")
        if continuing:
            st.caption(
                "Согласование уже сохранено. Продолжится отправка прежнего текста; действие не повторится."
            )
        else:
            st.caption(
                "Текст будет передан без изменений в тестовый канал после подтверждения."
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


def _results(view):
    result = st.session_state.get("_last_result")
    if result:
        status = result.get("status", "")
        message = STATUS_LABELS.get(status, "Запрос обработан")
        (
            st.warning
            if status in ("stale", "delivery_failed", "failed")
            else st.success
        )(message)
        if result.get("reason"):
            st.text(result["reason"])
    operations = [event for event in view["events"] if event["kind"] == "operation"]
    if operations:
        st.subheader("Фактический результат")
        try:
            st.json(json.loads(operations[-1]["text"]))
        except (ValueError, TypeError):
            st.text(operations[-1]["text"])
    if view["deliveries"]:
        st.subheader("Доставлено в тестовый канал")
        for delivery in view["deliveries"]:
            st.text(delivery["text"])


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
        st.text(INSTRUCTION)
    snapshot, _ = service.store.snapshot()
    users = snapshot["users"]
    with st.sidebar:
        st.subheader("Обращения")
        if not users:
            st.warning("В рабочем магазине нет клиентов.")
            return
        st.selectbox(
            "Клиент для нового обращения",
            list(users),
            key="user_id",
            format_func=lambda user_id: _user_label(user_id, users),
        )
        st.button("Новое обращение", key="new_case", on_click=_new_case)
        cases = service.store.list_cases(EMPLOYEE)
        if not cases:
            st.info("Создайте обращение для выбранного клиента.")
            return
        case_map = {case["id"]: case for case in cases}
        selected = st.session_state.get("case_id")
        if selected not in case_map:
            selected = cases[0]["id"]
            st.session_state["case_id"] = selected
            _clear_case_fields()
        st.session_state["case_selector"] = selected
        st.selectbox(
            "Открыть обращение",
            list(case_map),
            key="case_selector",
            format_func=lambda case_id: f"{_user_label(case_map[case_id]['user_id'], users)} · {case_id[:8]}",
            on_change=_select_case,
        )
    if st.session_state.get("_ui_error"):
        st.error(st.session_state["_ui_error"])
    view = service.view(selected, EMPLOYEE)
    st.subheader(_user_label(view["case"]["user_id"], users))
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


if __name__ == "__main__":
    main()
