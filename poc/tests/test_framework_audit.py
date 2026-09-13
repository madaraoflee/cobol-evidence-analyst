from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework_audit import audit_framework, validate_profile


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "framework-flow-v1"


class FrameworkAuditTests(unittest.TestCase):
    def setUp(self):
        self.profile = json.loads((FIXTURE / "profile.json").read_text())

    def audit(self, entry="BATCHJOB", profile=True):
        return audit_framework(FIXTURE / "source", entry, self.profile if profile else None)

    def test_controller_performs_keep_host_and_original_source(self):
        result = self.audit()
        self.assertTrue(result["scope"]["source_expansion_complete"])
        self.assertEqual(len(result["observations"]["performs"]), 5)
        for perform in result["observations"]["performs"]:
            self.assertEqual(perform["target_status"], "unique_definition")
            ref = perform["references"][0]
            self.assertEqual(ref["origin"]["relative_path"], "copybooks/BATCHCTL.cpy")
            self.assertEqual(ref["include_chain"][0]["relative_path"], "programs/BATCHJOB.cbl")
            self.assertEqual(ref["include_chain"][0]["line"], 9)
            raw = (FIXTURE / "source" / ref["origin"]["relative_path"]).read_bytes()
            self.assertEqual(ref["origin"]["source_hash"], hashlib.sha256(raw).hexdigest())
            self.assertIn("PERFORM", raw.decode().splitlines()[ref["origin"]["line"] - 1])

    def test_batch_and_online_do_not_share_section_meaning_by_number(self):
        batch = self.audit()
        online = self.audit("SCREENJOB")
        self.assertEqual(batch["declared_contracts"]["entry"]["mode"], "batch")
        self.assertEqual(online["declared_contracts"]["entry"]["mode"], "online")
        self.assertIn("4000-CLOSE", [p["target"] for p in batch["observations"]["performs"]])
        self.assertNotIn("4000-CLOSE", [p["target"] for p in online["observations"]["performs"]])
        self.assertIn("4000-NEXT-SCREEN", [p["target"] for p in online["observations"]["performs"]])
        for result in (batch, online):
            for section in result["declared_contracts"]["entry"]["sections"]:
                self.assertEqual(section["role_status"], "declared_not_verified")
        self.profile["entries"][0]["sections"] = [{"name": "CONTROL-ENTRY", "role": "initialize"}]
        paragraph = self.audit()["declared_contracts"]["entry"]["sections"][0]
        self.assertFalse(paragraph["definition_observed"])

    def test_missing_profile_does_not_guess_framework(self):
        result = self.audit(profile=False)
        self.assertIsNone(result["profile"])
        self.assertIsNone(result["declared_contracts"]["entry"])
        self.assertIn("framework_profile_missing", [b["reason"] for b in result["boundaries"]])

    def test_io_operations_never_claim_values_reaching_call(self):
        result = self.audit()
        self.assertEqual(len(result["observations"]["calls"]), 3)
        for call in result["observations"]["calls"]:
            self.assertEqual(call["io_contract"], "DATAACCESS")
            self.assertEqual(call["function_value_at_call"], "not_resolved")
            self.assertFalse(call["callee_body_analyzed"])
        contract = result["declared_contracts"]["io_contracts"][0]
        self.assertIn("NEXT", [op["value"] for op in contract["operations"]])
        self.assertEqual(result["declared_contracts"]["record_decisions"][0]["field"], "RECORD-DECISION")
        self.assertNotEqual(contract["status_field"], "RECORD-DECISION")

    def test_missing_dictionary_is_explicit(self):
        result = self.audit()
        self.assertEqual(result["declared_contracts"]["artifact_requirements"][0]["status"], "missing_from_snapshot")

    def test_present_dictionary_is_not_interpreted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ENTRY.cbl").write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRY.\nPROCEDURE DIVISION.\nGOBACK.\n")
            (root / "definition.lf").write_text("A KEYFIELD\n")
            profile = deepcopy(self.profile)
            profile["artifact_requirements"] = [{"kind": "logical_file", "relative_path": "definition.lf"}]
            result = audit_framework(root, "ENTRY", profile)
            self.assertEqual(result["declared_contracts"]["artifact_requirements"][0]["status"], "present_not_interpreted")

    def test_company_provenance_is_still_not_runtime_proof(self):
        self.profile["provenance"]["kind"] = "company_validated"
        result = self.audit()
        self.assertEqual(result["profile"]["semantic_status"], "declared_not_verified")
        for key in ("runtime_verified", "compiler_equivalent", "control_flow_complete", "question_answered"):
            self.assertFalse(result["scope"][key])

    def test_profile_hash_changes_without_mutating_source_manifest(self):
        before = self.audit()
        self.profile["profile_version"] = "2"
        self.profile["io_contracts"][0]["operations"][0]["reads_record"] = False
        after = self.audit()
        self.assertEqual(before["source_manifest_hash"], after["source_manifest_hash"])
        self.assertNotEqual(before["profile_hash"], after["profile_hash"])

    def test_validation_does_not_mutate_profile(self):
        original = deepcopy(self.profile)
        self.audit()
        self.assertEqual(self.profile, original)

    def test_profile_cannot_inject_verification_flags(self):
        self.profile["runtime_verified"] = True
        with self.assertRaises(ValueError):
            self.audit()

    def test_profile_rejects_duplicate_entries(self):
        self.profile["entries"].append(deepcopy(self.profile["entries"][0]))
        with self.assertRaises(ValueError):
            self.audit()

    def test_profile_rejects_missing_version(self):
        del self.profile["profile_version"]
        with self.assertRaises(ValueError):
            self.audit()

    def test_profile_rejects_collapsed_state_channels(self):
        self.profile["record_decisions"][0]["field"] = "ACCESS-STATUS"
        with self.assertRaises(ValueError):
            self.audit()

    def test_profile_rejects_outside_paths(self):
        for path in ("../secret.lf", "/secret.lf", "C:/secret.lf", "a\\b.lf", "./a.lf"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                profile = deepcopy(self.profile)
                profile["artifact_requirements"][0]["relative_path"] = path
                validate_profile(profile)

    def test_missing_controller_prevents_positive_expansion_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "entry.cbl").write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. BATCHJOB.\nPROCEDURE DIVISION.\nCOPY BATCHCTL.\nGOBACK.\n")
            result = audit_framework(root, "BATCHJOB", self.profile)
            self.assertFalse(result["scope"]["source_expansion_complete"])
            self.assertFalse(result["declared_contracts"]["entry"]["control_copy_observed"])
            self.assertFalse(result["observations"]["performs"])


if __name__ == "__main__":
    unittest.main()
