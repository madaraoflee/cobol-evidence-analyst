"""Program identity is source syntax; quoted displays cannot declare programs."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from compilation_context import CompilationContext
from parser_adapter import BuiltinParserAdapter
from statement_facts import program_identifier, program_identifiers
from structural_index import build_structural_index
from semantic_service import FactStore, SemanticService


class ProgramIdentifierTests(unittest.TestCase):
    def test_real_quoted_unquoted_and_split_headers_ignore_literal_and_comment_names(self):
        for header in ("PROGRAM-ID. REALPG.", "PROGRAM-ID. 'REALPG'.", 'PROGRAM-ID.\n"REALPG".'):
            with self.subTest(header=header):
                text = "IDENTIFICATION DIVISION.\n" + header + "\nPROCEDURE DIVISION.\n" \
                    "DISPLAY 'PROGRAM-ID. FALSEPG. *> still literal'.\n*> PROGRAM-ID. COMMENTPG.\nGOBACK."
                self.assertEqual(program_identifier(text), "REALPG")
                self.assertEqual([name for name, _, _ in program_identifiers(text)], ["REALPG"])
                fixed = "\n".join("       " + line for line in text.splitlines())
                self.assertEqual(program_identifier(fixed, source_format="fixed"), "REALPG")
        self.assertIsNone(program_identifier("DISPLAY 'PROGRAM-ID. FALSEPG.'."))
        self.assertIsNone(program_identifier("DISPLAY PROGRAM-ID. FALSEPG."))
        self.assertIsNone(program_identifier("PROGRAM-ID. TRUNCATED"))
        self.assertEqual(program_identifier("PROGRAM-ID. REALPG.\nEXEC SQL % dialect remainder"), "REALPG")

    def test_builtin_identity_and_unknown_compilation_do_not_become_confirmed_names(self):
        text = "IDENTIFICATION DIVISION.\nPROGRAM-ID. 'REALPG'.\nPROCEDURE DIVISION.\n" \
               "MAIN.\nGOBACK.\nTEXT-NOTE.\nDISPLAY 'PROGRAM-ID. FALSEPG.'."
        ir = BuiltinParserAdapter().parse(text, relative_path="entry.cbl")
        self.assertEqual(ir.adapter["program_identifiers"], [{"name": "REALPG", "compilation_status": "active"}])
        unknown = BuiltinParserAdapter().parse(">>IF FEATURE\n" + text + "\n>>END-IF", relative_path="entry.cbl")
        self.assertEqual(unknown.adapter["program_identifiers"], [{"name": "REALPG", "compilation_status": "candidate"}])

    def test_literal_false_name_cannot_change_static_callee_identity_or_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            template = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
                "WORKING-STORAGE SECTION.\n01 IN-AMOUNT PIC 9(5).\n01 OUT-AMOUNT PIC 9(5).\n"
                "PROCEDURE DIVISION.\nMAIN.\n{body}\n")
            (root / "main.cbl").write_text(template.format(name="REALPG", body="CALL 'LEAFPG'\nGOBACK."))
            (root / "leaf.cbl").write_text(template.format(name="'LEAFPG'", body="COMPUTE OUT-AMOUNT = IN-AMOUNT * 2\n"
                "GOBACK.\nTEXT-NOTE.\nDISPLAY 'PROGRAM-ID. FALSEPG.'."))
            database = root / "index.sqlite"
            build_structural_index(root, database, source_format="free")
            facts = FactStore(database, root, source_format="free")
            self.assertEqual(facts.resolve("LEAFPG"), "leaf.cbl")
            self.assertIsNone(facts.resolve("FALSEPG"))
            self.assertEqual(facts.program("leaf.cbl")["name"], "LEAFPG")
            service = SemanticService(facts).build("main.cbl")
            self.assertIn("LEAFPG", service.dependencies)
            self.assertNotIn("FALSEPG", service.dependencies)
            self.assertTrue(any(node.kind == "COMPUTE" for node in service.nodes.values()))

    def test_unclosed_known_guard_keeps_parsed_candidate_and_no_asserted_initial_value(self):
        text = "IDENTIFICATION DIVISION.\nPROGRAM-ID. REALPG.\nDATA DIVISION.\nWORKING-STORAGE SECTION.\n" \
               "01 OUT-AMOUNT PIC 9.\nPROCEDURE DIVISION.\nMAIN.\n>>IF FEATURE\nMOVE 7 TO OUT-AMOUNT."
        context = CompilationContext(compile_conditions=(("FEATURE", True),))
        ir = BuiltinParserAdapter().parse(text, relative_path="entry.cbl", context=context)
        self.assertFalse(ir.parse_complete)
        statement = next(row for row in ir.statements if row.statement_type == "MOVE")
        self.assertEqual((statement.parsed_status, statement.binding_status, statement.compilation_status),
                         ("parsed", "candidate", "candidate"))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "entry.cbl").write_text(text)
            database = root / "index.sqlite"
            build_structural_index(root, database, source_format="free", compilation_context=context)
            facts = FactStore(database, root, source_format="free")
            program = facts.program("entry.cbl")
            self.assertEqual(program["name"], "REALPG")
            self.assertIn("compile_condition_scope_unclosed", program["diagnostics"])
            service = SemanticService(facts).build("entry.cbl")
            self.assertFalse(any(node.statement == "MOVE 7 TO OUT-AMOUNT" for node in service.nodes.values()))


if __name__ == "__main__":
    unittest.main()
