"""Offline transport observations, independent of answer-validation error names."""
from pathlib import Path
import http.client
import io
import json
import socket
import ssl
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_policy import AgentPolicy
from company_api import APIClientError, CompanyAPIConfig, OpenAICompatibleChatClient, TransportResponse, UrllibTransport
from quality_trace import QualityTrace


def event(delta=None, finish=None):
    return ('data: ' + json.dumps({'choices': [{'index': 0, 'delta': delta or {},
                                               'finish_reason': finish}]}) + '\n\n').encode()


class Response:
    status = 200

    def __init__(self, chunks, stream=True):
        self.headers = {'Content-Type': 'text/event-stream' if stream else 'application/json'}
        self.chunks = iter(chunks)
        self.closed = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed.set()

    def read1(self, size):
        value = next(self.chunks, b'')
        return value() if callable(value) else value


class RemoteRequestLifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = CompanyAPIConfig('https://offline.example.invalid/v1', 'offline-model',
            api_key='offline-credential', max_output_tokens=16)
        self.trace = QualityTrace(self.root / 'index.sqlite', question='Explain the source',
            config=self.config, policy=AgentPolicy(max_model_requests=1))
        self.messages = [{'role': 'system', 'content': 'Explain supplied material'},
                         {'role': 'user', 'content': 'Explain the source'}]
        self.trace.prepare(stage='answer', payload={'question': 'Explain the source',
            'source_context': [{'pages': []}], 'framework_references': []}, messages=self.messages)
        self.addCleanup(patch.stopall)
        patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden')).start()
        patch.object(socket, 'create_connection', side_effect=AssertionError('Network forbidden')).start()

    def native(self, response=None, error=None, observer=None):
        transport = UrllibTransport()
        transport._opener = Mock()
        transport._opener.open.return_value = response
        transport._opener.open.side_effect = error
        client = OpenAICompatibleChatClient(self.config, transport=transport,
            request_observer=observer or self.trace.observe)
        return client, transport._opener.open

    def execute(self, client, **kwargs):
        try:
            result = client.complete(messages=self.messages, **kwargs)
            self.trace.record_remote_response(result)
            return result
        except APIClientError as exc:
            self.trace.finish_round(error=exc.code)
            raise
        finally:
            self.trace.record_request_outcome(client.last_request_outcome)

    def saved(self):
        return self.trace.run_store.load(self.trace.data['run_id'])

    def test_typed_native_preconnection_failures_are_not_remote_unknown(self):
        for failure in (socket.gaierror(-2, 'offline name unavailable'), ConnectionRefusedError()):
            with self.subTest(failure=type(failure).__name__):
                self.trace.prepare(stage='answer', payload={'question': 'Explain the source',
                    'source_context': [{'pages': []}]}, messages=self.messages)
                client, send = self.native(error=urllib.error.URLError(failure))
                with self.assertRaises(APIClientError) as caught:
                    self.execute(client)
                self.assertEqual(caught.exception.code, 'TRANSPORT_ERROR')
                self.assertEqual(client.last_request_outcome, 'not_sent')
                self.assertEqual(self.saved()['uncertain_requests'], [])
                self.assertEqual(send.call_count, 1)

    def test_cancellation_after_observation_before_native_send_is_not_sent(self):
        observed = False
        def observer(body):
            nonlocal observed
            self.trace.observe(body)
            observed = True
        def cancel():
            if observed:
                raise RuntimeError('user cancelled before dispatch')
        client, send = self.native(observer=observer)
        with self.assertRaisesRegex(RuntimeError, 'cancelled before dispatch'):
            self.execute(client, check_cancel=cancel)
        self.assertEqual(send.call_count, 0)
        self.assertEqual(client.last_request_outcome, 'not_sent')
        self.assertEqual(self.saved()['uncertain_requests'], [])

    def test_inflight_cancellation_does_not_reuse_a_prior_not_sent_annotation(self):
        cancel_now = threading.Event()
        cancelled = RuntimeError('user cancelled while receiving')
        cancelled.request_outcome = 'not_sent'
        def received():
            cancel_now.set()
            return event({'content': 'partial business text'})
        response = Response([received])
        client, send = self.native(response)
        def cancel():
            if cancel_now.is_set():
                raise cancelled
        with self.assertRaisesRegex(RuntimeError, 'while receiving'):
            self.execute(client, check_cancel=cancel, on_text_delta=lambda text: None)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(client.last_request_outcome, 'unknown')
        self.assertEqual(len(self.saved()['uncertain_requests']), 1)

    def test_incomplete_or_malformed_stream_remains_unknown(self):
        for chunks, code in (([event({'content': 'partial text'})], 'STREAM_INCOMPLETE'),
                             ([b'data: invalid-json\n\n'], 'INVALID_STREAM_RESPONSE')):
            with self.subTest(code=code):
                self.trace.prepare(stage='answer', payload={'question': 'Explain the source',
                    'source_context': [{'pages': []}]}, messages=self.messages)
                client, _ = self.native(Response(chunks))
                before = len(self.saved()['uncertain_requests'])
                with self.assertRaises(APIClientError) as caught:
                    self.execute(client, on_text_delta=lambda text: None)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(client.last_request_outcome, 'unknown')
                self.assertEqual(len(self.saved()['uncertain_requests']), before + 1)

    def test_preview_failure_before_terminal_event_is_unknown(self):
        client, _ = self.native(Response([event({'content': 'partial text'}), event(finish='stop')]))
        def preview(text):
            raise RuntimeError('preview unavailable')
        with self.assertRaises(APIClientError) as caught:
            self.execute(client, on_text_delta=preview)
        self.assertEqual(caught.exception.code, 'STREAM_CALLBACK_FAILED')
        self.assertEqual(client.last_request_outcome, 'unknown')
        self.assertEqual(len(self.saved()['uncertain_requests']), 1)

    def test_preview_failure_in_a_terminal_event_preserves_the_received_answer(self):
        client, _ = self.native(Response([event({'content': 'Complete business text'}, finish='stop')]))
        def preview(text):
            raise RuntimeError('preview unavailable')
        result = self.execute(client, on_text_delta=preview)
        self.assertEqual(result['choices'][0]['message']['content'], 'Complete business text')
        self.assertEqual(client.last_request_outcome, 'response_received')
        self.assertEqual(self.saved()['uncertain_requests'], [])

    def test_terminal_event_combines_fragments_before_redacting_preview(self):
        secret = self.config.api_key
        chunks = ('data: ' + json.dumps({'choices': [
            {'index': 0, 'delta': {'content': secret[:8]}},
            {'index': 0, 'delta': {'content': secret[8:]}, 'finish_reason': 'stop'}]}) + '\n\n').encode()
        client, _ = self.native(Response([chunks]))
        previews = []
        self.execute(client, on_text_delta=previews.append)
        self.assertEqual(''.join(previews), '[REDACTED]')

    def http_response(self, body, content_length):
        class Socket:
            def makefile(self, mode):
                return io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '
                                 + str(content_length).encode() + b'\r\n\r\n' + body)
        response = http.client.HTTPResponse(Socket())
        response.begin()
        return response

    def test_native_premature_http_eof_does_not_imply_complete_response(self):
        response = self.http_response(b'{', 120)
        client, _ = self.native(response)
        with self.assertRaises(APIClientError) as caught:
            self.execute(client)
        self.assertEqual(caught.exception.code, 'INVALID_JSON_RESPONSE')
        self.assertEqual(response.length, 119)
        self.assertEqual(client.last_request_outcome, 'unknown')
        self.assertEqual(len(self.saved()['uncertain_requests']), 1)

    def test_native_response_size_cutoff_does_not_imply_complete_response(self):
        response = self.http_response(b'x' * 120, 120)
        client, _ = self.native(response)
        with patch('company_api.MAX_RESPONSE_BYTES', 20):
            with self.assertRaises(APIClientError) as caught:
                self.execute(client)
        self.assertEqual(caught.exception.code, 'RESPONSE_TOO_LARGE')
        self.assertEqual(response.length, 99)
        self.assertEqual(client.last_request_outcome, 'unknown')
        self.assertEqual(len(self.saved()['uncertain_requests']), 1)

    def test_full_http_error_is_received_even_when_no_business_text_exists(self):
        error = urllib.error.HTTPError('https://offline.example.invalid/v1', 401,
            'offline unauthorized', {'Content-Type': 'application/json'}, io.BytesIO(b'{}'))
        client, _ = self.native(error=error)
        with self.assertRaises(APIClientError) as caught:
            self.execute(client)
        self.assertEqual(caught.exception.code, 'HTTP_ERROR')
        self.assertEqual(client.last_request_outcome, 'response_received')
        self.assertEqual(self.saved()['uncertain_requests'], [])

    def test_full_json_parse_failure_is_a_known_response_not_unknown_delivery(self):
        client, _ = self.native(Response([b'invalid-json'], stream=False))
        with self.assertRaises(APIClientError) as caught:
            self.execute(client)
        self.assertEqual(caught.exception.code, 'INVALID_JSON_RESPONSE')
        self.assertEqual(client.last_request_outcome, 'response_received')
        self.assertEqual(self.saved()['uncertain_requests'], [])

    def test_terminal_reasoning_delta_and_content_are_retained(self):
        client, _ = self.native(Response([event({'content': 'Received business answer',
            'reasoning_content': 'x' * 257}, finish='stop')]))
        result = self.execute(client, on_text_delta=lambda text: None)
        self.assertEqual(client.last_request_outcome, 'response_received')
        self.assertEqual(self.saved()['uncertain_requests'], [])
        saved = json.loads(self.saved()['requests'][0]['response_json'])
        self.assertEqual(saved['remote_response'], result)
        self.assertIn('Received business answer', json.dumps(saved))
        self.assertNotIn('request_outcome', saved)

    def test_nonterminal_reasoning_limit_still_has_unknown_outcome(self):
        client, _ = self.native(Response([event({'reasoning_content': 'x' * 257})]))
        with self.assertRaises(APIClientError) as caught:
            self.execute(client, on_text_delta=lambda text: None)
        self.assertEqual(caught.exception.code, 'MODEL_REASONING_LIMIT_EXCEEDED')
        self.assertEqual(client.last_request_outcome, 'unknown')
        self.assertEqual(len(self.saved()['uncertain_requests']), 1)

    def test_error_name_alone_cannot_close_a_request(self):
        self.trace.observe(b'{}')
        self.trace.finish_round(error='INVALID_JSON_RESPONSE')
        self.assertEqual(len(self.saved()['uncertain_requests']), 1)
        self.trace.record_request_outcome('response_received')
        self.assertEqual(self.saved()['uncertain_requests'], [])

    def test_untrusted_injected_transport_failure_is_not_assumed_unsent(self):
        def fail(request):
            raise ConnectionRefusedError()
        client = OpenAICompatibleChatClient(self.config, transport=fail,
            request_observer=self.trace.observe)
        with self.assertRaises(APIClientError):
            self.execute(client)
        self.assertEqual(client.last_request_outcome, 'unknown')
        self.assertEqual(len(self.saved()['uncertain_requests']), 1)

    def test_generic_tls_error_while_opening_can_follow_a_sent_request(self):
        client, _ = self.native(error=ssl.SSLError('failure while reading HTTP headers'))
        with self.assertRaises(APIClientError) as caught:
            self.execute(client)
        self.assertEqual(caught.exception.transport_reason, 'tls_handshake_failed')
        self.assertEqual(client.last_request_outcome, 'unknown')
        self.assertEqual(len(self.saved()['uncertain_requests']), 1)

    def test_late_complete_json_response_keeps_existing_timeout_but_is_not_unknown(self):
        clock = [0.0]
        def late(request):
            clock[0] = self.config.timeout_seconds + 1
            return TransportResponse(200, '{"choices":[{"message":{"content":"Received answer"},"finish_reason":"stop"}]}')
        client = OpenAICompatibleChatClient(self.config, transport=late,
            request_observer=self.trace.observe)
        with patch('company_api.time.monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(APIClientError) as caught:
                self.execute(client)
        self.assertEqual(caught.exception.code, 'REQUEST_TIMEOUT')
        self.assertEqual(client.last_request_outcome, 'response_received')
        self.assertEqual(self.saved()['uncertain_requests'], [])

    def test_native_json_eof_before_timeout_classification_is_a_known_response(self):
        eof = threading.Event()
        def complete_body():
            eof.set()
            return b''
        client, _ = self.native(Response([b'{}', complete_body], stream=False))
        def clock():
            if eof.is_set() and threading.current_thread().name == 'model-http-request':
                return self.config.timeout_seconds + 1
            return 0.0
        with patch('company_api.time.monotonic', side_effect=clock):
            with self.assertRaises(APIClientError) as caught:
                self.execute(client)
        self.assertEqual(caught.exception.code, 'REQUEST_TIMEOUT')
        self.assertEqual(client.last_request_outcome, 'response_received')
        self.assertEqual(self.saved()['uncertain_requests'], [])


if __name__ == '__main__':
    unittest.main()
