"""Запуск рабочего места с корректной очисткой соединений Windows."""

import asyncio
import sys
from pathlib import Path


def configure_event_loop() -> None:
    """Обойти зависание Proactor после WinError 10054 в Python 3.12 на Windows.

    Selector закрывает соединение без проблемного socket.shutdown. Этот запуск
    рассчитан на локальное рабочее место: асинхронные subprocess и pipes не нужны.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def main() -> None:
    configure_event_loop()

    from streamlit.web import cli

    cli.main(
        ["run", str(Path(__file__).with_name("ui.py")), *sys.argv[1:]],
        prog_name="support-agent",
    )


if __name__ == "__main__":
    main()
