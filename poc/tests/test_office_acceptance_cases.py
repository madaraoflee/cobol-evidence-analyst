from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "poc"))
from tools import office_acceptance_cases as suite
from live_business_eval import _cases, _reviewed_context, _turns
from release_acceptance import _live_case_scope, _range_covered, _required_ranges


class OfficeAcceptanceGoldTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        synthetic = self.source / "synthetic"
        synthetic.mkdir(parents=True)
        frozen = json.loads(suite.SYNTHETIC_MANIFEST.read_text(encoding="utf-8"))
        for name, item in frozen["source_files"].items():
            file = suite.checked_file(suite.SYNTHETIC_MANIFEST.parent / "source", item["path"], item["sha256"])
            shutil.copyfile(file, synthetic / name)
        for name, text in (("exception.cbl", suite.EXCEPTION_SOURCE), ("unknown.cbl", suite.UNKNOWN_SOURCE),
                           ("continuation.cbl", suite.CONTINUATION_SOURCE), ("reserved.cbl", suite.RESERVED_SOURCE)):
            (synthetic / name).write_bytes(text.encode())

    def test_all_prior_semantics_have_separate_real_question_and_source_bound_gold(self):
        items = suite.synthetic_cases(self.source)
        coverage = {tag for item in items for tag in item["coverage"]}
        self.assertTrue({"add_giving", "same_entity_input_output", "end_words", "same_line_occurrences",
                         "display_literal_relations", "literal_comment_marker", "qualified_identity",
                         "perform_section", "wrong_formula_999", "static_a_b_c", "later_overwrite",
                         "forward_slice", "size_error", "unknown_effect", "legal_continuation"} <= coverage)
        path = self.root / "cases.json"
        suite.write_json(path, {"cases": items})
        self.assertEqual(_cases(path), items)
        errors = []
        self.assertTrue(_live_case_scope(items, errors))
        self.assertEqual(errors, [])
        self.assertEqual(sum(len(_turns(item)) for item in items), 17)
        for item in items:
            for turn in _turns(item):
                merged = {**item, **turn}
                required = _required_ranges(merged)
                self.assertTrue(required, merged["id"])
                context = _reviewed_context(self.source, merged, "")
                self.assertTrue(context["sources"])
                self.assertTrue(all(_range_covered(span, context["sources"]) for span in required))
                self.assertNotIn("history", merged)
                self.assertNotIn("fake_correct_answer", json.dumps(merged))

    def test_gold_does_not_follow_changed_source_or_hide_absence_as_confirmed(self):
        items = suite.synthetic_cases(self.source)
        case = next(item for item in items if item["id"] == "formula-999-regression")
        file = self.source / "synthetic/overwrite.cbl"
        file.write_bytes(file.read_bytes().replace(b"* 2.", b"* 999."))
        with self.assertRaisesRegex(ValueError, "REVIEWED_SOURCE_CHANGED"):
            _reviewed_context(self.source, case, "")
        unknown = next(item for item in items if item["id"] == "unknown-effect-boundary")
        self.assertEqual(len(unknown["gold"]["required_findings"]), 2)
        self.assertTrue(any("未知调用" in row["description"] for row in unknown["gold"]["forbidden_claims"]))

    def test_review_ranges_cannot_escape_or_have_unreviewed_line_numbers(self):
        with self.assertRaisesRegex(ValueError, "RANGE_INVALID"):
            suite.source_range(self.source, "synthetic/overwrite.cbl", 1, 999)
        outside = self.root / "outside.cbl"
        outside.write_bytes(b"MOVE 9 TO RESULT.\n")
        with self.assertRaisesRegex(ValueError, "PATH_ESCAPE"):
            suite.source_range(self.source, "../outside.cbl", 1)
        with self.assertRaisesRegex(ValueError, "ANCHOR_AMBIGUOUS"):
            suite.anchor(self.source, "synthetic/frontend.cbl", "MOVE")

    def fake_public_root(self):
        """Small neutral corpus for packaging checks, never a business accuracy fixture."""
        public = self.root / "public"
        public.mkdir()
        original = json.loads(suite.PUBLIC_MANIFEST.read_text(encoding="utf-8"))
        for item in original["files"]:
            path = public / item["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            lines = ["       *> Package metadata contract fixture"] * 600
            if path.name == "CBACT04C.cbl":
                lines[110] = "       COPY CVACT01Y."
            path.write_bytes(("\n".join(lines) + "\n").encode())
            item["sha256"] = suite.digest(path)
        (public / "LICENSE").write_bytes(b"Neutral packaging contract license fixture.\n")
        original["license_sha256"] = suite.digest(public / "LICENSE")
        manifest = self.root / "public-manifest.json"
        suite.write_json(manifest, original)
        return public, manifest

    def test_generated_delivery_is_deterministic_and_has_24_frozen_turns(self):
        public, manifest = self.fake_public_root()
        with patch.object(suite, "PUBLIC_MANIFEST", manifest):
            first = suite.generate_suite(self.root / "suite-a", public)
            second = suite.generate_suite(self.root / "suite-b", public)
        self.assertEqual(first["user_turn_count"], 24)
        self.assertEqual(first["maximum_model_requests"], 72)
        self.assertEqual(first["source_files"], 18)
        self.assertEqual(suite.digest(first["cases"]), suite.digest(second["cases"]))
        self.assertEqual(suite.digest(first["lock"]), suite.digest(second["lock"]))
        document = json.loads(first["cases"].read_text(encoding="utf-8"))
        self.assertEqual(len(document["cases"]), 22)
        self.assertEqual(document["budget"]["real_model_calls_performed"], 0)
        public_cases = [item for item in document["cases"] if item["id"].startswith("public-")]
        self.assertEqual(len(public_cases), 7)
        self.assertTrue({"public-monthly-interest-999", "public-fees-invention"} <= {item["id"] for item in public_cases})
        for item in document["cases"]:
            for turn in _turns(item):
                merged = {**item, **turn}
                context = _reviewed_context(first["source"], merged, "")
                self.assertTrue(all(_range_covered(span, context["sources"]) for span in _required_ranges(merged)))
        self.assertEqual((first["source"] / "app/cbl/CBACT04C.cbl").read_bytes(),
                         (public / "app/cbl/CBACT04C.cbl").read_bytes())
        self.assertEqual((first["source"].parent / "PUBLIC-SOURCE-LICENSE.txt").read_bytes(),
                         (public / "LICENSE").read_bytes())

    def test_changed_missing_public_or_license_rejects_before_creating_output(self):
        public, manifest = self.fake_public_root()
        output = self.root / "rejected"
        file = public / "app/cbl/CBACT04C.cbl"
        file.write_bytes(file.read_bytes() + b"CHANGED\n")
        with patch.object(suite, "PUBLIC_MANIFEST", manifest), self.assertRaisesRegex(ValueError, "FROZEN_SOURCE_CHANGED"):
            suite.generate_suite(output, public)
        self.assertFalse(output.exists())
        with self.assertRaisesRegex(ValueError, "LOCAL_PUBLIC_SOURCE_REQUIRED"):
            suite.generate_suite(output, None)
        self.assertFalse(output.exists())

    def test_scale_suite_freezes_all_files_and_five_real_source_bound_turns(self):
        public, manifest = self.fake_public_root()
        with patch.object(suite, "PUBLIC_MANIFEST", manifest):
            result = suite.generate_suite(self.root / "large-suite", public, include_scale=True)
        self.assertEqual(result["user_turn_count"], 29)
        self.assertEqual(result["maximum_model_requests"], 87)
        self.assertEqual(result["source_files"], 12042)
        self.assertTrue((result["source"] / "scale/member-11999.cbl").is_file())
        self.assertFalse((result["source"].parent / "scale-source").exists())
        lock = json.loads(result["lock"].read_text(encoding="utf-8"))
        frozen = json.loads(suite.SYNTHETIC_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(lock["scale_source"]["files"], frozen["large_corpus"]["expected"]["files"])
        self.assertEqual(lock["scale_source"]["source_fingerprint"], frozen["large_corpus"]["expected"]["source_fingerprint"])
        actual_files = {path.relative_to(result["source"]).as_posix(): path for path in result["source"].rglob("*") if path.is_file()}
        self.assertEqual(set(actual_files), {item["path"] for item in lock["files"]})
        for item in lock["files"]:
            path = actual_files[item["path"]]
            self.assertEqual(suite.digest(path), item["sha256"])
            self.assertEqual(path.stat().st_size, item["bytes"])
        items = _cases(result["cases"])
        case = next(item for item in items if item["id"] == "large-repository-real")
        self.assertEqual(len(case["turns"]), 5)
        for turn in case["turns"]:
            spans = _required_ranges(turn)
            self.assertTrue(spans)
            self.assertEqual({span["path"] for span in spans}, {"scale/member-11999.cbl"})
            context = _reviewed_context(result["source"], turn, "")
            self.assertTrue(all(_range_covered(span, context["sources"]) for span in spans))
            excerpts = "\n".join(item["excerpt"] for item in context["sources"])
            for actual in ("COMPUTE BILL-AMOUNT = INPUT-000 * 1", "IF INPUT-000 > 0",
                           "IF INPUT-001 > 1", "COMPUTE BILL-AMOUNT = INPUT-001 * 2", "01 INPUT-000 PIC 9(9)."):
                self.assertIn(actual, excerpts)
            self.assertFalse(_range_covered(spans[0], [{**context["sources"][0], "path": "scale/member-00000.cbl"}]))

    def test_measurement_plan_cannot_be_mistaken_for_execution_or_complete_office_review(self):
        plan = suite.measurement_plan()
        self.assertEqual(plan["status"], "planned_not_executed")
        self.assertIsNone(plan["result"])
        self.assertTrue(plan["no_model_calls_performed"])
        self.assertTrue(plan["office_checks_are_not_automatically_passed"])
        self.assertTrue(all(item["result"] is None for item in plan["requirements"]))
        self.assertEqual({item["id"] for item in plan["requirements"]}, {"question_latency", "large_repository",
            "desktop_start", "source_import", "conversation_recovery", "cancellation_recovery",
            "historical_evidence", "company_api_ca"})


if __name__ == "__main__":
    unittest.main()
