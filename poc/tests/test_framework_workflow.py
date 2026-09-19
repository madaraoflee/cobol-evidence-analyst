from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from analyze_source import AnalysisCancelled, analyze_source  # noqa: E402
from company_api import CompanyAPIConfig, TransportResponse  # noqa: E402
from run_agent import run_investigation  # noqa: E402
from web_app import WorkbenchState  # noqa: E402


TEST_KEY = "workflow-credential-not-for-prompts-or-reports"
READY = {"agent_readiness": {"ready": True, "mode": "JSON_FALLBACK", "provider_strict_json": False}}
SOURCE = """IDENTIFICATION DIVISION.
PROGRAM-ID. INVENTORYENTRY.
DATA DIVISION.
WORKING-STORAGE SECTION.
COPY OPTIONALAREA.
01 OP-CODE PIC X(8) VALUE 'GET'.
01 ROW-AREA PIC X(20).
PROCEDURE DIVISION.
MAIN-FLOW.
    CALL 'RECORDIO' USING OP-CODE ROW-AREA.
    GOBACK.
"""
REFERENCE = """# Processing reference
<!-- SOURCE_PAGE: 2 / 3 -->
## Generated record access
RECORDIO accepts OP-CODE and ROW-AREA. OP-CODE declares the requested operation;
the returned row and status still depend on the selected view and current data.

<!-- SOURCE_PAGE: 3 / 3 -->
## Separate optional module
COLOURBUFFER describes visual settings. Unrelated reference passage must stay local.
"""


class SourceDrivenTransport:
    """Return scripted decisions whose references come from actual tool results."""

    def __init__(self):
        self.payloads: list[dict] = []
        self.framework: dict = {}
        self.evidence_id: str | None = None
        self.framework_reference_ids: list[str] = []

    def __call__(self, request):
        if request.endpoint != "chat/completions":
            raise AssertionError("Only the mocked chat endpoint is expected")
        payload = json.loads(request.body)
        self.payloads.append(payload)
        messages = payload["messages"]
        for message in messages:
            if message["role"] == "user" and "UNTRUSTED_FRAMEWORK_REFERENCE" in message["content"]:
                for line in message["content"].splitlines():
                    if line.startswith('{"type":"UNTRUSTED_FRAMEWORK_REFERENCE"'):
                        self.framework = json.loads(line)["data"]
        results = []
        for message in messages:
            if message["role"] == "user":
                try:
                    item = json.loads(message["content"])
                except json.JSONDecodeError:
                    continue
                if item.get("type") == "TOOL_RESULT":
                    results.append(item["result"])
        if not results:
            action, arguments = "inspect_symbol", {"name": "INVENTORYENTRY", "symbol_type": "Program"}
        elif len(results) == 1:
            call = next(
                relation for relation in results[0]["matches"][0]["outgoing_relations"]
                if relation["relation_type"] == "CALLS" and relation["target"]["name"] == "RECORDIO"
            )
            self.evidence_id = call["evidence_ref"]["evidence_id"]
            action, arguments = "read_evidence", {"evidence_ids": [self.evidence_id]}
        else:
            references = [reference for reference in self.framework.get("references", [])
                          if "RECORDIO" in reference["matched_terms"]]
            if references:
                self.framework_reference_ids = [references[0]["reference_id"]]
                claim = {
                    "kind": "framework_interpretation",
                    "claim": "资料将 RECORDIO 定义为由操作码驱动的记录访问入口；源码向它传入操作码与记录区，实际返回记录仍取决于现场视图和数据。",
                    "support_status": "unverified", "evidence_ids": [self.evidence_id],
                    "framework_reference_ids": self.framework_reference_ids,
                }
            else:
                claim = {
                    "kind": "code_fact", "claim": "源码调用 RECORDIO，并传入 OP-CODE 与 ROW-AREA。",
                    "support_status": "partial", "evidence_ids": [self.evidence_id],
                    "code_anchors": ["RECORDIO", "OP-CODE", "ROW-AREA"],
                }
            action, arguments = "final_answer", {
                "claims": [claim], "evidence_ids": [self.evidence_id],
                "boundaries": ["调用对象与部分字段定义没有源码；这里只解释当前可见调用及资料约定。"],
            }
        body = {"choices": [{"message": {"role": "assistant", "content": json.dumps(
            {"action": action, "arguments": arguments}, ensure_ascii=False,
        )}}]}
        return TransportResponse(status_code=200, body=json.dumps(body).encode("utf-8"))


class FrameworkWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "export"
        self.source.mkdir()
        (self.source / "inventory.cbl").write_text(SOURCE, encoding="utf-8")
        self.reference = self.root / "processing-reference.md"
        self.reference.write_text(REFERENCE, encoding="utf-8")
        self.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "workflow-model", api_key=TEST_KEY)
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, {"FRAMEWORK_REFERENCE_PATH": ""}).start()
        self.probe = mock.patch("run_agent.probe_capabilities", return_value=READY).start()

    def run_analysis(self, output: Path, reference):
        transport = SourceDrivenTransport()
        report = analyze_source(
            self.source, output, entry="INVENTORYENTRY", question="Explain the requested record operation",
            source_format="free", index_mode="catalog", allow_network=True,
            config=self.config, transport=transport, framework_reference_path=reference,
        )
        result = json.loads((output / "agent-result.json").read_text(encoding="utf-8"))
        return report, result, transport

    def assert_partial_answer(self, report, result, transport):
        self.assertEqual(result["runner_status"], "COMPLETED", result)
        self.assertEqual(result["agent_result"]["stop_reason"], "completed", result)
        self.assertEqual(report["question_status"], "PARTIAL")
        self.assertEqual(result["agent_result"]["status"], "PARTIAL")
        self.assertEqual(result["agent_result"]["tool_calls_used"], 2)
        self.assertEqual(len(transport.payloads), 3)
        self.assertTrue(report["source_manifest_verified"])
        self.assertFalse(result["agent_result"]["claims_semantically_verified"])

    def test_real_workflow_explains_incomplete_source_with_selected_reference(self):
        output = self.root / "analysis"
        report, result, transport = self.run_analysis(output, self.reference)
        self.assert_partial_answer(report, result, transport)
        self.assertEqual(report["framework_context"]["status"], "MATCHED")
        kinds = {item["relation_type"] for item in report["unresolved_dependencies"]}
        self.assertTrue({"CALLS", "INCLUDES_COPY"} <= kinds)
        claim = result["agent_result"]["claims"][0]
        self.assertEqual(claim["kind"], "framework_interpretation")
        self.assertEqual(claim["framework_reference_ids"], transport.framework_reference_ids)
        self.assertEqual(claim["evidence_ids"], [transport.evidence_id])
        self.assertTrue(result["agent_result"]["evidence_refs"][0]["relative_path"].endswith("inventory.cbl"))
        payloads = json.dumps(transport.payloads, ensure_ascii=False)
        self.assertIn("RECORDIO accepts OP-CODE", payloads)
        self.assertNotIn("Unrelated reference passage must stay local", payloads)
        self.assertNotIn("COLOURBUFFER", payloads)
        self.assertNotIn(TEST_KEY, payloads)
        self.assertNotIn(str(self.reference), payloads)
        persisted = json.loads((output / "framework-context.json").read_text(encoding="utf-8"))
        digest = hashlib.sha256(self.reference.read_bytes()).hexdigest()
        self.assertEqual(persisted["document"]["sha256"], digest)
        self.assertEqual(persisted["status"], "MATCHED")
        self.assertIn(digest, (output / "diagnosis.md").read_text(encoding="utf-8"))
        answer = (output / "agent-result.md").read_text(encoding="utf-8")
        self.assertIn("框架解释", answer)
        self.assertIn(digest, answer)
        self.assertIn(transport.framework_reference_ids[0], answer)
        self.assertNotIn(TEST_KEY, json.dumps(report, ensure_ascii=False) + json.dumps(result, ensure_ascii=False))

    def test_missing_or_unconfigured_document_preserves_visible_source_answer(self):
        for reference, expected in (("", "NOT_CONFIGURED"), (self.root / "missing-reference.md", "UNAVAILABLE")):
            with self.subTest(expected=expected):
                report, result, transport = self.run_analysis(self.root / expected.lower(), reference)
                self.assert_partial_answer(report, result, transport)
                self.assertEqual(report["framework_context"]["status"], expected)
                self.assertEqual(result["agent_result"]["claims"][0]["kind"], "code_fact")
                self.assertEqual(result["agent_result"]["framework_context"]["references"], [])
                self.assertNotIn("framework_reference_ids", result["agent_result"]["claims"][0])

    def test_reference_update_replaces_previous_context_and_answer_provenance(self):
        output = self.root / "updated"
        _, first, _ = self.run_analysis(output, self.reference)
        old_digest = first["framework_context"]["document"]["sha256"]
        old_reference = first["agent_result"]["claims"][0]["framework_reference_ids"][0]
        self.reference.write_text(REFERENCE.replace("requested operation", "selected retrieval operation with revised rules"), encoding="utf-8")
        report, current, transport = self.run_analysis(output, self.reference)
        self.assert_partial_answer(report, current, transport)
        new_digest = current["framework_context"]["document"]["sha256"]
        self.assertNotEqual(new_digest, old_digest)
        self.assertNotEqual(current["agent_result"]["claims"][0]["framework_reference_ids"][0], old_reference)
        self.assertEqual(report["build_report"]["files"]["indexed_or_updated"], 0)
        self.assertNotIn(old_digest, (output / "agent-result.md").read_text(encoding="utf-8"))
        self.assertNotIn(old_reference, (output / "framework-context.json").read_text(encoding="utf-8"))

    def test_workbench_job_propagates_framework_readiness_diagnosis_and_answer(self):
        transport = SourceDrivenTransport()
        completed = threading.Event()

        def analyze(*args, **kwargs):
            try:
                # This fixture exercises the legacy structured claim contract.
                kwargs["analysis_mode"] = "strict"
                return analyze_source(*args, transport=transport, **kwargs)
            finally:
                completed.set()

        app = WorkbenchState(analyzer=analyze, config_provider=lambda: self.config)
        output = self.root / "web-analysis"
        with mock.patch.dict(os.environ, {"FRAMEWORK_REFERENCE_PATH": str(self.reference)}):
            before = app.state()
            self.assertEqual(before["framework_knowledge"]["status"], "LOADED")
            self.assertNotIn("references", before["framework_knowledge"])
            job = app.start({
                "source": str(self.source), "output": str(output), "entry": "INVENTORYENTRY",
                "question": "Explain the requested record operation", "source_format": "free", "allow_network": True,
            })
            self.assertTrue(completed.wait(5), "Local analysis did not complete")
            deadline = time.monotonic() + 5
            while app.get_job(job["job_id"])["status"] == "RUNNING" and time.monotonic() < deadline:
                threading.Event().wait(0.01)
            state = app.state()
        self.assertEqual(state["job"]["status"], "COMPLETED", state["job"])
        self.assertIsNone(state["active_job_id"])
        self.assertEqual(state["project"]["diagnosis"]["framework_context"]["status"], "MATCHED")
        agent = state["project"]["agent"]["agent_result"]
        self.assertEqual(agent["status"], "PARTIAL")
        self.assertEqual(agent["claims"][0]["kind"], "framework_interpretation")
        self.assertEqual(state["project"]["snapshot_id"], agent["snapshot_id"])
        self.assertNotIn(TEST_KEY, json.dumps(state, ensure_ascii=False))
        evidence = app.evidence(agent["evidence_ids"][0])
        self.assertEqual(evidence["status"], "OK")
        self.assertIn("CALL 'RECORDIO'", evidence["spans"][0]["source_text"])

    def test_source_changed_during_model_call_discards_framework_matches(self):
        output = self.root / "changed-during-model"
        transport = SourceDrivenTransport()

        def changing_transport(request):
            response = transport(request)
            if len(transport.payloads) == 3:
                (self.source / "inventory.cbl").write_text(SOURCE + "*> Changed during analysis.\n", encoding="utf-8")
            return response

        report = analyze_source(
            self.source, output, entry="INVENTORYENTRY", question="Explain the requested record operation",
            source_format="free", index_mode="catalog", allow_network=True,
            config=self.config, transport=changing_transport, framework_reference_path=self.reference,
        )
        self.assertEqual(transport.framework["status"], "MATCHED")
        self.assertEqual(report["runner_status"], "BLOCKED")
        self.assertFalse(report["source_manifest_verified"])
        self.assertEqual(report["framework_context"]["status"], "LOADED")
        self.assertEqual(report["framework_context"]["references"], [])
        self.assertEqual(report["framework_context"]["source_matches"], [])
        persisted = json.loads((output / "framework-context.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted, report["framework_context"])
        agent = json.loads((output / "agent-result.json").read_text(encoding="utf-8"))
        self.assertIsNone(agent["agent_result"])
        self.assertNotIn(transport.framework_reference_ids[0], (output / "diagnosis.md").read_text(encoding="utf-8"))

    def test_cancellation_after_reference_matching_retains_document_only(self):
        output = self.root / "cancelled-after-matching"
        transport = SourceDrivenTransport()

        def progress(event):
            if event["phase"] == "investigating":
                active = json.loads((output / "framework-context.json").read_text(encoding="utf-8"))
                self.assertEqual(active["status"], "MATCHED")
                raise AnalysisCancelled("Stop after matching")

        with self.assertRaises(AnalysisCancelled):
            analyze_source(
                self.source, output, entry="INVENTORYENTRY", question="Explain the requested record operation",
                source_format="free", index_mode="catalog", allow_network=True, progress=progress,
                config=self.config, transport=transport, framework_reference_path=self.reference,
            )
        report = json.loads((output / "diagnosis.json").read_text(encoding="utf-8"))
        self.assertEqual(report["runner_status"], "CANCELLED")
        self.assertFalse(report["source_manifest_verified"])
        self.assertEqual(report["framework_context"]["status"], "LOADED")
        self.assertEqual(report["framework_context"]["references"], [])
        self.assertEqual(report["framework_context"]["source_matches"], [])
        self.assertEqual(report["framework_context"]["document"]["sha256"], hashlib.sha256(self.reference.read_bytes()).hexdigest())
        self.assertEqual(transport.payloads, [])

    def test_runner_drops_missing_or_mismatched_reference_snapshot_before_model(self):
        output = self.root / "runner-stale-reference"
        prepared = analyze_source(
            self.source, output, entry="INVENTORYENTRY", source_format="free", index_mode="catalog",
            framework_reference_path=self.reference,
        )
        self.assertEqual(prepared["framework_context"]["status"], "MATCHED")
        for snapshot in (None, "outdated-snapshot"):
            with self.subTest(snapshot=snapshot):
                stale = json.loads(json.dumps(prepared["framework_context"]))
                if snapshot is None:
                    stale["coverage"].pop("snapshot_id")
                else:
                    stale["coverage"]["snapshot_id"] = snapshot
                stale["references"][0]["text"] += " Stale reference content must not enter model requests."
                transport = SourceDrivenTransport()
                result = run_investigation(
                    "Explain the requested record operation", output / "structural-index.sqlite", self.config,
                    entry_program="INVENTORYENTRY", framework_context=stale, transport=transport, allow_network=True,
                )
                self.assertEqual(result["runner_status"], "COMPLETED", result)
                self.assertEqual(result["agent_result"]["claims"][0]["kind"], "code_fact")
                self.assertEqual(result["agent_result"]["status"], "PARTIAL")
                framework = result["framework_context"]
                self.assertEqual(framework["status"], "LOADED")
                self.assertEqual(framework["reason_code"], "FRAMEWORK_SOURCE_CONTEXT_STALE")
                self.assertEqual(framework["references"], [])
                self.assertEqual(framework["source_matches"], [])
                self.assertTrue(framework["boundaries"])
                self.assertEqual(transport.framework["references"], [])
                self.assertEqual(transport.framework["source_matches"], [])
                self.assertNotIn("Stale reference content", json.dumps(transport.payloads))


if __name__ == "__main__":
    unittest.main()
