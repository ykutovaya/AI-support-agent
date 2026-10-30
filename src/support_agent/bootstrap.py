"""Создание сервиса: учебный режим по умолчанию, API только по явной настройке."""

import json
import os
from pathlib import Path


def _path(value: str, root: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else root / path


def create_service(root: Path | None = None):
    """Загрузить магазин один раз и подключить модель без вызова API.

    SUPPORT_AGENT_MODE=api разрешает чтение .env из корня приложения.
    Другие режимы не читают файл с ключами. Рабочие данные остаются в SQLite.
    """
    # Установить до любого импорта tau2: его настройки иначе могут загрузить .env.
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    root = (
        Path(root).resolve()
        if root is not None
        else Path(__file__).resolve().parents[2]
    )
    mode = os.environ.get("SUPPORT_AGENT_MODE", "demo")
    if mode not in {"demo", "api"}:
        raise ValueError("SUPPORT_AGENT_MODE должен быть demo или api")
    if mode == "api":
        from dotenv import dotenv_values

        from support_agent.adapters.llm import LiteLLMClient

        values = dotenv_values(root / ".env") if (root / ".env").is_file() else {}
        for name in (
            "OPENAI_API_KEY",
            "OPENAI_MODEL",
            "OPENAI_BASE_URL",
            "OPENAI_ORGANIZATION",
        ):
            if not os.environ.get(name) and values.get(name):
                os.environ[name] = values[name]
        if not os.environ.get("OPENAI_API_KEY"):
            raise ValueError(
                "Для режима API задайте OPENAI_API_KEY в окружении или .env приложения"
            )
        model_name = os.environ.get("OPENAI_MODEL", "gpt-6-luna")
        if model_name in {"gpt-6-luna", "openai/gpt-6-luna"}:
            model_name = "openai/responses/gpt-6-luna"
        model = LiteLLMClient(
            model_name,
            {
                "reasoning_effort": "low",
                "max_tokens": 8192,
                "timeout": 90,
                "num_retries": 1,
            },
        )
    else:
        from support_agent.adapters.demo_model import DemoModel

        model = DemoModel()

    from support_agent.adapters.retail import RetailAdapter
    from support_agent.storage import Store
    from support_agent.workflow import SupportService

    database = _path(os.environ.get("SUPPORT_AGENT_DB", "var/support.sqlite3"), root)
    store = Store(database)
    with store.connection() as conn:
        initialized = conn.execute("SELECT 1 FROM retail_state WHERE id=1").fetchone()
    if initialized is None:
        default_source = root.parent / "tau2-bench/data/tau2/domains/retail/db.json"
        source = _path(
            os.environ.get("SUPPORT_AGENT_RETAIL_DB", str(default_source)), root
        )
        try:
            snapshot = json.loads(source.read_text(encoding="utf-8"))
            policy = source.with_name("policy.md").read_text(encoding="utf-8")
        except (OSError, ValueError) as error:
            raise ValueError(
                "Не удалось прочитать исходные db.json и соседний policy.md; задайте SUPPORT_AGENT_RETAIL_DB"
            ) from error
        store.initialize(RetailAdapter(snapshot, policy).snapshot(), policy)
    service = SupportService(
        store,
        model,
        RetailAdapter,
        recovery_mode=os.environ.get("SUPPORT_AGENT_RECOVERY_MODE", "manual"),
    )
    service.mode = mode
    return service
