"""Version/evidence boundaries for effects in the default framework path."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from business_index import build_business_index
from compilation_context import CompilationContext
from effect_contract import resource_profile
from framework_semantics import build_framework_facts, visible_framework_facts
from test_framework_semantics import MANUAL, SOURCE


class EffectContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.path = self.source / "entry.cbl"
        self.path.write_text(SOURCE)
        self.manual = self.root / "reference.md"
        self.manual.write_text(MANUAL)
        self.database = self.root / "index.sqlite"

    def facts(self, *, context=None, manual=None):
        reference = manual or self.manual
        build_business_index(self.source, self.database, source_format="free", quiet=True,
                             framework_reference_path=reference)
        supplied = {"evidence_id": "ev-source", "relative_path": "entry.cbl", "start_line": 1,
                    "end_line": len(SOURCE.splitlines()), "source_text": SOURCE,
                    "source_sha256": hashlib.sha256(self.path.read_bytes()).hexdigest()}
        result = build_framework_facts(self.database, [supplied], reference_path=reference,
                                       compilation_context=context)
        return result, supplied

    def test_existing_default_manual_binding_carries_versioned_contract(self):
        result, _ = self.facts()
        fact = result["facts"][0]
        contract = fact["effect_contract"]
        self.assertEqual(contract["framework_versions"], ["R1.0"])
        self.assertEqual(contract["applicable_call"]["target"], "ROWSIO")
        self.assertEqual(contract["applicable_call"]["arguments"][0]["passing_mode"], "REFERENCE")
        self.assertEqual(contract["status"], "bound")
        self.assertEqual(contract["provenance"]["kind"], "manual")
        self.assertEqual(contract["status_codes"][0]["value"], "DONE")
        self.assertIn("argument_modifications", contract["unknown_effects"])
        self.assertFalse(contract["runtime_verified"])

    def test_contract_status_evidence_is_pruned_when_request_did_not_supply_it(self):
        result, supplied = self.facts()
        fact = result["facts"][0]
        operation_refs = [row for row in result["references"] if row["reference_id"] in fact["reference_ids"]]
        visible = visible_framework_facts([fact], [supplied], operation_refs)[0]
        self.assertTrue(visible["dependency_covered"])
        self.assertEqual(visible["effect_contract"]["status_codes"], [])
        self.assertTrue(visible["effect_contract"]["evidence_visibility_checked"])

    def test_selected_framework_version_conflict_is_explicit_and_withdraws_coverage(self):
        result, _ = self.facts(context=CompilationContext(framework_version="R2.0"))
        fact = result["facts"][0]
        self.assertFalse(fact["dependency_covered"])
        self.assertEqual(fact["effect_contract"]["status"], "version_conflict")
        conflict = result["effect_contract_conflicts"][0]
        self.assertEqual(conflict["manual_versions"], ["R1.0"])
        self.assertEqual(conflict["selected_version"], "R2.0")
        self.assertTrue(conflict["reference_ids"])

    def test_conflicting_manual_versions_remain_candidates(self):
        directory = self.root / "manuals"
        directory.mkdir()
        (directory / "one.md").write_text(MANUAL)
        (directory / "two.md").write_text(MANUAL.replace("R1.0", "R2.0"))
        result, _ = self.facts(manual=directory)
        fact = result["facts"][0]
        self.assertFalse(fact["dependency_covered"])
        self.assertEqual(fact["effect_contract"]["status"], "version_conflict")
        self.assertEqual(fact["effect_contract"]["framework_versions"], ["R1.0", "R2.0"])
        self.assertEqual(len(fact["effect_contract"]["manual_candidates"]), 2)

    def test_source_implementation_retains_precedence_over_manual_contract(self):
        (self.source / "service.cbl").write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. ROWSIO.\n"
                                               "PROCEDURE DIVISION.\nGOBACK.\n")
        result, _ = self.facts()
        fact = result["facts"][0]
        self.assertFalse(fact["dependency_covered"])
        self.assertEqual(fact["reason"], "source_implementation_takes_precedence")
        self.assertEqual(fact["effect_contract"]["status"], "source_implementation_required")

    def test_runtime_configuration_is_bound_and_cited_without_runtime_or_layout_proof(self):
        fixture = Path(__file__).resolve().parents[1] / "fixtures/framework-path-v2/runtime-contract.json"
        runtime = json.loads(fixture.read_text())
        runtime["provenance"]["target_version"] = "R1.0"
        runtime["services"][0].update(program="ROWSIO", argument="ROWS-PARAMS", function_field="ROWS-FUNCTION", status_field="ROWS-STATUS")
        runtime["services"][0]["operations"] = [{"value": "ADVANCE", "action": "next"}]
        self.manual.with_suffix(".runtime-contract.json").write_text(json.dumps(runtime, indent=2))
        result, supplied = self.facts()
        fact = result["facts"][0]
        configured = fact["effect_contract"]["explicit_configuration_candidates"][0]
        self.assertEqual(configured["provenance"]["kind"], "configuration")
        self.assertEqual(configured["target_validation"], "unvalidated_candidate")
        self.assertFalse(configured["record_layout_source_verified"])
        self.assertFalse(configured["runtime_verified"])
        self.assertEqual(configured["cursor"]["action"], "next")
        self.assertTrue(set(configured["reference_ids"]) <= {row["reference_id"] for row in result["references"]})
        no_configuration = [row for row in result["references"] if row["selection_reason"] != "effect_contract_configuration"]
        visible = visible_framework_facts([fact], [supplied], no_configuration)[0]
        self.assertEqual(visible["effect_contract"]["explicit_configuration_candidates"], [])
        self.assertIn("configuration_evidence_not_supplied", visible["effect_contract"]["unknown_effects"])

    def test_target_resource_profile_does_not_turn_public_sample_into_target_validation(self):
        profile = resource_profile(CompilationContext(dialect="target-cobol", target_profile="target-batch",
            resource_kinds=("cobol-file", "dds", "cl", "sql", "jcl", "cics")))
        self.assertFalse(profile["target_validated"])
        status = {row["kind"]: row["support_status"] for row in profile["resources"]}
        self.assertEqual(status["dds"], "candidate")
        self.assertEqual(status["cl"], "candidate")
        self.assertEqual(status["sql"], "bounded")
        self.assertEqual(status["cics"], "unsupported")


if __name__ == "__main__":
    unittest.main()
