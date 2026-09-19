from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyze_source import analyze_source
from company_api import CompanyAPIConfig, TransportResponse
from web_app import WorkbenchState


class SourceAwareChatTransport:
    """Exercise the ordinary-chat path using references from its actual inputs."""

    def __init__(self):
        self.payloads = []

    def __call__(self, request):
        if request.endpoint != "chat/completions":
            raise AssertionError("Business explanation must not require a feature probe")
        payload = json.loads(request.body)
        self.payloads.append(payload)
        if "tools" in payload or "response_format" in payload:
            raise AssertionError("This endpoint supports ordinary chat only")
        content = "\n".join(item["content"] for item in payload["messages"])
        source_ids = list(dict.fromkeys(re.findall(r"ev_page_[a-f0-9]+", content)))
        framework_ids = list(dict.fromkeys(re.findall(r"fw:[a-f0-9]+:\d+-\d+", content)))
        citations = " ".join(f"[{value}]" for value in source_ids[:2] + framework_ids[:1])
        if "FINAL-AMOUNT" in content:
            text = "## 业务处理\n程序在 APPLY-FINAL 中计算 FINAL-AMOUNT，倍率为 7，并调用 LATEHELP。" + citations
        else:
            text = "## 已读流程\n本段提供声明和处理步骤，后续按已提供的调用关系汇总。" + citations
        return TransportResponse(200, json.dumps({"choices": [{"message": {
            "role": "assistant", "content": text}, "finish_reason": "stop"}]}, ensure_ascii=False))


class BusinessWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="business-workflow-test-key")
        self.reference = self.root / "reference.md"
        self.reference.write_text("# Record processing reference\n<!-- SOURCE_PAGE: 3 -->\n## Final adjustment\n"
                                  "APPLY-FINAL is the final adjustment stage. LATEHELP returns status to the caller.\n", encoding="utf-8")

    def write_source(self, large=False):
        lines = ["IDENTIFICATION DIVISION.", "PROGRAM-ID. LARGEENTRY.", "DATA DIVISION.",
                 "WORKING-STORAGE SECTION.", "COPY OPTIONALAREA.", "01 BASE-AMOUNT PIC 9(6).",
                 "01 FINAL-AMOUNT PIC 9(8).", "PROCEDURE DIVISION.", "MAIN.", "PERFORM APPLY-FINAL."]
        if large:
            for number in range(600):
                lines.extend([f"STEP-{number:04d}.", "CONTINUE."])
            lines.extend(["*> Neutral filler for a late-source retrieval regression."] * (80000 - len(lines)))
        lines.extend(["APPLY-FINAL.", "COMPUTE FINAL-AMOUNT = BASE-AMOUNT * 7.", "CALL 'LATEHELP'.", "GOBACK."])
        (self.source / "entry.cbl").write_text("\n".join(lines), encoding="utf-8")
        (self.source / "helper.cbl").write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. LATEHELP.\nPROCEDURE DIVISION.\nGOBACK.\n", encoding="utf-8")

    def test_long_program_tail_dependency_and_framework_reach_plain_chat(self):
        self.write_source(large=True)
        transport = SourceAwareChatTransport()
        events = []
        output = self.root / "output"
        report = analyze_source(self.source, output, entry="entry.cbl", question="解释 APPLY-FINAL 如何计算 FINAL-AMOUNT 并调用 LATEHELP",
                                index_mode="catalog", source_format="free", allow_network=True,
                                config=self.config, transport=transport, framework_reference_path=self.reference,
                                analysis_mode="business", max_source_pages=4, capture_api_responses=True, progress=events.append)
        runner = json.loads((output / "agent-result.json").read_text(encoding="utf-8"))
        answer = runner["agent_result"]
        self.assertIn(runner["runner_status"], {"COMPLETED", "PARTIAL"})
        self.assertIn(answer["status"], {"ANALYZED", "PARTIAL"})
        self.assertIn("FINAL-AMOUNT", answer["narrative"]["text"])
        self.assertEqual(answer["claims"], [])
        self.assertEqual(answer["narrative"]["verification"], "unverified")
        self.assertGreater(answer["reading_coverage"]["total_pages"], 4)
        self.assertFalse(answer["reading_coverage"]["complete"])
        self.assertTrue(any(ref["start_line"] > 79000 for ref in answer["evidence_refs"]))
        sent = json.dumps(transport.payloads, ensure_ascii=False)
        self.assertIn("final adjustment stage", sent)
        self.assertIn("COMPUTE FINAL-AMOUNT = BASE-AMOUNT * 7", sent)
        self.assertIn("helper.cbl", sent)
        self.assertEqual(report["scope"]["detail_file_count"], 2)
        self.assertTrue(report["unresolved_dependencies"])
        self.assertTrue(any(event["phase"] == "analyzing_pages" for event in events))
        self.assertLessEqual(len(transport.payloads), 5)
        self.assertNotIn(self.config.api_key, sent)

    def test_web_default_produces_readable_answer_and_clickable_source_page_without_tools(self):
        self.write_source()
        transport = SourceAwareChatTransport()

        def analyzer(source, output, **options):
            return analyze_source(source, output, transport=transport, framework_reference_path=self.reference, **options)

        app = WorkbenchState(analyzer=analyzer, config_provider=lambda: self.config)
        job_id = app.start({"source": str(self.source), "output": str(self.root / "web-output"), "entry": "entry.cbl",
                            "question": "解释 FINAL-AMOUNT 的业务处理", "allow_network": True, "max_source_pages": 12})["job_id"]
        deadline = time.monotonic() + 10
        while app.get_job(job_id)["status"] == "RUNNING" and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        job = app.get_job(job_id)
        self.assertEqual(job["status"], "COMPLETED", job.get("error"))
        answer = job["result"]["agent"]["agent_result"]
        self.assertEqual(answer["analysis_mode"], "source_reading")
        self.assertIn("FINAL-AMOUNT", answer["narrative"]["text"])
        reference = answer["evidence_refs"][0]
        evidence = app.evidence(reference["evidence_id"])
        self.assertEqual(evidence["spans"][0]["integrity"], "VALID")
        self.assertFalse(evidence["spans"][0]["span_truncated"])
        self.assertEqual(evidence["snapshot_id"], answer["snapshot_id"])
        self.assertIn(answer["narrative"]["text"], (self.root / "web-output" / "agent-result.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
