"""Обычный Streamlit должен останавливаться после сброса соединения Windows."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(sys.platform != "win32", reason="Консольное событие Windows")
def test_streamlit_stops_after_connection_reset(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(ROOT / "evals/shutdown/check_shutdown.py"),
            "--project-root",
            str(ROOT),
            "--case",
            "connected",
            "--inject-reset",
            "--output",
            str(tmp_path / "shutdown"),
        ],
        cwd=ROOT,
        capture_output=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout.decode(
        "utf-8", errors="replace"
    ) + result.stderr.decode("utf-8", errors="replace")
