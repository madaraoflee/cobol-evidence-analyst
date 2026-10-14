from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
from exception_cfg import build_exception_cfg
from statement_facts import assignment_facts, clean_cobol_line, data_references, dependency_facts, join_continuation, program_identifier
from statement_parser import parse_statements
from structural_index import SourceDocument, build_structural_index, normalize_cobol_lines, parse_document, read_source_document


HEADER = """IDENTIFICATION DIVISION.
PROGRAM-ID. SEMANTIC-CASE.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 A PIC 9(6)V99.
01 B PIC 9(6)V99.
01 C PIC 9(6)V99.
01 INPUT-REC.
 05 AMOUNT PIC 9(6)V99.
01 OUTPUT-REC.
 05 AMOUNT PIC 9(6)V99.
PROCEDURE DIVISION.
MAIN.
"""


def document(body):
    text = HEADER + body + "\n"
    lines, hint = normalize_cobol_lines(text, "free")
    return SourceDocument("semantic.cbl", hashlib.sha256(text.encode()).hexdigest(), "utf-8", False,
                          hint, "cobol_program", tuple(text.splitlines()), lines)


def relations(parsed, kind):
    return [edge.target_name for edge in parsed.relations if edge.relation_type == kind]


class FrontendSemanticRegressionTests(unittest.TestCase):
    def test_add_giving_reads_only_senders_and_writes_only_receivers(self):
        for source, expected_reads, expected_writes in (
            ("ADD A TO B GIVING C END-ADD.", ["A", "B"], ["C"]),
            ("ADD A TO B GIVING A END-ADD.", ["A", "B"], ["A"]),
            ("ADD A TO B END-ADD.", ["A", "B"], ["B"]),
            ("ADD A B GIVING C ROUNDED END-ADD.", ["A", "B"], ["C"]),
        ):
            with self.subTest(source=source):
                parsed = parse_document(document(source))
                self.assertEqual(relations(parsed, "READS"), expected_reads)
                self.assertEqual(relations(parsed, "WRITES"), expected_writes)
                ast = parse_statements(source)
                self.assertTrue(ast.complete, ast.boundaries)
                self.assertEqual(list(ast.statements[0].reads), expected_reads)
                self.assertEqual(list(ast.statements[0].writes), expected_writes)

    def test_scope_terminators_and_exception_bodies_are_not_operands(self):
        for source in ("COMPUTE C ROUNDED = A * 2 END-COMPUTE.", "ADD A TO B GIVING C ROUNDED END-ADD."):
            parsed = parse_document(document(source))
            self.assertFalse(any(name.startswith("END-") for name in relations(parsed, "READS") + relations(parsed, "WRITES")))
            for edge in parsed.relations:
                if edge.relation_type == "WRITES":
                    self.assertNotIn("END-", edge.metadata["expression"])
                    self.assertNotIn("ROUNDED", edge.metadata["expression"])
        ast = parse_statements("ADD A TO B GIVING C ON SIZE ERROR MOVE ZERO TO C END-ADD.")
        self.assertTrue(ast.complete, ast.boundaries)
        self.assertTrue(ast.statements[0].effects_unknown)
        self.assertEqual(ast.writes, ())
        facts = assignment_facts("ADD A TO B GIVING C ON SIZE ERROR MOVE ZERO TO C END-ADD.")
        self.assertTrue(facts.effects_unknown)
        self.assertEqual(facts.writes, ())

    def test_same_physical_line_has_independent_occurrences_and_access(self):
        parsed = parse_document(document("ADD A TO B GIVING C END-ADD MOVE C TO A COMPUTE B = A * 2 END-COMPUTE."))
        statements = [unit for unit in parsed.code_units if unit.unit_type == "Statement"]
        self.assertEqual([unit.name for unit in statements], ["ADD", "MOVE", "COMPUTE"])
        self.assertEqual(len({unit.unit_id for unit in statements}), 3)
        actual = []
        for unit in statements:
            actual.append((relations_for(parsed, unit, "READS"), relations_for(parsed, unit, "WRITES")))
        self.assertEqual(actual, [(["A", "B"], ["C"]), (["C"], ["A"]), (["A"], ["B"])])

    def test_display_literals_never_create_code_dependencies(self):
        body = "DISPLAY \"CALL 'GHOST' PERFORM LOST\".\nDISPLAY 'it''s CALL OTHER PERFORM LOST'."
        parsed = parse_document(document(body))
        self.assertFalse(any(edge.relation_type in {"CALLS", "CALL_TARGET_FROM", "PERFORMS", "PERFORMS_THRU"}
                             for edge in parsed.relations))
        self.assertEqual(dependency_facts(body), ())

    def test_quoted_comment_marker_is_preserved_in_both_frontends(self):
        source = "MOVE 'CALL *> PERFORM' TO A. *> CALL 'GHOST'"
        parsed = parse_document(document(source))
        move = next(unit for unit in parsed.code_units if unit.name == "MOVE")
        self.assertIn("'CALL *> PERFORM'", move.normalized_text)
        self.assertEqual(relations(parsed, "WRITES"), ["A"])
        self.assertEqual(relations(parsed, "READS"), [])
        self.assertFalse(any(edge.relation_type == "CALLS" for edge in parsed.relations))

    def test_qualified_fields_bind_to_their_exact_declared_storage(self):
        source = "COMPUTE AMOUNT OF OUTPUT-REC = AMOUNT OF INPUT-REC * 2 END-COMPUTE."
        parsed = parse_document(document(source))
        self.assertEqual(relations(parsed, "READS"), ["AMOUNT OF INPUT-REC"])
        self.assertEqual(relations(parsed, "WRITES"), ["AMOUNT OF OUTPUT-REC"])
        self.assertEqual([item.identity for item in data_references("AMOUNT IN INPUT-REC + AMOUNT OF OUTPUT-REC")],
                         ["AMOUNT OF INPUT-REC", "AMOUNT OF OUTPUT-REC"])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "semantic.cbl").write_text(HEADER + source, encoding="utf-8")
            for builder in (build_structural_index, build_business_index):
                database = root / (builder.__name__ + ".sqlite")
                builder(root, database, source_format="free", quiet=True)
                with closing(sqlite3.connect(database)) as connection:
                    if builder is build_structural_index:
                        rows = connection.execute("SELECT r.relation_type,r.target_name,r.status,s.qualified_name "
                            "FROM relations r LEFT JOIN symbols s ON s.symbol_id=r.target_entity_id "
                            "WHERE r.relation_type IN ('READS','WRITES')").fetchall()
                        self.assertEqual({(kind, name, status, qualified) for kind, name, status, qualified in rows}, {
                            ("READS", "AMOUNT OF INPUT-REC", "confirmed", "SEMANTIC-CASE::AMOUNT OF INPUT-REC"),
                            ("WRITES", "AMOUNT OF OUTPUT-REC", "confirmed", "SEMANTIC-CASE::AMOUNT OF OUTPUT-REC"),
                        })
                    else:
                        reads, writes = connection.execute("SELECT reads_json,writes_json FROM business_rules").fetchone()
                        self.assertEqual(json.loads(reads), ["AMOUNT OF INPUT-REC"])
                        self.assertEqual(json.loads(writes), ["AMOUNT OF OUTPUT-REC"])

    def test_section_perform_resolves_and_returns_after_all_section_paragraphs(self):
        body = "PERFORM CALCULATE-SECTION.\nGOBACK.\nCALCULATE-SECTION SECTION.\nFIRST-PARA.\nMOVE 1 TO A.\nSECOND-PARA.\nMOVE A TO C.\nNEXT-SECTION SECTION.\nMOVE 9 TO C.\n"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "semantic.cbl").write_text(HEADER + body, encoding="utf-8")
            for builder in (build_structural_index, build_business_index):
                database = root / (builder.__name__ + ".sqlite")
                builder(root, database, source_format="free", quiet=True)
                with closing(sqlite3.connect(database)) as connection:
                    relation = connection.execute("SELECT r.status,s.symbol_type,s.name FROM relations r "
                        "LEFT JOIN symbols s ON s.symbol_id=r.target_entity_id WHERE r.relation_type='PERFORMS'").fetchone()
                    self.assertEqual(relation, ("confirmed", "Section", "CALCULATE-SECTION"))
                if builder is build_structural_index:
                    cfg = build_exception_cfg(database, "SEMANTIC-CASE")
                    self.assertTrue(cfg["summary"]["supported_graph_closed"], cfg["boundaries"])
                    moves = [node for node in cfg["nodes"] if node["kind"] == "MOVE"]
                    self.assertEqual([(node["targets"], node["source"]) for node in moves], [(["A"], "1"), (["C"], "A")])

    def test_sparse_shape_deduplication_retains_branch_occurrences(self):
        source = "IF A > 0 ADD A TO B GIVING C END-ADD ELSE ADD A TO B GIVING C END-ADD END-IF."
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "semantic.cbl").write_text(HEADER + source, encoding="utf-8")
            database = root / "sparse.sqlite"
            build_business_index(root, database, source_format="free")
            with closing(sqlite3.connect(database)) as connection:
                rows = connection.execute("SELECT rule_kind,occurrence_count,reads_json,writes_json FROM business_rules WHERE rule_kind='ADD'").fetchall()
                self.assertEqual(rows, [("ADD", 2, '["A", "B"]', '["C"]')])
                occurrences = connection.execute("SELECT o.start_column,o.scope_json FROM business_rule_occurrences o JOIN business_rules b "
                    "ON b.rule_id=o.rule_id WHERE b.rule_kind='ADD' ORDER BY o.start_column").fetchall()
                self.assertEqual(len(occurrences), 2)
                self.assertLess(occurrences[0][0], occurrences[1][0])
                self.assertEqual([json.loads(row[1])[0]["outcome"] for row in occurrences], [True, False])

    def test_legal_fixed_continuation_literal_and_expression_keep_original_spans(self):
        source = "000100     MOVE 'FIRST\n000200-    'SECOND' TO A.\n000300     COMPUTE C = A +\n000400-        B END-COMPUTE."
        parsed = parse_statements(source, source_format="fixed", start_line=40)
        self.assertTrue(parsed.complete, parsed.boundaries)
        self.assertEqual(parsed.statements[0].writes, ("A",))
        self.assertEqual((parsed.statements[0].span.start_line, parsed.statements[0].span.end_line), (40, 41))
        self.assertEqual(parsed.statements[1].reads, ("A", "B"))
        lines, _ = normalize_cobol_lines(source, "fixed")
        self.assertIn("'FIRSTSECOND'", lines[0].text)
        self.assertNotIn("END-COMPUTE", assignment_facts(lines[1].text).expressions[0][1])

    def test_numeric_prefix_procedures_resolve_and_cfg_follows_real_thru_body(self):
        body = ("PERFORM 1000-CALCULATE-SECTION.\nPERFORM 1300-FIRST THRU 1400-LAST.\nGOBACK.\n"
                "1000-CALCULATE-SECTION SECTION.\n1100-PART.\nMOVE 1 TO A.\n"
                "1200-OTHER-SECTION SECTION.\n1300-FIRST.\nMOVE A TO B.\n1400-LAST.\nMOVE B TO C.\n")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "semantic.cbl").write_text(HEADER + body)
            database = root / "facts.sqlite"
            build_structural_index(root, database, source_format="free")
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT relation_type,target_name,status FROM relations WHERE relation_type LIKE 'PERFORMS%' ORDER BY relation_type").fetchall(),
                    [("PERFORMS", "1000-CALCULATE-SECTION", "confirmed"), ("PERFORMS_THRU", "1300-FIRST", "confirmed")])
            cfg = build_exception_cfg(database, "SEMANTIC-CASE")
            self.assertTrue(cfg["summary"]["supported_graph_closed"], cfg["boundaries"])
            self.assertEqual([node["targets"] for node in cfg["nodes"] if node["kind"] == "MOVE"], [["A"], ["B"], ["C"]])
        ast = parse_statements("PERFORM 1300-FIRST THRU 1400-LAST.")
        self.assertTrue(ast.complete, ast.boundaries)
        self.assertEqual(dependency_facts(ast.statements[0].text), (("PERFORMS_THRU", "1300-FIRST", "1400-LAST"),))

    def test_scientific_decimal_does_not_become_a_numeric_prefix_name(self):
        ast = parse_statements("COMPUTE C = A * 1E+2 END-COMPUTE.")
        self.assertTrue(ast.complete, ast.boundaries)
        self.assertEqual(ast.statements[0].reads, ("A",))

    def test_continued_literal_keeps_trailing_blanks_inside_its_value(self):
        first = "000100     MOVE 'ab  "
        second = "000200-    'cd' TO A."
        cleaned_first = clean_cobol_line(first, "fixed")[0]
        self.assertTrue(cleaned_first.endswith("ab  "))
        joined = join_continuation(cleaned_first, clean_cobol_line(second, "fixed")[0])
        self.assertEqual(assignment_facts(joined).expressions, (("A", "'ab  cd'"),))
        normalized, _ = normalize_cobol_lines(first + "\n" + second, "fixed")
        self.assertIn("'ab  cd'", normalized[0].text)
        ast = parse_statements(first + "\n" + second, source_format="fixed")
        self.assertTrue(ast.complete, ast.boundaries)
        self.assertEqual(ast.statements[0].expression, "'ab  cd'")

    def test_conditional_compilation_has_effective_and_candidate_facts(self):
        body = ">>IF FEATURE-A\nCALL 'WORKER'.\nMOVE 1 TO C.\n>>ELSE\nMOVE 2 TO C.\n>>END-IF\nGOBACK.\n"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "semantic.cbl").write_text(HEADER + body)
            (root / "worker.cbl").write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. WORKER.\nPROCEDURE DIVISION.\nGOBACK.\n")
            for builder in (build_structural_index, build_business_index):
                database = root / (builder.__name__ + ".sqlite")
                unknown = builder(root, database, source_format="free", quiet=True)
                with closing(sqlite3.connect(database)) as db:
                    call = db.execute("SELECT status,metadata_json FROM relations WHERE relation_type='CALLS'").fetchone()
                    self.assertEqual(call[0], "candidate")
                    self.assertEqual(json.loads(call[1])["compilation_guards"][0]["expression"], "FEATURE-A")
                    if builder is build_structural_index:
                        self.assertTrue(all(row[0] == "candidate" for row in db.execute("SELECT status FROM relations WHERE relation_type='WRITES'")))
                    else:
                        rows = db.execute("SELECT parse_status,scope_json FROM business_rule_occurrences").fetchall()
                        self.assertTrue(all(row[0] == "candidate" for row in rows))
                        self.assertTrue(all(json.loads(row[1])[-1]["support_basis"] == "source_syntax_only" for row in rows))
                if builder is build_structural_index:
                    cfg = build_exception_cfg(database, "SEMANTIC-CASE")
                    self.assertFalse(cfg["summary"]["supported_graph_closed"])
                    self.assertEqual(cfg["summary"]["boundary_counts"], {"compile_condition_unknown": 1})
                    self.assertFalse(any(node["kind"] in {"MOVE", "CALL"} for node in cfg["nodes"]))
                disabled = builder(root, database, source_format="free", quiet=True,
                    compilation_context={"compile_conditions": {"FEATURE-A": False}})
                self.assertTrue(disabled["source_options_rebuild_required"])
                with closing(sqlite3.connect(database)) as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM relations WHERE relation_type='CALLS'").fetchone()[0], 0)
                    if builder is build_structural_index:
                        statements = db.execute("SELECT normalized_text FROM code_units WHERE unit_type='Statement' AND name='MOVE'").fetchall()
                        self.assertEqual(statements, [("MOVE 2 TO C.",)])
                        self.assertEqual(db.execute("SELECT status FROM relations WHERE relation_type='WRITES'").fetchall(), [("confirmed",)])
                    else:
                        self.assertEqual(db.execute("SELECT normalized_text FROM business_rules WHERE rule_kind='MOVE'").fetchall(), [("MOVE 2 TO C.",)])
                    self.assertTrue(db.execute("SELECT count(*) FROM code_units WHERE unit_type='InactiveSource'").fetchone()[0])
                if builder is build_structural_index:
                    cfg = build_exception_cfg(database, "SEMANTIC-CASE")
                    self.assertTrue(cfg["summary"]["supported_graph_closed"], cfg["boundaries"])
                    self.assertEqual([(node["source"], node["targets"]) for node in cfg["nodes"] if node["kind"] == "MOVE"], [("2", ["C"])])
                enabled = builder(root, database, source_format="free", quiet=True,
                    compilation_context={"compile_conditions": {"FEATURE-A": True}})
                self.assertTrue(enabled["source_options_rebuild_required"])
                with closing(sqlite3.connect(database)) as db:
                    self.assertEqual(db.execute("SELECT status FROM relations WHERE relation_type='CALLS'").fetchall(), [("confirmed",)])
                warm = builder(root, database, source_format="free", quiet=True,
                    compilation_context={"compile_conditions": {"FEATURE-A": True}})
                self.assertFalse(warm["source_options_rebuild_required"])

    def test_incomplete_compile_scope_does_not_confirm_executable_facts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "semantic.cbl").write_text(HEADER + ">>IF FEATURE-A\nCALL 'WORKER'.\nMOVE 1 TO C.\n")
            (root / "worker.cbl").write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. WORKER.\nPROCEDURE DIVISION.\nGOBACK.\n")
            for builder in (build_structural_index, build_business_index):
                database = root / (builder.__name__ + ".sqlite")
                builder(root, database, source_format="free", quiet=True,
                    compilation_context={"compile_conditions": {"FEATURE-A": True}})
                with closing(sqlite3.connect(database)) as db:
                    call = db.execute("SELECT status,metadata_json FROM relations WHERE relation_type='CALLS'").fetchone()
                    self.assertEqual(call[0], "candidate")
                    self.assertEqual(json.loads(call[1])["diagnostics"], ["compile_condition_scope_unclosed"])
                    self.assertEqual(db.execute("SELECT parse_status FROM code_units WHERE unit_type='Program' AND name='SEMANTIC-CASE'").fetchone()[0], "complete")
                    if builder is build_business_index:
                        self.assertEqual(db.execute("SELECT DISTINCT parse_status FROM business_rule_occurrences").fetchall(), [("candidate",)])

    def test_unknown_compile_scope_retains_preceding_cfg_facts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "semantic.cbl").write_text(HEADER + "MOVE 5 TO A.\n>>IF FEATURE-A\nMOVE 1 TO C.\n>>END-IF\nGOBACK.\n")
            database = root / "facts.sqlite"
            build_structural_index(root, database, source_format="free", quiet=True)
            cfg = build_exception_cfg(database, "SEMANTIC-CASE")
            self.assertFalse(cfg["summary"]["supported_graph_closed"])
            self.assertEqual([(node["source"], node["targets"]) for node in cfg["nodes"] if node["kind"] == "MOVE"], [("5", ["A"])])
            self.assertEqual(cfg["summary"]["boundary_counts"], {"compile_condition_unknown": 1})

    def test_program_declaration_inside_literal_does_not_change_program_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "semantic.cbl"
            path.write_text(HEADER + "DISPLAY 'PROGRAM-ID. PHANTOM'.\nMOVE A TO C.\n")
            source = read_source_document(path, root, source_format="free")
            self.assertEqual(program_identifier("\n".join(source.raw_lines)), "SEMANTIC-CASE")
            parsed = parse_document(source)
            self.assertEqual([unit.name for unit in parsed.code_units if unit.unit_type == "Program"], ["SEMANTIC-CASE"])
            self.assertTrue(all(unit.program_name == "SEMANTIC-CASE" for unit in parsed.code_units if unit.unit_type == "Statement"))
            for builder in (build_structural_index, build_business_index):
                database = root / (builder.__name__ + ".sqlite")
                builder(root, database, source_format="free", quiet=True)
                with closing(sqlite3.connect(database)) as db:
                    self.assertEqual(db.execute("SELECT name FROM symbols WHERE symbol_type='Program'").fetchall(), [("SEMANTIC-CASE",)])


def relations_for(parsed, unit, kind):
    return [edge.target_name for edge in parsed.relations
            if edge.from_entity_id == unit.unit_id and edge.relation_type == kind]


if __name__ == "__main__":
    unittest.main()
