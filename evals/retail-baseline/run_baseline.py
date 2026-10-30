"""Запуск исходной оценки retail без изменения файлов tau2."""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'

p = argparse.ArgumentParser()
p.add_argument('--repo', type=Path, required=True)
p.add_argument('--run-dir', type=Path, required=True)
p.add_argument('--task-ids', nargs='+', required=True)
p.add_argument('--dry-run', action='store_true')
a = p.parse_args()
if not set(a.task_ids) <= {'75', '0', '6', '95', '105'}:
    raise SystemExit('Only the fixed baseline task IDs are allowed')
from dotenv import dotenv_values, load_dotenv
cfg = dotenv_values(a.repo / '.env')
load_dotenv(a.repo / '.env', override=True)
model = cfg.get('OPENAI_MODEL')
if not model or not os.environ.get('OPENAI_API_KEY'):
    raise SystemExit('OPENAI_MODEL and OPENAI_API_KEY must be configured')
if model != 'gpt-6-luna':
    raise SystemExit('Model changed since preparation; review configuration first')
# Используем доверенные сертификаты Windows с включённой проверкой TLS.
if sys.platform == 'win32' and not os.environ.get('SSL_CERT_FILE'):
    import ssl
    import tempfile
    import certifi
    certs = []
    for cert_store in ('ROOT', 'CA'):
        for cert, encoding, trust in ssl.enum_certificates(cert_store):
            if encoding == 'x509_asn' and (trust is True or ssl.Purpose.SERVER_AUTH.oid in trust):
                certs.append(ssl.DER_cert_to_PEM_cert(cert))
    bundle = Path(tempfile.gettempdir()) / 'tau-baseline-windows-ca.pem'
    bundle.write_text(Path(certifi.where()).read_text(encoding='ascii') + '\n' + '\n'.join(certs), encoding='ascii')
    os.environ['SSL_CERT_FILE'] = str(bundle)

os.environ['TAU2_DATA_DIR'] = str(a.repo / 'data')
from tau2.data_model.simulation import TextRunConfig
from tau2.runner import run_domain
from tau2.utils.llm_utils import set_llm_log_mode
import tau2.evaluator.evaluator_nl_assertions as nl

api_model = 'openai/responses/' + model
llm_args = {'reasoning_effort': 'low', 'max_tokens': 8192,
            'timeout': 90, 'num_retries': 1}
nl.DEFAULT_LLM_NL_ASSERTIONS = api_model
nl.DEFAULT_LLM_NL_ASSERTIONS_ARGS = dict(llm_args)
config = TextRunConfig(domain='retail', benchmark=None, language='en',
    task_split_name='train', task_ids=a.task_ids,
    agent='llm_agent', user='user_simulator',
    llm_agent=api_model, llm_user=api_model,
    llm_args_agent=dict(llm_args), llm_args_user=dict(llm_args),
    num_trials=1, max_steps=60, max_errors=5, timeout=600,
    max_concurrency=1, workers=0, max_retries=0,
    seed=300, verbose_logs=True, log_level='INFO',
    auto_resume=False, auto_review=False, hallucination_retries=0,
    save_to=str(a.run_dir.resolve()))
config.validate()
if a.dry_run:
    print(json.dumps({'valid': True, 'config': config.model_dump(mode='json'),
        'nl_judge': {'model': api_model, 'args': llm_args}}, indent=2))
    raise SystemExit(0)
if a.run_dir.exists():
    raise SystemExit('Run directory already exists; do not overwrite an attempt')
a.run_dir.mkdir(parents=True)
files = ['uv.lock', 'data/tau2/domains/retail/db.json',
         'data/tau2/domains/retail/tasks.json', 'data/tau2/domains/retail/split_tasks.json',
         'data/tau2/domains/retail/policy.md', 'src/tau2/domains/retail/tools.py']
manifest = {'started_at_utc': datetime.now(timezone.utc).isoformat(),
    'status': 'running', 'config': config.model_dump(mode='json'),
    'nl_judge': {'model': api_model, 'args': llm_args},
    'llm_log_mode': 'all', 'monetary_cap': None,
    'volume_limit': 'one trial per task, no task retries; one retry for transient API errors',
    'tls': 'verification enabled; certifi plus trusted Windows ROOT/CA certificates',
    'pricing_source': 'https://developers.openai.com/api/docs/models/gpt-6-luna',
    'price_usd_per_million': {'input': 0.10, 'cached_input': 0.01, 'output': 0.50},
    'sha256_before': {f: hashlib.sha256((a.repo / f).read_bytes()).hexdigest() for f in files}}
mp = a.run_dir / 'run_manifest.json'
mp.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
set_llm_log_mode('all')
t0 = time.perf_counter()
try:
    result = run_domain(config)
    manifest['status'] = 'finished'
    manifest['simulations'] = [{'task_id': s.task_id,
        'termination_reason': str(s.termination_reason),
        'reward': s.reward_info.reward if s.reward_info else None} for s in result.simulations]
except BaseException as e:
    manifest['status'] = 'runner_error'
    manifest['error_type'] = type(e).__name__
    # Подробности сбоя остаются в штатных журналах; перед сохранением удаляем секреты.
    raise
finally:
    manifest['wall_seconds'] = round(time.perf_counter()-t0, 3)
    manifest['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
    manifest['sha256_after'] = {f: hashlib.sha256((a.repo / f).read_bytes()).hexdigest() for f in files}
    manifest['input_files_unchanged'] = manifest['sha256_before'] == manifest['sha256_after']
    mp.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    secrets = [str(v) for k,v in cfg.items() if v and any(s in k.upper() for s in ('KEY','SECRET','TOKEN','PASSWORD'))]
    for fp in a.run_dir.rglob('*'):
        if fp.is_file() and fp.suffix in ('.json', '.log', '.txt'):
            text = fp.read_text(encoding='utf-8')
            for value in secrets:
                text = text.replace(value, '[REDACTED]')
            text = re.sub(r'sk-[A-Za-z0-9_\-*]{8,}', '[REDACTED]', text)
            fp.write_text(text, encoding='utf-8')

