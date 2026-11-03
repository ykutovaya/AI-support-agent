"""Запуск согласованной серии с проверкой TLS через доверенные сертификаты Windows."""
import os
import sys
import tempfile
from pathlib import Path
os.environ['PYTHON_DOTENV_DISABLED'] = '1'
os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
os.environ['TAU2_DATA_DIR'] = r'C:\Users\USER\Documents\portfolio\tau2-bench\data'
sys.path[:0] = [str(Path.cwd()), str(Path.cwd() / 'src')]
if sys.platform == 'win32' and not os.environ.get('SSL_CERT_FILE'):
    import ssl
    import certifi
    certificates = []
    for cert_store in ('ROOT', 'CA'):
        for cert, encoding, trust in ssl.enum_certificates(cert_store):
            if encoding == 'x509_asn' and (trust is True or ssl.Purpose.SERVER_AUTH.oid in trust):
                certificates.append(ssl.DER_cert_to_PEM_cert(cert))
    bundle = Path(tempfile.gettempdir()) / 'support-known-cases-trusted-ca.pem'
    bundle.write_text(Path(certifi.where()).read_text(encoding='ascii') + '\n' + '\n'.join(certificates), encoding='ascii')
    os.environ['SSL_CERT_FILE'] = str(bundle)
from evals.investigation.run_investigation import main
try:
    result = main(sys.argv[1:])
except Exception as error:
    print({'error_type': type(error).__name__})
    raise SystemExit(1) from None
raise SystemExit(result)
