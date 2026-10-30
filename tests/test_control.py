"""Проверки запуска оценки без оплачиваемых запросов."""

import builtins
import hashlib
import importlib.util
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

pytest.importorskip("tau2")


@pytest.fixture
def control():
    path = Path(__file__).parents[1] / "evals/core-integration/run_control.py"
    spec = importlib.util.spec_from_file_location("run_control", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dry_run_has_fixed_budget_and_does_not_read_credentials(
    control, tmp_path, monkeypatch, capsys
):
    baseline = control.build_config(None).model_dump(mode="json")
    baseline["agent"] = "llm_agent"
    monkeypatch.setattr(control, "verify_inputs", lambda *_: ({"config": baseline}, {}))

    def forbidden(*args, **kwargs):
        pytest.fail("Сухая проверка не должна читать ключи или вызывать API")

    monkeypatch.setattr(control, "load_credentials", forbidden)
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.startswith("support_agent"):
            pytest.fail("Сухая проверка не должна импортировать адаптер")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert control.main(["--repo", str(tmp_path), "--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    config = result["config"]
    assert result["valid"] is True
    assert config["agent"] == "support_agent"
    assert config["task_ids"] == ["75"]
    assert config["task_split_name"] == "train"
    assert config["llm_agent"] == config["llm_user"] == "openai/responses/gpt-6-luna"
    assert config["llm_args_agent"] == {
        "reasoning_effort": "low",
        "max_tokens": 8192,
        "timeout": 90,
        "num_retries": 1,
    }
    assert (config["num_trials"], config["max_retries"], config["workers"]) == (1, 0, 0)
    assert (config["max_steps"], config["max_errors"], config["timeout"]) == (
        60,
        5,
        600,
    )
    assert (config["max_concurrency"], config["seed"]) == (1, 300)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("existing", [False, True])
def test_live_run_requires_a_new_explicit_directory(control, tmp_path, existing):
    args = ["--repo", str(tmp_path), "--run"]
    if existing:
        args += ["--run-dir", str(tmp_path)]
    with pytest.raises(SystemExit):
        control.main(args)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("mismatch", ["dataset", "commit"])
def test_changed_baseline_is_rejected(control, tmp_path, monkeypatch, mismatch):
    expected = {}
    for name in control.INPUT_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"baseline")
        expected[name] = hashlib.sha256(b"baseline").hexdigest()
    monkeypatch.setattr(control, "LANGUAGE_HASHES", {})
    manifest = tmp_path / "baseline.json"
    manifest.write_text(json.dumps({"sha256_before": expected}), encoding="utf-8")
    monkeypatch.setattr(
        control,
        "git_commit",
        lambda _: "changed" if mismatch == "commit" else control.TAU_COMMIT,
    )
    if mismatch == "dataset":
        (tmp_path / "data/tau2/domains/retail/db.json").write_bytes(b"changed")
    with pytest.raises(ValueError, match="baseline"):
        control.verify_inputs(tmp_path, manifest)


def test_changed_model_or_budget_is_not_silently_compared(control):
    config = control.build_config(None)
    baseline = config.model_dump(mode="json")
    baseline["agent"] = "llm_agent"
    baseline["max_steps"] = 61
    with pytest.raises(ValueError, match="max_steps"):
        control.check_config(config, baseline)


def test_saved_artifacts_redact_credentials_preserving_results(control, tmp_path):
    result = tmp_path / "results.json"
    result.write_text(
        json.dumps({"raw_data": "credential-value", "reward": 1}), encoding="utf-8"
    )
    log = tmp_path / "provider.log"
    log.write_text("Bearer sk-testabcdefgh123456", encoding="utf-8")
    control.redact_artifacts(tmp_path, ["credential-value"])
    assert json.loads(result.read_text(encoding="utf-8")) == {
        "raw_data": "[REDACTED]",
        "reward": 1,
    }
    assert "sk-testabcdefgh123456" not in log.read_text(encoding="utf-8")


def test_empty_dotenv_value_does_not_hide_active_process_secret(
    control, tmp_path, monkeypatch
):
    import dotenv

    (tmp_path / ".env").write_text("OPENAI_API_KEY", encoding="utf-8")
    monkeypatch.setattr(dotenv, "dotenv_values", lambda _: {"OPENAI_API_KEY": None})
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-process-secret")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    assert "synthetic-process-secret" in control.load_credentials(tmp_path)


def test_integrity_check_failure_still_redacts_saved_model_data(
    control, tmp_path, monkeypatch
):
    baseline = control.build_config(None).model_dump(mode="json")
    baseline["agent"] = "llm_agent"
    monkeypatch.setattr(
        control,
        "verify_inputs",
        lambda *_: ({"config": baseline}, {"sha256_before": {}, "tau2_commit": "test"}),
    )
    monkeypatch.setattr(control, "load_credentials", lambda _: ["synthetic-secret"])
    monkeypatch.setattr(control, "trusted_tls", lambda _: nullcontext("test"))
    monkeypatch.setattr(control, "git_commit", lambda _: "test")

    def missing_input(_):
        raise FileNotFoundError("input removed during run")

    monkeypatch.setattr(control, "input_hashes", missing_input)
    for name in (
        "support_agent",
        "support_agent.adapters",
        "support_agent.adapters.tau_bench",
    ):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    sys.modules["support_agent.adapters.tau_bench"].register_agent = lambda **_: None
    import tau2.runner

    result = SimpleNamespace(
        simulations=[
            SimpleNamespace(
                task_id="75",
                seed=626729,
                termination_reason="user_stop",
                reward_info=SimpleNamespace(reward=1.0),
            )
        ],
        model_dump=lambda **_: {"raw_data": "synthetic-secret"},
    )
    monkeypatch.setattr(tau2.runner, "run_domain", lambda _: result)
    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text("{}", encoding="utf-8")
    run_dir = tmp_path / "attempt"
    assert (
        control.main(
            [
                "--repo",
                str(tmp_path),
                "--baseline-manifest",
                str(baseline_file),
                "--run",
                "--run-dir",
                str(run_dir),
            ]
        )
        == 1
    )
    assert "synthetic-secret" not in (run_dir / "results.json").read_text(
        encoding="utf-8"
    )
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["input_files_unchanged"] is False
    assert manifest["integrity_check_error"] == "FileNotFoundError"


@pytest.mark.parametrize(
    "reason,reward,status",
    [("user_stop", 1.0, "finished"), ("infrastructure_error", None, "incomplete")],
)
def test_technical_failure_is_not_reported_as_a_completed_score(
    control, reason, reward, status
):
    result = SimpleNamespace(
        simulations=[
            SimpleNamespace(
                task_id="75",
                seed=626729,
                termination_reason=reason,
                reward_info=SimpleNamespace(reward=reward)
                if reward is not None
                else None,
            )
        ]
    )
    actual_status, simulations = control.summarize_result(result)
    assert actual_status == status
    assert simulations[0]["reward"] == reward
