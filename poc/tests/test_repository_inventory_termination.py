"""Actual inventory-loop convergence with default, unbounded deployment policy."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]
from agent_policy import AgentPolicy
from business_chat import run_business_chat
from company_api import TransportResponse
from repository_inventory import _cover
from repository_inventory_store import InventoryStore
from run_store import RunStore
from test_repository_formula_inventory import program


class RepositoryInventoryTerminationTests(unittest.TestCase):
    def setUp(self):
        from test_repository_formula_inventory import RepositoryFormulaInventoryTests
        self.fixture = RepositoryFormulaInventoryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        patcher = patch("repository_inventory.PAGED_FILE_THRESHOLD", 1)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fixture.prepare({f"calc-{i:02}.cbl": program(f"ENTRY-{i:02}",
            f"COMPUTE PREMIUM-AMT = BASIS * FACTOR + {i}.") for i in range(12)})
        self.payloads = []
        self.handler = None

    def transport(self, request):
        payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
        self.payloads.append(payload)
        if len(self.payloads) > 20:
            # A failing old loop must not consume requests indefinitely.
            return TransportResponse(500, '{"error":{"message":"Offline test safety stop"}}')
        if payload.get("schema") == "semantic-answer-review/v1":
            evidence = payload["evidence"][0]
            response = {"answer_sha256": payload["answer_sha256"], "evidence_sha256": payload["evidence_sha256"],
                "reviews": [{"unit_id": unit["unit_id"], "verdict": "supported", "reason": "Offline source protocol.",
                    "evidence": [{"reference_id": evidence["reference_id"], "quote": evidence["text"].splitlines()[0]}]}
                    for unit in payload["units"]], "omissions": []}
        else:
            response = self.handler(payload)
        return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant",
            "content": json.dumps(response)}, "finish_reason": "stop"}]}))

    def classified(self, payload):
        pages = [page for bundle in payload["source_context"] for page in bundle["pages"]]
        return {"answer": "Business formula conditions and original references are preserved.",
            "inventory_findings": self.fixture.findings(payload["repository_inventory"]["items"], pages)}

    def run_turn(self, previous=None):
        options = {"continue_investigation": True, "resume_run_id": previous["run_id"]} if previous else {}
        return run_business_chat("列出系统所有计算所有premium formula 的地方", self.fixture.database,
            self.fixture.source, self.fixture.config, transport=self.transport, allow_network=False,
            framework_reference_path="", conversation_id="inventory-convergence", policy=AgentPolicy(), **options)["agent_result"]

    def assert_stopped_with_pending(self, result):
        self.assertEqual(result["status"], "PARTIAL")
        self.assertGreater(result["repository_inventory"]["counts"]["pending"], 0)
        self.assertFalse(result["repository_inventory"]["complete"])
        self.assertTrue(any(row.get("reason") == "no_evidence_progress" and
            row.get("stage", "").startswith("repository_inventory_")
            for row in result.get("boundaries", [])), result.get("boundaries"))
        self.assertFalse(result.get("errors"), result.get("errors"))

    def test_invalid_archive_lookup_stops_without_default_request_or_time_limits(self):
        self.handler = lambda payload: {"inventory_lookup": {"query": ""}}
        result = self.run_turn()
        self.assertEqual(len(self.payloads), 2)
        self.assert_stopped_with_pending(result)
        self.assertEqual(result["repository_inventory"]["counts"]["pending"], 24)

    def test_valid_empty_lookup_accepts_coordinate_priorities_and_stops_repeated_work(self):
        self.handler = lambda payload: {"inventory_lookup": {"query": "PREMIUM", "cursor": 0}}
        result = self.run_turn()
        self.assertEqual(len(self.payloads), 2)
        self.assertEqual(self.payloads[1]["repository_inventory"]["lookup_result"]["rows"], [])
        self.assert_stopped_with_pending(result)

    def test_changing_empty_lookup_queries_does_not_create_source_progress(self):
        self.handler = lambda payload: {"inventory_lookup": {"query": "unmatched-" + str(len(self.payloads))}}
        result = self.run_turn()
        self.assertEqual(len(self.payloads), 2)
        self.assert_stopped_with_pending(result)

    def test_lookup_recalls_original_archived_source_then_all_other_work_can_finish(self):
        recalled = []
        def handler(payload):
            if len(self.payloads) == 1:
                return self.classified(payload)
            if len(self.payloads) == 2:
                return {"inventory_lookup": {"query": "calc-00.cbl", "cursor": 0}}
            lookup = payload["repository_inventory"].get("lookup_result")
            if lookup and not recalled:
                self.assertTrue(lookup["rows"])
                required = {identifier for row in lookup["rows"] for identifier in row["evidence_ids"]}
                pages = {page["evidence_id"]: page for bundle in payload["source_context"] for page in bundle["pages"]}
                self.assertTrue(required <= set(pages), (required, set(pages)))
                for identifier in required:
                    page = pages[identifier]
                    self.assertEqual(page["relative_path"], "calc-00.cbl")
                    self.assertIn("COMPUTE PREMIUM-AMT", page["source_text"])
                recalled.extend(required)
            return self.classified(payload)
        self.handler = handler
        result = self.run_turn()
        self.assertEqual(result["status"], "ANALYZED", result.get("errors"))
        self.assertTrue(recalled)
        self.assertEqual(result["repository_inventory"]["counts"]["included"], 12)
        self.assertEqual(result["repository_inventory"]["counts"]["pending"], 0)

    def test_repeated_nonempty_lookup_stops_and_keeps_previously_classified_findings(self):
        self.handler = lambda payload: (self.classified(payload) if len(self.payloads) == 1 else
            {"inventory_lookup": {"query": "calc-00.cbl", "cursor": 0}})
        result = self.run_turn()
        self.assertLessEqual(len(self.payloads), 4)
        self.assert_stopped_with_pending(result)
        self.assertGreater(result["repository_inventory"]["counts"]["reviewed"], 0)
        checkpoint = RunStore(self.fixture.database.with_name("investigation-runs.sqlite")).load(result["run_id"])["checkpoint"]
        handle = checkpoint["repository_inventory_state"]["store_handle"]
        retained = InventoryStore(self.fixture.database).load(handle)
        self.assertEqual(retained["reviewed_count"], result["repository_inventory"]["counts"]["reviewed"])
        self.assertEqual(len(retained["items"]), 24)

    def test_uncertain_rewording_checks_every_unseen_item_then_stops_without_losing_pending(self):
        supplied_ids = set()
        def handler(payload):
            items = payload["repository_inventory"]["items"]
            pages = [page for bundle in payload["source_context"] for page in bundle["pages"]]
            rows = [{"candidate_id": item["candidate_id"], "disposition": "uncertain",
                "summary": "Unresolved source interpretation " + str(len(self.payloads)),
                "evidence_ids": _cover(item, pages)} for item in items]
            supplied_ids.update(row["candidate_id"] for row in rows if row["evidence_ids"])
            return {"answer": "Known scope and unresolved business interpretations remain saved.", "inventory_findings": rows}
        self.handler = handler
        result = self.run_turn()
        self.assertLess(len(self.payloads), 12)
        self.assertEqual(len(supplied_ids), 24)
        self.assert_stopped_with_pending(result)
        self.assertEqual(result["repository_inventory"]["counts"]["reviewed"], 0)
        self.assertEqual(result["repository_inventory"]["counts"]["pending"], 24)
        checkpoint = RunStore(self.fixture.database.with_name("investigation-runs.sqlite")).load(result["run_id"])["checkpoint"]
        restored = InventoryStore(self.fixture.database).load(checkpoint["repository_inventory_state"]["store_handle"])
        self.assertEqual(len(restored["findings"]), 24)
        self.assertTrue(all(row["disposition"] == "uncertain" for row in restored["findings"].values()))

    def test_explicit_continue_after_stall_resolves_saved_items_without_rebuilding_scope(self):
        self.handler = lambda payload: {"inventory_lookup": {"query": ""}}
        first = self.run_turn()
        self.assert_stopped_with_pending(first)
        self.payloads.clear()
        self.handler = self.classified
        second = self.run_turn(first)
        self.assertEqual(second["status"], "ANALYZED", second.get("errors"))
        self.assertEqual(second["repository_inventory"]["counts"]["included"], 12)
        self.assertEqual(second["repository_inventory"]["counts"]["pending"], 0)
        saved = RunStore(self.fixture.database.with_name("investigation-runs.sqlite")).load(second["run_id"])["checkpoint"]
        self.assertEqual(saved["repository_inventory_state"]["catalog_builds"], 1)
