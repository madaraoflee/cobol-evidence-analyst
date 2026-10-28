"""Final source-reading answers stream without exposing internal summaries."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_analysis import run_business_analysis
from company_api import CompanyAPIConfig, TransportResponse


def plan(count=1):
    pages = [{"evidence_id": f"ev_page_{index}", "relative_path": "request.cbl",
              "start_line": index * 3 + 1, "end_line": index * 3 + 3,
              "source_sha256": "synthetic-source-version", "span_truncated": False,
              "source_text": "IF VALID-FLAG = 'Y'\nWRITE REQUEST-RECORD\nEND-IF."}
             for index in range(count)]
    return {"snapshot_id": "synthetic-snapshot", "pages": pages,
            "outline": [], "boundaries": [], "programs": [],
            "coverage": {"total_pages": count, "selected_pages": count, "complete": True}}


def response(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}


def sse(*parts):
    return "".join("data: " + json.dumps({"choices": [{"index": 0,
        "delta": {"content": part}, "finish_reason": None}]}, ensure_ascii=False) + "\n\n"
        for part in parts) + 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'


class BusinessAnalysisStreamingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "source-test-model",
                                      api_key="source-test-credential")

    def tearDown(self):
        self.temporary.cleanup()

    def run_analysis(self, *, prepared=None, **kwargs):
        with mock.patch("business_analysis.prepare_source_reading", return_value=prepared or plan()), \
             mock.patch("business_analysis.read_source_page_batch", side_effect=lambda _, pages, **kw: pages):
            return run_business_analysis("Explain request handling", self.root / "index.sqlite", self.root,
                self.config, entry_program="REQUEST", framework_context={}, **kwargs)

    def test_direct_stream_requests_content_and_flushes_the_last_delta(self):
        requests, events = [], []

        def transport(request):
            requests.append(json.loads(request.body))
            hidden = 'data: {"choices":[{"index":0,"delta":{"reasoning_content":"hidden-analysis-marker"},"finish_reason":null}]}\n\n'
            return TransportResponse(200, hidden + sse("A valid request", " is written. [ev_page_0]"),
                                     {"Content-Type": "text/event-stream"})

        result = self.run_analysis(transport=transport, progress=events.append)
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0]["stream"])
        previews = [item["answer_preview"] for item in events if "answer_preview" in item]
        self.assertEqual(previews[0]["text"], "A valid request")
        self.assertEqual(previews[-1]["text"], "A valid request is written. [ev_page_0]")
        self.assertEqual(previews[-1]["round"], 1)
        self.assertEqual(result["agent_result"]["answer"], previews[-1]["text"])
        self.assertNotIn("hidden-analysis-marker", json.dumps(events))

    def test_full_chain_streams_final_answer_and_keeps_reduction_summaries_internal(self):
        requests, events = [], []

        def transport(request):
            payload = json.loads(request.body)
            requests.append(payload)
            if payload.get("stream"):
                return TransportResponse(200, sse("Requests are accepted", " when valid. [ev_page_0]"),
                                         {"Content-Type": "text/event-stream"})
            return TransportResponse(200, json.dumps(response("Internal page summary. [ev_page_0]")))

        output = self.run_analysis(prepared=plan(3), reading_strategy="full_chain", max_pages=2,
                                   transport=transport, progress=events.append)
        self.assertEqual(len(requests), 5)
        self.assertEqual([bool(item.get("stream")) for item in requests], [False] * 4 + [True])
        previews = [item["answer_preview"] for item in events if "answer_preview" in item]
        self.assertEqual(previews[-1]["round"], 5)
        self.assertEqual(previews[0]["text"], "Requests are accepted")
        self.assertNotIn("Internal page summary", json.dumps(previews))
        self.assertEqual(output["agent_result"]["model_turns"], 5)

    def test_preview_arrives_before_completion_and_holds_protected_prefixes(self):
        events, seen = [], []
        values = [self.config.base_url, self.config.chat_model, self.config.resolve_api_key()]

        def complete(**kwargs):
            self.assertIs(kwargs["check_cancel"], check_cancel)
            on_text = kwargs["on_text_delta"]
            chunks = ["Known result. "]
            for value in values:
                chunks.extend([value[:8], value[8:], " next. "])
            with mock.patch("business_analysis.time.monotonic", side_effect=range(100)):
                for chunk in chunks:
                    on_text(chunk)
                    seen.append([event["answer_preview"]["text"] for event in events
                                 if "answer_preview" in event])
            self.assertEqual(seen[0], ["Known result."])
            for value, index in zip(values, (1, 4, 7)):
                self.assertNotIn(value[:8], seen[index][-1])
            return response("".join(chunks))

        check_cancel = mock.Mock()
        with mock.patch("business_analysis.OpenAICompatibleChatClient.complete", side_effect=complete):
            self.run_analysis(transport=mock.Mock(), progress=events.append, check_cancel=check_cancel)
        serialized = json.dumps(events)
        for value in values:
            self.assertNotIn(value, serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_protocol_and_structured_content_are_not_live_answer_previews(self):
        for text in ('{"findings":[]}', '```json\n{"read":[]}\n```'):
            events = []
            with self.subTest(text=text):
                self.run_analysis(transport=lambda _: TransportResponse(200, sse(text),
                    {"Content-Type": "text/event-stream"}), progress=events.append)
                self.assertFalse(any("answer_preview" in item for item in events))

    def test_json_fallback_has_no_retry_and_no_progress_does_not_request_streaming(self):
        for show_progress in (True, False):
            requests, events = [], []

            def transport(request):
                requests.append(json.loads(request.body))
                return TransportResponse(200, json.dumps(response("Accepted. [ev_page_0]")))

            self.run_analysis(transport=transport, progress=events.append if show_progress else None)
            self.assertEqual(len(requests), 1)
            self.assertEqual(bool(requests[0].get("stream")), show_progress)
            if show_progress:
                previews = [item["answer_preview"] for item in events if "answer_preview" in item]
                self.assertEqual(previews[-1]["text"], "Accepted. [ev_page_0]")


if __name__ == "__main__":
    unittest.main()
