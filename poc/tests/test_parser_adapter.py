from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from dataclasses import replace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from compilation_context import CompilationContext, resolve_compilation_context
from parser_adapter import BuiltinParserAdapter, IsolatedParserAdapter, PROLEAP, CHE4Z, compare_normalized_ir, expression_tree
from semantic_ir import bind_reference, compilation_ir_from_dict, overlapping_region_ids


def source(body):
    return ("IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRY.\nDATA DIVISION.\n"
        "WORKING-STORAGE SECTION.\n01 INPUT-REC.\n05 AMOUNT PIC S9(5)V99 COMP-3.\n"
        "01 OUTPUT-REC.\n05 AMOUNT PIC 9(5)V99.\n01 ROWS-REC.\n"
        "05 ROW-ITEM OCCURS 3 TIMES.\n10 ROW-AMOUNT PIC 9(4).\n"
        "01 ALIAS-REC REDEFINES ROWS-REC.\n05 RAW-TEXT PIC X(12).\n"
        "PROCEDURE DIVISION.\nMAIN.\n" + body + "\nGOBACK.\n")


class ParserAdapterTests(unittest.TestCase):
    def test_known_compilation_branch_masks_inactive_declarations_and_writes(self):
        text = "IDENTIFICATION DIVISION.\nPROGRAM-ID. TRIALPG.\nDATA DIVISION.\nWORKING-STORAGE SECTION.\n" \
            ">>IF FEATURE\n01 INPUT-VALUE PIC 9.\n>>ELSE\n01 DISABLED-VALUE PIC 9.\n>>END-IF\n" \
            "01 RESULT-VALUE PIC 9.\nPROCEDURE DIVISION.\nMAIN.\n>>IF FEATURE\n" \
            "MOVE INPUT-VALUE TO RESULT-VALUE\n>>ELSE\nMOVE 999 TO RESULT-VALUE\n>>END-IF\nGOBACK."
        for format_name in ("free", "fixed"):
            with self.subTest(format_name=format_name):
                physical = text if format_name == "free" else "\n".join("       " + line for line in text.splitlines())
                ir = BuiltinParserAdapter().parse(physical, relative_path="entry.cbl",
                    context=CompilationContext(source_format=format_name, compile_conditions=(("FEATURE", True),)))
                self.assertNotIn("DISABLED-VALUE", {item.name for item in ir.data_items})
                self.assertFalse(any("999" in row.text for row in ir.statements))
                assignment = next(row for row in ir.statements if row.statement_type == "MOVE")
                self.assertEqual(assignment.binding_status, "bound")
                self.assertEqual(assignment.compilation_status, "active")

    def test_unknown_compilation_preserves_candidates_without_unique_binding(self):
        text = "DATA DIVISION.\nWORKING-STORAGE SECTION.\n>>IF FEATURE\n01 SOURCE-VALUE PIC 9.\n" \
            ">>END-IF\n01 RESULT-VALUE PIC 9.\nPROCEDURE DIVISION.\nMAIN.\n" \
            ">>IF FEATURE\nMOVE SOURCE-VALUE TO RESULT-VALUE\n>>END-IF\nGOBACK."
        ir = BuiltinParserAdapter().parse(text, relative_path="entry.cbl")
        declared = next(item for item in ir.data_items if item.name == "SOURCE-VALUE")
        statement = next(row for row in ir.statements if row.statement_type == "MOVE")
        self.assertEqual(declared.declaration_status, "parsed")
        self.assertEqual(declared.compilation_status, "candidate")
        self.assertEqual(statement.parsed_status, "parsed")
        self.assertEqual(statement.binding_status, "candidate")
        self.assertEqual(statement.reads[0].binding_status, "candidate")
        self.assertEqual(statement.flow_status, "not_established")
        self.assertTrue(any(row["reason"] == "compile_condition_unknown" for row in ir.diagnostics))
    def test_arithmetic_ir_preserves_decimal_text_precedence_and_left_associativity(self):
        exponent = expression_tree("-A ** 2 ** 3")
        self.assertEqual(exponent.value, "**")
        self.assertEqual(exponent.operands[0].value, "**")
        self.assertEqual(exponent.operands[0].operands[0].kind, "unary")
        amount = expression_tree("0.10 + RATE * 2")
        self.assertEqual(amount.operands[0].value, "0.10")
        self.assertEqual(amount.operands[1].value, "*")
    def test_qualified_identity_declaration_and_three_independent_states(self):
        ir = BuiltinParserAdapter().parse(source("COMPUTE AMOUNT OF OUTPUT-REC = AMOUNT OF INPUT-REC * 2."),
                                        relative_path="entry.cbl")
        statement = ir.statements[0]
        self.assertTrue(ir.parse_complete)
        self.assertEqual(statement.reads[0].identity, "AMOUNT OF INPUT-REC")
        self.assertEqual(statement.writes[0].identity, "AMOUNT OF OUTPUT-REC")
        self.assertNotEqual(statement.reads[0].candidates, statement.writes[0].candidates)
        self.assertEqual((statement.parsed_status, statement.binding_status, statement.flow_status),
                         ("parsed", "bound", "not_established"))
        declared = next(item for item in ir.data_items if item.name == "AMOUNT" and item.ancestors == ("INPUT-REC",))
        self.assertEqual((declared.picture, declared.usage, declared.scale, declared.digits), ("S9(5)V99", "COMP-3", 2, 7))
        self.assertEqual(statement.expressions[0][1].operands[1].value, "2")
        self.assertEqual(bind_reference("AMOUNT", ir.data_items).binding_status, "ambiguous")
        self.assertEqual(bind_reference("AMOUNT OF ABSENT", ir.data_items).binding_status, "unresolved")
        self.assertEqual(compilation_ir_from_dict(json.loads(json.dumps(ir.to_dict()))).fingerprint, ir.fingerprint)

    def test_rule_shape_is_reused_but_same_line_and_branch_occurrences_are_distinct(self):
        ir = BuiltinParserAdapter().parse(source("IF AMOUNT OF INPUT-REC > 0\n"
            "MOVE 0 TO AMOUNT OF OUTPUT-REC MOVE 0 TO AMOUNT OF OUTPUT-REC\nELSE\n"
            "MOVE 0 TO AMOUNT OF OUTPUT-REC\nEND-IF."), relative_path="entry.cbl")
        writes = [statement for statement in ir.statements if statement.statement_type == "MOVE"]
        self.assertEqual(len(writes), 3)
        self.assertEqual(len({statement.occurrence_id for statement in writes}), 3)
        self.assertEqual(len({statement.rule_shape_id for statement in writes}), 1)
        self.assertNotEqual(writes[0].source_refs[0].start_column, writes[1].source_refs[0].start_column)
        self.assertNotEqual(writes[0].scope, writes[2].scope)

    def test_arrays_group_aliases_and_unknown_layout_are_conservative(self):
        ir = BuiltinParserAdapter().parse(source("MOVE ROW-AMOUNT(2) TO AMOUNT OF OUTPUT-REC."), relative_path="entry.cbl")
        row = next(item for item in ir.data_items if item.name == "ROW-AMOUNT")
        region = next(region for region in ir.storage_regions if region.item_id == row.item_id)
        self.assertTrue(region.array)
        self.assertIsNone(region.offset_bytes)
        alias = next(item for item in ir.data_items if item.name == "ALIAS-REC")
        alias_region = next(region for region in ir.storage_regions if region.item_id == alias.item_id)
        self.assertEqual(alias_region.root_id, region.root_id)
        self.assertIn(alias_region.region_id, overlapping_region_ids(ir.storage_regions, region.region_id))
        self.assertIn("array_or_reference_modification_region_not_resolved", ir.statements[0].diagnostics)

    def test_copy_instances_replacement_trace_and_raw_hash_are_retained(self):
        text = "MOVE 1 TO AMOUNT.\nMOVE 1 TO AMOUNT."
        mapping = [{"origin": {"relative_path": "step.cpy", "source_hash": "raw-copy-hash", "line": 1},
                    "include_chain": [{"relative_path": "entry.cbl", "line": number}],
                    "replacement_trace": [{"from": "VALUE", "to": "AMOUNT"}]} for number in (10, 20)]
        ir = BuiltinParserAdapter().parse(text, relative_path="entry.cbl", source_map=mapping)
        refs = [statement.source_refs[0] for statement in ir.statements]
        self.assertEqual({ref.source_sha256 for ref in refs}, {"raw-copy-hash"})
        self.assertNotEqual(refs[0].copy_instance, refs[1].copy_instance)
        self.assertEqual(refs[0].replacement_trace[0]["from"], "VALUE")
        self.assertEqual(refs[0].coordinate_status, "mapped_line_only")

    def test_legal_fixed_continued_literal_keeps_expression_and_physical_range(self):
        text = "000100 01 RESULT PIC X(10).\n000200 PROCEDURE DIVISION.\n" \
               "000300 MOVE 'ab\n000400-    'cd' TO RESULT.\n000500 GOBACK."
        ir = BuiltinParserAdapter().parse(text, relative_path="entry.cbl", context=CompilationContext(source_format="fixed"))
        self.assertTrue(ir.parse_complete)
        statement = ir.statements[0]
        self.assertEqual((statement.source_refs[0].start_line, statement.source_refs[0].end_line), (3, 4))
        self.assertEqual(statement.expressions[0][1].value, "'abcd'")

    def test_unknown_syntax_reports_boundary_without_promoting_flow(self):
        ir = BuiltinParserAdapter().parse(source("MOVE 0 TO AMOUNT OF OUTPUT-REC.\n"
            "ALTER OLD-PATH TO PROCEED TO NEW-PATH.\nCOMPUTE AMOUNT OF OUTPUT-REC = 999."), relative_path="entry.cbl")
        self.assertFalse(ir.parse_complete)
        self.assertTrue(ir.diagnostics)
        self.assertEqual(len(ir.statements), 1)
        self.assertEqual(ir.statements[0].flow_status, "not_established")

    def test_compilation_options_have_stable_layer_cache_identity(self):
        context = resolve_compilation_context({"copy_libraries": ["a", "b"], "copy_library_order_known": True,
                                               "compile_conditions": {"FEATURE": True}})
        self.assertEqual(context.condition("feature"), True)
        self.assertIsNone(context.condition("unknown"))
        self.assertNotEqual(context.fingerprint, replace(context, copy_libraries=("b", "a")).fingerprint)
        self.assertNotEqual(context.fingerprint, replace(context, framework_version="manual-b").fingerprint)
        with self.assertRaises(ValueError):
            CompilationContext(copy_libraries=("../outside",))

    def test_uninstalled_external_candidates_are_explicitly_unavailable(self):
        for spec in (PROLEAP, CHE4Z):
            with self.subTest(spec=spec):
                ir = IsolatedParserAdapter(spec).parse("MOVE A TO B.", relative_path="entry.cbl")
                self.assertFalse(ir.parse_complete)
                self.assertEqual(ir.diagnostics[0]["reason"], "parser_unavailable")
                self.assertEqual(compare_normalized_ir(BuiltinParserAdapter().parse("MOVE A TO B.",
                    relative_path="entry.cbl"), ir)["counts"], {"correct": 0, "unsupported": 1, "incorrect": 0})

    def test_isolated_json_bridge_checks_artifacts_version_and_source_mapping(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = Path(folder) / "bridge.py"
            candidate = BuiltinParserAdapter().parse("MOVE A TO B.", relative_path="entry.cbl").to_dict()
            candidate["adapter"] = {"name": PROLEAP.name, "version": PROLEAP.version}
            def write_bridge(parser_version=None, mapped_hash=None):
                result = json.loads(json.dumps(candidate))
                if mapped_hash:
                    result["statements"][0]["source_refs"][0]["source_sha256"] = mapped_hash
                spec = {"name": PROLEAP.name, "version": parser_version or PROLEAP.version,
                        "upstream": PROLEAP.upstream, "release": PROLEAP.release}
                bridge.write_text("import json, sys\nrequest=json.load(sys.stdin)\nprint(" +
                    repr(json.dumps({"schema_version": "parser-bridge/v1", "parser": spec, "ir": result})) + ")\n", encoding="utf-8")
                return {str(Path(sys.executable).resolve()): hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest(),
                        str(bridge): hashlib.sha256(bridge.read_bytes()).hexdigest()}
            hashes = write_bridge()
            adapter = IsolatedParserAdapter(PROLEAP, command=(sys.executable, str(bridge)), artifact_hashes=hashes)
            ir = adapter.parse("MOVE A TO B.", relative_path="entry.cbl")
            self.assertTrue(ir.parse_complete)
            self.assertEqual(compare_normalized_ir(BuiltinParserAdapter().parse("MOVE A TO B.",
                relative_path="entry.cbl"), ir)["counts"]["correct"], 1)
            changed = write_bridge(parser_version="unverified")
            self.assertEqual(adapter.parse("MOVE A TO B.", relative_path="entry.cbl").diagnostics[0]["reason"], "parser_artifact_hash_mismatch")
            mismatch = IsolatedParserAdapter(PROLEAP, command=(sys.executable, str(bridge)), artifact_hashes=changed)
            self.assertEqual(mismatch.parse("MOVE A TO B.", relative_path="entry.cbl").diagnostics[0]["reason"], "parser_bridge_version_mismatch")
            hashes = write_bridge(mapped_hash="wrong-version")
            mismatch = IsolatedParserAdapter(PROLEAP, command=(sys.executable, str(bridge)), artifact_hashes=hashes)
            self.assertEqual(mismatch.parse("MOVE A TO B.", relative_path="entry.cbl").diagnostics[0]["reason"], "parser_source_mapping_mismatch")


if __name__ == "__main__":
    unittest.main()
