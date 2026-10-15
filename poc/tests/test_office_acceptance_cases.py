from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "poc"))
sys.path.insert(0, str(ROOT / "tools"))
from tools import office_acceptance_cases as suite
from live_business_eval import _cases, _reviewed_context, _turns
from parser_adapter import BuiltinParserAdapter
from release_acceptance import _live_case_scope, _range_covered, _required_ranges
from statement_facts import assignment_facts, data_references
from tools.desktop_live_acceptance import validate_gold
from tools.desktop_acceptance_scenarios import CONVERSATION_QUESTIONS, _conversation_source


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
                           ("continuation.cbl", suite.CONTINUATION_SOURCE), ("reserved.cbl", suite.RESERVED_SOURCE),
                           ("report.cbl", _conversation_source())):
            (synthetic / name).write_bytes(text.encode("utf-8"))
        for name, text in suite.activation_sources().items():
            (synthetic / name).write_bytes(text.encode("utf-8"))

    def test_suite_can_be_invoked_as_script_outside_repository(self):
        result = subprocess.run([sys.executable, str(ROOT / "tools/office_acceptance_cases.py"), "--help"],
                                cwd=self.root, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--include-scale", result.stdout)

    def test_all_prior_semantics_have_separate_real_question_and_source_bound_gold(self):
        items = suite.synthetic_cases(self.source)
        coverage = {tag for item in items for tag in item["coverage"]}
        self.assertTrue({"add_giving", "same_entity_input_output", "end_words", "same_line_occurrences",
                         "display_literal_relations", "literal_comment_marker", "qualified_identity",
                         "perform_section", "wrong_formula_999", "static_a_b_c", "later_overwrite",
                         "forward_slice", "size_error", "unknown_effect", "legal_continuation",
                         "bare_group_field", "qualified_in_alias", "ambiguous_group_field",
                         "value_initialization_source", "page_account_multiturn"} <= coverage)
        path = self.root / "cases.json"
        suite.write_json(path, {"cases": items})
        self.assertEqual(_cases(path), items)
        errors = []
        self.assertTrue(_live_case_scope(items, errors))
        self.assertEqual(errors, [])
        self.assertEqual(sum(len(_turns(item)) for item in items), 24)
        self.assertTrue({"activation_trigger", "caller_condition", "external_scheduler_unknown"} <= coverage)
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

    def test_three_group_reference_questions_require_real_fields_formula_and_candidates(self):
        items = suite.record_binding_cases(self.source)
        by_id = {item["id"]: item for item in items}
        self.assertEqual(set(by_id), {"bare_group_field", "qualified_output_scope", "ambiguous_group_field"})
        validate_gold(self.source, items, "")
        for item in items:
            self.assertEqual(item["split"], "regression")
            self.assertEqual(len(item["gold"]["required_findings"]), 3)
            self.assertTrue(item["gold"]["forbidden_claims"])
            self.assertNotIn("score", item)
            self.assertNotIn("passed", item)
            self.assertNotIn("fake_correct_answer", item)
            spans = _required_ranges(item)
            context = _reviewed_context(self.source, item, "")
            self.assertTrue(all(_range_covered(span, context["sources"]) for span in spans))
            for span in spans:
                self.assertEqual(span["source_sha256"], suite.digest(self.source / span["path"]))

        bare = by_id["bare_group_field"]
        self.assertEqual(bare["expected_source_paths"], ["synthetic/record.cbl"])
        self.assertIn("bare_group_field", bare["coverage"])
        bare_context = _reviewed_context(self.source, bare, "")
        bare_text = "\n".join(row["excerpt"] for row in bare_context["sources"])
        literal = "COMPUTE VALUE-00000 = VALUE-00001 * 2."
        self.assertIn(literal, bare_text)
        facts = assignment_facts(literal)
        self.assertEqual(facts.reads, ("VALUE-00001",))
        self.assertEqual(facts.writes, ("VALUE-00000",))
        self.assertEqual(dict(facts.expressions)["VALUE-00000"], "VALUE-00001 * 2")
        record = self.source / "synthetic/record.cbl"
        ir = BuiltinParserAdapter().parse(record.read_text(), relative_path="synthetic/record.cbl",
                                         source_sha256=suite.digest(record))
        declarations = {item.item_id: item for item in ir.data_items}
        formula = next(statement for statement in ir.statements if statement.statement_type == "COMPUTE")
        self.assertEqual(formula.binding_status, "bound")
        for references, name in ((formula.reads, "VALUE-00001"), (formula.writes, "VALUE-00000")):
            self.assertEqual(len(references), 1)
            self.assertEqual(len(references[0].candidates), 1)
            item = declarations[references[0].candidates[0]]
            self.assertEqual((item.name, item.ancestors, item.picture), (name, ("SOURCE-RECORD",), "9(9)"))

        qualified = by_id["qualified_output_scope"]
        self.assertIn("qualified_in_alias", qualified["coverage"])
        for reference in ("AMOUNT IN OUTPUT-REC", "AMOUNT IN INPUT-REC"):
            self.assertIn(reference, qualified["question"])
        self.assertEqual(tuple(ref.identity for ref in data_references("AMOUNT IN OUTPUT-REC")),
                         ("AMOUNT OF OUTPUT-REC",))
        qualified_context = _reviewed_context(self.source, qualified, "")
        qualified_text = "\n".join(row["excerpt"] for row in qualified_context["sources"])
        literal = "COMPUTE AMOUNT OF OUTPUT-REC = AMOUNT OF INPUT-REC * 2 END-COMPUTE."
        self.assertIn(literal, qualified_text)
        facts = assignment_facts(literal)
        self.assertEqual(facts.reads, ("AMOUNT OF INPUT-REC",))
        self.assertEqual(facts.writes, ("AMOUNT OF OUTPUT-REC",))
        self.assertEqual(dict(facts.expressions)["AMOUNT OF OUTPUT-REC"], "AMOUNT OF INPUT-REC * 2")

        ambiguous = by_id["ambiguous_group_field"]
        self.assertEqual(ambiguous["question"], "SEMANTIC-CASE AMOUNT 怎样计算？")
        self.assertIn("ambiguous_group_field", ambiguous["coverage"])
        front = self.source / "synthetic/frontend.cbl"
        ir = BuiltinParserAdapter().parse(front.read_text(), relative_path="synthetic/frontend.cbl",
                                         source_sha256=suite.digest(front))
        candidates = {item.qualified_name for item in ir.data_items if item.name == "AMOUNT"}
        self.assertEqual(candidates, {"AMOUNT OF INPUT-REC", "AMOUNT OF OUTPUT-REC"})
        gold_text = "\n".join(row["description"] for row in ambiguous["gold"]["required_findings"])
        self.assertTrue(all(name in gold_text for name in candidates))
        self.assertIn("不能自动任选", gold_text)
        self.assertIn("* 2", gold_text)
        self.assertEqual(ambiguous["expected_source_paths"], ["synthetic/frontend.cbl"])

    def test_group_reference_gold_rejects_changed_record_and_missing_qualified_formula(self):
        items = suite.record_binding_cases(self.source)
        bare = next(item for item in items if item["id"] == "bare_group_field")
        record = self.source / "synthetic/record.cbl"
        original = record.read_bytes()
        record.write_bytes(original.replace(b"* 2.", b"* 999."))
        with self.assertRaisesRegex(ValueError, "REVIEWED_SOURCE_CHANGED"):
            _reviewed_context(self.source, bare, "")
        with self.assertRaisesRegex(ValueError, "ANCHOR_MISSING"):
            suite.record_binding_cases(self.source)
        record.write_bytes(original)
        front = self.source / "synthetic/frontend.cbl"
        front.write_bytes(front.read_bytes().replace(
            b"COMPUTE AMOUNT OF OUTPUT-REC = AMOUNT OF INPUT-REC * 2", b"DISPLAY AMOUNT OF OUTPUT-REC"))
        with self.assertRaisesRegex(ValueError, "ANCHOR_MISSING"):
            suite.record_binding_cases(self.source)

    def test_actual_native_report_conversation_has_source_bound_control_and_value_gold(self):
        items = suite.report_conversation_cases(self.source)
        self.assertEqual(len(items), 1)
        case = items[0]
        self.assertEqual(case["id"], "report-summary-multiturn")
        self.assertEqual([turn["question"] for turn in case["turns"]], CONVERSATION_QUESTIONS)
        self.assertTrue({"value_initialization_source", "page_account_multiturn"} <= set(case["coverage"]))
        report = self.source / "synthetic/report.cbl"
        self.assertEqual(report.read_bytes(), _conversation_source().encode("utf-8"))
        validate_gold(self.source, items, "")
        ir = BuiltinParserAdapter().parse(report.read_text(encoding="utf-8"),
            relative_path="synthetic/report.cbl", source_sha256=suite.digest(report))
        declarations = {item.name: item for item in ir.data_items}
        self.assertEqual(declarations["TRAN-AMT"].value, "25.00")
        for name in ("PAGE-SUM", "ACCOUNT-SUM", "GRAND-SUM"):
            self.assertEqual(declarations[name].value, "0")
        adds = assignment_facts("ADD TRAN-AMT TO PAGE-SUM ACCOUNT-SUM.")
        self.assertEqual(set(adds.reads), {"TRAN-AMT", "PAGE-SUM", "ACCOUNT-SUM"})
        self.assertEqual(set(adds.writes), {"PAGE-SUM", "ACCOUNT-SUM"})
        for turn in case["turns"]:
            merged = {**case, **turn}
            self.assertNotIn("score", merged)
            self.assertNotIn("passed", merged)
            self.assertNotIn("history", merged)
            spans = _required_ranges(merged)
            context = _reviewed_context(self.source, merged, "")
            self.assertTrue(all(_range_covered(span, context["sources"]) for span in spans))
            self.assertEqual({span["path"] for span in spans}, {"synthetic/report.cbl"})
            self.assertTrue(all(span["source_sha256"] == suite.digest(report) for span in spans))
            text = "\n".join(row["excerpt"] for row in context["sources"])
            for actual in ("WORKING-STORAGE SECTION.", "VALUE 25.00", "VALUE 0",
                           "ADD TRAN-AMT TO PAGE-SUM ACCOUNT-SUM.",
                           "IF CURR-CARD NOT = TRAN-CARD", "IF FIRST-ITEM = 'N'",
                           "MOVE ACCOUNT-SUM TO REPORT-ACCOUNT-SUM.", "MOVE 0 TO ACCOUNT-SUM."):
                self.assertIn(actual, text)
            gold = "\n".join(row["description"] for row in turn["gold"]["required_findings"])
            self.assertIn("首次激活", gold)
            self.assertIn("不能", gold)
            self.assertTrue(turn["gold"]["forbidden_claims"])
        first = _reviewed_context(self.source, {**case, **case["turns"][0]}, "")
        first_text = "\n".join(row["excerpt"] for row in first["sources"])
        for actual in ("IF LINE-COUNT >= PAGE-SIZE", "MOVE PAGE-SUM TO REPORT-PAGE-SUM.",
                       "ADD PAGE-SUM TO GRAND-SUM.", "MOVE 0 TO PAGE-SUM."):
            self.assertIn(actual, first_text)
        for turn in case["turns"][1:]:
            focus = next(row for row in turn["gold"]["required_findings"]
                         if row["id"] == "report-second-account-focus")
            self.assertIn("前一轮第二种情况指ACCOUNT-SUM", focus["description"])

    def test_report_conversation_gold_does_not_accept_changed_value_or_removed_reset(self):
        items = suite.report_conversation_cases(self.source)
        case = items[0]
        report = self.source / "synthetic/report.cbl"
        original = report.read_bytes()
        report.write_bytes(original.replace(b"VALUE 25.00", b"VALUE 999.00"))
        with self.assertRaisesRegex(ValueError, "NATIVE_FIXTURE_CHANGED"):
            suite.report_conversation_cases(self.source)
        for turn in case["turns"]:
            with self.assertRaisesRegex(ValueError, "REVIEWED_SOURCE_CHANGED"):
                _reviewed_context(self.source, {**case, **turn}, "")
        report.write_bytes(original.replace(b"MOVE 0 TO ACCOUNT-SUM.", b"DISPLAY ACCOUNT-SUM."))
        with self.assertRaisesRegex(ValueError, "NATIVE_FIXTURE_CHANGED"):
            suite.report_conversation_cases(self.source)
        with self.assertRaisesRegex(ValueError, "REVIEWED_SOURCE_CHANGED"):
            validate_gold(self.source, items, "")

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

    def test_generated_delivery_is_deterministic_and_has_31_frozen_turns(self):
        public, manifest = self.fake_public_root()
        with patch.object(suite, "PUBLIC_MANIFEST", manifest):
            first = suite.generate_suite(self.root / "suite-a", public)
            second = suite.generate_suite(self.root / "suite-b", public)
        self.assertEqual(first["user_turn_count"], 31)
        self.assertEqual(first["maximum_model_requests"], 93)
        self.assertEqual(first["source_files"], 21)
        self.assertEqual(suite.digest(first["cases"]), suite.digest(second["cases"]))
        self.assertEqual(suite.digest(first["lock"]), suite.digest(second["lock"]))
        document = json.loads(first["cases"].read_text(encoding="utf-8"))
        self.assertEqual(len(document["cases"]), 27)
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
        self.assertEqual(result["user_turn_count"], 36)
        self.assertEqual(result["maximum_model_requests"], 108)
        self.assertEqual(result["source_files"], 12045)
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
