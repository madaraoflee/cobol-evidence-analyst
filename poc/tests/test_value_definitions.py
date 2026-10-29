"""VALUE clauses are conditional source definitions, never live value proof."""

from pathlib import Path
from contextlib import closing
from dataclasses import asdict
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from structural_index import build_structural_index
from semantic_service import FactStore, SemanticService
from agent_policy import AgentPolicy
from analyze_source import analyze_source
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from question_investigation import _flow_input_origins
from repository_discovery import ensure_repository_search
from semantic_scope import prepare_semantic_scope, build_business_evidence
from source_session import QuestionSourceSession
from test_business_prompt_compaction import decoded_navigation


class ValueDefinitionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "facts.sqlite"

    def write(self, declarations, body, *, section="WORKING-STORAGE", name="ENTRYPOINT", path="main.cbl", using=""):
        (self.root / path).write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. " + name + ".\n"
            "DATA DIVISION.\n" + section + " SECTION.\n" + declarations + "\n"
            "PROCEDURE DIVISION" + (" USING " + using if using else "") + ".\nMAIN.\n" + body + "\n")

    def build(self, *, procedure=None):
        build_structural_index(self.root, self.database, source_format="free")
        return SemanticService(FactStore(self.database, self.root, source_format="free")).build(
            "main.cbl", procedure=procedure)

    def origin(self, service, statement, field):
        occurrence = next(key for key, node in service.nodes.items() if node.statement == statement)
        return next(item for item in service.read_origins(occurrence) if item["reader_field"] == field)

    def test_value_is_an_actual_source_origin_with_declaration_and_activation_evidence(self):
        self.write("01 TRAN-AMT PIC S9(7)V99 VALUE 25.00.\n01 PAGE-SUM PIC S9(7)V99 VALUE 0.",
                   "ADD TRAN-AMT TO PAGE-SUM.\nGOBACK.")
        service = self.build()
        for field, value in (("TRAN-AMT", "25.00"), ("PAGE-SUM", "0")):
            with self.subTest(field=field):
                proof = self.origin(service, "ADD TRAN-AMT TO PAGE-SUM", field)
                self.assertEqual(proof["status"], "resolved_source_origin")
                self.assertEqual(proof["unknown_reasons"], [])
                self.assertFalse(proof["execution_verified"])
                initial = proof["initial_definitions"][0]
                self.assertEqual(initial["value"], value)
                self.assertEqual(initial["storage_section"], "WORKING-STORAGE")
                self.assertIn("first_activation", initial["activation_semantics"])
                self.assertFalse(initial["runtime_value_known"])
                line = 5 if field == "TRAN-AMT" else 6
                self.assertTrue(any(ref["start_line"] <= line <= ref["end_line"]
                                    for ref in proof["source_refs"]))

    def test_ir_value_clause_uses_real_tokens_after_the_declaration_name(self):
        from parser_adapter import BuiltinParserAdapter
        for name in ("BASE-VALUE", "VALUE", "VALUES", "VALUE-COUNT", "VALUEx"):
            with self.subTest(name=name):
                self.write("01 " + name + " PIC 9 VALUE IS 2.\n01 OUT-AMOUNT PIC 9.",
                           "MOVE " + name + " TO OUT-AMOUNT.\nGOBACK.")
                ir = BuiltinParserAdapter().parse((self.root / "main.cbl").read_text(), relative_path="main.cbl",
                                                context={"source_format": "free"})
                self.assertEqual(ir.data_items[0].value, "2")
                self.assertEqual(ir.adapter["version"], "builtin-ir-adapter-v4")
        self.write("01 BASE-VALUE PIC 9.\n01 LABEL PIC X(20) VALUE 'VALUE ARE 25.'.",
                   "GOBACK.")
        ir = BuiltinParserAdapter().parse((self.root / "main.cbl").read_text(), relative_path="main.cbl",
                                        context={"source_format": "free"})
        self.assertIsNone(ir.data_items[0].value)
        self.assertEqual(ir.data_items[1].value, "'VALUE ARE 25.'")

    def test_base_value_name_does_not_hide_the_valid_initial_definition(self):
        self.write("01 BASE-VALUE PIC 9 VALUE 2.\n01 OUT-AMOUNT PIC 9.",
                   "MOVE BASE-VALUE TO OUT-AMOUNT.\nGOBACK.")
        proof = self.origin(self.build(), "MOVE BASE-VALUE TO OUT-AMOUNT", "BASE-VALUE")
        self.assertEqual(proof["status"], "resolved_source_origin")
        self.assertEqual(proof["initial_definitions"][0]["value"], "2")

    def test_adapter_version_prevents_reusing_the_old_value_bug_for_identical_source(self):
        from parser_adapter import BuiltinParserAdapter
        from semantic_ir import digest
        self.write("01 BASE-VALUE PIC 9 VALUE 2.\n01 OUT-AMOUNT PIC 9.",
                   "MOVE BASE-VALUE TO OUT-AMOUNT.\nGOBACK.")
        build_business_index(self.root, self.database, source_format="free")
        ensure_repository_search(self.database, self.root)
        anchor = {"relative_path": "main.cbl", "line": 9, "fields": ["BASE-VALUE", "OUT-AMOUNT"]}
        with QuestionSourceSession(self.database, self.root) as session:
            with mock.patch.object(BuiltinParserAdapter, "version", "builtin-ir-adapter-v3"):
                with prepare_semantic_scope(self.database, session, anchors=[anchor], policy=AgentPolicy()) as old_scope:
                    old_key, old_manifest = old_scope.scope_key, old_scope.input_manifest
                    document = old_scope.read_ir("main.cbl")
                    document["data_items"][0]["value"] = "PIC 9 VALUE 2"
                    with closing(sqlite3.connect(old_scope.database_path)) as database:
                        database.execute("UPDATE semantic_ir_documents SET ir_json=?,ir_fingerprint=? WHERE relative_path=?",
                                         (json.dumps(document), digest(document), "main.cbl"))
                        database.commit()
            with prepare_semantic_scope(self.database, session, anchors=[anchor], policy=AgentPolicy()) as scope:
                self.assertEqual(scope.input_manifest, old_manifest)
                self.assertNotEqual(scope.scope_key, old_key)
                self.assertFalse(scope.cache_hit)
                self.assertEqual(scope.read_ir("main.cbl")["data_items"][0]["value"], "2")
                group = build_business_evidence(scope, session, anchor=anchor, policy=AgentPolicy())
                self.assertEqual(group.semantic_analysis["assignments"][0]["read_origins"][0]["status"], "resolved_source_origin")

    def test_loop_carried_sum_stays_unknown_but_the_declared_amount_source_is_known(self):
        self.write("01 TRAN-AMT PIC S9(7)V99 VALUE 25.00.\n01 PAGE-SUM PIC S9(7)V99 VALUE 0.\n"
                   "01 STOP-FLAG PIC 9.", "PERFORM UNTIL STOP-FLAG = 1\n"
                   "ADD TRAN-AMT TO PAGE-SUM\nEND-PERFORM\nGOBACK.")
        service = self.build()
        amount = self.origin(service, "ADD TRAN-AMT TO PAGE-SUM", "TRAN-AMT")
        total = self.origin(service, "ADD TRAN-AMT TO PAGE-SUM", "PAGE-SUM")
        self.assertEqual(amount["status"], "resolved_source_origin")
        self.assertEqual(total["status"], "unresolved_source_origin")
        self.assertIn("loop_carried_origin_not_closed", total["unknown_reasons"])
        self.assertNotIn("entry_state_or_external_input_not_defined", amount["unknown_reasons"])

    def test_local_storage_has_its_own_every_activation_contract(self):
        self.write("01 INPUT-AMOUNT PIC 9(5) VALUE 25.\n01 OUT-AMOUNT PIC 9(5).",
                   "MOVE INPUT-AMOUNT TO OUT-AMOUNT.\nGOBACK.", section="LOCAL-STORAGE")
        proof = self.origin(self.build(), "MOVE INPUT-AMOUNT TO OUT-AMOUNT", "INPUT-AMOUNT")
        self.assertEqual(proof["status"], "resolved_source_origin")
        self.assertEqual(proof["initial_definitions"][0]["activation_semantics"], "each_program_activation")

    def test_qualified_fields_keep_their_own_initial_values(self):
        self.write("01 INPUT-REC.\n05 AMOUNT PIC 9(5) VALUE 25.\n01 OUTPUT-REC.\n"
                   "05 AMOUNT PIC 9(5) VALUE 9.\n01 RESULT-AMOUNT PIC 9(5).",
                   "MOVE AMOUNT OF INPUT-REC TO RESULT-AMOUNT.\nGOBACK.")
        proof = self.origin(self.build(), "MOVE AMOUNT OF INPUT-REC TO RESULT-AMOUNT", "AMOUNT OF INPUT-REC")
        self.assertEqual(proof["status"], "resolved_source_origin")
        self.assertEqual(proof["initial_definitions"][0]["field"], "AMOUNT OF INPUT-REC")
        self.assertEqual(proof["initial_definitions"][0]["value"], "25")
        self.assertTrue(any(ref["start_line"] <= 5 <= ref["end_line"] for ref in proof["source_refs"]))

    def test_parent_shared_storage_and_overlapping_group_values_do_not_close_the_leaf(self):
        for modifier in ("EXTERNAL", "GLOBAL", "BASED", "VALUE '00000'"):
            with self.subTest(modifier=modifier):
                self.write("01 SHARED-REC " + modifier + ".\n05 INPUT-AMOUNT PIC 9(5) VALUE 25.\n"
                           "01 OUT-AMOUNT PIC 9(5).", "MOVE INPUT-AMOUNT TO OUT-AMOUNT.\nGOBACK.")
                proof = self.origin(self.build(), "MOVE INPUT-AMOUNT TO OUT-AMOUNT", "INPUT-AMOUNT")
                self.assertEqual(proof["status"], "unresolved_source_origin")
                self.assertTrue(any(reason in proof["unknown_reasons"] for reason in
                    ("initialization_shared_storage_requires_context", "overlapping_group_initialization_not_supported")))

    def test_branch_merge_keeps_both_initial_and_later_assignment_sources_without_predicting_a_value(self):
        self.write("01 INPUT-AMOUNT PIC 9(5) VALUE 25.\n01 OUT-AMOUNT PIC 9(5).\n01 FLAG PIC 9.",
            "IF FLAG = 1\nMOVE 19 TO INPUT-AMOUNT\nEND-IF\nMOVE INPUT-AMOUNT TO OUT-AMOUNT.\nGOBACK.")
        proof = self.origin(self.build(), "MOVE INPUT-AMOUNT TO OUT-AMOUNT", "INPUT-AMOUNT")
        self.assertEqual(proof["status"], "resolved_source_origin")
        self.assertEqual(proof["initial_definitions"][0]["value"], "25")
        self.assertEqual(len(proof["write_occurrences"]), 1)
        self.assertFalse(proof["execution_verified"])

    def test_uninitialized_linkage_arrays_aliases_and_unsupported_values_do_not_close(self):
        variants = [
            ("WORKING-STORAGE", "01 INPUT-AMOUNT PIC 9(5).", "INPUT-AMOUNT", ""),
            ("LINKAGE", "01 INPUT-AMOUNT PIC 9(5) VALUE 25.", "INPUT-AMOUNT", "OUT-AMOUNT"),
            ("WORKING-STORAGE", "01 BASE-AMOUNT PIC 9(5) VALUE 25.\n"
             "01 INPUT-AMOUNT REDEFINES BASE-AMOUNT PIC 9(5) VALUE 25.", "INPUT-AMOUNT", ""),
            ("WORKING-STORAGE", "01 INPUT-AMOUNT PIC 9(5) OCCURS 3 TIMES VALUE 25.", "INPUT-AMOUNT(1)", ""),
            ("WORKING-STORAGE", "01 INPUT-AMOUNT PIC 9(2) VALUE 250.", "INPUT-AMOUNT", ""),
            ("WORKING-STORAGE", "01 INPUT-AMOUNT PIC 9(5) VALUE -1.", "INPUT-AMOUNT", ""),
            ("WORKING-STORAGE", "01 INPUT-AMOUNT PIC 9(5) VALUE ALL '2'.", "INPUT-AMOUNT", ""),
            ("WORKING-STORAGE", "01 INPUT-AMOUNT PIC X VALUE 'TOO-LONG'.", "INPUT-AMOUNT", ""),
            ("WORKING-STORAGE", "01 INPUT-AMOUNT EXTERNAL PIC 9(5) VALUE 25.", "INPUT-AMOUNT", ""),
        ]
        for section, declaration, reference, using in variants:
            with self.subTest(declaration=declaration):
                self.write(declaration + "\n01 OUT-AMOUNT PIC 9(5).",
                           "MOVE " + reference + " TO OUT-AMOUNT.\nGOBACK.", section=section, using=using)
                proof = self.origin(self.build(), "MOVE " + reference + " TO OUT-AMOUNT", reference)
                self.assertEqual(proof["status"], "unresolved_source_origin")
                self.assertTrue(proof["unknown_reasons"])

    def test_explicit_linkage_parameter_locates_source_but_value_clause_does_not_set_runtime_value(self):
        self.write("01 INPUT-AMOUNT PIC 9(5) VALUE 25.\n01 OUT-AMOUNT PIC 9(5).",
                   "MOVE INPUT-AMOUNT TO OUT-AMOUNT.\nGOBACK.", section="LINKAGE",
                   using="INPUT-AMOUNT OUT-AMOUNT")
        service = self.build()
        proof = self.origin(service, "MOVE INPUT-AMOUNT TO OUT-AMOUNT", "INPUT-AMOUNT")
        self.assertEqual(proof["status"], "resolved_source_origin")
        self.assertFalse(proof["execution_verified"])
        self.assertEqual(len(proof["initial_definitions"]), 1)
        parameter = proof["initial_definitions"][0]
        self.assertEqual(parameter["status"], "declared_parameter_source")
        self.assertIsNone(parameter["value"])
        self.assertFalse(parameter["runtime_value_known"])
        self.assertIn("caller_supplied_at_program_entry", parameter["activation_semantics"])
        initialization = service.facts.initial_source_definition("main.cbl", "INPUT-AMOUNT")
        self.assertEqual(initialization["status"], "initialization_not_established")
        self.assertEqual(initialization["unknown_reason"], "value_clause_not_program_storage_initialization")
        self.assertFalse(initialization["runtime_value_known"])

    def test_explicit_later_overwrite_replaces_the_initial_definition(self):
        self.write("01 INPUT-AMOUNT PIC 9(5) VALUE 25.\n01 OUT-AMOUNT PIC 9(5).",
                   "MOVE 19 TO INPUT-AMOUNT.\nMOVE INPUT-AMOUNT TO OUT-AMOUNT.\nGOBACK.")
        proof = self.origin(self.build(), "MOVE INPUT-AMOUNT TO OUT-AMOUNT", "INPUT-AMOUNT")
        self.assertEqual(proof["status"], "resolved_source_origin")
        self.assertEqual(proof["initial_definitions"], [])
        self.assertEqual(len(proof["write_occurrences"]), 1)

    def test_unknown_call_does_not_get_erased_by_a_known_value_clause(self):
        self.write("01 INPUT-AMOUNT PIC 9(5) VALUE 25.\n01 OUT-AMOUNT PIC 9(5).",
                   "CALL 'UNKNOWN-SERVICE'.\nMOVE INPUT-AMOUNT TO OUT-AMOUNT.\nGOBACK.")
        proof = self.origin(self.build(), "MOVE INPUT-AMOUNT TO OUT-AMOUNT", "INPUT-AMOUNT")
        self.assertEqual(proof["status"], "unresolved_source_origin")
        self.assertIn("reaching_write_effect_not_bounded", proof["unknown_reasons"])
        self.assertEqual(proof["initial_definitions"][0]["value"], "25")

    def test_procedure_only_entry_does_not_assume_its_caller_preserved_the_initial_value(self):
        self.write("01 INPUT-AMOUNT PIC 9(5) VALUE 25.\n01 OUT-AMOUNT PIC 9(5).",
                   "MOVE INPUT-AMOUNT TO OUT-AMOUNT.\nGOBACK.")
        proof = self.origin(self.build(procedure="MAIN"), "MOVE INPUT-AMOUNT TO OUT-AMOUNT", "INPUT-AMOUNT")
        self.assertEqual(proof["status"], "unresolved_source_origin")
        self.assertIn("procedure_entry_initial_state_not_demonstrated", proof["unknown_reasons"])

    def test_repeat_static_calls_preserve_working_storage_uncertainty_but_local_storage_reinitializes(self):
        for section in ("WORKING-STORAGE", "LOCAL-STORAGE"):
            with self.subTest(section=section):
                self.write("01 OUT-AMOUNT PIC 9(5).\n01 FIRST-AMOUNT PIC 9(5).\n01 SECOND-AMOUNT PIC 9(5).",
                    "CALL 'VALUE-SERVICE' USING OUT-AMOUNT.\nMOVE OUT-AMOUNT TO FIRST-AMOUNT.\n"
                    "CALL 'VALUE-SERVICE' USING OUT-AMOUNT.\nMOVE OUT-AMOUNT TO SECOND-AMOUNT.\nGOBACK.")
                self.write("01 INITIAL-AMOUNT PIC 9(5) VALUE 25.\nLINKAGE SECTION.\n01 RETURN-AMOUNT PIC 9(5).",
                    "MOVE INITIAL-AMOUNT TO RETURN-AMOUNT.\nGOBACK.", section=section,
                    name="VALUE-SERVICE", path="service.cbl", using="RETURN-AMOUNT")
                service = self.build()
                first = self.origin(service, "MOVE OUT-AMOUNT TO FIRST-AMOUNT", "OUT-AMOUNT")
                second = self.origin(service, "MOVE OUT-AMOUNT TO SECOND-AMOUNT", "OUT-AMOUNT")
                self.assertEqual(first["status"], "resolved_source_origin")
                if section == "WORKING-STORAGE":
                    self.assertEqual(second["status"], "unresolved_source_origin")
                    self.assertIn("working_storage_reentry_state_not_defined", second["unknown_reasons"])
                else:
                    self.assertEqual(second["status"], "resolved_source_origin")

    def test_caller_value_is_traced_through_formal_linkage_without_taking_formal_value(self):
        self.write("01 INPUT-AMOUNT PIC 9(5) VALUE 25.\n01 OUT-AMOUNT PIC 9(5).\n01 FINAL-AMOUNT PIC 9(5).",
            "CALL 'VALUE-SERVICE' USING INPUT-AMOUNT OUT-AMOUNT.\nMOVE OUT-AMOUNT TO FINAL-AMOUNT.\nGOBACK.")
        self.write("01 PARAM-IN PIC 9(5).\n01 PARAM-OUT PIC 9(5).",
            "COMPUTE PARAM-OUT = PARAM-IN * 2.\nGOBACK.", section="LINKAGE", name="VALUE-SERVICE",
            path="service.cbl", using="PARAM-IN PARAM-OUT")
        proof = self.origin(self.build(), "MOVE OUT-AMOUNT TO FINAL-AMOUNT", "OUT-AMOUNT")
        self.assertEqual(proof["status"], "resolved_source_origin")
        self.assertEqual(proof["initial_definitions"][0]["field"], "INPUT-AMOUNT")
        self.assertEqual(proof["initial_definitions"][0]["storage_section"], "WORKING-STORAGE")

    def test_initial_program_identity_is_supplied_before_reentrant_working_storage_can_close(self):
        self.write("01 OUT-AMOUNT PIC 9(5).\n01 FIRST-AMOUNT PIC 9(5).\n01 SECOND-AMOUNT PIC 9(5).",
            "CALL 'VALUE-SERVICE' USING OUT-AMOUNT.\nMOVE OUT-AMOUNT TO FIRST-AMOUNT.\n"
            "CALL 'VALUE-SERVICE' USING OUT-AMOUNT.\nMOVE OUT-AMOUNT TO SECOND-AMOUNT.\nGOBACK.")
        self.write("01 INITIAL-AMOUNT PIC 9(5) VALUE 25.\nLINKAGE SECTION.\n01 RETURN-AMOUNT PIC 9(5).",
            "MOVE INITIAL-AMOUNT TO RETURN-AMOUNT.\nGOBACK.", name="VALUE-SERVICE",
            path="service.cbl", using="RETURN-AMOUNT")
        path = self.root / "service.cbl"
        path.write_text(path.read_text().replace("PROGRAM-ID. VALUE-SERVICE.", "PROGRAM-ID. VALUE-SERVICE IS INITIAL PROGRAM."))
        proof = self.origin(self.build(), "MOVE OUT-AMOUNT TO SECOND-AMOUNT", "OUT-AMOUNT")
        self.assertEqual(proof["status"], "resolved_source_origin")
        initial = proof["initial_definitions"][0]
        self.assertTrue(initial["initial_program"])
        self.assertEqual(initial["activation_semantics"], "each_program_activation_for_initial_program")
        self.assertTrue(any(ref["relative_path"] == "service.cbl" and ref["start_line"] <= 2 <= ref["end_line"]
                            for ref in initial["source_refs"]))

    def test_source_version_changed_after_indexing_cannot_supply_an_initial_definition(self):
        self.write("01 INPUT-AMOUNT PIC 9(5) VALUE 25.\n01 OUT-AMOUNT PIC 9(5).",
                   "MOVE INPUT-AMOUNT TO OUT-AMOUNT.\nGOBACK.")
        build_structural_index(self.root, self.database, source_format="free")
        path = self.root / "main.cbl"
        path.write_text(path.read_text().replace("VALUE 25", "VALUE 99"))
        with self.assertRaisesRegex(ValueError, "source_version_mismatch"):
            FactStore(self.database, self.root, source_format="free").initial_source_definition("main.cbl", "INPUT-AMOUNT")

    def test_stale_ir_initial_declaration_cannot_attach_to_a_current_program_snapshot(self):
        from parser_adapter import BuiltinParserAdapter
        self.write("01 INPUT-AMOUNT PIC 9(5) VALUE 25.\n01 OUT-AMOUNT PIC 9(5).",
                   "MOVE INPUT-AMOUNT TO OUT-AMOUNT.\nGOBACK.")
        build_structural_index(self.root, self.database, source_format="free")
        ir = BuiltinParserAdapter().parse((self.root / "main.cbl").read_text(), relative_path="main.cbl",
                                        context={"source_format": "free"}).to_dict()
        ir["data_items"][0]["source_refs"][0]["source_sha256"] = "0" * 64
        with closing(sqlite3.connect(self.database)) as database:
            database.execute("CREATE TABLE semantic_ir_documents (relative_path TEXT, ir_json TEXT)")
            database.execute("INSERT INTO semantic_ir_documents VALUES (?,?)", ("main.cbl", json.dumps(ir)))
            database.commit()
        service = SemanticService(FactStore(self.database, self.root, source_format="free")).build("main.cbl")
        proof = self.origin(service, "MOVE INPUT-AMOUNT TO OUT-AMOUNT", "INPUT-AMOUNT")
        self.assertEqual(proof["status"], "unresolved_source_origin")
        self.assertIn("initialization_source_version_not_confirmed", proof["unknown_reasons"])

    def page(self):
        raw = (self.root / "main.cbl").read_bytes()
        lines = raw.decode().splitlines()
        return {"relative_path": "main.cbl", "source_sha256": hashlib.sha256(raw).hexdigest(),
                "start_line": 1, "end_line": len(lines), "source_text": "\n".join(lines),
                "evidence_id": "source-main", "span_truncated": False}

    def test_initial_declaration_is_physically_supplied_and_missing_or_altered_value_cannot_close(self):
        self.write("01 INPUT-AMOUNT PIC 9(5) VALUE 25.\n01 OUT-AMOUNT PIC 9(5).",
                   "MOVE INPUT-AMOUNT TO OUT-AMOUNT.\nGOBACK.")
        build_business_index(self.root, self.database, source_format="free")
        ensure_repository_search(self.database, self.root)
        anchor = {"relative_path": "main.cbl", "line": 9, "fields": ["INPUT-AMOUNT", "OUT-AMOUNT"]}
        with QuestionSourceSession(self.database, self.root) as session:
            with prepare_semantic_scope(self.database, session, anchors=[anchor], policy=AgentPolicy()) as scope:
                group = asdict(build_business_evidence(scope, session, anchor=anchor, policy=AgentPolicy()))
        proof = group["semantic_analysis"]["assignments"][0]["read_origins"][0]
        self.assertEqual(proof["status"], "resolved_source_origin")
        self.assertTrue(all(ref.get("excerpt_sha256") for ref in proof["source_refs"]))
        self.assertTrue(all(ref.get("excerpt_sha256") for initial in proof["initial_definitions"]
                            for ref in initial["source_refs"]))
        self.assertTrue(any("VALUE 25" in page["source_text"] for page in group["supplied_locations"]))
        candidate = {"kind": "business_steps", "relative_path": "main.cbl", "source_sha256": self.page()["source_sha256"],
            "start_line": 9, "end_line": 9, "statement": "MOVE INPUT-AMOUNT TO OUT-AMOUNT.",
            "occurrence_count": 1, "reads": ["INPUT-AMOUNT"], "writes": ["OUT-AMOUNT"]}
        closed, _, witnesses = _flow_input_origins([candidate], [self.page()], [group])
        self.assertEqual(closed, {("main.cbl", "INPUT-AMOUNT")})
        self.assertEqual(witnesses[0]["status"], "source_origin_supplied")
        for mutation in ("VALUE 99", "VALUE  "):
            with self.subTest(mutation=mutation):
                page = self.page()
                page["source_text"] = page["source_text"].replace("VALUE 25", mutation)
                self.assertEqual(_flow_input_origins([candidate], [page], [group])[0], set())

    def test_copy_replacing_initialization_uses_original_hashes_and_supplies_the_replacement_directive(self):
        self.write("01 OUT-AMOUNT PIC 9(5).\nCOPY INITIAL-VALUES REPLACING ==25== BY ==31==.",
                   "MOVE INPUT-AMOUNT TO OUT-AMOUNT.\nGOBACK.")
        (self.root / "initial-values.cpy").write_text("01 INPUT-AMOUNT PIC 9(5) VALUE 25.\n")
        build_business_index(self.root, self.database, source_format="free")
        ensure_repository_search(self.database, self.root)
        anchor = {"relative_path": "main.cbl", "line": 9, "fields": ["INPUT-AMOUNT", "OUT-AMOUNT"]}
        with QuestionSourceSession(self.database, self.root) as session:
            with prepare_semantic_scope(self.database, session, anchors=[anchor], policy=AgentPolicy()) as scope:
                group = asdict(build_business_evidence(scope, session, anchor=anchor, policy=AgentPolicy()))
        proof = group["semantic_analysis"]["assignments"][0]["read_origins"][0]
        initial = proof["initial_definitions"][0]
        self.assertEqual(proof["status"], "resolved_source_origin")
        self.assertEqual(initial["value"], "31")
        self.assertFalse(initial["runtime_value_known"])
        self.assertTrue(any(ref.get("replacement_trace") for ref in initial["source_refs"]))
        for ref in initial["source_refs"]:
            self.assertEqual(ref["source_sha256"], hashlib.sha256((self.root / ref["relative_path"]).read_bytes()).hexdigest())
        self.assertTrue(any("REPLACING ==25== BY ==31==" in page["source_text"] for page in group["supplied_locations"]))
        self.assertTrue(any("VALUE 25" in page["source_text"] and page.get("include_chain")
                            for page in group["supplied_locations"]))
        candidate = {"kind": "business_steps", "relative_path": "main.cbl", "source_sha256": self.page()["source_sha256"],
            "start_line": 9, "end_line": 9, "statement": "MOVE INPUT-AMOUNT TO OUT-AMOUNT.",
            "occurrence_count": 1, "reads": ["INPUT-AMOUNT"], "writes": ["OUT-AMOUNT"]}
        self.assertEqual(_flow_input_origins([candidate], [self.page(), *group["supplied_locations"]], [group])[0],
                         {("main.cbl", "INPUT-AMOUNT")})

    def indexed_question(self, question, answer, *, history=(), output=None):
        if output is None:
            temporary = tempfile.TemporaryDirectory()
            self.addCleanup(temporary.cleanup)
            output = Path(temporary.name)
        config = CompanyAPIConfig("https://gateway.example.invalid/v1", "offline-value-model", api_key="offline-only")
        requests = []
        def transport(request):
            # Decode the documented wire projection before inspecting semantic
            # proofs. Shared fields are inherited facts, never omitted VALUEs.
            payload = decoded_navigation(json.loads(json.loads(request.body)["messages"][-1]["content"]))
            requests.append(payload)
            pages = [page for bundle in payload["source_context"] for page in bundle.get("pages", [])]
            supplied = next(page for page in pages if page["relative_path"] == "main.cbl" and "VALUE 25" in page["source_text"])
            response = answer + "[" + supplied["evidence_id"] + "]"
            return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": response},
                                                                  "finish_reason": "stop"}]}, ensure_ascii=False))
        analyze_source(self.root, output, source_format="free", analysis_mode="business", reading_strategy="retrieval",
                       allow_network=False, framework_reference_path="")
        result = analyze_source(self.root, output, question=question, config=config, transport=transport,
            source_format="free", analysis_mode="business", reading_strategy="retrieval", allow_network=True,
            framework_reference_path="", conversation_history=list(history),
            agent_policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0))
        self.assertTrue(result["index_reused"])
        self.assertEqual(len(requests), 1)
        agent = json.loads((output / "agent-result.json").read_text())["agent_result"]
        return agent, requests[0]

    def test_default_indexed_chat_supplies_actual_value_source_and_its_activation_condition(self):
        self.write("01 INPUT-AMOUNT PIC 9(5) VALUE 25.\n01 OUT-AMOUNT PIC 9(5).",
                   "MOVE INPUT-AMOUNT TO OUT-AMOUNT.\nGOBACK.")
        result, request = self.indexed_question("ENTRYPOINT 的 OUT-AMOUNT 从哪里来的？",
            "INPUT-AMOUNT 的源码声明为 VALUE 25；WORKING-STORAGE 在首次激活或重新初始化时适用，"
            "这不是当前运行值的证明。MOVE INPUT-AMOUNT TO OUT-AMOUNT 把该字段传给输出。")
        investigation = result["investigation_state"]["question_investigation"]
        self.assertEqual(result["status"], "ANALYZED")
        item = next(item for item in investigation["required_items"] if item["kind"] == "inputs")
        self.assertEqual(item["status"], "SATISFIED")
        self.assertIn(item["reason"], {"source_flow_origins_supplied", "source_candidates_supplied"})
        groups = request["evidence_groups"]
        initial = next(initial for group in groups for assignment in group.get("semantic_analysis", {}).get("assignments", [])
                       for proof in assignment.get("read_origins", []) for initial in proof.get("initial_definitions", []))
        self.assertEqual(initial["value"], "25")
        self.assertIn("first_activation", initial["activation_semantics"])
        self.assertFalse(initial["runtime_value_known"])
        self.assert_initial_source_supplied(initial, request)
        self.assertFalse(result["claims_semantically_verified"])

    def assert_initial_source_supplied(self, initial, request):
        self.assertEqual(initial["status"], "conditional_initialization_source")
        self.assertIn("subsequent_activations_may_retain_prior_state", initial["activation_semantics"])
        self.assertTrue(initial["source_refs"])
        pages = [page for bundle in request["source_context"] for page in bundle["pages"]]
        self.assertTrue(any(initial["declaration"] in page["source_text"] for page in pages))
        for reference in initial["source_refs"]:
            source = self.root / reference["relative_path"]
            self.assertEqual(reference["source_sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            text = "\n".join(source.read_text(encoding="utf-8").splitlines()[
                reference["start_line"] - 1:reference["end_line"]])
            self.assertEqual(reference["excerpt_sha256"], hashlib.sha256(text.encode()).hexdigest())
            self.assertTrue(any(page["relative_path"] == reference["relative_path"]
                and page["source_sha256"] == reference["source_sha256"]
                and page["start_line"] <= reference["start_line"] <= reference["end_line"] <= page["end_line"]
                and page.get("include_chain", []) == reference.get("include_chain", [])
                and page.get("replacement_trace", []) == reference.get("replacement_trace", [])
                and not page.get("span_truncated") for page in pages))

    def test_default_indexed_chat_unknown_call_preserves_declared_fact_without_closing_current_input(self):
        self.write("01 INPUT-AMOUNT PIC 9(5) VALUE 25.\n01 OUT-AMOUNT PIC 9(5).",
                   "CALL 'UNKNOWN-SERVICE'.\nMOVE INPUT-AMOUNT TO OUT-AMOUNT.\nGOBACK.")
        result, _ = self.indexed_question("ENTRYPOINT 的 OUT-AMOUNT 从哪里来的？",
            "INPUT-AMOUNT 声明 VALUE 25 是初始化来源；之后调用 UNKNOWN-SERVICE，该实现不在源码中，"
            "调用后的实际字段内容可能改变。随后 MOVE INPUT-AMOUNT TO OUT-AMOUNT。")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("VALUE 25", result["answer"])
        inputs = next(item for item in result["investigation_state"]["question_investigation"]["required_items"]
                      if item["kind"] == "inputs")
        self.assertNotEqual(inputs["status"], "SATISFIED")

    def test_default_indexed_chat_loop_keeps_constant_origin_and_specific_accumulation_unknown(self):
        self.write("01 TRAN-AMT PIC S9(7)V99 VALUE 25.00.\n01 PAGE-SUM PIC S9(7)V99 VALUE 0.\n"
                   "01 STOP-FLAG PIC 9.", "PERFORM UNTIL STOP-FLAG = 1\n"
                   "ADD TRAN-AMT TO PAGE-SUM\nEND-PERFORM\nGOBACK.")
        result, _ = self.indexed_question("ENTRYPOINT 的 PAGE-SUM 从哪里来的？为何不断累加？",
            "TRAN-AMT 的 VALUE 25.00 声明给出了初始化来源。PAGE-SUM 初始声明为 0，"
            "每次循环执行 ADD TRAN-AMT TO PAGE-SUM；循环次数和累加的最终值尚未确定。")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("VALUE 25.00", result["answer"])
        inputs = next(item for item in result["investigation_state"]["question_investigation"]["required_items"]
                      if item["kind"] == "inputs")
        witnesses = inputs["source_flow_origins"]
        constant = next(item for item in witnesses if item["field"] == "TRAN-AMT")
        total = next(item for item in witnesses if item["field"] == "PAGE-SUM")
        self.assertEqual(constant["status"], "source_origin_supplied")
        self.assertEqual(total["status"], "source_origin_unresolved")
        self.assertIn("loop_carried_origin_not_closed", total["unknown_reasons"])

    def test_actual_desktop_conversation_fixture_has_value_origins_without_entry_undefined_claims(self):
        from tools.desktop_acceptance_scenarios import _conversation_source
        (self.root / "main.cbl").write_text(_conversation_source(), encoding="utf-8")
        service = self.build()
        for field in ("TRAN-AMT", "PAGE-SUM", "ACCOUNT-SUM"):
            with self.subTest(field=field):
                proof = self.origin(service, "ADD TRAN-AMT TO PAGE-SUM ACCOUNT-SUM", field)
                self.assertEqual(proof["status"], "resolved_source_origin")
                self.assertNotIn("entry_state_or_external_input_not_defined", proof["unknown_reasons"])
                self.assertTrue(proof["initial_definitions"])

    def test_actual_desktop_conversation_default_chain_supplies_the_initial_source_contract(self):
        from tools.desktop_acceptance_scenarios import _conversation_source, CONVERSATION_QUESTIONS
        (self.root / "main.cbl").write_text(_conversation_source(), encoding="utf-8")
        result, request = self.indexed_question(CONVERSATION_QUESTIONS[0],
            "ADD TRAN-AMT TO PAGE-SUM ACCOUNT-SUM 分别累加两种汇总。TRAN-AMT 的 VALUE 25.00 是源码"
            "初始化声明，WORKING-STORAGE 在首次激活或重新初始化时适用，不能当成当前运行值。"
            "换页执行 PAGE-TOTALS，把页汇总移到报告字段、加到 GRAND-SUM，再 MOVE 0 TO PAGE-SUM。"
            "CURR-CARD NOT = TRAN-CARD 且 FIRST-ITEM = 'N' 时执行 ACCOUNT-TOTALS，先输出再 MOVE 0 TO ACCOUNT-SUM。")
        self.assertIn("VALUE 25.00", result["answer"])
        initials = [initial for group in request["evidence_groups"]
            for assignment in group.get("semantic_analysis", {}).get("assignments", [])
            for proof in assignment.get("read_origins", []) for initial in proof.get("initial_definitions", [])]
        initial = next(item for item in initials if item["field"] == "TRAN-AMT")
        self.assertEqual(initial["value"], "25.00")
        self.assertIn("first_activation", initial["activation_semantics"])
        self.assertFalse(initial["runtime_value_known"])
        self.assert_initial_source_supplied(initial, request)
        self.assertFalse(result["claims_semantically_verified"])

    def test_actual_desktop_three_turn_inputs_close_from_occurrences_and_value_source_evidence(self):
        from tools.desktop_acceptance_scenarios import _conversation_source, CONVERSATION_QUESTIONS
        (self.root / "main.cbl").write_text(_conversation_source(), encoding="utf-8")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output, history = Path(temporary.name), []
        for turn, question in enumerate(CONVERSATION_QUESTIONS, 1):
            result, _ = self.indexed_question(question,
                "第一种是 PAGE-SUM，第二种是 ACCOUNT-SUM。ADD TRAN-AMT TO PAGE-SUM ACCOUNT-SUM 分别累加；"
                "TRAN-AMT 的 VALUE 25.00 是初次激活时的源码初始化声明，不是当前运行值。"
                "换页执行 PAGE-TOTALS，先移到报告再加 GRAND-SUM，再 MOVE 0 TO PAGE-SUM。"
                "CURR-CARD NOT = TRAN-CARD 且 FIRST-ITEM = 'N' 时执行 ACCOUNT-TOTALS，先移到报告再 MOVE 0 TO ACCOUNT-SUM，"
                "这个账户清零段不写 GRAND-SUM。", history=history, output=output)
            inputs = next(item for item in result["investigation_state"]["question_investigation"]["required_items"]
                          if item["kind"] == "inputs")
            with self.subTest(turn=turn):
                self.assertEqual(inputs["status"], "SATISFIED", {"reason": inputs["reason"], "origins": [
                    {key: witness.get(key) for key in ("field", "status", "unknown_reasons", "missing_reader_occurrences")}
                    for witness in inputs.get("source_flow_origins", [])]})
                for witness in inputs.get("source_flow_origins", []):
                    self.assertEqual(witness["status"], "source_origin_supplied", witness)
                    self.assertFalse(witness["execution_verified"])
            history.extend(({"role": "user", "content": question}, {"role": "assistant", "content": result["answer"],
                "evidence_refs": result["evidence_refs"], "investigation_state": result["investigation_state"],
                "cited_evidence_ids": [ref["evidence_id"] for ref in result["narrative"]["citations"] if ref.get("evidence_id")]}))


if __name__ == "__main__":
    unittest.main()
