"""Independent regressions for streaming fallback and terminal cancellation."""

import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api_diagnostics import APIResponseDiagnostics
from company_api import CompanyAPIConfig, OpenAICompatibleChatClient, TransportResponse, UrllibTransport


class StreamingReviewBoundaryTests(unittest.TestCase):
    def config(self):
        return CompanyAPIConfig("https://example.invalid/v1", "test-model", api_key="review-test-secret")

    def test_json_fallback_omits_reasoning_from_return_and_diagnostics(self):
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "Final answer",
            "reasoning_content": "private-reasoning-marker", "reasoning": "private-analysis-marker"},
            "finish_reason": "stop"}]})
        diagnostic = APIResponseDiagnostics(protected_values=("review-test-secret",))
        transport = Mock(return_value=TransportResponse(200, body, {"Content-Type": "application/json"}))
        client = OpenAICompatibleChatClient(self.config(), transport=transport, diagnostics=diagnostic)
        texts = []
        result = client.complete(messages=[{"role": "user", "content": "Explain the result"}],
                                 on_text_delta=texts.append)
        self.assertEqual(texts, ["Final answer"])
        self.assertEqual(transport.call_count, 1)
        for value in (result, diagnostic.to_dict()):
            serialized = json.dumps(value)
            self.assertNotIn("private-reasoning-marker", serialized)
            self.assertNotIn("private-analysis-marker", serialized)

    def test_already_complete_native_response_reaches_caller_before_cancellation(self):
        self._complete_with_late_cancellation("Complete final answer.")

    def test_complete_json_fallback_survives_cancelled_progress_callback(self):
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "Final answer"},
                                        "finish_reason": "stop"}]})
        transport = Mock(return_value=TransportResponse(200, body, {"Content-Type": "application/json"}))
        client = OpenAICompatibleChatClient(self.config(), transport=transport)
        callback = Mock(side_effect=RuntimeError("Local cancellation observed by workbench progress"))
        result = client.complete(messages=[{"role": "user", "content": "Explain the result"}],
                                 on_text_delta=callback)
        self.assertEqual(result["choices"][0]["message"]["content"], "Final answer")
        self.assertEqual(callback.call_count, 1)
        self.assertEqual(transport.call_count, 1)

    def test_held_credential_prefix_cannot_trigger_callback_after_terminal_cancellation(self):
        # A benign suffix can equal the beginning of the credential. The text
        # emitter holds it until terminal decoding, when progress callbacks may
        # independently observe cancellation just as WorkbenchState does.
        self._complete_with_late_cancellation("Complete review")

    def _complete_with_late_cancellation(self, answer):
        event = {"choices": [{"index": 0, "delta": {"content": answer},
                              "finish_reason": "stop"}]}
        body = ("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode()
        response = Mock()
        response.status = 200
        response.headers = {"Content-Type": "text/event-stream"}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read1 = Mock(side_effect=[body, b""])
        transport = UrllibTransport()
        transport._opener = Mock()
        transport._opener.open.return_value = response
        client = OpenAICompatibleChatClient(self.config(), transport=transport)
        cancelled = threading.Event()

        def check_cancel():
            if cancelled.is_set():
                raise RuntimeError("Local cancellation after final content")

        def on_text(_):
            check_cancel()
            cancelled.set()

        result = client.complete(messages=[{"role": "user", "content": "Explain the result"}],
                                 on_text_delta=on_text, check_cancel=check_cancel)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(result["choices"][0]["message"]["content"], answer)
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")
        self.assertEqual(transport._opener.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
