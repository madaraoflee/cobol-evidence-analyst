from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search


class QualityTraceTests(unittest.TestCase):
    def test_trace_records_tool_result_ids_and_cursor_from_actual_read(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            (source / "entry.cbl").write_text("PROGRAM-ID. ENTRY.\nPROCEDURE DIVISION.\n" +
                "\n".join(f"DISPLAY 'RECORD-{index:03d}'." for index in range(100)) + "\n",
                encoding="utf-8")
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", verify_content=True)
            ensure_repository_search(database, source)
            config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="local-test-secret")
            calls = []
            def transport(request):
                calls.append(request.body)
                content = ('{"read":[{"relative_path":"entry.cbl","start_line":1,"end_line":102}]}'
                           if len(calls) == 1 else "The source displays records.")
                return TransportResponse(200, json.dumps({"choices": [{"message": {
                    "role": "assistant", "content": content}, "finish_reason": "stop"}]}))
            output = run_business_chat("Explain ENTRY", database, source, config, transport=transport)
            trace = json.loads(Path(output["agent_result"]["metrics"]["quality_trace_path"]).read_text())
            self.assertEqual(trace["pipeline_version"], "question-evidence-v9")
            self.assertEqual(trace["prompt_version"], "business-chat-v15")
            read = next(row for row in trace["rounds"][0]["tool_results"] if row["action"] == "read")
            self.assertTrue(read["actual_result_ids"])
            self.assertEqual(len(calls), 2)

    def test_trace_matches_actual_request_body_and_requires_opt_in_for_excerpts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            (source / "request.cbl").write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. REQUEST.\n"
                "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 INPUT-VALUE PIC 9.\n"
                "01 RESULT-VALUE PIC 9.\nPROCEDURE DIVISION.\nMAIN.\n"
                "IF INPUT-VALUE > 2\nCOMPUTE RESULT-VALUE = INPUT-VALUE * 3\nEND-IF.\n",
                encoding="utf-8")
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", verify_content=True)
            ensure_repository_search(database, source)
            config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="local-test-secret")
            bodies = []
            def transport(request):
                bodies.append(request.body)
                return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": "结果按三倍计算。"}, "finish_reason": "stop"}]}))
            output = run_business_chat("RESULT-VALUE 如何计算？", database, source, config, transport=transport)
            trace = json.loads(Path(output["agent_result"]["metrics"]["quality_trace_path"]).read_text())
            row = trace["rounds"][0]
            self.assertEqual(row["request_bytes"], len(bodies[0]))
            self.assertEqual(row["request_body_sha256"], hashlib.sha256(bodies[0]).hexdigest())
            self.assertGreater(len(row["sources"]), 0)
            self.assertNotIn("context", row)
            self.assertNotIn("local-test-secret", json.dumps(trace))
            output = run_business_chat("RESULT-VALUE 如何计算？", database, source, config,
                                       transport=transport, capture_context=True)
            captured = json.loads(Path(output["agent_result"]["metrics"]["quality_trace_path"]).read_text())
            self.assertTrue(captured["rounds"][0]["context"]["sources"][0]["excerpt"])


if __name__ == "__main__":
    unittest.main()
