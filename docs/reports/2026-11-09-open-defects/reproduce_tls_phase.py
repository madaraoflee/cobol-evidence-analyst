"""Loopback-only audit: TLS succeeds, then response framing fails after POST."""
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import threading
from unittest.mock import patch

ROOT = Path('/Users/madara/Documents/工作 - Cobol 业务分析')
sys.path.insert(0, str(ROOT / 'poc'))
from company_api import APIClientError, CompanyAPIConfig, OpenAICompatibleChatClient

observed = {'tls_handshake_completed': False, 'http_post_received': False, 'body_bytes_received': 0}
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(str(ROOT / 'tools/fixtures/loopback-server.crt'), str(ROOT / 'tools/fixtures/loopback-server.key'))
listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen(1)
listener.settimeout(5)
port = listener.getsockname()[1]

def respond():
    try:
        connection, _ = listener.accept()
        connection.settimeout(5)
        with context.wrap_socket(connection, server_side=True) as tls:
            observed['tls_handshake_completed'] = True
            observed['negotiated_tls_version'] = tls.version()
            data = b''
            while b'\r\n\r\n' not in data:
                data += tls.recv(4096)
            head, body = data.split(b'\r\n\r\n', 1)
            length = next(int(row.split(b':',1)[1]) for row in head.split(b'\r\n') if row.lower().startswith(b'content-length:'))
            while len(body) < length:
                body += tls.recv(length - len(body))
            observed['http_post_received'] = head.startswith(b'POST /v1/chat/completions HTTP/1.1')
            observed['body_bytes_received'] = len(body)
            raw = socket.socket(fileno=tls.detach())
            with raw:
                raw.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}')
    except Exception as error:
        observed['server_exception_type'] = type(error).__name__
    finally:
        listener.close()

thread = threading.Thread(target=respond, daemon=True)
thread.start()
with patch.dict(os.environ, {'SSL_CERT_FILE':str(ROOT/'tools/fixtures/loopback-server.crt'), 'NO_PROXY':'*'}, clear=True):
    client = OpenAICompatibleChatClient(CompanyAPIConfig(base_url=f'https://127.0.0.1:{port}/v1', chat_model='offline-model', api_key='neutral-fixture-key', timeout_seconds=5), allow_network=True)
    try:
        client.complete(messages=[{'role':'user','content':'Synthetic loopback diagnostic.'}])
        observed['unexpected_success'] = True
    except APIClientError as error:
        observed['actual_safe_error'] = error.to_safe_dict()
        observed['request_outcome'] = client.last_request_outcome
thread.join(6)
observed['thread_stopped'] = not thread.is_alive()
observed['diagnostic_phase_misclassified'] = (observed['tls_handshake_completed'] and observed['http_post_received'] and observed.get('actual_safe_error',{}).get('transport_reason') == 'tls_handshake_failed')
observed['scope'] = 'Native urllib and verified loopback TLS; synthetic request only, zero remote/model calls; not company TLS reproduction.'
output = Path('/private/tmp/cobol-field-audit-20261109/tls-phase-reproduction.json')
output.write_text(json.dumps(observed, ensure_ascii=False, indent=2)+'\n')
print(json.dumps(observed, ensure_ascii=False, indent=2))
assert observed['diagnostic_phase_misclassified'] and observed['request_outcome'] == 'unknown'
