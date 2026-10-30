import os
import subprocess
import sys

from test_core import FakeModel

from support_agent.demo import run_example
from support_agent.models import Message, ToolCall


def test_application_stops_at_write_proposal():
    model = FakeModel(
        Message(
            role="assistant",
            tool_calls=[
                ToolCall(
                    id="c1",
                    name="request_cancellation",
                    arguments={"order_id": "DEMO-1"},
                )
            ],
        )
    )
    result = run_example(model)
    assert result["kind"] == "action_proposal"
    assert result["executed"] is False and result["sent"] is False
    assert result["message"]["tool_calls"][0]["name"] == "request_cancellation"
    assert len(model.requests) == 1


def test_application_keeps_response_as_unsent_draft():
    result = run_example(
        FakeModel(Message(role="assistant", content="Уточните номер заказа."))
    )
    assert result["kind"] == "message_draft" and result["sent"] is False
    assert result["message"]["content"] == "Уточните номер заказа."


def test_demo_runs_when_tau_imports_are_forbidden():
    script = """
import sys, runpy
class BlockTau:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "tau2" or fullname.startswith("tau2."):
            raise RuntimeError("tau2 forbidden in application")
sys.meta_path.insert(0, BlockTau())
runpy.run_module("support_agent.demo", run_name="__main__")
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        capture_output=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, result.stderr
    assert '"kind": "action_proposal"' in result.stdout
    assert '"executed": false' in result.stdout
