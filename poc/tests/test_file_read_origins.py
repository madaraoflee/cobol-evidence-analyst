"""Source-origin closure is conditional on a supplied successful READ branch."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from business_index import build_business_index
from business_map import build_business_map
from field_origin import supplied_file_read_origins
from question_investigation import build_question_investigation
from repository_discovery import ensure_repository_search
from tools.business_onboarding_fixture import BATCH_QUESTION, sources


SOURCE = sources()["settle.cbl"]


def page(text=SOURCE, *, path="settle.cbl", first=1, sha=None, identifier="ev_source"):
    return {"relative_path": path, "source_sha256": sha or hashlib.sha256(text.encode()).hexdigest(),
            "source_text": text, "start_line": first, "end_line": first + len(text.splitlines()) - 1,
            "format_hint": "free", "evidence_id": identifier}


def candidates(text=SOURCE, *, path="settle.cbl"):
    rows = []
    for number, line in enumerate(text.splitlines(), 1):
        for field, target in (("ORDER-ID", "SETTLEMENT-ID"), ("ORDER-AMOUNT", "SETTLEMENT-AMOUNT")):
            if line.strip().rstrip(".") == f"MOVE {field} TO {target}":
                rows.append({"kind": "business_steps", "relative_path": path,
                    "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "start_line": number, "end_line": number, "occurrence_count": 1,
                    "statement": line.strip(), "reads": [field], "writes": [target]})
    return rows


class FileReadOriginTests(unittest.TestCase):
    def resolve(self, text=SOURCE):
        return supplied_file_read_origins(candidates(text), [page(text)])

    def test_real_neutral_batch_inputs_have_source_origin_without_runtime_certification(self):
        with mock.patch("builtins.open", side_effect=AssertionError("only supplied source is allowed")):
            resolved, witnesses = self.resolve()
        self.assertEqual(resolved, {("settle.cbl", "ORDER-ID"), ("settle.cbl", "ORDER-AMOUNT")})
        for witness in witnesses:
            self.assertEqual(witness["status"], "source_origin_supplied")
            self.assertEqual((witness["file_name"], witness["record_name"], witness["assigned_name"]),
                             ("ORDER-IN", "ORDER-REC", "ORDERIN"))
            self.assertIn("NOT AT END", witness["activation_semantics"][0])
            for flag in ("runtime_value_known", "execution_verified", "external_identity_verified",
                         "input_validity_verified"):
                self.assertFalse(witness[flag])
            self.assertEqual(witness["evidence_ids"], ["ev_source"])
            for ref in witness["source_refs"]:
                excerpt = "\n".join(SOURCE.splitlines()[ref["start_line"] - 1:ref["end_line"]])
                self.assertEqual(ref["excerpt_sha256"], hashlib.sha256(excerpt.encode()).hexdigest())
                self.assertEqual(ref["source_sha256"], page()["source_sha256"])
            covered = {number for ref in witness["source_refs"]
                       for number in range(ref["start_line"], ref["end_line"] + 1)}
            self.assertTrue({2, 7, 11, 12, 13, 15, 22, 26, 28, 30, 31, 35}.issubset(covered))

    def test_adjacent_pages_can_be_reversed_but_missing_physical_lines_cannot_close(self):
        rows, sha = SOURCE.splitlines(), page()["source_sha256"]
        pages = [page("\n".join(rows[:20]), sha=sha, identifier="ev_layout"),
                 page("\n".join(rows[20:]), first=21, sha=sha, identifier="ev_body")]
        resolved, witnesses = supplied_file_read_origins(candidates(), list(reversed(pages)))
        self.assertEqual(len(resolved), 2)
        self.assertEqual(set(witnesses[0]["evidence_ids"]), {"ev_layout", "ev_body"})
        for missing in (6, 10, 12, 25, 27, 34):
            kept = [page("\n".join(rows[:missing]), sha=sha, identifier="ev_prefix"),
                    page("\n".join(rows[missing + 1:]), first=missing + 2, sha=sha, identifier="ev_tail")]
            with self.subTest(missing_line=missing + 1):
                self.assertEqual(supplied_file_read_origins(candidates(), kept)[0], set())

    def test_wrong_versions_conflicts_truncation_and_copy_instances_are_not_joined(self):
        original = page()
        variants = [[{**original, "source_sha256": "0" * 64}],
                    [original, {**original, "source_text": SOURCE.replace("ORDERIN", "OTHERIN"),
                                "evidence_id": "ev_conflict"}],
                    [{**original, "span_truncated": True}],
                    [{**original, "include_chain": ["copy-instance"]}],
                    [{**original, "replacement_trace": ["replacement-instance"]}],
                    [{**original, "end_line": original["end_line"] + 1}]]
        for pages in variants:
            with self.subTest(pages=pages[0].get("evidence_id")):
                self.assertEqual(supplied_file_read_origins(candidates(), pages)[0], set())

    def test_failure_branch_or_read_after_use_is_not_a_successful_input_origin(self):
        variants = [
            SOURCE.replace("NOT AT END\n", "").replace(
                "AT END MOVE 1 TO END-OF-ORDERS", "AT END"),
            SOURCE.replace("READ ORDER-IN\nAT END MOVE 1 TO END-OF-ORDERS\nNOT AT END\n", "").replace(
                "END-READ", "READ ORDER-IN AT END CONTINUE NOT AT END CONTINUE END-READ"),
            SOURCE.replace("END-READ", ""),
            SOURCE.replace("READ ORDER-IN", "READ SETTLEMENT-OUT"),
            SOURCE.replace("READ ORDER-IN", "READ ORDER-IN INTO OTHER-REC"),
        ]
        for text in variants:
            with self.subTest(text=text):
                self.assertEqual(self.resolve(text)[0], set())

    def test_repeated_or_aliased_layouts_do_not_close_by_field_spelling(self):
        variants = [
            SOURCE.replace("WORKING-STORAGE SECTION.", "WORKING-STORAGE SECTION.\n01 ORDER-ID PIC 9(8)."),
            SOURCE.replace("01 ORDER-REC.", "01 ORDER-REC REDEFINES OTHER-REC."),
            SOURCE.replace("05 ORDER-AMOUNT PIC 9(7)V99.", "05 ORDER-AMOUNT OCCURS 2 PIC 9(7)V99."),
            SOURCE.replace("FD SETTLEMENT-OUT.", "01 OTHER-REC.\n05 OTHER-ID PIC 9(8).\nFD SETTLEMENT-OUT."),
            SOURCE.replace("01 ORDER-REC.", "COPY RECORD-LAYOUT."),
            SOURCE.replace("FILE SECTION.", "WORKING-STORAGE SECTION."),
        ]
        for text in variants:
            with self.subTest(text=text):
                resolved = self.resolve(text)[0]
                self.assertNotIn(("settle.cbl", "ORDER-ID"), resolved)
                if "01 ORDER-ID PIC" not in text:
                    self.assertEqual(resolved, set())

    def test_intervening_call_group_write_or_input_overwrite_keeps_origin_unknown(self):
        for operation in ('CALL "OTHER-RULE" USING ORDER-REC', "MOVE ZERO TO ORDER-REC",
                          "MOVE ZERO TO ORDER-ID", "INITIALIZE ORDER-REC",
                          "COMPUTE ORDER-ID = 1", "PERFORM UPDATE-INPUT"):
            text = SOURCE.replace("NOT AT END\n", "NOT AT END\n" + operation + "\n")
            with self.subTest(operation=operation):
                self.assertNotIn(("settle.cbl", "ORDER-ID"), self.resolve(text)[0])

    def test_all_occurrences_and_exact_reader_text_are_required(self):
        original = candidates()[0]
        variants = [
            {**original, "statement": "MOVE OTHER-ID TO SETTLEMENT-ID"},
            {**original, "source_sha256": "0" * 64},
            {**original, "occurrence_count": 2},
            {**original, "occurrences": [{"start_line": original["start_line"], "end_line": original["end_line"]},
                                        {"start_line": 37, "end_line": 37}]},
            {**original, "reads": ["ORDER-ID OF ORDER-REC"]},
        ]
        for candidate in variants:
            with self.subTest(candidate=candidate):
                self.assertEqual(supplied_file_read_origins([candidate], [page()])[0], set())

    def test_other_program_same_field_never_supplies_this_programs_origin(self):
        foreign = page(path="other.cbl")
        self.assertEqual(supplied_file_read_origins(candidates(), [foreign])[0], set())
        combined = SOURCE + "\n" + SOURCE.replace("SETTLE.", "ANOTHER.")
        self.assertEqual(self.resolve(combined)[0], set())

    def test_budgets_remain_bounded_and_each_path_is_collected_once(self):
        import field_origin
        with mock.patch.object(field_origin, "collect_file_impact", wraps=field_origin.collect_file_impact) as collect:
            self.assertEqual(len(self.resolve()[0]), 2)
            self.assertEqual(collect.call_count, 1)
        self.assertEqual(supplied_file_read_origins(candidates(), [page()], max_source_chars=32)[0], set())

    def test_real_index_input_obligation_is_satisfied_without_erasing_runtime_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, database = root / "source", root / "index.sqlite"
            source.mkdir()
            (source / "settle.cbl").write_bytes(SOURCE.encode("utf-8"))
            build_business_index(source, database, source_format="free")
            ensure_repository_search(database, source)
            mapping = build_business_map(database, source, BATCH_QUESTION)
            result = build_question_investigation(BATCH_QUESTION, mapping, database_path=database,
                                                 source_pages=[page()])
            inputs = next(item for item in result["required_items"] if item["kind"] == "inputs")
            self.assertEqual(inputs["status"], "SATISFIED")
            self.assertEqual({row["field"] for row in inputs["source_flow_origins"]},
                             {"ORDER-ID", "ORDER-AMOUNT"})
            self.assertFalse(result["semantic_execution_verified"])
            with mock.patch("question_investigation._flow_input_origins",
                            return_value=(set(), {("settle.cbl", "ORDER-ID")}, [])):
                blocked = build_question_investigation(BATCH_QUESTION, mapping, database_path=database,
                                                      source_pages=[page()])
            inputs = next(item for item in blocked["required_items"] if item["kind"] == "inputs")
            self.assertIn("ORDER-ID", inputs["fields"])
            self.assertNotEqual(inputs["status"], "SATISFIED")


if __name__ == "__main__":
    unittest.main()
