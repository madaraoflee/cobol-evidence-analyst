from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from context_paths import contextualize_errors


def field(context, symbol, name):
    return {"context_id": context, "symbol_id": symbol, "name": name,
            "field_instance_id": context + ":" + symbol}


def mapping(context, parent, actual, formal, mode="REFERENCE"):
    return {"binding_id": context + ":" + formal,
            "caller_field": field(parent, actual, actual),
            "callee_field": field(context, formal, formal),
            "passing_mode": mode, "evidence_refs": []}


def fixture():
    contexts = [{"context_id": "root", "parent_context_id": None,
                 "program_name": "ENTRY", "parameter_mappings": []}]
    for branch in ("A", "B"):
        wrapper, leaf = "wrap" + branch, "leaf" + branch
        contexts.extend([
            {"context_id": wrapper, "parent_context_id": "root", "program_name": "WRAP",
             "parameter_mappings": [mapping(wrapper, "root", "STATUS-" + branch, "WRAP-STATUS")]},
            {"context_id": leaf, "parent_context_id": wrapper, "program_name": "LEAF",
             "parameter_mappings": [mapping(leaf, wrapper, "WRAP-STATUS", "LEAF-STATUS")]},
        ])
    audit = {"snapshot_id": "test-snapshot", "contexts": contexts}
    errors = {"snapshot_id": "test-snapshot", "programs": [
        {"program_name": "WRAP", "status_fields": ["WRAP-STATUS"], "output_fields": []},
        {"program_name": "LEAF", "status_fields": ["LEAF-STATUS"], "output_fields": []}],
        "observations": [{"program_name": "LEAF", "kind": "error_status_origin"}]}
    return audit, errors


class ContextPathTests(unittest.TestCase):
    def test_reused_nested_callee_keeps_two_correct_outer_status_fields(self):
        result = contextualize_errors(*fixture())
        routes = [r for r in result["parameter_routes"] if r["program_name"] == "LEAF"]
        self.assertEqual({r["context_id"]: r["fields_inner_to_outer"][-1]["name"] for r in routes},
                         {"leafA": "STATUS-A", "leafB": "STATUS-B"})
        self.assertTrue(all(r["reference_return_candidate"] for r in routes))
        self.assertTrue(all(not r["runtime_error_propagation_proven"] for r in routes))

    def test_outer_content_copy_blocks_inner_reference_return(self):
        audit, errors = fixture()
        audit["contexts"][1]["parameter_mappings"][0]["passing_mode"] = "CONTENT"
        routes = contextualize_errors(audit, errors)["parameter_routes"]
        route = next(r for r in routes if r["context_id"] == "leafA")
        self.assertFalse(route["reference_return_candidate"])
        self.assertTrue(route["copy_boundary_blocks_outer_reference_return"])
        self.assertEqual([s["passing_mode"] for s in route["steps_inner_to_outer"]], ["REFERENCE", "CONTENT"])
        self.assertTrue(next(r for r in routes if r["context_id"] == "leafB")["reference_return_candidate"])

    def test_outer_value_also_blocks_return(self):
        audit, errors = fixture()
        audit["contexts"][1]["parameter_mappings"][0]["passing_mode"] = "VALUE"
        route = next(r for r in contextualize_errors(audit, errors)["parameter_routes"] if r["context_id"] == "leafA")
        self.assertFalse(route["reference_return_candidate"])

    def test_local_work_field_stops_route_without_inventing_assignment(self):
        audit, errors = fixture()
        audit["contexts"][2]["parameter_mappings"][0]["caller_field"] = field("wrapA", "LOCAL-STATUS", "LOCAL-STATUS")
        route = next(r for r in contextualize_errors(audit, errors)["parameter_routes"] if r["context_id"] == "leafA")
        self.assertEqual(route["terminal_reason"], "no_incoming_correspondence_observed")
        self.assertFalse(route["reference_return_candidate"])
        self.assertEqual(len(route["steps_inner_to_outer"]), 1)
        self.assertEqual(route["fields_inner_to_outer"][-1]["context_id"], "wrapA")

    def test_observations_are_referenced_not_presented_as_separate_executions(self):
        result = contextualize_errors(*fixture())
        leaves = [c for c in result["contexts"] if c["program_name"] == "LEAF"]
        self.assertEqual([c["local_observation_indexes"] for c in leaves], [[0], [0]])
        self.assertIn("same_local_source", result["observation_reference_scope"])

    def test_budgets_bound_emitted_routes_links_and_references(self):
        audit, errors = fixture()
        result = contextualize_errors(audit, errors, max_routes=1, max_links=1, max_observation_refs=1)
        self.assertLessEqual(len(result["parameter_routes"]), 1)
        self.assertEqual(result["summary"]["parameter_links"], 1)
        self.assertEqual(result["summary"]["observation_references"], 1)
        self.assertTrue(result["summary"]["truncated"])
        for value in (True, 0, -1, 1.5, "2", 100001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                contextualize_errors(audit, errors, max_links=value)

    def test_snapshot_parent_identity_and_duplicate_context_fail_closed(self):
        audit, errors = fixture()
        errors["snapshot_id"] = "different"
        with self.assertRaises(ValueError):
            contextualize_errors(audit, errors)
        audit, errors = fixture()
        audit["contexts"][2]["parameter_mappings"][0]["caller_field"]["context_id"] = "wrapB"
        with self.assertRaises(ValueError):
            contextualize_errors(audit, errors)
        audit, errors = fixture()
        audit["contexts"].append(copy.deepcopy(audit["contexts"][0]))
        with self.assertRaises(ValueError):
            contextualize_errors(audit, errors)


if __name__ == "__main__":
    unittest.main()
