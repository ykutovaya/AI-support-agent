"""Отдельный сервер: настоящий Ctrl+C, обрыв TCP и сохранность учебной базы."""

import argparse
import ctypes
import hashlib
import importlib.util
import json
import os
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(sys.executable)
HELPER = Path(__file__).resolve()


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def wait_for(predicate, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    return False


def emit_ctrl_c(pid):
    """Послать Ctrl+C только отдельной консоли стенда; остальные не затрагиваются."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.FreeConsole()
    if not kernel.AttachConsole(pid):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not kernel.SetConsoleCtrlHandler(None, True):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.GenerateConsoleCtrlEvent(0, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        time.sleep(0.4)
    finally:
        kernel.FreeConsole()


def main():
    global ROOT
    if len(sys.argv) == 3 and sys.argv[1] == "--send":
        emit_ctrl_c(int(sys.argv[2]))
        return 0
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--case", choices=["idle", "connected", "reset"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--inject-reset", action="store_true")
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    ROOT = args.project_root.resolve()
    if os.name != "nt":
        parser.error("Проверка консольного Ctrl+C рассчитана на Windows")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    if args.database:
        database = args.database.resolve()
        assert database.is_relative_to(output.parent) and database.is_file(), (
            "База должна находиться внутри каталога этой проверки"
        )
    else:
        # Новый учебный обмен создаётся прежним проверенным сценарием без API.
        source = ROOT / "evals/acceptance/2026-10-31/check_demo_flow.py"
        spec = importlib.util.spec_from_file_location("shutdown_demo_flow", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        seeded = module.run(ROOT, output, False)
        (output / "seed.json").write_text(
            json.dumps(seeded, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        database = output / "normal.sqlite3"
    before = hashlib.sha256(database.read_bytes()).hexdigest()

    def counts():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            return {
                name: db.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
                for name in ("cases", "operations", "deliveries")
            }

    initial_counts = counts()
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    wrapper = output / "run_server.py"
    wrapper.write_text(
        """import asyncio,ctypes,faulthandler,os,runpy,sys
ctypes.windll.kernel32.SetConsoleCtrlHandler(None,False)
print('SERVER_PID',os.getpid(),flush=True)
faulthandler.enable()
faulthandler.dump_traceback_later(25,repeat=True)
if os.environ.get('PROBE_INJECT_RESET')=='1':
    import socket
    port=int(sys.argv[sys.argv.index('--server.port')+1])
    original_shutdown=socket.socket.shutdown
    injected=False
    def failed_shutdown(sock,how):
        global injected
        if not injected and sock.getsockname()[1]==port:
            injected=True
            print('INJECTED_WINERROR_10054',flush=True)
            raise ConnectionResetError(10054,'peer reset during shutdown')
        return original_shutdown(sock,how)
    socket.socket.shutdown=failed_shutdown
runpy.run_module('streamlit',run_name='__main__')
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    for key in list(env):
        if key.endswith(("_API_KEY", "_TOKEN")) or key in ("PYTHONHOME", "PYTHONPATH"):
            env.pop(key, None)
    env.update(
        SUPPORT_AGENT_MODE="demo",
        SUPPORT_AGENT_RECOVERY_MODE="manual",
        SUPPORT_AGENT_DB=str(database),
        SUPPORT_AGENT_RETAIL_DB=str(ROOT / "evals/reapproval/base_shop.json"),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHON_DOTENV_DISABLED="1",
        LITELLM_LOCAL_MODEL_COST_MAP="True",
        PYTHONIOENCODING="utf-8",
        TAU2_DATA_DIR=str(ROOT.parent / "tau2-bench/data"),
    )
    command = [
        str(PYTHON),
        "-B",
        str(wrapper),
        "run",
        "src/support_agent/ui.py",
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
        "--logger.level",
        "debug",
    ]
    env["PROBE_INJECT_RESET"] = "1" if args.inject_reset else "0"
    powershell = str(
        Path(os.environ["SystemRoot"])
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    log_path = output / "console.log"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def health():
        try:
            with opener.open(f"http://127.0.0.1:{port}/_stcore/health", timeout=1) as r:
                return r.status == 200
        except OSError:
            return False

    def ready():
        return "POWERSHELL_READY_AFTER_CTRL_C" in log_path.read_text(
            "utf-8", errors="replace"
        )

    result = {
        "case": args.case,
        "injected_reset": args.inject_reset,
        "port": port,
        "signal": "CTRL_C_EVENT",
        "physical_keyboard": False,
        "database": str(database),
        "counts_before": initial_counts,
    }
    ws = None
    with log_path.open("w", encoding="utf-8") as log:
        shell = subprocess.Popen(
            [powershell, "-NoLogo", "-NoProfile", "-NoExit", "-Command", "-"],
            cwd=ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            startupinfo=startup,
        )
        result["shell_pid"] = shell.pid
        try:
            shell.stdin.write("& " + " ".join(map(literal, command)) + "\n")
            shell.stdin.flush()
            assert wait_for(health, 30), "Сервер не запустился"
            result["health"] = True
            if args.case != "idle":
                from streamlit.proto.BackMsg_pb2 import BackMsg
                from streamlit.proto.ClientState_pb2 import ClientState
                from streamlit.proto.ForwardMsg_pb2 import ForwardMsg
                from websockets.sync.client import connect

                ws = connect(
                    f"ws://127.0.0.1:{port}/_stcore/stream",
                    origin=f"http://127.0.0.1:{port}",
                    subprotocols=["streamlit"],
                    proxy=None,
                    max_size=32 * 1024 * 1024,
                    legacy=True,
                )
                ws.send(BackMsg(rerun_script=ClientState()).SerializeToString())
                from google.protobuf.json_format import MessageToDict

                rendered = []
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    message = ForwardMsg.FromString(ws.recv(timeout=10))
                    rendered.append(
                        json.dumps(MessageToDict(message), ensure_ascii=False)
                    )
                    if message.WhichOneof("type") == "script_finished":
                        result["ui_script_finished"] = int(message.script_finished)
                        break
                assert result.get("ui_script_finished") == 0, (
                    "Нет успешного результата экрана"
                )
                result["saved_case_visible"] = all(
                    value in "\n".join(rendered)
                    for value in ("exchange requested", "2.35")
                )
                assert result["saved_case_visible"], "Не показана сохранённая история"
                if args.case == "reset":
                    ws.socket.setsockopt(
                        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("HH", 1, 0)
                    )
                    ws.socket.close()
                    ws = None
                    for attempt in range(30):
                        conn = socket.create_connection(("127.0.0.1", port), timeout=2)
                        conn.sendall(
                            b"GET /_stcore/health HTTP/1.1\r\nHost: localhost\r\n\r\n"
                        )
                        conn.setsockopt(
                            socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("HH", 1, 0)
                        )
                        conn.close()
                        time.sleep(0.02)
                    time.sleep(1)
            started = time.monotonic()
            sent = subprocess.run(
                [str(PYTHON), "-B", str(HELPER), "--send", str(shell.pid)],
                capture_output=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            assert sent.returncode == 0, sent.stderr
            shell.stdin.write("Write-Output 'POWERSHELL_READY_AFTER_CTRL_C'\n")
            shell.stdin.flush()
            result["shell_ready"] = bool(wait_for(ready, 8))
            result["shutdown_seconds"] = round(time.monotonic() - started, 3)
            result["port_closed"] = not health()
            result["database_unchanged"] = (
                hashlib.sha256(database.read_bytes()).hexdigest() == before
            )
            result["counts_after"] = counts()
            if result["shell_ready"]:
                shell.stdin.write("exit\n")
                shell.stdin.flush()
                shell.wait(timeout=5)
        except Exception as e:
            result["error"] = repr(e)
        finally:
            if ws:
                ws.close()
            result["forced_cleanup"] = shell.poll() is None
            if shell.poll() is None:
                cleanup = subprocess.run(
                    ["taskkill", "/PID", str(shell.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                result["cleanup_exit_code"] = cleanup.returncode
                shell.wait(timeout=5)
            result["passed"] = (
                result.get("shell_ready", False)
                and result.get("port_closed", False)
                and result.get("database_unchanged", False)
                and not result["forced_cleanup"]
            )
            (output / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
