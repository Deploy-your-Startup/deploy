"""Run the shipped health command against a real local TLS server."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import ssl
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / '.github/actions/deployment-health/check.py'


@pytest.fixture
def endpoint(tmp_path):
    certificate, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-days', '1', '-keyout', str(key), '-out', str(certificate),
                    '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'],
                   check=True, capture_output=True)
    state = {'statuses': [200], 'requests': 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            state['requests'] += 1
            status = state['statuses'].pop(0) if len(state['statuses']) > 1 else state['statuses'][0]
            self.send_response(status)
            if status == 302:
                self.send_header('Location', 'http://localhost/private')
            self.end_headers()
            self.wfile.write(b'PRIVATE_RESPONSE_MUST_NOT_BE_LOGGED')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'https://localhost:{server.server_port}/health', certificate, state
    server.shutdown()
    thread.join()
    server.server_close()


def invoke(tmp_path, url, *, certificate='', timeout='1'):
    summary = tmp_path / 'summary'
    result = subprocess.run([sys.executable, str(SCRIPT)], env={
        **os.environ, 'STARTUP_HEALTH_URL': url, 'STARTUP_HEALTH_TIMEOUT': timeout,
        'SSL_CERT_FILE': str(certificate), 'GITHUB_STEP_SUMMARY': str(summary),
    }, capture_output=True, text=True, timeout=15)
    assert 'PRIVATE_RESPONSE_MUST_NOT_BE_LOGGED' not in result.stdout + result.stderr
    return result, summary.read_text()


@pytest.mark.parametrize('statuses,success', [([200], True), ([503, 200], True),
                                             ([503], False), ([302], False), ([401], False)])
def test_deployed_endpoint_must_return_success_with_trusted_tls(tmp_path, endpoint, statuses, success):
    # GIVEN an application endpoint, including startup delays and denied access.
    url, certificate, state = endpoint
    state['statuses'] = statuses.copy()
    # WHEN the exact post-deploy command checks the endpoint over real HTTPS.
    result, summary = invoke(tmp_path, url, certificate=certificate,
                             timeout='5' if len(statuses) > 1 else '1')
    # THEN only HTTP 2xx succeeds; redirects, denial and persistent failures block.
    assert (result.returncode == 0) is success, result.stderr
    assert ('HTTP 2xx' in summary) is success
    assert state['requests'] >= 1


def test_untrusted_certificate_cannot_verify_health(tmp_path, endpoint):
    # GIVEN an endpoint whose certificate the runner does not trust,
    url, _, _ = endpoint
    # WHEN the real checker attempts TLS,
    result, summary = invoke(tmp_path, url)
    # THEN it fails rather than disabling certificate verification.
    assert result.returncode == 1
    assert 'failed' in summary


@pytest.mark.parametrize('url', ['http://localhost/health', 'https://user:SECRET@localhost',
                                'https://localhost?token=SECRET', 'https://localhost/#SECRET'])
def test_invalid_health_configuration_fails_without_disclosing_it(tmp_path, url):
    # GIVEN a URL that is insecure or contains credentials,
    # WHEN the public health command validates configuration,
    result, summary = invoke(tmp_path, url)
    # THEN no request is authorized and no secret appears in output.
    assert result.returncode == 1
    assert 'SECRET' not in result.stdout + result.stderr + summary
    assert 'ValueError' in summary


def test_unconfigured_health_is_explicitly_unverified(tmp_path):
    # GIVEN a project that has not supplied its endpoint,
    # WHEN deployment reaches health verification,
    result, summary = invoke(tmp_path, '')
    # THEN compatibility is preserved and the missing evidence is explicit.
    assert result.returncode == 0
    assert 'skipped' in summary and 'unverified' in summary
