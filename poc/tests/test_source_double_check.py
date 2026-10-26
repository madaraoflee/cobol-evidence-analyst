"""Source range recovery must retain exact physical text and a useful cursor."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
from agent_policy import AgentPolicy
import repository_discovery
from repository_discovery import ensure_repository_search, read_repository_context
from source_session import QuestionSourceSession
import test_business_chat as chat_fixture
import test_read_long_line_budget as long_line_fixture


class SourceDoubleCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.path = self.source / "entry.cbl"
        self.database = self.root / "index.sqlite"
        guard = patch("socket.socket.connect", side_effect=AssertionError("Real network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    def build(self, body):
        self.lines = ["IDENTIFICATION DIVISION.", "PROGRAM-ID. MAIN-RULE.",
                      "PROCEDURE DIVISION.", *body, "GOBACK."]
        self.path.write_bytes(("\n".join(self.lines) + "\n").encode("utf-8"))
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)

    def remove_excerpt(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            row = db.execute("SELECT evidence_id,start_line,end_line FROM repo_pages "
                             "WHERE relative_path='entry.cbl' ORDER BY start_line LIMIT 1").fetchone()
            db.execute("DELETE FROM evidence_spans WHERE evidence_id=?", (row[0],))
        return row

    def assert_exact(self, result):
        for page in result["pages"]:
            self.assertFalse(page.get("span_truncated"))
            self.assertEqual(page["source_sha256"], self.digest)
            self.assertEqual(page["source_text"], "\n".join(self.lines[page["start_line"] - 1:page["end_line"]]))

    def test_missing_excerpt_can_be_recovered_by_its_known_reference(self):
        self.build(["MAIN.", "MOVE 9 TO RESULT-CODE."])
        reference, first, last = self.remove_excerpt()
        result = read_repository_context(self.database, self.source, evidence_id=reference)
        self.assertTrue(result["range_complete"])
        self.assertEqual(result["requested_range"],
                         {"relative_path": "entry.cbl", "start_line": first, "end_line": last})
        self.assertEqual(result["pages"][0]["evidence_id"], reference)
        self.assert_exact(result)

    def test_missing_reference_from_another_source_version_is_not_relabelled(self):
        self.build(["MAIN.", "MOVE 9 TO RESULT-CODE."])
        reference, _, _ = self.remove_excerpt()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE repo_pages SET source_sha256=? WHERE evidence_id=?", ("0" * 64, reference))
        with self.assertRaisesRegex(ValueError, "EVIDENCE_NOT_FOUND"):
            read_repository_context(self.database, self.source, evidence_id=reference)

    def test_complete_indexed_line_reports_exact_size_when_read_allowance_is_smaller(self):
        condition = "IF FLAG = 'Y' " + "OR FLAG = 'Y' " * 70
        self.build([condition, "MOVE 'REVIEW' TO STATE-CODE", "END-IF."])
        result = read_repository_context(self.database, self.source, relative_path="entry.cbl",
                                         start_line=4, end_line=6, max_chars=512)
        self.assertEqual(result["pages"], [])
        self.assertFalse(result["range_complete"])
        self.assertEqual(result["next_start_line"], 4)
        self.assertNotIn("raw_fallback", result)
        self.assertEqual(result["read_obstruction"], {
            "reason": "source_line_exceeds_read_budget", "relative_path": "entry.cbl",
            "source_sha256": self.digest, "start_line": 4, "required_characters": len(condition)})
        complete = read_repository_context(self.database, self.source, relative_path="entry.cbl",
                                           start_line=4, end_line=6, max_chars=4096)
        self.assertTrue(complete["range_complete"])
        self.assertNotIn("read_obstruction", complete)
        self.assert_exact(complete)

    def test_normal_pagination_never_marks_a_short_next_line_as_oversized(self):
        self.build([f"DISPLAY 'ROW-{index:03d}'." for index in range(150)])
        result = read_repository_context(self.database, self.source, relative_path="entry.cbl",
                                         start_line=4, end_line=len(self.lines), max_chars=512)
        self.assertFalse(result["range_complete"])
        self.assertGreater(result["next_start_line"], 4)
        self.assertNotIn("read_obstruction", result)
        self.assert_exact(result)

    def test_middle_cache_gap_and_tail_are_all_read_by_advancing_the_cursor(self):
        self.build(["*> " + str(index).zfill(4) + " " + "x" * 995 for index in range(70)])
        with closing(sqlite3.connect(self.database)) as db, db:
            page = db.execute("SELECT page_id FROM repo_pages ORDER BY start_line LIMIT 1 OFFSET 2").fetchone()
            db.execute("DELETE FROM repo_pages WHERE page_id=?", page)
        cursor, supplied = 1, {}
        with QuestionSourceSession(self.database, self.source) as session:
            for _ in range(20):
                result = read_repository_context(self.database, self.source, relative_path="entry.cbl",
                    start_line=cursor, end_line=len(self.lines), max_chars=12000, source_session=session)
                self.assert_exact(result)
                for page in result["pages"]:
                    for number, text in enumerate(page["source_text"].split("\n"), page["start_line"]):
                        supplied[number] = text
                if result["range_complete"]:
                    self.assertTrue(result["end_of_file"])
                    self.assertIsNone(result["next_start_line"])
                    break
                self.assertFalse(result["end_of_file"])
                self.assertGreater(result["next_start_line"], cursor)
                cursor = result["next_start_line"]
            else:
                self.fail("An exact finite source range must finish without re-reading the same cursor")
        self.assertEqual(supplied, dict(enumerate(self.lines, 1)))

    def test_line_beyond_reader_ceiling_cannot_be_hidden_by_its_supplied_tail(self):
        self.build(["*> " + "x" * 130000, "MOVE 9 TO RESULT-CODE."])
        result = read_repository_context(self.database, self.source, relative_path="entry.cbl",
                                         start_line=4, end_line=len(self.lines), max_chars=128000)
        self.assertFalse(result["range_complete"])
        self.assertFalse(result["end_of_file"])
        self.assertEqual(result["next_start_line"], 4)
        self.assertTrue(result["raw_fallback"]["failures"])
        self.assert_exact(result)

    def test_actual_chat_supplies_indexed_whole_line_with_one_local_expansion(self):
        case = long_line_fixture.LongLineReadBudgetTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        condition = "IF FLAG = 'Y' " + "OR FLAG = 'Y' " * 70
        calls, pages, _, _ = case.exercise_read(
            [condition, "MOVE 'REVIEW' TO REQUEST-STATE", "END-IF."],
            read_characters=512, source_characters=4096)
        self.assertEqual(len(condition), 994)
        self.assertEqual([call["max_chars"] for call in calls], [512, 4096])
        self.assertFalse(calls[0]["range_complete"])
        self.assertTrue(calls[1]["range_complete"])
        self.assertTrue(any(condition in page["source_text"] for page in pages))

    def test_actual_chat_recovers_missing_reference_within_exclusive_scope(self):
        case = chat_fixture.BusinessChatTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        path = case.write("entry.cbl", "ENTRY-WORK",
            "*> ENTRY-TOPIC processing\n" + ("*> unrelated " + "x" * 180 + "\n") * 1200 +
            "MOVE 'REVIEW' TO REQUEST-STATE.", "01 REQUEST-STATE PIC X(12).")
        case.write("other.cbl", "OTHER-WORK", "DISPLAY 'OTHER'.")
        case.build()
        with closing(sqlite3.connect(case.database)) as db:
            reference = db.execute("SELECT evidence_id FROM repo_pages WHERE relative_path='entry.cbl' "
                                   "ORDER BY start_line DESC LIMIT 1").fetchone()[0]
        seen = []
        original = repository_discovery.read_repository_context

        def observed(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get("evidence_id") == reference:
                seen.append(result)
            return result

        def respond(payload, envelope):
            self.assertTrue(payload["business_map"]["source_identity"]["exclusive_source_scope"])
            if len(case.requests) == 1:
                with closing(sqlite3.connect(case.database)) as db, db:
                    db.execute("DELETE FROM evidence_spans WHERE evidence_id=?", (reference,))
                # Citation identity wins over the incidental supplied path.
                return chat_fixture.reply(json.dumps({"read": [{"evidence_id": reference,
                                                                "relative_path": "other.cbl"}]}))
            pages = chat_fixture.source_pages(payload)
            self.assertEqual({page["relative_path"] for page in pages}, {"entry.cbl"})
            page = next(page for page in pages if "MOVE 'REVIEW'" in page["source_text"])
            return chat_fixture.reply(f"入口将请求转入复核步骤。[{page['evidence_id']}]")

        with patch.object(repository_discovery, "read_repository_context", side_effect=observed):
            result = case.ask("仅分析 entry.cbl 的 ENTRY-TOPIC 处理，不读取其他文件。", respond,
                policy=AgentPolicy(max_model_requests=2, max_answer_revisions=0,
                    max_business_context_actions_per_turn=0))["agent_result"]
        self.assertEqual(len(case.requests), 2)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0]["range_complete"])
        for page in seen[0]["pages"]:
            self.assertEqual(page["source_text"], "\n".join(
                path.read_text().splitlines()[page["start_line"] - 1:page["end_line"]]))
        self.assertFalse(any(action.get("outcome") == "unavailable" for action in
                             result["investigation_state"]["completed_actions"] if "read" in action))


if __name__ == "__main__":
    unittest.main()
