"""Derived statements retain only their exact supplied source instances."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_index import build_business_index
from evidence_context import EvidenceContext, EvidenceGroup
from repository_discovery import ensure_repository_search
from semantic_scope import build_business_evidence, prepare_semantic_scope
from source_session import QuestionSourceSession


def page(line, **extra):
    return {"evidence_id": "ev_" + str(line), "relative_path": "entry.cbl", "source_sha256": "version-a",
        "start_line": line, "end_line": line, "source_text": "MOVE " + str(line) + " TO AMOUNT.", **extra}


def node(line):
    return {"occurrence_id": "statement-" + str(line), "statement": "SOURCE-STATEMENT-" + str(line),
        "source_refs": [{key: value for key, value in page(line).items()
                         if key in {"relative_path", "source_sha256", "start_line", "end_line"}}]}


def payload(pages, analysis):
    return {"source_context": [{"pages": pages, "call_chain": {"links": []}, "outline": []}],
        "evidence_groups": [{"group_id": "neutral-group", "required_evidence_ids": [],
            "open_frontier": [], "semantic_analysis": analysis}]}


class SemanticVisibilityProjectionTests(unittest.TestCase):
    def setUp(self):
        self.nodes = [node(line) for line in range(1, 5)]
        identifiers = [item["occurrence_id"] for item in self.nodes]
        self.analysis = {"slice_statements": self.nodes,
            "assignments": [{"statement": item} for item in self.nodes],
            "tool_results": [{"occurrence_id": identifiers[0], "statements": self.nodes,
                "occurrences": identifiers, "later_writes": identifiers[1:],
                "reaching_definitions": {"AMOUNT": [*identifiers, "@entry:AMOUNT"]}}]}

    def test_repeated_projection_is_stable_and_further_trim_adds_only_new_omissions(self):
        request = payload([page(1), page(2)], deepcopy(self.analysis))
        EvidenceContext.reconcile_payload(request)
        first = json.dumps(request, sort_keys=True)
        self.assertEqual(request["evidence_groups"][0]["semantic_analysis"]["withheld_statement_count"], 2)
        EvidenceContext.reconcile_payload(request)
        self.assertEqual(json.dumps(request, sort_keys=True), first)
        request["source_context"][0]["pages"] = [page(1)]
        EvidenceContext.reconcile_payload(request)
        analysis = request["evidence_groups"][0]["semantic_analysis"]
        self.assertEqual(analysis["withheld_statement_count"], 3)
        self.assertEqual([item["occurrence_id"] for item in analysis["slice_statements"]], ["statement-1"])
        self.assertEqual(analysis["tool_results"][0]["reaching_definitions"],
                         {"AMOUNT": ["statement-1", "@entry:AMOUNT"]})
        self.assertEqual(analysis["tool_results"][0]["later_writes"], [])
        second = json.dumps(request, sort_keys=True)
        self.assertNotIn("SOURCE-STATEMENT-2", second)
        self.assertNotIn("SOURCE-STATEMENT-3", second)
        EvidenceContext.reconcile_payload(request)
        self.assertEqual(json.dumps(request, sort_keys=True), second)

    def test_next_main_request_restores_from_unchanged_canonical_evidence_group(self):
        group = EvidenceGroup("neutral-group", {}, "explain", semantic_analysis=deepcopy(self.analysis))
        request = payload([page(1)], group.semantic_analysis)
        EvidenceContext.reconcile_payload(request)
        self.assertEqual(len(request["evidence_groups"][0]["semantic_analysis"]["slice_statements"]), 1)
        self.assertEqual(len(group.semantic_analysis["slice_statements"]), 4)
        self.assertNotIn("withheld_statement_count", group.semantic_analysis)
        restored = payload([page(line) for line in range(1, 5)], group.semantic_analysis)
        EvidenceContext.reconcile_payload(restored)
        self.assertEqual(restored["evidence_groups"][0]["semantic_analysis"]["withheld_statement_count"], 0)
        self.assertEqual(len(restored["evidence_groups"][0]["semantic_analysis"]["slice_statements"]), 4)

    def test_wrong_version_instance_replacement_and_truncated_text_cannot_supply_nodes(self):
        for changes in ({"source_sha256": "version-b"}, {"include_chain": [{"path": "other.cbl", "line": 8}]},
                        {"replacement_trace": [{"from": "OLD-AMOUNT", "to": "NEW-AMOUNT"}]},
                        {"span_truncated": True}, {"source_text": ""}, {"source_sha256": None}):
            with self.subTest(changes=changes):
                request = payload([page(1, **changes)], {"slice_statements": [node(1)]})
                EvidenceContext.reconcile_payload(request)
                self.assertEqual(request["evidence_groups"][0]["semantic_analysis"]["slice_statements"], [])
                self.assertEqual(request["evidence_groups"][0]["semantic_analysis"]["withheld_statement_count"], 1)

    def test_matching_expanded_instance_preserves_its_nodes(self):
        extra = {"include_chain": [{"path": "entry.cbl", "line": 8}],
                 "replacement_trace": [{"from": "OLD-AMOUNT", "to": "NEW-AMOUNT"}]}
        item = node(1)
        item["source_refs"][0].update(extra)
        request = payload([page(1, **extra)], {"slice_statements": [item]})
        EvidenceContext.reconcile_payload(request)
        self.assertEqual(request["evidence_groups"][0]["semantic_analysis"]["slice_statements"], [item])

    def test_real_copy_semantic_pages_match_their_source_reference_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "entry.cbl").write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRY.\nDATA DIVISION.\n"
                "WORKING-STORAGE SECTION.\n01 AMOUNT PIC 9(5).\nPROCEDURE DIVISION.\nMAIN.\n"
                "COPY STEPCTL.\nADD 1 TO AMOUNT.\nGOBACK.\n")
            (source / "stepctl.cpy").write_text("MOVE 2 TO AMOUNT.\n")
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", framework_reference_path="", quiet=True)
            ensure_repository_search(database, source)
            with QuestionSourceSession(database, source) as session:
                with prepare_semantic_scope(database, session, anchors=[{"relative_path": "entry.cbl", "line": 9}],
                                            policy=AgentPolicy()) as scope:
                    group = build_business_evidence(scope, session, anchor={"relative_path": "entry.cbl", "line": 9},
                                                    policy=AgentPolicy())
                    copy_nodes = [item for item in group.semantic_analysis.get("slice_statements", [])
                                  if any(ref.get("include_chain") for ref in item["source_refs"])]
                    self.assertTrue(copy_nodes, group.open_frontier)
                    request = payload(group.supplied_locations, group.semantic_analysis)
                    EvidenceContext.reconcile_payload(request)
                    supplied = {item["occurrence_id"] for item in request["evidence_groups"][0]["semantic_analysis"]["slice_statements"]}
                    self.assertLessEqual({item["occurrence_id"] for item in copy_nodes}, supplied)


if __name__ == "__main__":
    unittest.main()
