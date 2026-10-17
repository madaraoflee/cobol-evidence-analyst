"""Streaming content is progressive, cancellable and never a provider transcript."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import company_api
from company_api import APIClientError, CompanyAPIConfig, OpenAICompatibleChatClient, TransportResponse, UrllibTransport
from api_diagnostics import APIResponseDiagnostics


STREAM_HEADERS = {"Content-Type": "text/event-stream; charset=utf-8"}
SECRET = "stream-test-credential"


def event(delta=None, *, finish=None, **extra):
    return ("data: " + json.dumps({
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}], **extra,
    }, ensure_ascii=False) + "\n\n").encode("utf-8")


def completion(*parts, finish="stop"):
    return b"".join(event({"content": part}) for part in parts) + event(finish=finish) + b"data: [DONE]\n\n"


class ChunkResponse:
    status = 200
    headers = STREAM_HEADERS

    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.closed = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed.set()

    def read1(self, size):
        value = next(self.chunks, b"")
        return value() if callable(value) else value


class StreamingClientTests(unittest.TestCase):
    def config(self, timeout=2):
        return CompanyAPIConfig("https://example.invalid/v1", "test-model", api_key=SECRET,
                                timeout_seconds=timeout)

    def complete(self, client, callback, **kwargs):
        return client.complete(messages=[{"role": "user", "content": "Explain the calculation"}],
                               on_text_delta=callback, **kwargs)

    def injected(self, body, status=200, headers=STREAM_HEADERS):
        transport = mock.Mock(return_value=TransportResponse(status, body, headers))
        return OpenAICompatibleChatClient(self.config(), transport=transport), transport

    def native(self, chunks, timeout=2):
        response = ChunkResponse(chunks)
        transport = UrllibTransport()
        transport._opener = mock.Mock()
        transport._opener.open.return_value = response
        return OpenAICompatibleChatClient(self.config(timeout), transport=transport), transport, response

    def test_native_text_arrives_before_next_read_finishes_on_caller_thread(self):
        delivered = threading.Event()
        threads, texts = [], []
        caller = threading.get_ident()

        def next_chunk():
            self.assertTrue(delivered.wait(1), "First text must arrive while HTTP is still reading")
            return event({"content": "后续结果"}, finish="stop")

        def on_text(text):
            threads.append(threading.get_ident())
            texts.append(text)
            delivered.set()

        client, transport, _ = self.native([event({"content": "已确认。"}), next_chunk, b"data: [DONE]\n\n"])
        result = self.complete(client, on_text)
        self.assertEqual("".join(texts), "已确认。后续结果")
        self.assertEqual(threads, [caller, caller])
        self.assertEqual(result["choices"][0]["message"]["content"], "已确认。后续结果")
        self.assertEqual(transport._opener.open.call_count, 1)

    def test_split_utf8_crlf_comments_and_multiline_json(self):
        payload = json.dumps({"choices": [{"index": 0, "delta": {"content": "中文结果"},
                                           "finish_reason": "stop"}]}, ensure_ascii=False)
        body = ("\ufeff: keepalive\r\nid: ignored\r\nevent: message\r\ndata: "
                + payload[:12] + "\r\ndata: " + payload[12:]
                + "\r\n\r\ndata: [DONE]\r\n\r\n").encode("utf-8")
        client, _, _ = self.native([body[index:index + 1] for index in range(len(body))])
        texts = []
        result = self.complete(client, texts.append)
        self.assertEqual(texts, ["中文结果"])
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")

    def test_only_content_is_emitted_and_tool_calls_usage_are_reconstructed(self):
        body = event({"reasoning_content": "private-analysis-marker", "reasoning": "hidden-marker"})
        body += event({"content": "已找到相关段落。", "tool_calls": [{"index": 0, "id": "call-1",
                      "type": "function", "function": {"name": "read_source", "arguments": '{"file":'}}]})
        body += event({"tool_calls": [{"index": 0, "function": {"arguments": '"MAIN.cbl"}'}}]}, finish="tool_calls")
        body += b'data: {"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15,"provider_note":"hidden","completion_tokens_details":{"reasoning_tokens":2}}}\n\n'
        body += b"data: [DONE]\n\n"
        client, _ = self.injected(body)
        texts = []
        result = self.complete(client, texts.append)
        self.assertEqual(texts, ["已找到相关段落。"])
        self.assertEqual(result["choices"][0]["message"]["tool_calls"], [{
            "id": "call-1", "type": "function",
            "function": {"name": "read_source", "arguments": '{"file":"MAIN.cbl"}'},
        }])
        self.assertEqual(result["usage"]["total_tokens"], 15)
        self.assertEqual(result["usage"]["completion_tokens_details"], {"reasoning_tokens": 2})
        self.assertNotIn("private-analysis-marker", json.dumps(result))
        self.assertNotIn("hidden", json.dumps(result))

    def test_credential_spanning_chunks_is_never_emitted(self):
        client, _ = self.injected(completion("Result: " + SECRET[:8], SECRET[8:], " confirmed"))
        texts = []
        result = self.complete(client, texts.append)
        self.assertEqual("".join(texts), "Result: [REDACTED] confirmed")
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn(SECRET[:8], "".join(texts))

    def test_length_finish_reason_and_refusal_are_preserved(self):
        client, _ = self.injected(completion("Incomplete sentence", finish="length"))
        self.assertEqual(self.complete(client, lambda _: None)["choices"][0]["finish_reason"], "length")
        client, _ = self.injected(event({"refusal": "Refusal explanation"}, finish="content_filter"))
        texts = []
        result = self.complete(client, texts.append)
        self.assertEqual(texts, [])
        self.assertEqual(result["choices"][0]["message"]["refusal"], "Refusal explanation")
        self.assertEqual(result["choices"][0]["finish_reason"], "content_filter")

    def test_terminal_choice_without_done_is_accepted_but_truncated_stream_is_not(self):
        client, _ = self.injected(event({"content": "Complete"}, finish="stop").rstrip())
        self.assertEqual(self.complete(client, lambda _: None)["choices"][0]["message"]["content"], "Complete")
        for body in (event({"content": "Cut short"}), event({"content": "Cut short"}) + b"data: [DONE]\n\n", b""):
            with self.subTest(body=body):
                client, _ = self.injected(body)
                with self.assertRaisesRegex(APIClientError, "STREAM_INCOMPLETE"):
                    self.complete(client, lambda _: None)

    def test_invalid_stream_and_provider_error_are_safe(self):
        for body, code in ((b"data: " + SECRET.encode() + b"\n\n", "INVALID_STREAM_RESPONSE"),
                           (b"data: \xff\n\n", "INVALID_STREAM_RESPONSE"),
                           (b'data: {"error":{"message":"' + SECRET.encode() + b'"}}\n\n', "MODEL_ERROR_RESPONSE")):
            with self.subTest(code=code):
                client, transport = self.injected(body)
                with self.assertRaises(APIClientError) as raised:
                    self.complete(client, lambda _: None)
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn(SECRET, str(raised.exception.to_safe_dict()))
                self.assertEqual(transport.call_count, 1)

    def test_diagnostics_retain_assembled_answer_but_never_raw_reasoning_or_errors(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                diagnostics = APIResponseDiagnostics(protected_values=(SECRET,))
                body = event({"reasoning_content": "private-reasoning-marker"})
                body += (b'data: {"error":{"message":"raw-provider-error-marker"}}\n\n'
                         if fail else completion("Final answer"))
                transport = mock.Mock(return_value=TransportResponse(200, body, STREAM_HEADERS))
                client = OpenAICompatibleChatClient(self.config(), transport=transport, diagnostics=diagnostics)
                if fail:
                    with self.assertRaisesRegex(APIClientError, "MODEL_ERROR_RESPONSE"):
                        self.complete(client, lambda _: None)
                else:
                    self.complete(client, lambda _: None)
                records = diagnostics.to_dict()
                self.assertEqual(records["request_count"], 1)
                self.assertNotIn("private-reasoning-marker", json.dumps(records))
                self.assertNotIn("raw-provider-error-marker", json.dumps(records))
                self.assertEqual(records["exchanges"][0]["http_status"], 200)
                if fail:
                    self.assertEqual(records["exchanges"][0]["body_text"], "")
                else:
                    self.assertIn("Final answer", records["exchanges"][0]["body_text"])

    def test_json_gateway_compatibility_uses_same_request(self):
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "Answer " + SECRET,
                                                   "reasoning_content": "private"}}]})
        client, transport = self.injected(body, headers={"Content-Type": "application/json"})
        texts = []
        self.complete(client, texts.append)
        self.assertEqual(texts, ["Answer [REDACTED]"])
        self.assertEqual(transport.call_count, 1)
        request = transport.call_args.args[0]
        self.assertTrue(json.loads(request.body)["stream"])
        self.assertIn("text/event-stream", request.headers["Accept"])

    def test_unsupported_stream_is_never_retried(self):
        client, transport = self.injected('{"error":{"message":"stream unsupported"}}', status=400,
                                          headers={"Content-Type": "application/json"})
        texts = []
        with self.assertRaisesRegex(APIClientError, "HTTP_ERROR"):
            self.complete(client, texts.append)
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(texts, [])

    def test_nonstream_request_remains_unchanged(self):
        client, transport = self.injected('{"choices":[]}', headers={})
        client.complete(messages=[{"role": "user", "content": "question"}])
        request = transport.call_args.args[0]
        self.assertNotIn("stream", json.loads(request.body))
        self.assertEqual(request.headers["Accept"], "application/json")

    def test_stream_callback_errors_do_not_disclose_exception_or_retry(self):
        client, transport, _ = self.native([completion("Answer")])
        with self.assertRaises(APIClientError) as raised:
            self.complete(client, mock.Mock(side_effect=RuntimeError(SECRET)))
        self.assertEqual(raised.exception.code, "STREAM_CALLBACK_FAILED")
        self.assertNotIn(SECRET, str(raised.exception.to_safe_dict()))
        self.assertEqual(transport._opener.open.call_count, 1)

    def test_response_size_limit_applies_to_raw_sse_not_only_final_text(self):
        body = b": ignored padding " + b"x" * company_api.MAX_RESPONSE_BYTES + b"\n\n" + completion("Small")
        for native in (False, True):
            with self.subTest(native=native):
                if native:
                    client, _, _ = self.native([body])
                else:
                    client, _ = self.injected(body)
                with self.assertRaisesRegex(APIClientError, "RESPONSE_TOO_LARGE"):
                    self.complete(client, lambda _: None)

    def test_cancelled_native_stream_has_no_late_callbacks(self):
        release = threading.Event()
        delivered = threading.Event()
        self.addCleanup(release.set)
        failure = RuntimeError("local cancellation")

        def blocked_chunk():
            release.wait(1)
            return event({"content": "late answer"}, finish="stop")

        client, _, response = self.native([event({"content": "early answer"}), blocked_chunk])
        texts = []

        def on_text(text):
            texts.append(text)
            delivered.set()

        def cancel():
            if delivered.is_set():
                raise failure

        with self.assertRaises(RuntimeError) as raised:
            self.complete(client, on_text, check_cancel=cancel)
        self.assertIs(raised.exception, failure)
        release.set()
        self.assertTrue(response.closed.wait(1))
        self.assertEqual(texts, ["early answer"])

    def test_streaming_read_still_obeys_single_request_timeout(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def blocked_chunk():
            release.wait(1)
            return event({"content": "late answer"}, finish="stop")

        client, transport, response = self.native([event({"content": "early answer"}), blocked_chunk], timeout=0.12)
        texts = []
        started = time.monotonic()
        with self.assertRaisesRegex(APIClientError, "REQUEST_TIMEOUT"):
            self.complete(client, texts.append)
        self.assertLess(time.monotonic() - started, 0.6)
        release.set()
        self.assertTrue(response.closed.wait(1))
        self.assertEqual(texts, ["early answer"])
        self.assertEqual(transport._opener.open.call_count, 1)

    def test_active_generation_can_cross_sixty_seconds_without_a_total_cutoff(self):
        clock = [0.0]

        def after_interval(body):
            def read():
                clock[0] += 40.0
                return body
            return read

        chunks = [after_interval(event({"content": text})) for text in ("First. ", "Second. ", "Third.")]
        chunks += [after_interval(event(finish="stop") + b"data: [DONE]\n\n")]
        client, transport, response = self.native(chunks, timeout=60)
        texts = []
        with mock.patch.object(company_api.time, "monotonic", side_effect=lambda: clock[0]):
            result = self.complete(client, texts.append)
            self.assertTrue(response.closed.wait(1))
        self.assertEqual(clock[0], 160.0)
        self.assertEqual("".join(texts), "First. Second. Third.")
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")
        self.assertEqual(transport._opener.open.call_count, 1)

    def test_tool_argument_progress_can_cross_the_idle_interval(self):
        clock = [0.0]
        chunks = [event({"tool_calls": [{"index": 0, "id": "call-1", "type": "function",
                   "function": {"name": "read_source", "arguments": '{"file":'}}]}),
                  event({"tool_calls": [{"index": 0, "function": {"arguments": '"MAIN.cbl"'}}]}),
                  event({"tool_calls": [{"index": 0, "function": {"arguments": '}'}}]}, finish="tool_calls"),
                  b"data: [DONE]\n\n"]

        def step(body):
            def read():
                clock[0] += 40
                return body
            return read

        client, _, response = self.native([step(body) for body in chunks], timeout=60)
        texts = []
        with mock.patch.object(company_api.time, "monotonic", side_effect=lambda: clock[0]):
            result = self.complete(client, texts.append)
            self.assertTrue(response.closed.wait(1))
        self.assertEqual(clock[0], 160)
        self.assertEqual(texts, [])
        self.assertEqual(result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"],
                         '{"file":"MAIN.cbl"}')

    def test_heartbeat_reasoning_and_empty_content_do_not_renew_idle_timeout(self):
        for body in (b": heartbeat\n\n", event({"role": "assistant"}), event({"content": "   "}),
                     event({"reasoning_content": "private analysis"}),
                     event({"tool_calls": [{"index": 0, "id": "call-1", "type": "function"}]})):
            with self.subTest(body=body):
                clock = [0.0]

                def inactive_chunk():
                    clock[0] += 20
                    return body

                client, transport, response = self.native([inactive_chunk] * 10, timeout=60)
                texts = []
                with mock.patch.object(company_api.time, "monotonic", side_effect=lambda: clock[0]):
                    with self.assertRaisesRegex(APIClientError, "REQUEST_TIMEOUT"):
                        self.complete(client, texts.append)
                    self.assertTrue(response.closed.wait(1))
                self.assertEqual(clock[0], 60)
                self.assertFalse("".join(texts).strip())
                self.assertEqual(transport._opener.open.call_count, 1)

    def test_nonstream_body_retains_its_total_deadline(self):
        clock = [0.0]

        def step(body):
            def read():
                clock[0] += 40
                return body
            return read

        client, transport, response = self.native([step(b'{"choices":'), step(b'[]}')], timeout=60)
        response.headers = {"Content-Type": "application/json"}
        with mock.patch.object(company_api.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(APIClientError, "REQUEST_TIMEOUT"):
                client.complete(messages=[{"role": "user", "content": "Question"}])
            self.assertTrue(response.closed.wait(1))
        self.assertEqual(clock[0], 80)
        self.assertEqual(transport._opener.open.call_count, 1)

    def test_explicit_cancellation_still_wins_during_continuous_progress(self):
        clock = [0.0]
        failure = RuntimeError("explicit question deadline")

        def active_chunk():
            clock[0] += 40
            return event({"content": "Continued answer. "})

        def check_cancel():
            if clock[0] >= 90:
                raise failure

        client, _, response = self.native([active_chunk] * 10, timeout=60)
        texts = []
        with mock.patch.object(company_api.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(RuntimeError) as raised:
                self.complete(client, texts.append, check_cancel=check_cancel)
            self.assertTrue(response.closed.wait(1))
        self.assertIs(raised.exception, failure)
        self.assertEqual(clock[0], 120)
        self.assertEqual(texts, ["Continued answer. ", "Continued answer. "])

    def test_done_finishes_without_waiting_for_a_persistent_sse_connection(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def persistent_connection():
            release.wait(1)
            return b""

        client, transport, response = self.native([completion("Finished"), persistent_connection])
        texts = []
        started = time.monotonic()
        result = self.complete(client, texts.append)
        self.assertLess(time.monotonic() - started, 0.6)
        self.assertEqual(texts, ["Finished"])
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")
        self.assertEqual(transport._opener.open.call_count, 1)
        release.set()
        self.assertTrue(response.closed.wait(1))


if __name__ == "__main__":
    unittest.main()
