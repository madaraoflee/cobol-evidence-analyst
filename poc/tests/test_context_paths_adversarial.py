"""Negative cases for source-context composition, not runtime execution tests."""

from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from context_paths import contextualize_errors


def _field(context, name):
    return {"context_id": context, "symbol_id": name, "name": name,
            "field_instance_id": context + ":" + name}


def _mapping(context, parent, actual, formal, *, mode="REFERENCE", suffix=""):
    return {"binding_id": context + ":" + formal + suffix,
            "caller_field": _field(parent, actual),
            "callee_field": _field(context, formal),
            "passing_mode": mode, "evidence_refs": []}


def _context(name, parent, program, mappings=()):
    return {"context_id": name, "parent_context_id": parent,
            "program_name": program, "parameter_mappings": list(mappings)}


def _fixture():
    contexts = [
        _context("root", None, "ENTRY"),
        _context("middle", "root", "WRAPPER", [
            _mapping("middle", "root", "ROOT-STATUS", "WRAP-STATUS")]),
        _context("leaf", "middle", "LEAF", [
            _mapping("leaf", "middle", "WRAP-STATUS", "LEAF-STATUS")]),
    ]
    context_audit = {"snapshot_id": "source-snapshot", "contexts": contexts}
    errors = {
        "snapshot_id": "source-snapshot",
        "programs": [{"program_name": "LEAF", "status_fields": ["LEAF-STATUS"],
                      "output_fields": []}],
        "observations": [{"program_name": "LEAF", "kind": "error_status_origin"}],
    }
    return context_audit, errors


class ContextPathAdversarialTests(unittest.TestCase):
    def test_cycle_without_role_bearing_mappings_is_rejected(self):
        audit, errors = _fixture()
        audit["contexts"] = [
            _context("cycle-a", "cycle-b", "FIRST"),
            _context("cycle-b", "cycle-a", "SECOND"),
        ]
        with self.assertRaises(ValueError):
            contextualize_errors(audit, errors)

    def test_route_budget_cannot_hide_disconnected_cycle(self):
        audit, errors = _fixture()
        audit["contexts"].extend([
            _context("cycle-a", "cycle-b", "FIRST"),
            _context("cycle-b", "cycle-a", "SECOND"),
        ])
        with self.assertRaises(ValueError):
            contextualize_errors(audit, errors, max_routes=1, max_links=1)

    def test_multiple_roots_are_not_one_context_tree(self):
        audit, errors = _fixture()
        audit["contexts"].append(_context("second-root", None, "OTHER"))
        with self.assertRaises(ValueError):
            contextualize_errors(audit, errors)

    def test_empty_context_tree_is_rejected(self):
        audit, errors = _fixture()
        audit["contexts"] = []
        with self.assertRaises(ValueError):
            contextualize_errors(audit, errors)

    def test_duplicate_seed_formal_never_claims_unambiguous_reference_return(self):
        audit, errors = _fixture()
        leaf = audit["contexts"][2]
        leaf["parameter_mappings"].append(
            _mapping("leaf", "middle", "OTHER-STATUS", "LEAF-STATUS", suffix=":duplicate"))
        try:
            result = contextualize_errors(audit, errors)
        except ValueError:
            return
        routes = [route for route in result["parameter_routes"] if route["context_id"] == "leaf"]
        self.assertTrue(all(not route["reference_return_candidate"] for route in routes))
        self.assertTrue(all(route["terminal_reason"] == "ambiguous_incoming_parameter_correspondence"
                            for route in routes))
        self.assertTrue(routes or any(
            boundary["reason"] == "ambiguous_incoming_parameter_correspondence"
            for boundary in result["boundaries"]))

    def test_copy_beyond_link_budget_cannot_be_reported_as_outer_reference_return(self):
        audit, errors = _fixture()
        audit["contexts"][1]["parameter_mappings"][0]["passing_mode"] = "CONTENT"
        result = contextualize_errors(audit, errors, max_links=1)
        route = result["parameter_routes"][0]
        self.assertEqual(route["context_id"], "leaf")
        self.assertFalse(route["reference_return_candidate"])
        self.assertEqual(route["terminal_reason"], "parameter_route_link_budget_exhausted")
        self.assertTrue(result["summary"]["truncated"])

    def test_input_reports_remain_unchanged(self):
        audit, errors = _fixture()
        before = copy.deepcopy((audit, errors))
        contextualize_errors(audit, errors)
        self.assertEqual((audit, errors), before)

    def test_upstream_truncation_is_not_erased_by_a_small_projection(self):
        audit, errors = _fixture()
        audit["summary"] = {"truncated": True}
        result = contextualize_errors(audit, errors)
        self.assertTrue(result["summary"]["truncated"])
        self.assertIn("upstream_context_audit_truncated",
                      {boundary["reason"] for boundary in result["boundaries"]})

    def test_missing_parent_binding_never_returns_a_partial_route_as_root_candidate(self):
        audit, errors = _fixture()
        audit["contexts"][1]["parameter_mappings"] = []
        audit["contexts"][1]["parameter_mapping_complete"] = False
        route = contextualize_errors(audit, errors)["parameter_routes"][0]
        self.assertFalse(route["reference_return_candidate"])
        self.assertEqual(route["fields_inner_to_outer"][-1]["context_id"], "middle")
        self.assertNotEqual(route["terminal_reason"], "root_context_field")


if __name__ == "__main__":
    unittest.main()
