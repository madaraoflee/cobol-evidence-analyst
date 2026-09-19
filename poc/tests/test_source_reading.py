from __future__ import annotations

import hashlib
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import tracemalloc
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from investigation_tools import InvestigationTools
from source_reading import prepare_source_reading, read_source_page_batch, _verified_pages
from structural_index import build_structural_index


def program(name="MAIN-ENTRY", body='DISPLAY "READY".\nGOBACK.\n'):
    return f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nPROCEDURE DIVISION.\n{body}"


class SourceReadingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, relative, text, encoding="utf-8"):
        target = self.source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode(encoding))
        return target

    def build(self, **kwargs):
        return build_structural_index(self.source, self.database, quiet=True, source_format="free", **kwargs)

    def prepare(self, **kwargs):
        options = {"entry_program": "MAIN-ENTRY", "question": "Explain the calculation."}
        options.update(kwargs)
        return prepare_source_reading(self.database, self.source, **options)

    def test_small_scope_is_complete_and_second_page_has_real_stable_evidence(self):
        source_text = program(body='START-PARA.\n' + 'DISPLAY "READY".\n' * 50 + 'END-PARA.\nGOBACK.\n')
        path = self.write("entry.cbl", source_text)
        build = self.build()
        before = self.metadata()
        result = self.prepare(max_pages=12, page_chars=160)
        self.assertTrue(result["coverage"]["complete"])
        self.assertGreater(len(result["pages"]), 2)
        page = result["pages"][1]
        self.assertGreater(page["start_line"], 1)
        self.assertEqual(page["source_text"], "\n".join(source_text.splitlines()[page["start_line"] - 1:page["end_line"]]))
        self.assertEqual(page["source_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        spans = InvestigationTools(self.database).read_evidence([page["evidence_id"]])["spans"]
        self.assertEqual(spans[0]["source_text"], page["source_text"])
        self.assertEqual(spans[0]["integrity"], "VALID")
        self.assertFalse(spans[0]["span_truncated"])
        self.assertEqual(result["snapshot_id"], build["snapshot_id"])
        self.assertEqual(before, self.metadata())
        repeated = self.prepare(max_pages=12, page_chars=160)
        self.assertEqual(result["evidence_refs"], repeated["evidence_refs"])
        self.assertTrue(all("source_text" not in ref for ref in result["evidence_refs"]))

    def metadata(self):
        with closing(sqlite3.connect(self.database)) as connection:
            return {"metadata": connection.execute("SELECT * FROM metadata ORDER BY key").fetchall(),
                    "source_files": connection.execute("SELECT * FROM source_files ORDER BY relative_path").fetchall()}

    def test_eighty_thousand_line_tail_and_chinese_question_are_selected(self):
        text = program(body='START-PARA.\n' + '*> ordinary filler\n' * 79990 +
                       'FINAL-RULE.\n*> 期末結算規則\nCOMPUTE RESULT-AMOUNT = BASE-AMOUNT * 2.\nGOBACK.\n')
        self.write("entry.cbl", text)
        self.build()
        result = self.prepare(question="期末結算規則如何計算", max_pages=4, page_chars=1200)
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(len(result["pages"]), 4)
        self.assertGreater(result["coverage"]["total_pages"], 100)
        self.assertTrue(any("COMPUTE RESULT-AMOUNT" in page["source_text"] for page in result["pages"]))
        self.assertTrue(any(page["start_line"] > 79900 for page in result["pages"]))
        self.assertTrue(any("distributed_coverage" in page["selection_reasons"] for page in result["pages"]))
        self.assertEqual(result["coverage"]["total_lines"], len(text.splitlines()))

    def test_question_selects_target_beyond_five_hundred_units(self):
        body = 'START-PARA.\nPERFORM CHECK-0840.\n'
        body += "".join(f'CHECK-{index:04d}.\nDISPLAY "VALUE-{index:04d}".\n' for index in range(900))
        body += 'GOBACK.\n'
        self.write("entry.cbl", program(body=body))
        self.build()
        result = self.prepare(question="Explain CHECK-0840 and VALUE-0840", max_pages=5, page_chars=300)
        self.assertGreater(result["outline"][0]["code_unit_count"], 500)
        self.assertTrue(any('DISPLAY "VALUE-0840"' in page["source_text"] for page in result["pages"]))
        self.assertTrue(result["outline"][0]["outline_truncated"])

    def test_chinese_comment_in_middle_is_selected(self):
        text = program(body='*> ordinary filler\n' * 150 + '*> 特殊餘額調整\nMOVE 7 TO RESULT-AMOUNT.\n' + '*> ordinary filler\n' * 150)
        self.write("entry.cbl", text)
        self.build()
        result = self.prepare(question="特殊餘額調整如何執行", max_pages=4, page_chars=220)
        self.assertTrue(any("特殊餘額調整" in page["source_text"] for page in result["pages"]))

    def test_confirmed_called_program_is_prioritized_in_reading_plan(self):
        self.write("entry.cbl", program(body='CALL "WORKER-ENTRY".\n' + '*> ordinary entry filler\n' * 80))
        self.write("worker.cbl", program("WORKER-ENTRY", 'DISPLAY "WORKER".\n' + '*> worker filler\n' * 80))
        self.write("z-extra.cbl", program("EXTRA-ENTRY", 'DISPLAY "EXTRA".\n' + '*> extra filler\n' * 80))
        self.build()
        result = self.prepare(question="", max_pages=4, page_chars=150)
        worker = [page for page in result["pages"] if page["relative_path"] == "worker.cbl"]
        self.assertTrue(worker)
        self.assertIn("call_or_perform_relation", worker[0]["selection_reasons"])

    def test_repeated_entry_terms_cannot_evict_callee_or_middle_callsite(self):
        body = '*> settlement eligibility\n' * 120
        body += 'CALL "RULE-WORKER" USING REQUEST-AMOUNT REQUEST-STATE.\n'
        body += '*> settlement eligibility\n' * 120
        self.write("entry.cbl", program(body=body))
        self.write("worker.cbl", 'IDENTIFICATION DIVISION.\nPROGRAM-ID. RULE-WORKER.\n'
                   'DATA DIVISION.\nLINKAGE SECTION.\n01 REQUEST-AMOUNT PIC 9(5).\n'
                   '01 REQUEST-STATE PIC X(12).\nPROCEDURE DIVISION USING REQUEST-AMOUNT REQUEST-STATE.\n'
                   'IF REQUEST-AMOUNT <= 5000 MOVE "APPROVED" TO REQUEST-STATE END-IF.\nGOBACK.\n')
        self.write("z-unrelated.cbl", program("UNRELATED", '*> settlement eligibility\n' * 100))
        self.build()
        result = self.prepare(question="settlement eligibility", max_pages=4, page_chars=450)
        text = "\n".join(page["source_text"] for page in result["pages"])
        self.assertIn('CALL "RULE-WORKER" USING', text)
        self.assertIn('PROCEDURE DIVISION USING REQUEST-AMOUNT REQUEST-STATE', text)
        self.assertIn('MOVE "APPROVED"', text)
        link = result["call_chain"]["links"][0]
        self.assertTrue(link["selection_complete"])
        self.assertEqual(result["coverage"]["call_chain"]["covered_calls"], 1)
        references = {ref["evidence_id"] for ref in result["evidence_refs"]}
        self.assertTrue(set(link["caller_evidence_ids"] + link["target_evidence_ids"]) <= references)

    def test_deep_call_chain_and_shared_state_definitions_are_reading_targets(self):
        for index in range(6):
            name = "MAIN-ENTRY" if index == 0 else f"WORKER-{index}"
            call = f'CALL "WORKER-{index + 1}" USING SHARED-STATE.\n' if index < 5 else 'MOVE "APPROVED" TO REQUEST-STATE.\n'
            text = f'IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n'
            text += 'WORKING-STORAGE SECTION.\nCOPY "shared-state.cpy".\n'
            text += 'PROCEDURE DIVISION.\n' + call + '*> ordinary filler\n' * 200
            self.write(f"worker-{index}.cbl", text)
        self.write("shared-state.cpy", '01 SHARED-STATE.\n  05 REQUEST-STATE PIC X(12).\n')
        self.build()
        result = self.prepare(question="ordinary filler", max_pages=16, page_chars=500)
        paths = {page["relative_path"] for page in result["pages"]}
        self.assertTrue({f"worker-{index}.cbl" for index in range(6)} <= paths)
        self.assertIn("shared-state.cpy", paths)
        calls = [link for link in result["call_chain"]["links"] if link["relation_type"] == "CALLS"]
        self.assertEqual(len(calls), 5)
        self.assertEqual(max(link["depth"] for link in calls), 4)
        self.assertTrue(all(link["selection_complete"] for link in calls))
        copies = [link for link in result["call_chain"]["links"] if link["relation_type"] == "INCLUDES_COPY"]
        self.assertTrue(all(link["resolution"] == "literal_copy_candidate" for link in copies))
        self.assertTrue(any(link["target_selected"] for link in copies))
        self.assertFalse(result["coverage"]["complete"])

    def test_separated_linkage_and_multiline_using_are_not_confused_with_program_header(self):
        self.write("entry.cbl", program(body='*> amount rule\n' * 60 +
                   'CALL "RULE-WORKER"\n USING REQUEST-AMOUNT\n REQUEST-STATE.\n' + '*> amount rule\n' * 60))
        text = 'IDENTIFICATION DIVISION.\nPROGRAM-ID. RULE-WORKER.\nDATA DIVISION.\nWORKING-STORAGE SECTION.\n'
        text += '*> worker definitions\n' * 60
        text += 'LINKAGE SECTION.\n01 REQUEST-AMOUNT PIC 9(5).\n01 REQUEST-STATE PIC X(12).\n'
        text += 'PROCEDURE DIVISION USING\n REQUEST-AMOUNT\n REQUEST-STATE.\n'
        text += 'MOVE "APPROVED" TO REQUEST-STATE.\nGOBACK.\n'
        self.write("worker.cbl", text)
        self.build()
        result = self.prepare(question="amount rule", max_pages=8, page_chars=220)
        text = "\n".join(page["source_text"] for page in result["pages"])
        self.assertIn('CALL "RULE-WORKER"', text)
        self.assertIn('PROCEDURE DIVISION USING', text)
        self.assertIn('01 REQUEST-STATE PIC X(12)', text)
        self.assertTrue(result["call_chain"]["links"][0]["selection_complete"])

    def test_small_budget_reports_dependency_gap_instead_of_claiming_chain_is_read(self):
        self.write("entry.cbl", program(body='*> ordinary filler\n' * 60 + 'CALL "RULE-WORKER".\n'))
        self.write("worker.cbl", program("RULE-WORKER"))
        self.build()
        result = self.prepare(max_pages=1, page_chars=160)
        self.assertEqual(len(result["pages"]), 1)
        self.assertEqual(result["coverage"]["call_chain"]["covered_calls"], 0)
        self.assertEqual(result["coverage"]["call_chain"]["uncovered_calls"], 1)
        self.assertFalse(result["call_chain"]["links"][0]["selection_complete"])
        self.assertIn("dependency_context_incomplete", {item["reason"] for item in result["boundaries"]})

    def test_unrelated_changed_or_missing_source_is_excluded_without_stale_structure(self):
        self.write("entry.cbl", program())
        changed = self.write("changed.cbl", program("OLD-WORKER", 'STALE-RULE.\nDISPLAY "OLD".\n'))
        missing = self.write("missing.cbl", program("MISSING-WORKER"))
        self.build()
        before = self.metadata()
        changed.write_text(program("NEW-WORKER"), encoding="utf-8")
        missing.unlink()
        result = self.prepare()
        self.assertEqual({page["relative_path"] for page in result["pages"]}, {"entry.cbl"})
        self.assertEqual(result["coverage"]["total_files"], 3)
        self.assertEqual(result["coverage"]["verified_files"], 1)
        self.assertEqual(result["coverage"]["excluded_files"], 2)
        self.assertEqual(result["coverage"]["total_pages_scope"], "verified_sources")
        self.assertFalse(result["coverage"]["page_count_complete"])
        self.assertFalse(result["coverage"]["complete"])
        self.assertNotIn("STALE-RULE", repr(result["outline"]))
        self.assertNotIn("OLD-WORKER", repr(result["outline"]))
        self.assertEqual(before, self.metadata())
        spans = InvestigationTools(self.database).read_evidence([ref["evidence_id"] for ref in result["evidence_refs"]])["spans"]
        self.assertTrue(all(span["integrity"] == "VALID" for span in spans))

    def test_unverified_called_source_retains_only_the_verified_callsite(self):
        self.write("entry.cbl", program(body='CALL "RULE-WORKER".\nGOBACK.\n'))
        target = self.write("worker.cbl", program("RULE-WORKER", 'STALE-RULE.\nDISPLAY "OLD".\n'))
        self.build()
        target.write_text(program("OTHER-WORKER"), encoding="utf-8")
        result = self.prepare()
        link = result["call_chain"]["links"][0]
        self.assertTrue(link["caller_selected"])
        self.assertFalse(link["target_selected"])
        self.assertEqual(link["target_source_status"], "excluded")
        self.assertEqual(link["target_evidence_ids"], [])
        self.assertNotIn("STALE-RULE", repr(result))
        call = next(item for item in result["outline"] if item["relative_path"] == "entry.cbl")["calls"][0]
        self.assertIsNone(call["target_line"])
        self.assertEqual(call["status"], "target_unavailable")

    def test_ambiguous_calls_and_copy_names_are_not_reported_as_resolved(self):
        self.write("entry.cbl", 'IDENTIFICATION DIVISION.\nPROGRAM-ID. MAIN-ENTRY.\n'
                   'DATA DIVISION.\nCOPY "shared-state.cpy".\nPROCEDURE DIVISION.\nCALL "RULE-WORKER".\n')
        for directory in ("first", "second"):
            self.write(f"{directory}/worker.cbl", program("RULE-WORKER"))
            self.write(f"{directory}/shared-state.cpy", '01 SHARED-STATE PIC X.\n')
        self.build()
        result = self.prepare()
        self.assertTrue(all(link["target_path"] is None for link in result["call_chain"]["links"]))
        self.assertTrue(all(not link["selection_complete"] for link in result["call_chain"]["links"]))
        self.assertEqual(result["coverage"]["call_chain"]["unresolved_calls"], 1)

    def test_dynamic_call_field_is_not_mistaken_for_a_resolved_program(self):
        self.write("entry.cbl", 'IDENTIFICATION DIVISION.\nPROGRAM-ID. MAIN-ENTRY.\n'
                   'DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 NEXT-WORKER PIC X(20).\n'
                   'PROCEDURE DIVISION.\nCALL NEXT-WORKER.\nGOBACK.\n')
        self.build()
        result = self.prepare()
        link = result["call_chain"]["links"][0]
        self.assertEqual(link["relation_type"], "CALL_TARGET_FROM")
        self.assertEqual(link["resolution"], "unresolved")
        self.assertIsNone(link["target_path"])
        self.assertEqual(result["coverage"]["call_chain"]["resolved_calls"], 0)
        self.assertEqual(result["coverage"]["call_chain"]["unresolved_calls"], 1)
        self.assertIsNone(result["outline"][0]["calls"][0]["target_path"])

    def test_duplicate_entry_names_require_path_and_do_not_resolve_ambiguous_call(self):
        self.write("first/entry.cbl", program("SHARED-ENTRY", 'CALL "SHARED-ENTRY".\nDISPLAY "FIRST".\n' + '*> first\n' * 70))
        self.write("second/entry.cbl", program("SHARED-ENTRY", 'DISPLAY "SECOND".\n' + '*> second\n' * 70))
        self.build()
        with self.assertRaisesRegex(ValueError, "ENTRY_AMBIGUOUS"):
            self.prepare(entry_program="SHARED-ENTRY")
        result = self.prepare(entry_program="second/entry.cbl", max_pages=2, page_chars=120)
        self.assertEqual(result["entry"]["relative_path"], "second/entry.cbl")
        self.assertEqual({page["relative_path"] for page in result["pages"]}, {"second/entry.cbl"})
        self.assertEqual(result["coverage"]["total_files"], 2)
        keyed = self.prepare(entry_program=result["entry"]["entry_key"], max_pages=2, page_chars=120)
        self.assertEqual(keyed["evidence_refs"], result["evidence_refs"])

    def test_changed_source_is_rejected_before_page_evidence_is_inserted(self):
        path = self.write("entry.cbl", program())
        self.build()
        path.write_text(program(body='DISPLAY "CHANGED".\n'))
        with self.assertRaisesRegex(ValueError, "SOURCE_HASH_MISMATCH"):
            self.prepare()
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM evidence_spans WHERE evidence_id LIKE 'ev_page_%'").fetchone()[0], 0)

    def test_injected_paths_and_symlinked_ancestor_are_rejected(self):
        self.write("folder/entry.cbl", program())
        self.build()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE source_files SET relative_path='../outside.cbl'")
            connection.commit()
        with self.assertRaisesRegex(ValueError, "SOURCE_PATH_INVALID"):
            self.prepare()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE source_files SET relative_path='folder/entry.cbl' WHERE relative_path='../outside.cbl'")
            connection.commit()
        outside = self.root / "outside"
        (self.source / "folder").rename(outside)
        (self.source / "folder").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "SOURCE_PATH_INVALID"):
            self.prepare()

    def test_existing_utf_and_cp950_encodings_are_respected(self):
        for encoding in ("utf-8-sig", "utf-16", "cp950"):
            with self.subTest(encoding=encoding):
                text = program(body='*> 結算金額\nDISPLAY "READY".\n')
                self.write("entry.cbl", text, encoding)
                self.build(encoding=encoding)
                result = self.prepare()
                self.assertIn("結算金額", result["pages"][0]["source_text"])
                self.assertTrue(result["coverage"]["complete"])

    def test_long_line_is_explicitly_incomplete_and_never_persisted_as_whole(self):
        self.write("entry.cbl", program(body='DISPLAY "' + 'X' * 300 + '".\nGOBACK.\n'))
        self.build()
        result = self.prepare(page_chars=80)
        page = next(page for page in result["pages"] if page["span_truncated"])
        self.assertEqual(len(page["source_text"]), 80)
        self.assertFalse(result["coverage"]["complete"])
        self.assertNotIn(page["evidence_id"], {ref["evidence_id"] for ref in result["evidence_refs"]})
        evidence = InvestigationTools(self.database).read_evidence([page["evidence_id"]])
        self.assertEqual(evidence["missing_evidence_ids"], [page["evidence_id"]])

    def test_cancellation_and_wrong_root_leave_snapshot_unchanged(self):
        self.write("entry.cbl", program())
        self.build()
        before = self.metadata()
        def cancel():
            raise RuntimeError("cancelled")
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.prepare(check_cancel=cancel)
        with self.assertRaisesRegex(ValueError, "SOURCE_ROOT_MISMATCH"):
            prepare_source_reading(self.database, self.root, entry_program="MAIN-ENTRY", question="Read it")
        self.assertEqual(self.metadata(), before)

    def test_reading_works_without_posix_directory_descriptor_features(self):
        self.write("entry.cbl", program())
        self.build()
        with mock.patch("source_reading.os.open", side_effect=AssertionError("POSIX descriptor opens unavailable")), \
             mock.patch("source_reading.os.O_DIRECTORY", None, create=True), \
             mock.patch("source_reading.os.O_NOFOLLOW", None, create=True):
            result = self.prepare()
        self.assertTrue(result["coverage"]["complete"])

    def test_full_chain_exceeds_old_page_limit_and_rotates_all_programs(self):
        texts = {}
        for index in range(9):
            name = "MAIN-ENTRY" if index == 0 else f"WORKER-{index}"
            call = f'CALL "WORKER-{index + 1}".\n' if index < 8 else 'COMPUTE CHARGE = AMOUNT * 0.03.\n'
            text = program(name, call + ('*> regular business context\n' * (500 if index == 0 else 35)) + 'GOBACK.\n')
            relative = f"program-{index}.cbl"
            texts[relative] = text
            self.write(relative, text)
        self.build()
        result = self.prepare(reading_strategy="full_chain", max_pages=2, page_chars=100)
        self.assertGreater(len(result["pages"]), 128)
        self.assertTrue(result["coverage"]["complete"])
        self.assertFalse(result["coverage"]["model_reading_completed"])
        self.assertEqual(result["coverage"]["omitted_pages"], 0)
        self.assertEqual(len({page["relative_path"] for page in result["pages"][:9]}), 9)
        self.assertTrue(all("source_text" not in page for page in result["pages"]))
        self.assertEqual(result["coverage"]["call_chain"]["covered_calls"], 8)
        loaded = read_source_page_batch(self.database, result["pages"][-4:])
        for page in loaded:
            self.assertEqual(page["source_text"], "\n".join(texts[page["relative_path"]].splitlines()[page["start_line"] - 1:page["end_line"]]))
            self.assertEqual(InvestigationTools(self.database).read_evidence([page["evidence_id"]])["spans"][0]["integrity"], "VALID")

    def test_streaming_reader_verifies_more_than_sixteen_megabytes_with_bounded_memory(self):
        path = self.source / "large.cbl"
        line = ("*> " + "business context " * 5 + "\n").encode()
        count = 200_000
        digest = hashlib.sha256()
        with path.open("wb") as handle:
            for _ in range(count // 1000):
                chunk = line * 1000
                handle.write(chunk)
                digest.update(chunk)
        self.assertGreater(path.stat().st_size, 16 * 1024 * 1024)
        item = {"relative_path": "large.cbl", "sha256": digest.hexdigest(), "encoding": "utf-8", "line_count": count}
        tracemalloc.start()
        try:
            pages, lines = 0, 0
            for page in _verified_pages(self.source.resolve(), item, None, set(), 12000):
                pages += 1
                lines += page["end_line"] - page["start_line"] + 1
                self.assertLessEqual(len(page["source_text"]), 12000)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertGreater(pages, 128)
        self.assertEqual(lines, count)
        self.assertLess(peak, 4 * 1024 * 1024)

    def test_full_chain_late_hash_failure_rolls_back_staged_evidence(self):
        path = self.write("entry.cbl", program(body="*> original\n" * 1000))
        self.build()
        path.write_text(program(body="*> modified\n" * 1000))
        with self.assertRaisesRegex(ValueError, "SOURCE_HASH_MISMATCH"):
            self.prepare(reading_strategy="full_chain", max_pages=1, page_chars=100)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM evidence_spans WHERE evidence_id LIKE 'ev_page_%'").fetchone()[0], 0)

    def test_persisted_page_mutation_is_rejected_before_delivery(self):
        self.write("entry.cbl", program())
        self.build()
        prepared = self.prepare(reading_strategy="full_chain")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE evidence_spans SET text='changed body' WHERE evidence_id=?", (prepared["pages"][0]["evidence_id"],))
            connection.commit()
        with self.assertRaisesRegex(ValueError, "EVIDENCE_ID_CONFLICT"):
            read_source_page_batch(self.database, prepared["pages"])


if __name__ == "__main__":
    unittest.main()
