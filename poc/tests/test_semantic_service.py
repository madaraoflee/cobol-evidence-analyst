from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from structural_index import build_structural_index
from semantic_service import FactStore, SemanticService


class SemanticServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "facts.sqlite"

    def program(self, path, name, body, *, linkage=False):
        storage = "LINKAGE SECTION." if linkage else "WORKING-STORAGE SECTION."
        self.root.joinpath(path).write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. " + name +
            ".\nDATA DIVISION.\n" + storage + "\n01 IN-AMOUNT PIC 9(5).\n01 OUT-AMOUNT PIC 9(5).\n"
            "01 FLAG PIC 9.\nPROCEDURE DIVISION" + (" USING IN-AMOUNT OUT-AMOUNT" if linkage else "") +
            ".\nMAIN.\n" + body + "\n", encoding="utf-8")

    def build(self, path):
        build_structural_index(self.root, self.db, source_format="free")
        return SemanticService(FactStore(self.db, self.root, source_format="free")).build(path)

    def node(self, service, text):
        return next(key for key, node in service.nodes.items() if text in node.statement and node.kind not in {"IF", "EVALUATE"})

    def test_reaching_definition_excludes_overwritten_formula(self):
        self.program("main.cbl", "ENTRYPOINT", "MOVE 3 TO IN-AMOUNT\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2\n"
            "MOVE 0 TO OUT-AMOUNT\nMOVE OUT-AMOUNT TO IN-AMOUNT\nGOBACK.")
        service = self.build("main.cbl")
        final_read = self.node(service, "MOVE OUT-AMOUNT TO IN-AMOUNT")
        overwrite = self.node(service, "MOVE 0 TO OUT-AMOUNT")
        formula = self.node(service, "COMPUTE OUT-AMOUNT")
        self.assertEqual(service.reaching_in[final_read]["OUT-AMOUNT"], {overwrite})
        self.assertNotIn(formula, service.backward_slice(final_read)["occurrences"])
        self.assertIn(overwrite, service.explain_assignment(formula)["later_writes"])

    def test_mutually_exclusive_branches_merge_definitions(self):
        self.program("main.cbl", "ENTRYPOINT", "IF FLAG = 1 MOVE 10 TO OUT-AMOUNT ELSE MOVE 20 TO OUT-AMOUNT END-IF\n"
            "MOVE OUT-AMOUNT TO IN-AMOUNT\nGOBACK.")
        service = self.build("main.cbl")
        read = self.node(service, "MOVE OUT-AMOUNT TO IN-AMOUNT")
        self.assertEqual(len(service.reaching_in[read]["OUT-AMOUNT"]), 2)

    def test_section_perform_and_occurrence_context_remain_distinct(self):
        self.program("main.cbl", "ENTRYPOINT", "PERFORM CALC-SECTION\nPERFORM CALC-SECTION\nGOBACK.\n"
            "CALC-SECTION SECTION.\nCALC-ONE.\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2.")
        service = self.build("main.cbl")
        writes = [node for node in service.nodes.values() if node.kind == "COMPUTE"]
        self.assertEqual(len(writes), 2)
        self.assertNotEqual(writes[0].occurrence_id, writes[1].occurrence_id)
        self.assertNotEqual(writes[0].context, writes[1].context)

    def test_unknown_call_cannot_claim_unchanged_output(self):
        self.program("main.cbl", "ENTRYPOINT", "MOVE 3 TO OUT-AMOUNT\nCALL 'EXTERNAL'\nMOVE OUT-AMOUNT TO IN-AMOUNT\nGOBACK.")
        service = self.build("main.cbl")
        read = self.node(service, "MOVE OUT-AMOUNT TO IN-AMOUNT")
        self.assertEqual(len(service.reaching_in[read]["OUT-AMOUNT"]), 2)
        self.assertEqual(service.frontier[0]["affected_storage"], "all_visible_storage")
        self.assertTrue(service.summary("main.cbl")["conditional_outputs"])

    def test_three_program_static_chain_and_normal_return(self):
        self.program("main.cbl", "ENTRYPOINT", "MOVE 4 TO IN-AMOUNT\nCALL 'MIDDLE' USING IN-AMOUNT OUT-AMOUNT\n"
            "MOVE OUT-AMOUNT TO IN-AMOUNT\nGOBACK.")
        self.program("middle.cbl", "MIDDLE", "CALL 'LEAF' USING IN-AMOUNT OUT-AMOUNT\nGOBACK.", linkage=True)
        self.program("leaf.cbl", "LEAF", "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2\nGOBACK.", linkage=True)
        service = self.build("main.cbl")
        read = self.node(service, "MOVE OUT-AMOUNT TO IN-AMOUNT")
        formula = self.node(service, "COMPUTE OUT-AMOUNT")
        self.assertEqual(service.reaching_in[read]["OUT-AMOUNT"], {formula})
        self.assertIn(formula, service.backward_slice(read)["occurrences"])
        self.assertEqual(service.summary("main.cbl")["dependencies"], ["LEAF", "MIDDLE"])
        self.assertLess(service.facts.query_count, 15)

    def test_test_before_loop_keeps_zero_iteration_and_loop_carried_definitions(self):
        self.program("main.cbl", "ENTRYPOINT", "MOVE 3 TO OUT-AMOUNT\nPERFORM UNTIL FLAG = 1\n"
            "MOVE 5 TO OUT-AMOUNT\nEND-PERFORM\nMOVE OUT-AMOUNT TO IN-AMOUNT\nGOBACK.")
        service = self.build("main.cbl")
        read = self.node(service, "MOVE OUT-AMOUNT TO IN-AMOUNT")
        self.assertEqual(len(service.reaching_in[read]["OUT-AMOUNT"]), 2)
        self.assertTrue(any(edge[2] == "loop_back" for edge in service.edges))
        self.assertFalse(service.frontier)
        self.assertIn("LOOP_TEST", {node.kind for node in service.nodes.values()})

    def test_test_after_loop_executes_body_before_first_test(self):
        self.program("main.cbl", "ENTRYPOINT", "MOVE 3 TO OUT-AMOUNT\nPERFORM WITH TEST AFTER UNTIL FLAG = 1\n"
            "MOVE 5 TO OUT-AMOUNT\nEND-PERFORM\nMOVE OUT-AMOUNT TO IN-AMOUNT\nGOBACK.")
        service = self.build("main.cbl")
        read = self.node(service, "MOVE OUT-AMOUNT TO IN-AMOUNT")
        self.assertEqual({service.nodes[key].statement for key in service.reaching_in[read]["OUT-AMOUNT"]}, {"MOVE 5 TO OUT-AMOUNT"})
        self.assertTrue(any(edge[2] == "loop_test_after" for edge in service.edges))

    def test_varying_loop_has_initialization_increment_and_bound_reads(self):
        self.program("main.cbl", "ENTRYPOINT", "PERFORM VARYING FLAG FROM 1 BY 1 UNTIL FLAG > 3\n"
            "ADD IN-AMOUNT TO OUT-AMOUNT\nEND-PERFORM\nGOBACK.")
        service = self.build("main.cbl")
        self.assertTrue({"LOOP_INITIALIZE", "LOOP_INCREMENT", "LOOP_TEST"} <= {node.kind for node in service.nodes.values()})
        self.assertFalse(service.frontier)

    def test_size_error_and_normal_handlers_merge_real_receiver_writes(self):
        self.program("main.cbl", "ENTRYPOINT", "COMPUTE OUT-AMOUNT ROUNDED = IN-AMOUNT * 2\n"
            "ON SIZE ERROR MOVE ZERO TO OUT-AMOUNT\nNOT ON SIZE ERROR MOVE 1 TO FLAG\nEND-COMPUTE\n"
            "MOVE OUT-AMOUNT TO IN-AMOUNT\nGOBACK.")
        service = self.build("main.cbl")
        read = self.node(service, "MOVE OUT-AMOUNT TO IN-AMOUNT")
        definitions = service.reaching_in[read]["OUT-AMOUNT"]
        self.assertEqual({service.nodes[key].kind for key in definitions}, {"COMPUTE", "MOVE"})
        self.assertTrue({"size_error", "not_size_error"} <= {edge[2] for edge in service.edges})
        normal = next(node for node in service.nodes.values() if node.kind == "COMPUTE")
        self.assertEqual(normal.numeric_semantics["OUT-AMOUNT"]["receiving_scale"], 0)
        self.assertEqual(normal.numeric_semantics["OUT-AMOUNT"]["on_size_error_receiver"], "preserved")
        self.assertFalse(service.frontier)

    def test_exit_perform_skips_remaining_body_and_continues_after_loop(self):
        self.program("main.cbl", "ENTRYPOINT", "PERFORM UNTIL FLAG = 1\nMOVE 2 TO OUT-AMOUNT\nEXIT PERFORM\n"
            "MOVE 9 TO OUT-AMOUNT\nEND-PERFORM\nMOVE OUT-AMOUNT TO IN-AMOUNT\nGOBACK.")
        service = self.build("main.cbl")
        self.assertFalse(any(node.statement == "MOVE 9 TO OUT-AMOUNT" for node in service.nodes.values()))
        self.assertTrue(any(node.statement == "MOVE OUT-AMOUNT TO IN-AMOUNT" for node in service.nodes.values()))

    def test_exit_paragraph_and_section_preserve_branch_specific_paths(self):
        self.program("main.cbl", "ENTRYPOINT", "PERFORM CALC-SECTION\nGOBACK.\nCALC-SECTION SECTION.\n"
            "FIRST-PARA.\nIF FLAG = 1 EXIT SECTION END-IF\nMOVE 2 TO OUT-AMOUNT\nEXIT PARAGRAPH\n"
            "MOVE 9 TO OUT-AMOUNT.\nSECOND-PARA.\nMOVE 3 TO OUT-AMOUNT.")
        service = self.build("main.cbl")
        self.assertFalse(any(node.statement == "MOVE 9 TO OUT-AMOUNT" for node in service.nodes.values()))
        later = next(key for key, node in service.nodes.items() if node.statement == "MOVE 3 TO OUT-AMOUNT")
        early_exit = next(key for key, node in service.nodes.items() if node.statement == "EXIT SECTION")
        self.assertNotIn(later, service._descendants(early_exit))
        self.assertTrue(any(node.statement == "MOVE 2 TO OUT-AMOUNT" for node in service.nodes.values()))

    def test_actual_source_version_must_match_indexed_declaration_layout(self):
        self.program("main.cbl", "ENTRYPOINT", "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2\nGOBACK.")
        build_structural_index(self.root, self.db, source_format="free")
        source = self.root / "main.cbl"
        source.write_text(source.read_text().replace("PIC 9(5)", "PIC 9(7)"))
        with self.assertRaisesRegex(ValueError, "source_version_mismatch"):
            FactStore(self.db, self.root, source_format="free").program("main.cbl")

    def custom(self, declarations, body):
        self.root.joinpath("main.cbl").write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRYPOINT.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n" + declarations + "\nPROCEDURE DIVISION.\nMAIN.\n" + body + "\n")
        return self.build("main.cbl")

    def test_simple_and_qualified_reference_share_one_declared_identity(self):
        service = self.custom("01 RECORD-AREA.\n05 AMOUNT PIC 9(5).\n01 RESULT-VALUE PIC 9(5).",
            "MOVE 3 TO AMOUNT\nMOVE AMOUNT OF RECORD-AREA TO RESULT-VALUE\nGOBACK.")
        write = self.node(service, "MOVE 3 TO AMOUNT")
        read = self.node(service, "MOVE AMOUNT OF RECORD-AREA")
        self.assertEqual(service.nodes[read].reads, ("AMOUNT",))
        self.assertEqual(service.reaching_in[read]["AMOUNT"], {write})
        self.assertIn(write, service.backward_slice(read)["occurrences"])

    def test_group_write_reaches_each_subordinate_read(self):
        service = self.custom("01 INPUT-AREA.\n05 AMOUNT PIC 9(5).\n05 FLAG PIC 9.\n01 OUTPUT-AREA.\n"
            "05 AMOUNT PIC 9(5).\n05 FLAG PIC 9.\n01 RESULT-VALUE PIC 9(5).",
            "MOVE 1 TO AMOUNT OF OUTPUT-AREA\nMOVE INPUT-AREA TO OUTPUT-AREA\nMOVE AMOUNT OF OUTPUT-AREA TO RESULT-VALUE\nGOBACK.")
        group = self.node(service, "MOVE INPUT-AREA TO OUTPUT-AREA")
        read = self.node(service, "MOVE AMOUNT OF OUTPUT-AREA TO RESULT-VALUE")
        self.assertEqual(service.reaching_in[read]["AMOUNT OF OUTPUT-AREA"], {group})
        self.assertTrue(service.nodes[group].storage_effects["OUTPUT-AREA"]["group"])

    def test_redefinition_write_preserves_alternative_prior_definition(self):
        service = self.custom("01 BASE-VALUE PIC 9(5).\n01 ALIAS-VALUE REDEFINES BASE-VALUE PIC 9(5).\n01 RESULT-VALUE PIC 9(5).",
            "MOVE 1 TO BASE-VALUE\nMOVE 2 TO ALIAS-VALUE\nMOVE BASE-VALUE TO RESULT-VALUE\nGOBACK.")
        first = self.node(service, "MOVE 1 TO BASE-VALUE")
        alias = self.node(service, "MOVE 2 TO ALIAS-VALUE")
        read = self.node(service, "MOVE BASE-VALUE TO RESULT-VALUE")
        self.assertEqual(service.reaching_in[read]["BASE-VALUE"], {"@entry:BASE-VALUE", first, alias})
        self.assertTrue(service.nodes[alias].unknown_effect)
        self.assertEqual(set(service.nodes[alias].unknown_storage), {"BASE-VALUE", "ALIAS-VALUE"})
        self.assertNotIn("RESULT-VALUE", service.nodes[alias].unknown_storage)

    def test_array_unknown_index_covers_its_region_and_keeps_direct_fact(self):
        service = self.custom("01 RECORD-AREA.\n05 AMOUNTS PIC 9(5) OCCURS 3 TIMES.\n01 IDX PIC 9.\n01 RESULT-VALUE PIC 9(5).",
            "MOVE 2 TO AMOUNTS(IDX)\nMOVE AMOUNTS(1) TO RESULT-VALUE\nGOBACK.")
        write = self.node(service, "MOVE 2 TO AMOUNTS(IDX)")
        read = self.node(service, "MOVE AMOUNTS(1) TO RESULT-VALUE")
        self.assertEqual(service.nodes[write].writes, ("AMOUNTS",))
        self.assertIn("IDX", service.nodes[write].reads)
        self.assertTrue(service.nodes[write].unknown_effect)
        self.assertIn(write, service.reaching_in[read]["AMOUNTS"])
        self.assertNotIn("RESULT-VALUE", service.nodes[write].unknown_storage)

    def test_initial_declaration_is_separate_from_external_and_live_entry_state(self):
        self.program("main.cbl", "ENTRYPOINT", "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2\nGOBACK.")
        path = self.root / "main.cbl"
        path.write_text(path.read_text().replace("IN-AMOUNT PIC 9(5).", "IN-AMOUNT PIC 9(5) VALUE 3."))
        service = self.build("main.cbl")
        summary = service.summary("main.cbl")
        self.assertEqual(summary["inputs"], [])
        self.assertEqual(summary["initial_definitions"]["IN-AMOUNT"]["value"], "3")
        self.assertEqual(summary["initial_definitions"]["IN-AMOUNT"]["status"], "initial_value_not_runtime_proof")
        self.assertIn("first_activation", summary["initial_definitions"]["IN-AMOUNT"]["activation_semantics"])

    def test_phase_cancellation_and_field_state_budgets_preserve_direct_syntax(self):
        self.program("main.cbl", "ENTRYPOINT", "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2\nGOBACK.")
        service = self.build("main.cbl")
        node = self.node(service, "COMPUTE OUT-AMOUNT")
        def cancel():
            raise RuntimeError("LOCAL_ANALYSIS_CANCELLED")
        service.check_cancel = cancel
        with self.assertRaisesRegex(RuntimeError, "LOCAL_ANALYSIS_CANCELLED"):
            service._reaching()
        with self.assertRaisesRegex(RuntimeError, "LOCAL_ANALYSIS_CANCELLED"):
            service.backward_slice(node)
        with self.assertRaisesRegex(RuntimeError, "LOCAL_ANALYSIS_CANCELLED"):
            service.forward_slice(node)
        bounded = SemanticService(service.facts, max_flow_fields=1).build("main.cbl")
        self.assertFalse(bounded._flow_complete)
        self.assertTrue(any(node.kind == "COMPUTE" for node in bounded.nodes.values()))
        self.assertEqual(bounded.frontier[-1]["reason"], "flow_field_or_edge_budget")

    def test_manifest_preserves_explicit_encoding_and_fixed_format(self):
        rows = ["IDENTIFICATION DIVISION.", "PROGRAM-ID. ENCODED-FLOW.", "DATA DIVISION.",
                "WORKING-STORAGE SECTION.", "01 INPUT-AMOUNT PIC 9(5).", "01 OUTPUT-AMOUNT PIC 9(5).",
                "PROCEDURE DIVISION.", "MAIN.", "COMPUTE OUTPUT-AMOUNT = INPUT-AMOUNT * 2.", "GOBACK."]
        path = self.root / "encoded.cbl"
        path.write_bytes(("\n".join(f"SEQ{index:03d} {line}" for index, line in enumerate(rows, 1)) + "\n").encode("cp037"))
        build_structural_index(self.root, self.db, encoding="cp037", source_format="fixed")
        facts = FactStore(self.db, self.root, source_manifest=[{"relative_path": "encoded.cbl", "encoding": "cp037", "source_format": "fixed"}])
        service = SemanticService(facts).build("encoded.cbl")
        compute = next(node for node in service.nodes.values() if node.kind == "COMPUTE")
        self.assertEqual(compute.reads, ("INPUT-AMOUNT",))
        self.assertEqual(compute.writes, ("OUTPUT-AMOUNT",))
        self.assertEqual(compute.source_refs[0]["start_column"], 8)

    def test_node_budget_keeps_partial_syntax_without_complete_flow_claim(self):
        self.program("main.cbl", "ENTRYPOINT", "MOVE 4 TO IN-AMOUNT\n"
                     "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2\nMOVE 0 TO OUT-AMOUNT\nGOBACK.")
        service = self.build("main.cbl")
        bounded = SemanticService(service.facts, max_nodes=2).build("main.cbl")
        self.assertEqual(len(bounded.nodes), 2)
        self.assertFalse(bounded._flow_complete)
        self.assertFalse(bounded.summary("main.cbl")["flow_complete"])
        formula = self.node(bounded, "COMPUTE OUT-AMOUNT")
        self.assertEqual(bounded.explain_assignment(formula)["flow_status"], "incomplete_source_flow")
        self.assertEqual(bounded.backward_slice(formula)["flow_status"], "incomplete_source_flow")
        self.assertEqual(bounded.forward_slice(formula)["flow_status"], "incomplete_source_flow")
        self.assertEqual(bounded.frontier[0]["reason"], "semantic_node_budget")
        self.assertEqual(bounded.frontier[0]["affected_storage"], "all_visible_storage")

    def test_known_disabled_compile_branch_does_not_enter_execution_flow(self):
        self.program("main.cbl", "ENTRYPOINT", ">>IF FEATURE-A\nMOVE 9 TO OUT-AMOUNT\n>>ELSE\nMOVE 2 TO OUT-AMOUNT\n>>END-IF\nGOBACK.")
        build_structural_index(self.root, self.db, source_format="free",
                               compilation_context={"compile_conditions": {"FEATURE-A": False}})
        service = SemanticService(FactStore(self.db, self.root, source_format="free")).build("main.cbl")
        moves = [node.statement for node in service.nodes.values() if node.kind == "MOVE"]
        self.assertEqual(moves, ["MOVE 2 TO OUT-AMOUNT"])
        self.assertFalse(service.frontier)
