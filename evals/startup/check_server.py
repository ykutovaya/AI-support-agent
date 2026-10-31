"""Проверить локальный сервер и его перезапуск; сценарий UI проверяется отдельно."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8505)
    args = parser.parse_args()
    root, database, output = (
        args.project_root.resolve(),
        args.database.resolve(),
        args.output.resolve(),
    )
    assert database.is_file(), "Нужна сохранённая учебная база"
    output.mkdir(parents=True, exist_ok=False)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    env = os.environ.copy()
    for key in list(env):
        if key.endswith(("_API_KEY", "_TOKEN")) or key in {"PYTHONPATH", "PYTHONHOME"}:
            env.pop(key, None)
    env.update(
        SUPPORT_AGENT_MODE="demo",
        SUPPORT_AGENT_RECOVERY_MODE="manual",
        SUPPORT_AGENT_DB=str(database),
        SUPPORT_AGENT_RETAIL_DB=str(root / "evals/reapproval/base_shop.json"),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHON_DOTENV_DISABLED="1",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
        PYTHONIOENCODING="utf-8",
    )
    command = [
        sys.executable,
        "-B",
        "-m",
        "streamlit",
        "run",
        "src/support_agent/ui.py",
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(args.port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    url = f"http://127.0.0.1:{args.port}/_stcore/health"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def health():
        try:
            with opener.open(url, timeout=2) as response:
                return response.status, response.read().decode()
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            return None

    assert health() is None, "Выбранный порт уже занят"
    runs = []
    for attempt in (1, 2):
        with (output / f"server-{attempt}.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                command,
                cwd=root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                deadline = time.monotonic() + 30
                result = None
                while time.monotonic() < deadline and process.poll() is None:
                    result = health()
                    if result == (200, "ok"):
                        break
                    time.sleep(0.2)
                assert result == (200, "ok"), "Сервер не запустился; см. журнал"
                runs.append({"launch": attempt, "pid": process.pid, "health": result})
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=10)
            assert health() is None, "После остановки порт остался доступен"
            runs[-1]["stopped"] = True
    after = hashlib.sha256(database.read_bytes()).hexdigest()
    assert before == after, "Запуск сервера изменил базу"
    result = {
        "status": "passed",
        "command": command,
        "database_sha256": before,
        "runs": runs,
        "database_unchanged": True,
        "api_calls": 0,
        "scope": "HTTP health и остановка; сценарий интерфейса проверен отдельно AppTest",
    }
    (output / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    print(json.dumps({"status": "passed", "launches": 2, "api_calls": 0}))


if __name__ == "__main__":
    main()
