"""Acceptance-oracle unit checks and source HTTP rehearsal, not native proof."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.desktop_acceptance_framework import (REFERENCE, fixed_reads, fixture_identity,
    framework_sources, framework_supply_checks)
from tools.desktop_acceptance_scenarios import run_scenarios


class FrameworkNativeAcceptanceTests(unittest.TestCase):
    def case(self):
        manifest = json.loads((ROOT / "docs/eval/desktop-joint-v1/manifest.json").read_text())
        return next(row for row in manifest["cases"] if row["id"] == "framework_page_chain")

    def payload(self):
        sources, versions = framework_sources(), fixture_identity()
        pages = [{**request, "evidence_id": "ev-page-" + str(index),
                  "source_sha256": versions[request["relative_path"]],
                  "source_text": "\n".join(sources[request["relative_path"]].splitlines()[
                      request["start_line"] - 1:request["end_line"]])}
                 for index, request in enumerate(fixed_reads())]
        entry = sources["entry.cbl"].splitlines()
        first, last = entry.index("CALL 'ROWSIO'") + 1, entry.index("USING ROWS-PARAMS.") + 1
        fact = {"relative_path": "entry.cbl", "source_sha256": versions["entry.cbl"],
            "target_name": "ROWSIO", "start_line": first, "end_line": last,
            "function_field": "ROWS-FUNCTION", "argument": "ROWS-PARAMS", "operation": {"value": "ADVANCE"},
            "dependency_covered": True, "target_source_available": False, "runtime_verified": False,
            "return_status_binding": "not_bound", "reference_ids": ["fw:operation"],
            "source_evidence_ids": [page["evidence_id"] for page in pages],
            "source_ranges": [{"start_line": first, "end_line": last}, {"start_line": 8, "end_line": 8},
                {"relative_path": "ROW-LAYOUT.cpy", "source_sha256": versions["ROW-LAYOUT.cpy"],
                 "start_line": 1, "end_line": 3}],
            "effect_contract": {"runtime_verified": False,
                "unknown_effects": ["cursor", "transaction_result", "file_table_contents"]}}
        return {"source_context": [{"pages": pages}], "framework_facts": [fact],
            "framework_references": [{"reference_id": "fw:operation", "text": REFERENCE,
                                      "document_sha256": versions["reference.md"]}]}

    def test_manifest_freezes_exact_fixture_and_original_thresholds(self):
        case = self.case()
        self.assertEqual(case["fixture_sha256"], fixture_identity())
        self.assertEqual(case["repeats"], 3)
        self.assertEqual(case["limits"], {"cold_index_seconds": 60, "first_provider_seconds": 15, "question_seconds": 30})
        self.assertEqual(case["policy"]["max_model_requests"], 2)
        self.assertTrue(all(framework_supply_checks(self.payload()).values()))

    def test_missing_page_wrong_version_and_truncation_fail_supply(self):
        for kind in ("missing", "version", "truncated"):
            with self.subTest(kind=kind):
                payload = self.payload()
                pages = payload["source_context"][0]["pages"]
                if kind == "missing":
                    del pages[1]
                elif kind == "version":
                    pages[1]["source_sha256"] = "0" * 64
                else:
                    pages[1]["span_truncated"] = True
                checks = framework_supply_checks(payload)
                self.assertFalse(checks["completeness_framework_fact_supplied"])
                self.assertFalse(checks["evidence_framework_all_supports_supplied"])
                self.assertFalse(checks["evidence_call_requires_multiple_pages"])

    def test_single_covering_page_cannot_pass_a_split_page_probe(self):
        payload = self.payload()
        text = framework_sources()["entry.cbl"]
        payload["source_context"][0]["pages"].append({"relative_path": "entry.cbl",
            "source_sha256": fixture_identity()["entry.cbl"], "start_line": 1,
            "end_line": len(text.splitlines()), "source_text": text.rstrip("\n"), "evidence_id": "ev-whole"})
        self.assertFalse(framework_supply_checks(payload)["evidence_call_requires_multiple_pages"])

    def test_fact_cannot_narrow_its_own_required_source_support(self):
        for omitted in ("call", "assignment", "layout"):
            with self.subTest(omitted=omitted):
                payload = self.payload()
                pages = payload["source_context"][0]["pages"]
                fact = payload["framework_facts"][0]
                fact["source_ranges"] = []
                fact["source_evidence_ids"] = []
                obligations = [("call", "entry.cbl", start, end)
                               for start, end in ((9, 20), (21, 40), (41, 58))]
                obligations += [("assignment", "entry.cbl", 8, 8), ("layout", "ROW-LAYOUT.cpy", 1, 3)]
                for index, (kind, path, start, end) in enumerate(obligations):
                    if kind == omitted:
                        continue
                    identifier = "ev-independent-" + str(index)
                    span = {"relative_path": path, "source_sha256": fixture_identity()[path],
                            "start_line": start, "end_line": end}
                    pages.append({**span, "evidence_id": identifier, "source_text": "\n".join(
                        framework_sources()[path].splitlines()[start - 1:end])})
                    fact["source_ranges"].append(span)
                    fact["source_evidence_ids"].append(identifier)
                checks = framework_supply_checks(payload)
                self.assertTrue(checks["completeness_framework_fact_supplied"])
                self.assertTrue(checks["evidence_call_requires_multiple_pages"])
                self.assertFalse(checks["evidence_framework_all_supports_supplied"])

    def test_fact_must_cite_all_manual_binding_rules_not_only_present_documents(self):
        lines = ["CALL XXXXIO USING XXXX-PARAMS", "The operation is selected by XXXX-FUNCTION.",
                 next(line for line in REFERENCE.splitlines() if line.startswith("| ADVANCE |"))]
        for omitted in range(len(lines)):
            with self.subTest(omitted=omitted):
                payload = self.payload()
                payload["framework_references"] = [{"reference_id": "fw:rule-" + str(index), "text": line,
                    "document_sha256": fixture_identity()["reference.md"]} for index, line in enumerate(lines)]
                fact = payload["framework_facts"][0]
                fact["reference_ids"] = [ref["reference_id"] for ref in payload["framework_references"]]
                self.assertTrue(framework_supply_checks(payload)["evidence_framework_all_supports_supplied"])
                del fact["reference_ids"][omitted]
                self.assertFalse(framework_supply_checks(payload)["evidence_framework_all_supports_supplied"])

    def test_fact_and_reference_support_cannot_be_invented(self):
        payload = self.payload()
        mutations = [lambda p: p.update(framework_facts=[]),
            lambda p: p.update(framework_references=[]),
            lambda p: p["framework_references"][0].update(document_sha256="0" * 64),
            lambda p: p["framework_references"][0].update(text="Invented runtime guarantee."),
            lambda p: p["framework_facts"][0].update(source_evidence_ids=["ev-absent"]),
            lambda p: p["framework_facts"][0].update(source_sha256="0" * 64)]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                changed = deepcopy(payload)
                mutation(changed)
                self.assertFalse(framework_supply_checks(changed)["evidence_framework_all_supports_supplied"])
        for change in ({"runtime_verified": True}, {"target_source_available": True},
                       {"return_status_binding": "verified"},
                       {"effect_contract": {"runtime_verified": True, "unknown_effects": []}}):
            with self.subTest(change=change):
                changed = deepcopy(payload)
                changed["framework_facts"][0].update(change)
                self.assertFalse(framework_supply_checks(changed)["quality_closed_runtime_not_proven"])

    def test_source_http_exercises_actual_reads_framework_facts_and_final_answer(self):
        case = self.case()
        with tempfile.TemporaryDirectory() as directory:
            rows = run_scenarios([sys.executable, str(ROOT / "poc/desktop_app.py")], directory,
                                 {"cases": [{**case, "repeats": 1}]})
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["status"], "passed", row.get("error"))
            self.assertEqual(set(row["checks"]), set(case["assertions"]))
            self.assertTrue(all(row["checks"].values()))
            self.assertEqual(row["model_requests"], 2)
            self.assertIsNone(row["real_model_accuracy"])
            self.assertIsNone(row["business_answer_correctness"])
            self.assertTrue(row["run_id"])
            self.assertTrue(row["acceptance_diagnostics"])
            self.assertLessEqual(row["timings"]["question_seconds"], case["limits"]["question_seconds"])


if __name__ == "__main__":
    unittest.main()
