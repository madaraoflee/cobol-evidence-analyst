from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from analyze_source import analyze_source
from company_api import CompanyAPIConfig, TransportResponse
from framework_demo import load_framework_demo
from investigation_tools import InvestigationTools


class OrdinaryChat:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        payload = json.loads(request.body)
        self.requests.append(payload)
        if request.endpoint != "chat/completions" or "tools" in payload or "response_format" in payload:
            raise AssertionError("The example must use the ordinary business workflow")
        context = json.loads(payload["messages"][-1]["content"])
        refs = context.get("source_pages") or context.get("page_summaries", [])
        citation = f"[{refs[0]['evidence_id']}]" if refs else ""
        content = "已收到本次实际提供的源码或分段摘要，以下引用可用于回查。" + citation
        return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}, ensure_ascii=False))


class FrameworkPOCWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        shutil.copytree(POC_ROOT / "fixtures" / "framework-workbench" / "source", self.source)
        self.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="synthetic-workflow-key")

    def run_case(self, entry, output):
        transport = OrdinaryChat()
        report = analyze_source(self.source, output, entry=entry, question="解释业务流程、数据协作及错误返回，并引用本次源码。",
                                index_mode="catalog", source_format="free", allow_network=True,
                                config=self.config, transport=transport, framework_reference_path="",
                                analysis_mode="business", max_source_pages=12, capture_api_responses=True)
        runner = json.loads((output / "agent-result.json").read_text())
        self.assertEqual(runner["runner_status"], "COMPLETED")
        answer = runner["agent_result"]
        self.assertTrue(answer["narrative"]["text"])
        self.assertTrue(transport.requests)
        self.assertGreater(answer["reading_coverage"]["sent_pages"], 0)
        tools = InvestigationTools(output / "structural-index.sqlite")
        evidence = tools.read_evidence(answer["evidence_ids"][:1])
        self.assertEqual(evidence["spans"][0]["integrity"], "VALID")
        return report, runner, transport

    def test_all_three_framework_types_use_actual_sources_and_keep_missing_objects_visible(self):
        demo = load_framework_demo()
        for case in demo["cases"]:
            with self.subTest(case=case["id"]):
                report, runner, transport = self.run_case(case["entry_path"], self.root / case["id"])
                self.assertEqual(report["selected_entry"]["program_name"], case["entry_program"])
                self.assertTrue(report["unresolved_dependencies"])
                sent = json.dumps(transport.requests, ensure_ascii=False)
                self.assertIn(case["entry_program"], sent)
                self.assertIn("REQUESTSTORE", sent)
                self.assertNotIn(self.config.api_key, sent)
                self.assertTrue(runner["api_diagnostics"]["exchanges"])

    def test_copied_case_source_changes_reach_model_and_refresh_evidence(self):
        output = self.root / "changed"
        _, first, old_transport = self.run_case("programs/service-entry.cbl", output)
        entry = self.source / "programs" / "service-entry.cbl"
        before = entry.read_text()
        self.assertIn("REQUEST-AMOUNT <= 5000", before)
        entry.write_text(before.replace("REQUEST-AMOUNT <= 5000", "REQUEST-AMOUNT <= 7000"))
        _, second, new_transport = self.run_case("programs/service-entry.cbl", output)
        old_payload = json.dumps(old_transport.requests, ensure_ascii=False)
        new_payload = json.dumps(new_transport.requests, ensure_ascii=False)
        self.assertIn("REQUEST-AMOUNT <= 5000", old_payload)
        self.assertIn("REQUEST-AMOUNT <= 7000", new_payload)
        self.assertNotIn("REQUEST-AMOUNT <= 5000", new_payload)
        self.assertNotEqual(first["agent_result"]["snapshot_id"], second["agent_result"]["snapshot_id"])
        old_refs = [ref for ref in first["agent_result"]["evidence_refs"] if ref["relative_path"] == "programs/service-entry.cbl"]
        new_refs = [ref for ref in second["agent_result"]["evidence_refs"] if ref["relative_path"] == "programs/service-entry.cbl"]
        self.assertTrue(old_refs and new_refs)
        self.assertNotEqual(old_refs[0]["source_sha256"], new_refs[0]["source_sha256"])


if __name__ == "__main__":
    unittest.main()
