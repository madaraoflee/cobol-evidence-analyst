from contextlib import closing
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyze_source import analyze_source
from company_api import CompanyAPIConfig, TransportResponse
from web_app import _validate_options, _source_preferences


class CompilationWorkbenchTests(unittest.TestCase):
    def test_import_and_indexed_followup_share_compilation_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / "source", root / "output"
            source.mkdir()
            source.joinpath("entry.cbl").write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRYPOINT.\n"
                "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 IN-AMOUNT PIC 9(5) VALUE 2.\n"
                "01 OUT-AMOUNT PIC 9(5).\nPROCEDURE DIVISION.\nMAIN.\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nGOBACK.\n")
            context = root / "compilation.json"
            context.write_text(json.dumps({"dialect": "generic", "copy_libraries": ["copybooks", "shared"],
                "copy_library_order_known": True, "compile_conditions": {"FEATURE": True}, "framework_version": "1"}))
            analyze_source(source, output, index_mode="catalog", analysis_mode="business",
                           compilation_context_path=context, source_format="free")
            with closing(sqlite3.connect(output / "structural-index.sqlite")) as db:
                options = json.loads(db.execute("SELECT value FROM metadata WHERE key='source_options'").fetchone()[0])
            self.assertEqual(options["compilation_context"]["copy_libraries"], ["copybooks", "shared"])
            calls = []
            def transport(request):
                payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
                calls.append(payload)
                evidence = next(page["evidence_id"] for page in payload["source_context"][0]["pages"]
                                if "COMPUTE OUT-AMOUNT" in page["source_text"])
                return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant",
                    "content": f"OUT-AMOUNT = IN-AMOUNT * 2。[{evidence}]"}, "finish_reason": "stop"}]}))
            report = analyze_source(source, output, index_mode="catalog", analysis_mode="business",
                reading_strategy="retrieval", question="ENTRYPOINT OUT-AMOUNT 怎么计算？", allow_network=True,
                config=CompanyAPIConfig("https://offline.example.invalid/v1", "offline-model", api_key="offline-only"),
                transport=transport, compilation_context_path=context, source_format="free")
            self.assertTrue(report["index_reused"])
            self.assertEqual(len(calls), 1)
            self.assertIn("calculation", calls[0]["question_investigation"]["analysis_plan"]["intents"])
            options = _validate_options({"source": str(source), "output": str(output), "compilation_context_path": str(context)})
            self.assertEqual(options["compilation_context_path"], str(context))
            preferences = _source_preferences({**options, "source": str(source), "output": str(output)})
            self.assertEqual(preferences["compilation_context_path"], str(context))
