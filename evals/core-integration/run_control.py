"""Контроль задачи 75; без --run выполняется только проверка без API."""

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TAU_COMMIT = "da0835f6c67baef58bdda18f2a3b037dfb3a1754"
MODEL = "openai/responses/gpt-6-luna"
LLM_ARGS = {
    "reasoning_effort": "low",
    "max_tokens": 8192,
    "timeout": 90,
    "num_retries": 1,
}
INPUT_FILES = (
    "uv.lock",
    "data/tau2/domains/retail/db.json",
    "data/tau2/domains/retail/tasks.json",
    "data/tau2/domains/retail/split_tasks.json",
    "data/tau2/domains/retail/policy.md",
    "src/tau2/domains/retail/tools.py",
)
LANGUAGE_HASHES = {
    # Хеши файлов закреплённого коммита; среда отдельно хеширует выбранные задания.
    "data/tau2/multilingual/en/pack.yaml": "cd14ad9a7aaa298ce54c8c434f2cd1fb55500b13e6fdd1b824d92205a7e4f752",
    "data/tau2/multilingual/en/retail_tasks_en.json": "29b1ab84363d93161b3bb3827c4aeb8bce5c91909f5f96b8ced56253a26a0720",
    "data/tau2/multilingual/en/retail_metadata_en.json": "c4950eb78d61798a8c7d37598cb19038b24ef10b25b8e9c9b677669001320750",
}


def safe_imports(repo=None):
    """Запретить неявное чтение .env, кеш .pyc и загрузку цен из сети."""
    sys.dont_write_bytecode = True
    os.environ.update(
        {
            "PYTHON_DOTENV_DISABLED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        }
    )
    if repo is not None:
        os.environ["TAU2_DATA_DIR"] = str(repo / "data")


def git_commit(repo):
    return subprocess.check_output(
        [
            "git",
            "--no-optional-locks",
            "-c",
            f"safe.directory={repo.as_posix()}",
            "-C",
            str(repo),
            "rev-parse",
            "HEAD",
        ],
        text=True,
        stderr=subprocess.PIPE,
    ).strip()


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def input_hashes(repo):
    return {name: sha256(repo / name) for name in (*INPUT_FILES, *LANGUAGE_HASHES)}


def verify_inputs(repo, manifest_path):
    """Остановить запуск, если данные или коммит отличаются от исходной оценки."""
    baseline = json.loads(manifest_path.read_text(encoding="utf-8"))
    hashes = input_hashes(repo)
    expected = {name: baseline["sha256_before"][name] for name in INPUT_FILES}
    expected.update(LANGUAGE_HASHES)
    changed = [name for name, digest in hashes.items() if digest != expected[name]]
    commit = git_commit(repo)
    if changed or commit != TAU_COMMIT:
        raise ValueError(f"Inputs differ from baseline: {changed or 'tau2 commit'}")
    return baseline, {"sha256_before": hashes, "tau2_commit": commit}


def build_config(run_dir):
    safe_imports()
    from tau2.data_model.simulation import TextRunConfig

    config = TextRunConfig(
        domain="retail",
        benchmark=None,
        language="en",
        task_split_name="train",
        task_ids=["75"],
        agent="support_agent",
        user="user_simulator",
        llm_agent=MODEL,
        llm_user=MODEL,
        llm_args_agent=dict(LLM_ARGS),
        llm_args_user=dict(LLM_ARGS),
        num_trials=1,
        max_steps=60,
        max_errors=5,
        timeout=600,
        max_concurrency=1,
        workers=0,
        max_retries=0,
        seed=300,
        verbose_logs=True,
        log_level="INFO",
        auto_resume=False,
        auto_review=False,
        hallucination_retries=0,
        save_to=str(run_dir.resolve()) if run_dir else None,
    )
    config.validate()
    return config


def check_config(config, baseline):
    actual = config.model_dump(mode="json")
    changed = [
        key
        for key in set(actual) | set(baseline)
        if key not in {"agent", "save_to"} and actual.get(key) != baseline.get(key)
    ]
    if changed:
        raise ValueError(f"Configuration differs from baseline: {sorted(changed)}")


def load_credentials(repo):
    """Вызывать только после явного --run; значения не выводятся."""
    from dotenv import dotenv_values

    values = dotenv_values(repo / ".env") if (repo / ".env").exists() else {}
    model = values.get("OPENAI_MODEL") or os.environ.get("OPENAI_MODEL", "gpt-6-luna")
    if model != "gpt-6-luna":
        raise ValueError("OPENAI_MODEL differs from baseline")
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORGANIZATION"):
        if values.get(name):
            os.environ[name] = values[name]
    if not os.environ.get("OPENAI_API_KEY"):
        raise ValueError("OPENAI_API_KEY is required for --run")
    return [
        str(value)
        for name, value in (*os.environ.items(), *values.items())
        if value
        and any(part in name.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
    ]


@contextmanager
def trusted_tls(enabled):
    """Временный набор доверенных CA Windows; проверка TLS остаётся включённой."""
    if not enabled or sys.platform != "win32" or os.environ.get("SSL_CERT_FILE"):
        yield "TLS verification enabled; existing/default CA configuration"
        return
    import ssl

    import certifi

    certificates = []
    for store in ("ROOT", "CA"):
        for cert, encoding, trust in ssl.enum_certificates(store):
            if encoding == "x509_asn" and (
                trust is True or ssl.Purpose.SERVER_AUTH.oid in trust
            ):
                certificates.append(ssl.DER_cert_to_PEM_cert(cert))
    with tempfile.TemporaryDirectory(prefix="support-control-tls-") as temporary:
        bundle = Path(temporary) / "ca.pem"
        bundle.write_text(
            Path(certifi.where()).read_text(encoding="ascii")
            + "\n"
            + "\n".join(certificates),
            encoding="ascii",
        )
        os.environ["SSL_CERT_FILE"] = str(bundle)
        try:
            yield "TLS verification enabled; certifi plus trusted Windows ROOT/CA certificates"
        finally:
            os.environ.pop("SSL_CERT_FILE", None)


def redact_artifacts(run_dir, secrets):
    for path in run_dir.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".log", ".txt"}:
            content = path.read_text(encoding="utf-8")
            for value in sorted(set(secrets), key=len, reverse=True):
                content = content.replace(value, "[REDACTED]")
                content = content.replace(json.dumps(value)[1:-1], "[REDACTED]")
            content = re.sub(r"sk-[A-Za-z0-9_\-*]{8,}", "[REDACTED]", content)
            path.write_text(content, encoding="utf-8")


def summarize_result(result):
    simulations = [
        {
            "task_id": s.task_id,
            "seed": s.seed,
            "termination_reason": getattr(
                s.termination_reason, "value", s.termination_reason
            ),
            "reward": s.reward_info.reward if s.reward_info else None,
        }
        for s in result.simulations
    ]
    technical = {
        "infrastructure_error",
        "unexpected_error",
        "agent_error",
        "user_error",
        "timeout",
    }
    complete = (
        len(simulations) == 1
        and simulations[0]["task_id"] == "75"
        and simulations[0]["seed"] == 626729
        and simulations[0]["reward"] is not None
        and simulations[0]["termination_reason"] not in technical
    )
    return ("finished" if complete else "incomplete"), simulations


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="Каталог tau2-bench")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument(
        "--baseline-manifest",
        type=Path,
        default=PROJECT_ROOT
        / "evals/retail-baseline/2026-10-30/runs/task75/run_manifest.json",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--run", action="store_true", help="Выполнить оплачиваемый прогон"
    )
    mode.add_argument(
        "--dry-run", action="store_true", help="Проверить без API (по умолчанию)"
    )
    args = parser.parse_args(argv)
    if args.run and (args.run_dir is None or args.run_dir.exists()):
        parser.error(
            "--run requires an explicit new --run-dir; existing attempts are never overwritten"
        )
    repo = args.repo.resolve()
    safe_imports(repo)
    baseline, provenance = verify_inputs(repo, args.baseline_manifest)
    secrets = load_credentials(repo) if args.run else []
    with trusted_tls(args.run) as tls:
        config = build_config(args.run_dir)
        check_config(config, baseline["config"])
        if not args.run:
            print(
                json.dumps(
                    {
                        "valid": True,
                        "config": config.model_dump(mode="json"),
                        "nl_judge": {"model": MODEL, "args": LLM_ARGS},
                        **provenance,
                    },
                    indent=2,
                )
            )
            return 0

        # Импорт адаптера и регистрация доступны только после явного --run.
        sys.path.insert(0, str(PROJECT_ROOT / "src"))
        from tau2.evaluator import evaluator_nl_assertions as nl
        from tau2.runner import run_domain
        from tau2.utils.llm_utils import set_llm_log_mode

        from support_agent.adapters.tau_bench import register_agent

        call_number = 0

        def observe(event):
            nonlocal call_number
            call_number += 1
            call_dir = args.run_dir / "artifacts" / "agent_calls"
            call_dir.mkdir(parents=True, exist_ok=True)
            write_json(call_dir / f"{call_number:04d}.json", event)

        register_agent(name="support_agent", observer=observe)
        nl.DEFAULT_LLM_NL_ASSERTIONS = MODEL
        nl.DEFAULT_LLM_NL_ASSERTIONS_ARGS = dict(LLM_ARGS)
        set_llm_log_mode("all")
        code_files = sorted((PROJECT_ROOT / "src").rglob("*.py")) + [Path(__file__)]
        manifest = {
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "status": "running",
            "config": config.model_dump(mode="json"),
            "nl_judge": {"model": MODEL, "args": LLM_ARGS},
            "versions": {
                name: importlib.metadata.version(name)
                for name in ("tau2", "litellm", "openai", "pydantic", "python-dotenv")
            },
            "python": sys.version,
            "project_commit": git_commit(PROJECT_ROOT),
            **provenance,
            "code_sha256": {
                str(p.relative_to(PROJECT_ROOT)): sha256(p) for p in code_files
            },
            "baseline_manifest_sha256": sha256(args.baseline_manifest),
            "llm_log_mode": "all",
            "monetary_cap": None,
            "volume_limit": "one trial for task 75; no task retries; one transient API retry",
            "comparison": "Only agent implementation changed; employee work is not measured",
            "tls": tls,
        }
        args.run_dir.mkdir(parents=True)
        manifest_path = args.run_dir / "run_manifest.json"
        write_json(manifest_path, manifest)
        started = time.perf_counter()
        try:
            result = run_domain(config)
            write_json(args.run_dir / "results.json", result.model_dump(mode="json"))
            manifest["status"], manifest["simulations"] = summarize_result(result)
        except BaseException as error:
            manifest["status"] = "runner_error"
            manifest["error_type"] = type(error).__name__
            raise
        finally:
            manifest["wall_seconds"] = round(time.perf_counter() - started, 3)
            manifest["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
            try:
                manifest["sha256_after"] = input_hashes(repo)
                manifest["input_files_unchanged"] = (
                    manifest["sha256_before"] == manifest["sha256_after"]
                )
                manifest["tau2_commit_after"] = git_commit(repo)
                manifest["tau2_commit_unchanged"] = (
                    manifest["tau2_commit_after"] == manifest["tau2_commit"]
                )
            except (OSError, subprocess.SubprocessError) as error:
                manifest["integrity_check_error"] = type(error).__name__
                manifest["input_files_unchanged"] = False
                manifest["tau2_commit_unchanged"] = False
            finally:
                try:
                    write_json(manifest_path, manifest)
                finally:
                    redact_artifacts(args.run_dir, secrets)
        return (
            0
            if (
                manifest["status"] == "finished"
                and manifest["input_files_unchanged"]
                and manifest["tau2_commit_unchanged"]
            )
            else 1
        )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # Детали запросов остаются в очищенных логах; секреты не попадают в консоль.
        print(f"Control failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
