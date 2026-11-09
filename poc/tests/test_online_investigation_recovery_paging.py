from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_online_investigation as fixtures
import test_online_material_query as query_fixtures
from online_analysis import parser, query as cli_query
from online_investigation import _action, _execute, run_investigation
from online_material_format import MaterialError
from online_material_query import MaterialQuery


class OnlineInvestigationRecoveryPagingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.OnlineInvestigationTests(
            "test_default_requires_explicit_source_transfer_authorization")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def _many_incoming(self, count):
        source = self.fixture.root / "many-source"
        source.mkdir()
        content = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. MANY-CALLS.\n"
                   "PROCEDURE DIVISION.\nMAIN-PARA.\n"
                   + "CALL 'WORKER-ENTRY'.\n" * count + "GOBACK.\n").encode()
        (source / "main.cbl").write_bytes(content)
        database = self.fixture.root / "many.sqlite"
        report = fixtures.build_business_index(source, database, source_format="free",
            verify_content=True, framework_reference_path="")
        material = self.fixture.root / "many-material"
        fixtures.export_material(database, source, material, repository_id="example/source",
            commit="b" * 40, build_report=report)
        self.fixture.query = MaterialQuery(fixtures.MaterialReader(fixtures.DirectoryObjectStore(material)))
        self.fixture.source_bytes = {"main.cbl": content}
        return self.fixture.query

    def test_incoming_continues_after_first_hundred_matches(self):
        self._many_incoming(205)
        def planner(context):
            if context["source_context"]:
                return fixtures.answer(context)
            pages = [item["result"] for item in context["observations"]
                     if item["action"].get("tool") == "incoming"]
            if pages and not pages[-1]["truncated"]:
                return {"actions": [fixtures.read(end=10)]}
            return {"actions": [{"tool": "incoming", "target": "WORKER-ENTRY", "limit": 100,
                                  "offset": pages[-1]["next_offset"] if pages else 0}]}
        result = self.fixture.run_question([planner] * 12, max_model_calls=12)
        pages = [item["result"] for item in result["checkpoint"]["observations"]
                 if item["action"].get("tool") == "incoming"]
        self.assertTrue(all(0 < len(page["matches"]) <= 100 for page in pages))
        self.assertEqual(pages[0]["offset"], 0)
        self.assertEqual(pages[-1]["next_offset"], None)
        for previous, current in zip(pages, pages[1:]):
            self.assertEqual(current["offset"], previous["next_offset"])
        self.assertEqual(len({repr(item) for page in pages for item in page["matches"]}), 205)
        self.assertEqual(result["status"], "answered")

    def test_incoming_can_reach_matches_after_one_thousand(self):
        query = self._many_incoming(1005)
        tail = query.incoming("WORKER-ENTRY", limit=20, offset=1000)
        self.assertEqual(len(tail["matches"]), 5)
        self.assertEqual(tail["total_matches"], 1005)
        self.assertEqual(tail["offset"], 1000)
        self.assertFalse(tail["truncated"])
        self.assertEqual(_action({"tool": "incoming", "target": "WORKER-ENTRY",
                                  "limit": 20, "offset": 1000})["offset"], 1000)
        empty = query.incoming("WORKER-ENTRY", limit=20, offset=2000)
        self.assertEqual(empty["matches"], [])
        self.assertFalse(empty["truncated"])

    def test_incoming_cli_exposes_offset_with_backward_compatible_default(self):
        self._many_incoming(1005)
        arguments = ["incoming", "--material", str(self.fixture.root / "many-material"), "WORKER-ENTRY"]
        self.assertEqual(parser().parse_args(arguments).offset, 0)
        result = cli_query(parser().parse_args(arguments + ["--offset", "1000", "--limit", "20"]))
        self.assertEqual(result["offset"], 1000)
        self.assertEqual(len(result["matches"]), 5)
        self.assertFalse(result["truncated"])

    def test_search_tail_and_cli_remain_reachable_without_loading_earlier_metadata(self):
        reader = query_fixtures.FakeReader()
        paths = [f"caller-{number:04}.cbl" for number in range(1005)]
        for path in paths:
            reader.add(path)
        reader.routes[("terms", "shared")] = paths
        query = MaterialQuery(reader)
        action = _action({"tool": "search", "text": "shared", "limit": 20, "offset": 1000})
        result = _execute(query, None, action, {}, set())
        self.assertEqual([item["relative_path"] for item in result["matches"]], paths[1000:])
        self.assertEqual(reader.reads, [("terms", "shared")] + [("file", path) for path in paths[1000:]])
        self.assertEqual(result["offset"], 1000)
        self.assertEqual(result["total_matches"], 1005)
        self.assertFalse(result["truncated"])
        arguments = ["search", "--material", str(self.fixture.material), "shared"]
        self.assertEqual(parser().parse_args(arguments).offset, 0)
        with mock.patch("online_material_query.MaterialQuery", return_value=query):
            cli = cli_query(parser().parse_args(arguments + ["--offset", "1000", "--limit", "20"]))
        self.assertEqual([item["relative_path"] for item in cli["matches"]], paths[1000:])
        self.assertEqual(cli["offset"], 1000)

    def test_relation_tail_remains_reachable_after_one_thousand(self):
        query = self._many_incoming(1005)
        action = _action({"tool": "relations", "path": "main.cbl", "limit": 20, "offset": 1000})
        result = _execute(query, None, action, {}, set())
        self.assertEqual(len(result["relations"]), 5)
        self.assertEqual(result["offset"], 1000)
        self.assertEqual(result["total_matches"], 1005)
        self.assertFalse(result["truncated"])

    def test_deep_search_page_keeps_all_query_and_page_budgets(self):
        reader = query_fixtures.FakeReader()
        reader.routes[("terms", "shared")] = ["first.cbl", "last.cbl"]
        query = MaterialQuery(reader)
        for offset in (True, -1, 1.5, "1", 2 ** 63):
            with self.subTest(offset=offset):
                with self.assertRaises(MaterialError):
                    query.search("shared", offset=offset)
        self.assertFalse(reader.reads)
        with mock.patch("online_material_query._MAX_CANDIDATE_PATHS", 1):
            with self.assertRaisesRegex(MaterialError, "MATERIAL_QUERY_BUDGET_EXCEEDED"):
                query.search("shared", offset=1000)
        with self.assertRaisesRegex(MaterialError, "MATERIAL_INVALID_LIMIT"):
            query.search("shared", limit=1001, offset=1000)
        with self.assertRaises(MaterialError):
            _action({"tool": "search", "text": "shared", "limit": 101, "offset": 1000})

    def test_incoming_rejects_invalid_offsets_without_losing_old_checkpoint_actions(self):
        query = self.fixture.query
        for offset in (True, -1, 1.5, "1", 2 ** 63):
            with self.subTest(offset=offset):
                with self.assertRaises(MaterialError):
                    query.incoming("WORKER-ENTRY", offset=offset)
                with self.assertRaises(MaterialError):
                    _action({"tool": "incoming", "target": "WORKER-ENTRY", "offset": offset})
        first = self.fixture.run_question([{"actions": [fixtures.read(),
            {"tool": "incoming", "target": "WORKER-ENTRY"}]}], max_actions=1)
        checkpoint = deepcopy(first["checkpoint"])
        checkpoint["pending_actions"][0].pop("offset", None)
        resumed = self.fixture.run_question([fixtures.answer], checkpoint=checkpoint)
        self.assertEqual(resumed["status"], "answered")
        incoming = [item for item in resumed["checkpoint"]["observations"]
                    if item["action"].get("tool") == "incoming"]
        self.assertEqual(incoming[-1]["action"]["offset"], 0)

    def _answer_ready_checkpoint(self):
        saved = []
        def journal(event):
            if event["phase"] == "answer_ready":
                saved.append(deepcopy(event["checkpoint"]))
                raise OSError("interrupted after checkpoint write")
        failed = self.fixture.run_question([{"actions": [fixtures.read()]}, fixtures.answer], progress=journal)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(saved[0]["status"], "answered")
        return saved[0]

    def test_answer_ready_checkpoint_revalidates_without_repeating_model_request(self):
        checkpoint = self._answer_ready_checkpoint()
        result = run_investigation(self.fixture.query, self.fixture.source, checkpoint["question"],
            checkpoint=checkpoint, allow_source_transfer=True)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["source_requests"], 1)
        self.assertEqual(result["model_calls"], 0)

    def test_answer_ready_checkpoint_cannot_bypass_source_revocation_or_content_change(self):
        checkpoint = self._answer_ready_checkpoint()
        attempted = []
        def revoked(path, **options):
            attempted.append(path)
            raise MaterialError("SOURCE_FORBIDDEN")
        denied = self.fixture.run_question([], checkpoint=checkpoint, source_reader=revoked)
        self.assertEqual(denied["status"], "failed")
        self.assertEqual(denied["error"], "SOURCE_FORBIDDEN")
        self.assertEqual(attempted, ["main.cbl"])
        altered = self.fixture.run_question([], checkpoint=checkpoint,
            source_reader=lambda path, **kw: self.fixture.source_bytes[path].replace(b" > 0", b" > 9"))
        self.assertEqual(altered["status"], "failed")
        self.assertEqual(altered["error"], "MATERIAL_SOURCE_HASH_MISMATCH")
        self.assertEqual(denied["model_calls"], altered["model_calls"])
        self.assertEqual(denied["model_calls"], 0)

    def test_answer_ready_checkpoint_obeys_cancellation_before_source_read(self):
        checkpoint = self._answer_ready_checkpoint()
        def cancelled():
            raise MaterialError("ONLINE_CANCELLED")
        result = self.fixture.run_question([], checkpoint=checkpoint, check_cancel=cancelled)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["source_requests"], 0)
        self.assertEqual(result["model_calls"], 0)


if __name__ == "__main__":
    unittest.main()
