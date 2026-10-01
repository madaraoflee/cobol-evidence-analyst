"""Offline checks across the source payload and provider response boundaries."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from api_diagnostics import APIResponseDiagnostics, MAX_BODY_CHARACTERS
from business_analysis import _extract_text
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import (
    CompanyAPIConfig, MAX_REQUEST_BYTES, OpenAICompatibleChatClient, TransportResponse,
)
from repository_discovery import ensure_repository_search


def local_config():
    return CompanyAPIConfig(
        "https://service.example.invalid/v1", "analysis-model",
        api_key="offline-test-credential",
    )


class APIMessageIntegrityTests(unittest.TestCase):
    def test_large_source_retains_deep_formula_in_actual_transport_request(self):
        formula = "COMPUTE PREMIUM ROUNDED = INSURED-AMOUNT * BASE-RATE."
        source = ("      * 业务数据：保额、费率、保费\n"
                  + "           MOVE 1 TO TEMP-VALUE.\n" * 6000
                  + "           " + formula + "\n")
        messages = [
            {"role": "system", "content": "分析源码里的保费公式。"},
            {"role": "user", "content": json.dumps({
                "question": "保费如何计算？",
                "source_context": [{"relative_path": "premium.cbl", "source_text": source}],
            }, ensure_ascii=False)},
        ]
        requests = []
        observed = []

        def transport(request):
            requests.append(request.body)
            return TransportResponse(200, json.dumps({
                "choices": [{"message": {"role": "assistant", "content": "保费 = 保额 × 费率。"},
                             "finish_reason": "stop"}],
            }, ensure_ascii=False))

        OpenAICompatibleChatClient(local_config(), transport=transport,
            request_observer=observed.append).complete(messages=messages)

        self.assertEqual(len(requests), 1)
        self.assertGreater(len(requests[0]), 190_000)
        self.assertLess(len(requests[0]), MAX_REQUEST_BYTES)
        self.assertEqual(observed, requests)
        actual = json.loads(requests[0])
        self.assertEqual(actual["messages"], messages)
        supplied = json.loads(actual["messages"][-1]["content"])
        self.assertEqual(supplied["source_context"][0]["source_text"], source)
        self.assertTrue(supplied["source_context"][0]["source_text"].endswith(formula + "\n"))

    def test_bounded_diagnostic_preview_never_clips_accepted_response(self):
        config = local_config()
        answer = "逐项分析业务条件。" * 2500 + "保费 = 保额 × 费率。"
        response = {
            "choices": [{"message": {"role": "assistant", "content": answer},
                         "finish_reason": "length"}],
            "usage": {"prompt_tokens": 48000, "completion_tokens": 2048, "total_tokens": 50048},
        }
        diagnostics = APIResponseDiagnostics(protected_values=(
            config.api_key, config.base_url, config.chat_model,
        ))
        raw = json.dumps(response, ensure_ascii=False).encode("utf-8")
        actual = OpenAICompatibleChatClient(config, diagnostics=diagnostics,
            transport=lambda request: TransportResponse(200, raw)).complete(
                messages=[{"role": "user", "content": "分析保费规则。"}])

        self.assertEqual(actual, response)
        parsed = _extract_text(actual)
        self.assertEqual(parsed.text, answer)
        self.assertTrue(parsed.truncated)
        preview = diagnostics.to_dict()["exchanges"][0]
        self.assertTrue(preview["body_truncated"])
        self.assertEqual(len(preview["body_text"]), MAX_BODY_CHARACTERS)
        self.assertNotIn("保费 = 保额 × 费率。", preview["body_text"])
        self.assertEqual(preview["body_bytes"], len(raw))

    def test_alternate_response_choice_controls_answer_and_quality_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "premium.cbl").write_text(
                "IDENTIFICATION DIVISION.\nPROGRAM-ID. PREMIUM-CALC.\n"
                "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
                "01 INSURED-AMOUNT PIC 9(8)V99.\n01 BASE-RATE PIC 9V9999.\n"
                "01 PREMIUM PIC 9(8)V99.\nPROCEDURE DIVISION.\nMAIN.\n"
                "COMPUTE PREMIUM ROUNDED = INSURED-AMOUNT * BASE-RATE.\nGOBACK.\n",
                encoding="utf-8",
            )
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", verify_content=True)
            ensure_repository_search(database, source)
            first_choices = (
                {"message": {"role": "assistant", "content": ""}, "finish_reason": "length"},
                None,
            )
            for first_choice in first_choices:
                with self.subTest(first_choice=first_choice):
                    def transport(request):
                        envelope = json.loads(request.body)
                        payload = json.loads(envelope["messages"][-1]["content"])
                        page = next(page for bundle in payload["source_context"]
                                    for page in bundle["pages"] if "COMPUTE PREMIUM" in page["source_text"])
                        return TransportResponse(200, json.dumps({"choices": [first_choice,
                            {"message": {"role": "assistant", "content":
                                f"保费按保额乘以费率计算并舍入。[{page['evidence_id']}]"},
                             "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 700, "completion_tokens": 50, "total_tokens": 750},
                        }, ensure_ascii=False))

                    output = run_business_chat("PREMIUM-CALC 的保费公式是什么？", database,
                        source, local_config(), transport=transport, framework_reference_path="",
                        policy=AgentPolicy(max_model_requests=1))
                    result = output["agent_result"]
                    self.assertEqual(result["status"], "PARTIAL")
                    inputs = next(item for item in result["investigation_state"]["question_investigation"]["required_items"]
                                  if item["kind"] == "inputs")
                    self.assertEqual(inputs["reason"], "input_source_not_located")
                    self.assertEqual(set(inputs["fields"]), {"INSURED-AMOUNT", "BASE-RATE"})
                    self.assertIn("保费按保额乘以费率计算并舍入。", result["answer"])
                    self.assertEqual(len(result["narrative"]["citations"]), 1)
                    self.assertEqual(result["narrative"]["citations"][0]["kind"], "source_page")
                    self.assertEqual(result["metrics"]["model_requests"], 1)
                    trace = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text(encoding="utf-8"))
                    self.assertEqual(trace["rounds"][0]["response"]["finish_reason"], "stop")
                    self.assertEqual(trace["rounds"][0]["response"]["usage"]["total_tokens"], 750)


if __name__ == "__main__":
    unittest.main()
