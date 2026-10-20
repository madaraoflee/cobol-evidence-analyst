"""Independent physical evidence oracle, including misleading complete tails."""
import json
from contextlib import closing
from pathlib import Path
import sys
import sqlite3
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.framework_scale_fixture import audit_wire_memory, build_fixture, digest, evidence_coverage
from tools.audit_framework_scale_pages import audit, intervals


class FrameworkScaleOracleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.oracle = build_fixture(self.root, specification={
            "programs": 2, "copies": 4, "entry_lines": 180})
        self.source = self.root / "source"
        self.case = self.oracle["cases"][0]

    def page(self, span, start=None, end=None):
        start = span["start_line"] if start is None else start
        end = span["end_line"] if end is None else end
        lines = (self.source / span["path"]).read_text().splitlines()
        return {"relative_path": span["path"], "source_sha256": span["sha256"],
                "start_line": start, "end_line": end,
                "source_text": "\n".join(lines[start - 1:end]), "span_truncated": False}

    def test_oracle_is_recorded_from_original_physical_bytes_and_runtime_is_absent(self):
        for case in self.oracle["cases"]:
            self.assertEqual(case["semantic_gold"]["missing_targets"], ["LEDGERIO"])
            for span in case["required_ranges"]:
                raw = (self.source / span["path"]).read_bytes()
                self.assertEqual(digest(raw), span["sha256"])
                self.assertEqual(span["text"], self.page(span)["source_text"])
            self.assertEqual(len((self.source / case["entry_path"]).read_text().splitlines()), 180)
        self.assertFalse(any("PROGRAM-ID. LEDGERIO" in path.read_text()
                             for path in self.source.rglob("*.cbl")))
        self.assertLess(self.oracle["generation"]["unique_content_hashes"],
                        self.oracle["generation"]["files"])

    def test_distant_outer_guard_has_no_executable_sentence_terminator_before_override(self):
        rows = (self.source / self.case["entry_path"]).read_text().splitlines()
        start = next(i for i, row in enumerate(rows) if row == "IF ROUTE-ENABLED = 1")
        end = next(i for i, row in enumerate(rows) if row == "IF OVERRIDE-FLAG = 1")
        for row in rows[start:end]:
            if not row.startswith("COPY "):
                self.assertNotIn(".", row.split("*>")[0])
        leaf = (self.source / "active/rule-leaf.cpy").read_text()
        self.assertNotIn(".", leaf)
        self.assertIn("INPUT-AMOUNT * RATE-VALUE + SERVICE-FEE", leaf)
        self.assertIn("END-IF\nEND-IF.", "\n".join(rows[end:]))
        for path in self.source.glob("active/*root.cpy"):
            self.assertNotIn("REPLACING", path.read_text())
        self.assertIn("==FACTOR== BY ==3==.", "\n".join(rows))
        self.assertIn("==FACTOR== BY ==5==.", (self.source / "entry/batch.cbl").read_text())

    def test_ranked_formula_without_guard_or_later_overwrite_is_incomplete(self):
        formula = next(span for span in self.case["required_ranges"]
                       if span["path"] == "active/rule-leaf.cpy")
        observed = evidence_coverage(self.source, [self.page(formula)], self.case["required_ranges"])
        self.assertEqual(observed["covered_ranges"], 1)
        self.assertFalse(observed["complete"])

    def test_contiguous_actual_pages_cover_multiline_call_but_missing_middle_line_does_not(self):
        call = next(span for span in self.case["required_ranges"] if 'CALL "LEDGERIO"' in span["text"])
        split = call["start_line"] + 4
        pages = [self.page(call, end=split), self.page(call, start=split + 1)]
        self.assertTrue(evidence_coverage(self.source, pages, [call])["complete"])
        pages[1] = self.page(call, start=split + 2)
        result = evidence_coverage(self.source, pages, [call])
        self.assertFalse(result["complete"])
        self.assertEqual(result["ranges"][0]["missing_lines"], [split + 1])

    def test_correct_range_metadata_cannot_cover_clipped_or_forged_wire_text(self):
        span = self.case["required_ranges"][0]
        original = self.page(span)
        for altered in ({**original, "source_text": original["source_text"][:8]},
                        {**original, "source_sha256": "0" * 64},
                        {**original, "span_truncated": True}):
            with self.subTest(altered=altered):
                result = evidence_coverage(self.source, [altered], [span])
                self.assertFalse(result["complete"])
                self.assertEqual(result["covered_ranges"], 0)
                self.assertTrue(result["invalid_pages"])

    def test_long_physical_line_is_required_even_when_complete_tail_was_supplied(self):
        longline, tail = self.oracle["tool_chain_probes"]["longline"]
        self.assertGreater(len(longline["text"]), 12000)
        self.assertFalse(evidence_coverage(self.source, [self.page(tail)], [longline, tail])["complete"])
        self.assertTrue(evidence_coverage(self.source,
            [self.page(longline), self.page(tail)], [longline, tail])["complete"])

    def test_same_named_copy_and_duplicate_pages_do_not_add_missing_evidence(self):
        span = next(span for span in self.case["required_ranges"] if span["path"] == "active/layout-leaf.cpy")
        duplicate = next(row for row in self.oracle["files"] if row["path"].startswith("redundant/copy"))
        wrong = {"path": duplicate["path"], "sha256": duplicate["sha256"],
                 "start_line": 1, "end_line": duplicate["lines"]}
        self.assertFalse(evidence_coverage(self.source, [self.page(wrong)] * 5, [span])["complete"])
        checked = evidence_coverage(self.source, [self.page(span)] * 5, [span])
        self.assertTrue(checked["complete"])
        self.assertEqual(checked["unique_verified_spans"], 1)
        self.assertEqual(checked["covered_ranges"], 1)

    def test_empty_gold_cannot_report_complete(self):
        self.assertFalse(evidence_coverage(self.source, [], [])["complete"])

    def test_memory_requires_original_body_binding_and_actual_retransmitted_text(self):
        page = {**self.page(self.case["required_ranges"][0]), "evidence_id": "ev-test"}
        binding = {key: page[key] for key in ("relative_path", "source_sha256", "start_line", "end_line")}
        origin = {"round_id": "round-1", "request_body_sha256": "a" * 64, "binding": binding,
                  "binding_sha256": digest(json.dumps(binding, ensure_ascii=False, sort_keys=True,
                                                       separators=(",", ":")).encode()),
                  "excerpt_sha256": digest(page["source_text"].encode())}
        block = {"content_kind": "source_reference", "text": page["source_text"],
                 "evidence_ids": ["ev-test"], "origins": [origin]}
        prior = [{"sequence": 1, "request_body_sha256": "a" * 64, "pages": [page]}]
        def check(value, earlier=prior):
            return audit_wire_memory({"prior_source_memory": {"blocks": [value]}}, earlier)
        self.assertEqual(check(block)["retransmitted_pages"], [page])
        self.assertFalse(check({**block, "content_kind": "model_candidate"})["retransmitted_pages"])
        self.assertFalse(check({**block, "text": "Source was previously read"})["retransmitted_pages"])
        self.assertFalse(check(block, [{**prior[0], "request_body_sha256": "b" * 64}])["retransmitted_pages"])

    def test_full_index_audit_counts_gaps_and_overlap_independently(self):
        result = intervals([(1, 2), (4, 5)], 6)
        self.assertEqual(result["covered_lines"], 4)
        self.assertEqual([row["position"] for row in result["gaps"]], ["middle", "tail"])
        overlap = intervals([(1, 4), (3, 6)], 6)
        self.assertEqual(overlap, {"covered_lines": 6, "overlap_lines": 2, "gaps": []})

    def test_full_index_metadata_cannot_disguise_truncated_or_corrupt_page_text(self):
        root = self.root / "page-audit"
        (root / "source").mkdir(parents=True)
        (root / "measured").mkdir()
        raw = ("FIRST\n" + "X" * 13000 + "\nTAIL\nLAST\n").encode()
        version = digest(raw)
        (root / "source/entry.cbl").write_bytes(raw)
        (root / "oracle.json").write_text(json.dumps({"scale": "test", "files": [
            {"path": "entry.cbl", "sha256": version, "lines": 4, "bytes": len(raw)}]}))
        database = root / "measured/index.sqlite"
        with closing(sqlite3.connect(database)) as db, db:
            for name in ("source_files", "repo_sources"):
                db.execute(f"CREATE TABLE {name} (relative_path TEXT, sha256 TEXT, line_count INTEGER)")
                db.execute(f"INSERT INTO {name} VALUES (?,?,?)", ("entry.cbl", version, 4))
            db.execute("CREATE TABLE repo_pages (page_id INTEGER, evidence_id TEXT, relative_path TEXT, "
                       "start_line INTEGER, end_line INTEGER, source_sha256 TEXT, span_truncated INTEGER, fallback_text TEXT)")
            db.execute("CREATE TABLE evidence_spans (evidence_id TEXT, relative_path TEXT, start_line INTEGER, "
                       "end_line INTEGER, source_sha256 TEXT, text TEXT)")
            for number, first, last, text in ((1, 1, 1, "FIRST"), (3, 3, 4, "TAIL\nLAST")):
                db.execute("INSERT INTO repo_pages VALUES (?,?,?,?,?,?,?,?)",
                           (number, str(number), "entry.cbl", first, last, version, 0, ""))
                db.execute("INSERT INTO evidence_spans VALUES (?,?,?,?,?,?)",
                           (str(number), "entry.cbl", first, last, version, text))
            db.execute("INSERT INTO repo_pages VALUES (?,?,?,?,?,?,?,?)", (2, None, "entry.cbl", 2, 2, version, 1, "X" * 12000))
        result = audit(root, "measured")
        self.assertEqual(result["totals"]["metadata_covered_lines"], 4)
        self.assertEqual(result["totals"]["exact_complete_text_covered_lines"], 3)
        self.assertEqual(result["totals"]["valid_truncated_prefix_pages"], 1)
        self.assertEqual(result["totals"]["page_text_mismatches"], 0)
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("UPDATE evidence_spans SET text='FORGED' WHERE evidence_id='3'")
        broken = audit(root, "measured")["totals"]
        self.assertEqual(broken["metadata_covered_lines"], 4)
        self.assertEqual(broken["exact_complete_text_covered_lines"], 1)
        self.assertEqual(broken["page_text_mismatches"], 1)


if __name__ == "__main__":
    unittest.main()
