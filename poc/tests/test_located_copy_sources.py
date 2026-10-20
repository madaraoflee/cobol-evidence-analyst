"""Physical COPY supply remains separate from replacement/compile semantics."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from business_map import build_business_map
from company_api import APIClientError, CompanyAPIConfig
from complete_working_set import build_complete_working_set
from question_investigation import build_question_investigation, _located_copy_sources
from repository_discovery import ensure_repository_search
from source_session import QuestionSourceSession


class LocatedCopySourceTests(unittest.TestCase):
    question = "请详细解释 entry.cbl 的 FINAL-AMOUNT 公式、条件和输入来源。"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"

    def build(self, *, member='"active/rate-template.cpy"', duplicate=False, long=False):
        files = {"entry.cbl": ("IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRY-RULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 INPUT-AMOUNT PIC 9(5) VALUE 10.\n"
            "01 FINAL-AMOUNT PIC 9(7)V99.\n"
            f"COPY {member}\n REPLACING ==RATE-TOKEN== BY ==RATE-VALUE==\n ==FACTOR== BY ==3==.\n"
            "PROCEDURE DIVISION.\nMAIN.\nCOMPUTE FINAL-AMOUNT ROUNDED = INPUT-AMOUNT * RATE-VALUE.\n"
            + "*> ordinary caller padding\n" * 12 + "GOBACK.\n"),
            "active/rate-template.cpy": "01 RATE-TOKEN PIC 9 VALUE FACTOR.\n" +
                ("*> ordinary template padding\n" * 199 if long else "")}
        if duplicate:
            files["other/rate-template.cpy"] = "01 RATE-TOKEN PIC 9 VALUE 9.\n"
        for name, content in files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", quiet=True)
        ensure_repository_search(self.database, self.source)
        self.mapping = build_business_map(self.database, self.source, self.question)

    def page(self, path, first=1, last=None):
        raw = (self.source / path).read_bytes()
        lines = raw.decode().splitlines()
        last = len(lines) if last is None else last
        return {"relative_path": path, "source_sha256": hashlib.sha256(raw).hexdigest(),
                "start_line": first, "end_line": last, "source_text": "\n".join(lines[first - 1:last]),
                "evidence_id": f"ev-{path}-{first}-{last}"}

    def investigate(self, pages, **kwargs):
        return build_question_investigation(self.question, self.mapping,
            database_path=self.database, source_pages=pages, **kwargs)

    def test_named_replacement_template_gets_read_without_semantic_promotion(self):
        self.build()
        result = self.investigate([self.page("entry.cbl")])
        action, = [action for action in result["planned_actions"]
                   if action["reason"] == "located_copy_source_not_supplied"]
        self.assertEqual(action["arguments"], {"relative_path": "active/rate-template.cpy", "start_line": 1, "end_line": 1})
        self.assertFalse(result["can_answer"])
        self.assertNotIn("active/rate-template.cpy", result["candidate_paths"])
        after = self.investigate([self.page("entry.cbl"), self.page("active/rate-template.cpy")])
        target, = after["copy_source_candidates"]
        self.assertTrue(target["physical_complete"])
        self.assertEqual(target["binding_status"], "unbound_copy_candidate")
        self.assertFalse(target["semantic_execution_verified"])
        self.assertFalse(any(action["reason"] == "located_copy_source_not_supplied" for action in after["planned_actions"]))
        self.assertIn("copy_binding_unresolved", {gap["reason"] for gap in after["open_gaps"]})
        with closing(sqlite3.connect(self.database)) as db:
            status, bound = db.execute("SELECT status,target_entity_id FROM relations WHERE relation_type='INCLUDES_COPY'").fetchone()
        self.assertEqual((status, bound), ("candidate", None))

    def test_ambiguous_basename_is_not_selected_but_exact_path_is(self):
        self.build(member='"rate-template.cpy"', duplicate=True)
        self.assertNotIn("copy_source_candidates", self.investigate([self.page("entry.cbl")]))
        self.build(duplicate=True)
        self.assertEqual([row["relative_path"] for row in self.investigate([self.page("entry.cbl")])["copy_source_candidates"]],
                         ["active/rate-template.cpy"])

    def test_caller_must_be_actual_visible_same_version_copy(self):
        self.build()
        whole = self.page("entry.cbl")
        invalid = [{**whole, "source_sha256": "0" * 64}, {**whole, "span_truncated": True},
                   {**whole, "include_chain": [{"relative_path": "elsewhere.cbl"}]},
                   {**whole, "source_text": whole["source_text"].replace("==3==", "==4==")},
                   self.page("entry.cbl", 10)]
        for page in invalid:
            with self.subTest(page=page):
                self.assertEqual(_located_copy_sources(self.database, self.mapping, [page], ["entry.cbl"], []), [])

    def test_stale_or_truncated_template_does_not_satisfy_read(self):
        self.build()
        for changes in ({"source_sha256": "0" * 64}, {"span_truncated": True}, {"source_text": ""}):
            with self.subTest(changes=changes):
                result = self.investigate([self.page("entry.cbl"), {**self.page("active/rate-template.cpy"), **changes}])
                self.assertFalse(result["copy_source_candidates"][0]["source_supplied"])
                self.assertTrue(any(action["reason"] == "located_copy_source_not_supplied" for action in result["planned_actions"]))

    def test_long_copy_uses_bounded_preview_and_never_reports_whole_source(self):
        self.build(long=True)
        result = self.investigate([self.page("entry.cbl")])
        target, = result["copy_source_candidates"]
        self.assertEqual((target["start_line"], target["end_line"], target["file_total_lines"]), (1, 96, 200))
        after = self.investigate([self.page("entry.cbl"), self.page("active/rate-template.cpy", 1, 96)])
        self.assertTrue(after["copy_source_candidates"][0]["source_supplied"])
        self.assertFalse(after["copy_source_candidates"][0]["physical_complete"])

    def test_existing_read_allowance_and_scope_exclusion_are_preserved(self):
        self.build()
        pages = [self.page("entry.cbl")]
        self.assertEqual(self.investigate(pages, max_actions=0)["planned_actions"], [])
        done = [{"read": {"relative_path": "active/rate-template.cpy", "start_line": 1, "end_line": 1}}]
        result = self.investigate(pages, completed_actions=done)
        self.assertFalse(any(action["reason"] == "located_copy_source_not_supplied" for action in result["planned_actions"]))
        scope = [{"relative_path": "entry.cbl", "start_line": 10, "end_line": 15}]
        self.assertEqual(_located_copy_sources(self.database, self.mapping, pages, ["entry.cbl"], scope), [])

    def test_metadata_is_cached_while_actual_supply_is_rechecked(self):
        self.build()
        cache = {}
        first = _located_copy_sources(self.database, self.mapping, [self.page("entry.cbl")], ["entry.cbl"], [], cache)
        self.assertFalse(first[0]["source_supplied"])
        with patch("question_investigation.sqlite3.connect", side_effect=AssertionError("repeated metadata query")):
            after = _located_copy_sources(self.database, self.mapping,
                [self.page("entry.cbl"), self.page("active/rate-template.cpy")], ["entry.cbl"], [], cache)
        self.assertTrue(after[0]["source_supplied"])

    def test_working_set_boundary_distinguishes_located_source_from_binding(self):
        self.build()
        with QuestionSourceSession(self.database, self.source) as session:
            result = build_complete_working_set(self.database, session, self.mapping, AgentPolicy())
        frontier, = [row for row in result["metadata"]["frontier"] if row.get("relation_type") == "INCLUDES_COPY"]
        self.assertEqual(frontier["reason"], "copy_binding_unresolved")
        self.assertTrue(frontier["source_available"])
        self.assertEqual(frontier["target_path"], "active/rate-template.cpy")
        self.assertFalse(result["metadata"]["closure_complete"])

    def test_working_set_keeps_distinct_copy_occurrence_targets(self):
        self.build(member='"rate-template.cpy"', duplicate=True)
        path = self.source / "entry.cbl"
        text = path.read_text()
        statement = 'COPY "rate-template.cpy"\n REPLACING ==RATE-TOKEN== BY ==RATE-VALUE==\n ==FACTOR== BY ==3==.'
        text = text.replace(statement, statement.replace('"rate-template.cpy"', '"rate-template.cpy" IN FIRST')
                            + "\n" + statement.replace('"rate-template.cpy"', '"rate-template.cpy" IN SECOND'))
        path.write_text(text)
        build_business_index(self.source, self.database, source_format="free", quiet=True, verify_content=True)
        ensure_repository_search(self.database, self.source)
        mapping = build_business_map(self.database, self.source, self.question)
        edges = [edge for edge in mapping["relations"] if edge["relation_type"] == "INCLUDES_COPY"]
        self.assertEqual(len(edges), 2)
        # Exercise the boundary consumer with occurrence-specific navigation;
        # this does not claim the default compilation profile resolves libraries.
        for edge, target in zip(edges, ("active/rate-template.cpy", "other/rate-template.cpy")):
            edge.update(resolution="unbound_copy_candidate", target_path=target)
        with QuestionSourceSession(self.database, self.source) as session:
            result = build_complete_working_set(self.database, session, mapping, AgentPolicy())
        targets = [row["target_path"] for row in result["metadata"]["frontier"]
                   if row["reason"] == "copy_binding_unresolved"]
        self.assertCountEqual(targets, ["active/rate-template.cpy", "other/rate-template.cpy"])

    def test_first_actual_request_supplies_template_with_existing_allowance(self):
        self.build()
        requests = []
        def transport(request):
            requests.append(json.loads(json.loads(request.body)["messages"][-1]["content"]))
            raise APIClientError("OFFLINE_CAPTURE_ONLY")
        policy = AgentPolicy(initial_pages=1, max_complete_source_characters=512, max_evidence_groups=0)
        with patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")):
            result = run_business_chat(self.question, self.database, self.source,
                CompanyAPIConfig("https://offline.example.invalid/v1", "neutral", api_key="offline-placeholder"),
                transport=transport, allow_network=False, framework_reference_path="", policy=policy)
        self.assertEqual(len(requests), 1)
        pages = [page for bundle in requests[0]["source_context"] for page in bundle["pages"]]
        target, = [page for page in pages if page["relative_path"] == "active/rate-template.cpy"]
        self.assertEqual(target["source_text"], self.page("active/rate-template.cpy")["source_text"])
        self.assertEqual(target["source_sha256"], self.page("active/rate-template.cpy")["source_sha256"])
        self.assertEqual((target["start_line"], target["end_line"]), (1, 1))
        self.assertLessEqual(sum(len(page["source_text"]) for page in pages), policy.max_source_characters)
        self.assertLessEqual(result["agent_result"]["metrics"]["tool_calls"]["read"], policy.max_reads_per_turn)


if __name__ == "__main__":
    unittest.main()
