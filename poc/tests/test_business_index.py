from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyze_source import analyze_source
from business_index import build_business_index, _physical_lines
from company_api import CompanyAPIConfig, TransportResponse
from source_catalog import refresh_source_catalog
from structural_index import build_structural_index


def program(name="MAIN-ENTRY", body='DISPLAY "READY".\nGOBACK.\n'):
    return f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nPROCEDURE DIVISION.\nMAIN-PARA.\n{body}"


class BusinessIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.database = self.base / "index.sqlite"

    def write(self, relative, text, encoding="utf-8"):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode(encoding))
        return path

    def build(self, **options):
        return build_business_index(self.source, self.database, source_format="free", **options)

    def closure(self, entry="MAIN-ENTRY", **options):
        catalog = refresh_source_catalog(self.source, self.base / "catalog.sqlite", source_format="free")
        return self.build(catalog=catalog, entry_program=entry, **options)

    def test_sparse_facts_preserve_ranges_parameters_hashes_and_exact_evidence(self):
        text = ('IDENTIFICATION DIVISION.\nPROGRAM-ID. MAIN-ENTRY.\nDATA DIVISION.\n'
                'LINKAGE SECTION.\n01 INPUT-STATE PIC X(12).\n01 OUTPUT-STATE PIC X(12).\n'
                'PROCEDURE DIVISION USING\n INPUT-STATE\n OUTPUT-STATE.\n'
                'START-PARA.\nCALL "WORKER-ENTRY"\n USING INPUT-STATE\n OUTPUT-STATE.\n'
                'PERFORM END-PARA.\nMOVE INPUT-STATE TO OUTPUT-STATE.\n'
                'END-PARA.\nGOBACK.\n')
        self.write("main.cbl", text)
        self.write("worker.cbl", program("WORKER-ENTRY"))
        with mock.patch("structural_index.parse_document", side_effect=AssertionError("full statement parser must not run")), \
             mock.patch.object(Path, "read_bytes", side_effect=AssertionError("whole source reads must not run")):
            result = self.build()
        self.assertEqual(result["index_kind"], "business_sparse")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            unit = connection.execute("SELECT * FROM code_units WHERE unit_type='Program' AND name='MAIN-ENTRY'").fetchone()
            self.assertEqual(unit["end_line"], len(text.splitlines()))
            paragraph = connection.execute("SELECT * FROM code_units WHERE unit_type='Paragraph' AND name='START-PARA'").fetchone()
            self.assertEqual(paragraph["end_line"], 15)
            signature = connection.execute("SELECT * FROM code_units WHERE unit_type='ProcedureSignature' AND relative_path='main.cbl'").fetchone()
            self.assertEqual((signature["start_line"], signature["end_line"]), (7, 9))
            call = connection.execute("SELECT r.status,e.* FROM relations r JOIN evidence_spans e USING(evidence_id) WHERE r.relation_type='CALLS'").fetchone()
            self.assertEqual(call["status"], "confirmed")
            self.assertEqual((call["start_line"], call["end_line"]), (11, 13))
            self.assertEqual(call["text"], "\n".join(text.splitlines()[10:13]))
            self.assertEqual(call["source_sha256"], hashlib.sha256(text.encode()).hexdigest())
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM relations WHERE relation_type IN ('READS','WRITES','PASSES_AS')").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM call_bindings").fetchone()[0], 0)

    def test_stream_decoder_handles_bom_legacy_and_all_physical_newline_forms(self):
        text = program(body='*> 中性說明\nDISPLAY "READY".\n')
        for encoding in ("utf-8-sig", "utf-16", "utf-32", "cp950"):
            with self.subTest(encoding=encoding):
                self.write("main.cbl", text, encoding)
                result = self.build(encoding="auto")
                self.assertEqual(result["diagnostics"]["program_count"], 1)
                with closing(sqlite3.connect(self.database)) as connection:
                    self.assertEqual(connection.execute("SELECT line_count FROM source_files").fetchone()[0], len(text.splitlines()))
        for text in ("a\nb\r\nc\rd\vlast", "a\n\r", "a\r", "a", "", "\n"):
            path = self.write("lines.txt", text)
            self.assertEqual([line for _, line in _physical_lines(path, "utf-8")], text.splitlines())

    def test_cached_parser_boundaries_do_not_disappear_and_copy_candidate_is_still_read(self):
        self.write("main.cbl", program(body='COPY "shared.cpy" REPLACING ==OLD== BY ==NEW==.\nEXEC SQL\n SELECT 1\nEND-EXEC.\n'))
        self.write("shared.cpy", '01 OLD-STATE PIC X(12).\n')
        first = self.closure()
        second = self.closure()
        self.assertEqual(set(first["relative_paths"]), {"main.cbl", "shared.cpy"})
        self.assertFalse(first["scope"]["complete_dependency_closure"])
        self.assertFalse(second["scope"]["complete_dependency_closure"])
        self.assertEqual(first["missing_dependencies"], second["missing_dependencies"])
        self.assertEqual(second["files"]["skipped_unchanged"], 2)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT status FROM relations WHERE relation_type='INCLUDES_COPY'").fetchone()[0], "candidate")

    def test_comments_and_quoted_display_text_cannot_add_dependencies(self):
        self.write("main.cbl", program(body='*> CALL "FALSE-A".\nDISPLAY "CALL \'FALSE-B\' COPY LOST".\n'
                   'CALL\n "WORKER-ENTRY".\nCALL DYNAMIC-WORKER.\n'))
        self.write("worker.cbl", program("WORKER-ENTRY"))
        result = self.closure()
        self.assertEqual(set(result["relative_paths"]), {"main.cbl", "worker.cbl"})
        with closing(sqlite3.connect(self.database)) as connection:
            targets = {row[0] for row in connection.execute("SELECT target_name FROM relations")}
        self.assertEqual(targets, {"WORKER-ENTRY", "DYNAMIC-WORKER"})
        self.assertFalse(result["scope"]["complete_dependency_closure"])

    def test_mode_switch_rebuilds_without_retaining_old_semantic_facts(self):
        self.write("main.cbl", 'IDENTIFICATION DIVISION.\nPROGRAM-ID. MAIN-ENTRY.\nDATA DIVISION.\n'
                   'WORKING-STORAGE SECTION.\n01 VALUE-IN PIC 9.\n01 VALUE-OUT PIC 9.\n'
                   'PROCEDURE DIVISION.\nMOVE VALUE-IN TO VALUE-OUT.\nGOBACK.\n')
        first = build_structural_index(self.source, self.database, source_format="free", quiet=True)
        sparse = self.build()
        self.assertTrue(sparse["parser_rebuild_required"])
        self.assertEqual(first["snapshot_id"], sparse["snapshot_id"])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM relations").fetchone()[0], 0)
        strict = build_structural_index(self.source, self.database, source_format="free", quiet=True)
        self.assertTrue(strict["parser_rebuild_required"])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertGreater(connection.execute("SELECT COUNT(*) FROM relations WHERE relation_type='WRITES'").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT value FROM metadata WHERE key='index_kind'").fetchone()[0], "structural")

    def test_extensionless_control_copy_is_followed_without_spending_call_depth(self):
        self.write("main.cbl", program(body='COPY CONTROL.\nGOBACK.\n'))
        self.write("CONTROL", 'CONTROL-FLOW.\nCOPY NESTED.\n')
        self.write("NESTED", 'CALL "WORKER-ENTRY".\n')
        self.write("worker.cbl", program("WORKER-ENTRY"))
        catalog = refresh_source_catalog(self.source, self.base / "catalog.sqlite", include_extensionless=True, source_format="free")
        result = self.build(catalog=catalog, entry_program="MAIN-ENTRY", include_extensionless=True)
        self.assertEqual(set(result["relative_paths"]), {"main.cbl", "CONTROL", "NESTED", "worker.cbl"})
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM symbols WHERE symbol_type='Copybook'").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM relations WHERE relation_type='INCLUDES_COPY' AND status='confirmed'").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT end_line FROM code_units WHERE name='CONTROL-FLOW'").fetchone()[0], 2)

    def test_multiple_program_ranges_end_before_the_next_identification_division(self):
        first = program("FIRST-ENTRY")
        second = program("SECOND-ENTRY") + 'END PROGRAM SECOND-ENTRY.\n'
        self.write("combined.cbl", first + second)
        self.build()
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT end_line FROM code_units WHERE unit_type='Program' AND name='FIRST-ENTRY'").fetchone()[0], len(first.splitlines()))
            self.assertEqual(connection.execute("SELECT end_line FROM code_units WHERE unit_type='Program' AND name='SECOND-ENTRY'").fetchone()[0], len((first + second).splitlines()))

    def test_path_escape_symlink_and_mutation_are_rejected(self):
        path = self.write("main.cbl", program())
        for relative in ("../outside.cbl", "/outside.cbl", "C:/outside.cbl"):
            with self.subTest(relative=relative), self.assertRaises((ValueError, OSError)):
                self.build(include_paths=[relative])
        outside = self.write("outside.cbl", program("OUTSIDE-ENTRY"))
        link = self.source / "unsafe.cbl"
        link.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.build(include_paths=["unsafe.cbl"])
        link.unlink()
        def mutate(event):
            if event["phase"] == "reading" and event.get("file_bytes_completed"):
                path.write_text(program(body='DISPLAY "CHANGED".\n'))
        with self.assertRaisesRegex(ValueError, "SOURCE_CHANGED_DURING_READ"):
            self.build(include_paths=["main.cbl"], progress=mutate)

    def simulated_transport(self, observed):
        def send(request):
            payload = json.loads(request.body)
            prompt = json.loads(payload["messages"][-1]["content"])
            observed["requests"] = observed.get("requests", 0) + 1
            for page in prompt.get("source_pages", []):
                observed["source_pages"] = observed.get("source_pages", 0) + 1
                observed.setdefault("source_paths", set()).add(page["relative_path"])
                if "FINAL-BUSINESS-RULE" in page["source_text"]:
                    observed["tail_rule_sent"] = True
            return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content":
                "SIMULATED TEST RESPONSE: source pages were received; no real model or production transaction was used."}, "finish_reason": "stop"}]}))
        return send

    def analyze(self, observed, **options):
        index_mode = options.pop("index_mode", "catalog")
        return analyze_source(self.source, self.base / "output", entry="MAIN-ENTRY", question="Explain the business outcome",
            source_format="free", index_mode=index_mode, analysis_mode="business", reading_strategy="full_chain",
            allow_network=True, config=CompanyAPIConfig("https://service.example/v1", "test-text-model", api_key="test-only-key"),
            transport=self.simulated_transport(observed), framework_reference_path=self.base / "absent-reference.md", **options)

    def test_more_than_twenty_four_files_reach_actual_simulated_model_requests(self):
        for index in range(36):
            name = "MAIN-ENTRY" if index == 0 else f"WORKER-{index:03d}"
            body = f'CALL "WORKER-{index + 1:03d}".\nGOBACK.\n' if index < 35 else 'FINAL-BUSINESS-RULE.\nMOVE "APPROVED" TO REQUEST-STATE.\nGOBACK.\n'
            self.write(f"member-{index:03d}.cbl", program(name, body))
        observed = {}
        started = time.perf_counter()
        result = self.analyze(observed, max_source_pages=4)
        self.assertNotEqual(result["runner_status"], "BLOCKED", result["messages"])
        self.assertEqual(result["build_report"]["index_kind"], "business_sparse")
        self.assertEqual(result["scope"]["selected_file_count"], 36)
        self.assertEqual(observed["source_paths"], {f"member-{index:03d}.cbl" for index in range(36)})
        self.assertTrue(observed["tail_rule_sent"])
        self.assertGreater(observed["requests"], 36)
        self.last_workload_metrics = {"scenario": "static_call_chain", "files": 36,
            "bytes": sum(path.stat().st_size for path in self.source.iterdir()),
            "lines": sum(len(path.read_text().splitlines()) for path in self.source.iterdir()),
            "sent_pages": observed["source_pages"], "simulated_requests": observed["requests"],
            "elapsed_seconds": round(time.perf_counter() - started, 3)}

    def test_single_source_above_sixteen_mib_is_streamed_and_reaches_model(self):
        path = self.source / "large.cbl"
        with path.open("w", encoding="utf-8") as handle:
            handle.write('IDENTIFICATION DIVISION.\nPROGRAM-ID. MAIN-ENTRY.\nDATA DIVISION.\n'
                         'WORKING-STORAGE SECTION.\n01 REQUEST-AMOUNT PIC 9(7).\n01 WORK-AMOUNT PIC 9(7).\n'
                         'PROCEDURE DIVISION.\nMAIN-PARA.\n')
            filler = 'MOVE REQUEST-AMOUNT TO WORK-AMOUNT.\n' * 4096
            while handle.tell() <= 16 * 1024 * 1024:
                handle.write(filler)
            handle.write('FINAL-BUSINESS-RULE.\nCALL "TAIL-WORKER".\nGOBACK.\n')
        self.write("worker.cbl", program("TAIL-WORKER", 'MOVE "APPROVED" TO REQUEST-STATE.\nGOBACK.\n'))
        self.assertGreater(path.stat().st_size, 16 * 1024 * 1024)
        observed = {}
        started = time.perf_counter()
        with mock.patch("analyze_source.build_structural_index", side_effect=AssertionError("business must not build full statements")):
            result = self.analyze(observed, max_source_pages=12)
        self.assertNotEqual(result["runner_status"], "BLOCKED", result["messages"])
        self.assertNotEqual(result["reason_code"], "ENTRY_SCOPE_LIMIT")
        self.assertTrue(result["source_manifest_verified"])
        self.assertEqual(result["scope"]["selected_file_count"], 2)
        self.assertTrue(observed["tail_rule_sent"])
        self.assertIn("worker.cbl", observed["source_paths"])
        self.assertLess(result["build_report"]["database_counts"]["code_units"], 20)
        agent = json.loads((self.base / "output" / "agent-result.json").read_text())
        coverage = agent["agent_result"]["reading_coverage"]
        self.assertGreater(coverage["total_pages"], 128)
        self.assertEqual(coverage["total_pages"], coverage["sent_pages"])
        self.assertTrue(coverage["complete"])
        self.last_workload_metrics = {"scenario": "large_source", "large_file_bytes": path.stat().st_size,
            "total_files": coverage["total_files"], "total_lines": coverage["total_lines"],
            "total_pages": coverage["total_pages"], "sent_pages": coverage["sent_pages"],
            "simulated_requests": observed["requests"],
            "index_code_units": result["build_report"]["database_counts"]["code_units"],
            "elapsed_seconds": round(time.perf_counter() - started, 3)}

    def test_full_directory_business_mode_uses_sparse_index(self):
        self.write("main.cbl", program())
        with mock.patch("analyze_source.build_structural_index", side_effect=AssertionError("strict parser must not run")):
            result = analyze_source(self.source, self.base / "output", analysis_mode="business", index_mode="full")
        self.assertEqual(result["build_report"]["index_kind"], "business_sparse")
        self.assertEqual(result["scope"]["kind"], "full_directory")

    def test_full_directory_missing_copy_is_an_explicit_dependency_gap(self):
        self.write("main.cbl", program(body='COPY MISSING-CONTEXT.\nGOBACK.\n'))
        result = self.build()
        self.assertFalse(result["scope"]["complete_dependency_closure"])
        self.assertIn({"relative_path": "main.cbl", "relation_type": "INCLUDES_COPY",
                       "target_name": "MISSING-CONTEXT", "status": "MISSING_SOURCE"}, result["scope"]["boundaries"])
        observed = {}
        analysis = self.analyze(observed, index_mode="full")
        self.assertEqual(analysis["question_status"], "PARTIAL")
        self.assertGreater(observed["requests"], 0)


if __name__ == "__main__":
    unittest.main()
