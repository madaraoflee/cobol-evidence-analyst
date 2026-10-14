"""Default file questions require actual versioned declarations/layout/I/O."""
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "poc")]
from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from business_map import build_business_map
from company_api import CompanyAPIConfig, TransportResponse
from question_investigation import build_question_investigation, _flow_input_origins
from repository_discovery import ensure_repository_search
from semantic_scope import build_business_evidence, prepare_semantic_scope
from source_session import QuestionSourceSession


SOURCE = """IDENTIFICATION DIVISION.
PROGRAM-ID. FILE-RULE.
ENVIRONMENT DIVISION.
INPUT-OUTPUT SECTION.
FILE-CONTROL.
SELECT ACCOUNT-FILE ASSIGN TO ACCOUNT-DATA
 ORGANIZATION IS INDEXED RECORD KEY IS FD-ACCOUNT-ID
 FILE STATUS IS ACCOUNT-STATUS.
DATA DIVISION.
FILE SECTION.
FD ACCOUNT-FILE.
01 FD-ACCOUNT-RECORD.
 05 FD-ACCOUNT-ID PIC 9(5).
 05 FD-ACCOUNT-DATA PIC X(10).
WORKING-STORAGE SECTION.
COPY ACCOUNT-LAYOUT.
01 ACCOUNT-STATUS PIC XX.
PROCEDURE DIVISION.
MAIN.
READ ACCOUNT-FILE INTO ACCOUNT-RECORD.
REWRITE FD-ACCOUNT-RECORD FROM ACCOUNT-RECORD.
GOBACK.
"""
LAYOUT = "01 ACCOUNT-RECORD.\n 05 ACCOUNT-ID PIC 9(5).\n 05 ACCOUNT-AMOUNT PIC 9(8)V99.\n"


class FileQuestionObligationTests(unittest.TestCase):
    question = "FILE-RULE 的 ACCOUNT-FILE、FD-ACCOUNT-RECORD 和 ACCOUNT-RECORD 如何对应？能确认真实文件路径吗？"

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"

    def build(self, text=SOURCE, copy=True):
        (self.source / "file.cbl").write_text(text, encoding="utf-8")
        if copy:
            (self.source / "ACCOUNT-LAYOUT.cpy").write_text(LAYOUT, encoding="utf-8")
        build_business_index(self.source, self.database, source_format="auto", verify_content=True)
        ensure_repository_search(self.database, self.source)

    def page(self, path, first=1, last=None, *, version=None):
        raw = (self.source / path).read_bytes()
        lines = raw.decode().splitlines()
        last = last or len(lines)
        return {"relative_path": path, "start_line": first, "end_line": last,
                "source_sha256": version or hashlib.sha256(raw).hexdigest(),
                "source_text": "\n".join(lines[first-1:last]), "evidence_id": f"page-{path}-{first}-{last}"}

    def investigate(self, pages=(), question=None, **options):
        question = question or self.question
        mapping = build_business_map(self.database, self.source, question)
        return build_question_investigation(question, mapping, database_path=self.database,
                                            source_pages=pages, **options)

    def item(self, result, kind):
        return next(item for item in result["required_items"] if item["kind"] == kind)

    def test_file_mapping_has_typed_identity_and_each_actual_source_obligation(self):
        self.build()
        result = self.investigate([self.page("file.cbl"), self.page("ACCOUNT-LAYOUT.cpy")])
        self.assertEqual(result["analysis_plan"]["intents"], ("file_mapping",))
        entity = result["analysis_plan"]["file_entities"][0]
        self.assertEqual((entity["entity_type"], entity["file_name"], entity["binding_status"]),
                         ("FileRef", "ACCOUNT-FILE", "bound"))
        self.assertFalse(entity["external_identity_verified"])
        self.assertEqual(set(entity["record_names"]), {"FD-ACCOUNT-RECORD", "ACCOUNT-RECORD"})
        for kind, count in (("file_definitions", 2), ("file_layout", 1), ("copy_layout", 2), ("file_io", 2)):
            item = self.item(result, kind)
            self.assertEqual((item["status"], item["candidate_count"]), ("SATISFIED", count))
            self.assertTrue(item["evidence_ids"])
        self.assertEqual(result["open_gaps"], [])
        self.assertTrue(result["can_answer"])
        self.assertEqual(self.item(result, "business_steps")["status"], "NOT_APPLICABLE")
        self.assertFalse(result["semantic_execution_verified"])

    def test_copy_layout_requires_supplied_member_and_its_current_hash(self):
        self.build()
        before = self.investigate([self.page("file.cbl")])
        self.assertEqual(self.item(before, "copy_layout")["missing_count"], 1)
        self.assertTrue(any(action["arguments"]["relative_path"] == "ACCOUNT-LAYOUT.cpy"
                            for action in before["planned_actions"]))
        wrong = self.investigate([self.page("file.cbl"), self.page("ACCOUNT-LAYOUT.cpy", version="0" * 64)])
        self.assertNotEqual(self.item(wrong, "copy_layout")["status"], "SATISFIED")

    def test_missing_fd_or_copy_preserves_specific_mapping_unknown(self):
        self.build(SOURCE.replace("FD ACCOUNT-FILE.\n", ""), copy=False)
        result = self.investigate([self.page("file.cbl")])
        reasons = {gap["reason"] for gap in result["open_gaps"]}
        self.assertIn("file_declaration_missing", reasons)
        self.assertIn("copy_layout_member_not_located", reasons)
        self.assertIn("work_record_layout_ambiguous_or_missing", reasons)
        self.assertEqual(result["analysis_plan"]["file_entities"][0]["binding_status"], "candidate")

    def test_unknown_file_does_not_use_another_files_supplied_declaration(self):
        self.build()
        result = self.investigate([self.page("file.cbl"), self.page("ACCOUNT-LAYOUT.cpy")],
                                  "FILE-RULE 的 MISSING-FILE 对应什么记录和真实路径？")
        self.assertIn("file_entity_not_located", {gap["reason"] for gap in result["open_gaps"]})
        self.assertFalse(result["can_answer"])
        self.assertEqual(self.item(result, "file_definitions")["candidate_count"], 0)

    def test_duplicate_select_keeps_two_source_candidates_not_confirmed_identity(self):
        self.build(SOURCE.replace("DATA DIVISION.", "SELECT ACCOUNT-FILE ASSIGN TO SECOND-DATA.\nDATA DIVISION."))
        result = self.investigate([self.page("file.cbl"), self.page("ACCOUNT-LAYOUT.cpy")])
        entity = result["analysis_plan"]["file_entities"][0]
        self.assertEqual(entity["binding_status"], "candidate")
        self.assertEqual(len(entity["source_versions"]), 3)
        self.assertIn("file_reference_ambiguous", {gap["reason"] for gap in result["open_gaps"]})

    def test_ordinary_data_item_ending_file_keeps_dataref_calculation(self):
        self.build("IDENTIFICATION DIVISION.\nPROGRAM-ID. FIELD-RULE.\nDATA DIVISION.\n"
                   "WORKING-STORAGE SECTION.\n01 RESULT-FILE PIC 9.\n01 BASE-VALUE PIC 9 VALUE 2.\n"
                   "PROCEDURE DIVISION.\nCOMPUTE RESULT-FILE = BASE-VALUE * 2.\nGOBACK.\n", copy=False)
        result = self.investigate([self.page("file.cbl")], "FIELD-RULE RESULT-FILE 怎么计算？")
        self.assertEqual(result["analysis_plan"]["file_entities"], ())
        self.assertEqual(self.item(result, "formula")["status"], "SATISFIED")
        self.assertNotIn("file_mapping", result["analysis_plan"]["intents"])
        self.assertEqual(result["open_gaps"], [])
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            page = next(page for context in payload["source_context"] for page in context["pages"]
                        if "COMPUTE RESULT-FILE = BASE-VALUE * 2." in page["source_text"])
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content":
                "RESULT-FILE = BASE-VALUE * 2。[" + page["evidence_id"] + "]"}, "finish_reason": "stop"}]}))
        output = run_business_chat("FIELD-RULE RESULT-FILE 怎么计算？", self.database, self.source,
            CompanyAPIConfig("https://offline.example.invalid/v1", "offline-model", api_key="synthetic-only"),
            transport=transport, allow_network=False, framework_reference_path="", policy=AgentPolicy(max_model_requests=1))
        self.assertEqual(output["agent_result"]["status"], "ANALYZED")

    def test_file_declarations_follow_shared_compile_true_false_and_unknown(self):
        for condition, bound in (("TRUE", True), ("FALSE", False), ("UNKNOWN-SWITCH", False)):
            with self.subTest(condition=condition):
                source = SOURCE.replace("SELECT ACCOUNT-FILE", f">>IF {condition}\nSELECT ACCOUNT-FILE")
                source = source.replace("DATA DIVISION.", ">>END-IF\nDATA DIVISION.")
                self.build(source)
                result = self.investigate([self.page("file.cbl"), self.page("ACCOUNT-LAYOUT.cpy")])
                entity = result["analysis_plan"]["file_entities"][0]
                self.assertEqual(entity["binding_status"] == "bound", bound)
                reasons = {gap["reason"] for gap in result["open_gaps"]}
                if condition == "UNKNOWN-SWITCH":
                    self.assertIn("compile_condition_unknown", reasons)
                    self.assertNotEqual(self.item(result, "file_definitions")["status"], "SATISFIED")
                elif condition == "FALSE":
                    self.assertIn("file_declaration_missing", reasons)
                    self.assertEqual(self.item(result, "file_definitions")["candidate_count"], 1)
                else:
                    self.assertEqual(result["open_gaps"], [])

    def test_missing_flow_excerpt_invalidates_the_origin_despite_a_supplied_value(self):
        self.build("IDENTIFICATION DIVISION.\nPROGRAM-ID. FIELD-RULE.\nDATA DIVISION.\n"
                   "WORKING-STORAGE SECTION.\n01 INPUT-AMOUNT PIC 9(5) VALUE 25.\n"
                   "01 INTERMEDIATE-AMOUNT PIC 9(5).\n01 OUT-AMOUNT PIC 9(5).\n"
                   "PROCEDURE DIVISION.\nMOVE INPUT-AMOUNT TO INTERMEDIATE-AMOUNT.\n"
                   "MOVE INTERMEDIATE-AMOUNT TO OUT-AMOUNT.\nGOBACK.\n", copy=False)
        anchor = {"relative_path": "file.cbl", "line": 10,
                  "fields": ["INTERMEDIATE-AMOUNT", "OUT-AMOUNT"]}
        with QuestionSourceSession(self.database, self.source) as session:
            with prepare_semantic_scope(self.database, session, anchors=[anchor], policy=AgentPolicy()) as scope:
                group = asdict(build_business_evidence(scope, session, anchor=anchor, policy=AgentPolicy()))
        candidate = {"kind": "business_steps", "relative_path": "file.cbl",
                     "source_sha256": self.page("file.cbl")["source_sha256"],
                     "start_line": 10, "end_line": 10, "occurrence_count": 1,
                     "statement": "MOVE INTERMEDIATE-AMOUNT TO OUT-AMOUNT.",
                     "reads": ["INTERMEDIATE-AMOUNT"], "writes": ["OUT-AMOUNT"]}
        complete, invalidated, _ = _flow_input_origins([candidate], [self.page("file.cbl")], [group])
        self.assertEqual(complete, {("file.cbl", "INTERMEDIATE-AMOUNT")})
        self.assertEqual(invalidated, set())
        # The initial VALUE and the actual read remain visible, but the
        # intervening assignment required by the same proof is not supplied.
        closed, invalidated, witnesses = _flow_input_origins([candidate],
            [self.page("file.cbl", 1, 8), self.page("file.cbl", 10, 11)], [group])
        self.assertEqual(closed, set())
        self.assertEqual(invalidated, {("file.cbl", "INTERMEDIATE-AMOUNT")})
        self.assertEqual(witnesses[0]["status"], "source_origin_unresolved")

    def test_file_write_impact_preserves_value_and_unknown_effect_obligations(self):
        self.build(SOURCE.replace("REWRITE FD-ACCOUNT-RECORD", "COMPUTE ACCOUNT-AMOUNT = ACCOUNT-AMOUNT * 2.\n"
                    "CALL 'MISSING-HANDLER' USING ACCOUNT-RECORD.\nREWRITE FD-ACCOUNT-RECORD"))
        result = self.investigate([self.page("file.cbl"), self.page("ACCOUNT-LAYOUT.cpy")],
                                  "FILE-RULE 的 ACCOUNT-FILE 会写入哪些字段？")
        self.assertIn("impact", result["analysis_plan"]["intents"])
        self.assertGreater(self.item(result, "business_steps")["candidate_count"], 0)
        self.assertIn("ACCOUNT-AMOUNT", self.item(result, "inputs")["fields"])
        self.assertIn("MISSING-HANDLER", self.item(result, "dependencies")["targets"])
        self.assertNotIn("requested_field_not_bound", {gap["reason"] for gap in result["open_gaps"]})

    def default_answer(self, question):
        requests = []
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            pages = [page for context in payload["source_context"] for page in context.get("pages", ())]
            for page in pages:
                raw = (self.source / page["relative_path"]).read_bytes()
                self.assertEqual(page["source_sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(page["source_text"], "\n".join(raw.decode().splitlines()[page["start_line"]-1:page["end_line"]]))
            requests.append((payload, pages))
            host = next(page for page in pages if "SELECT ACCOUNT-FILE" in page["source_text"])
            copy = next(page for page in pages if page["relative_path"].lower().endswith(".cpy")
                        and "ACCOUNT-RECORD." in page["source_text"])
            answer = ("ACCOUNT-FILE 的SELECT以符号指定外部资源；FD对应物理记录，READ INTO与REWRITE FROM映射工作记录。"
                      f"[{host['evidence_id']}] 工作记录布局由已供给COPY定义。[{copy['evidence_id']}] "
                      "源码不能证明真实部署路径或运行记录金额。")
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": answer}, "finish_reason": "stop"}]}))
        config = CompanyAPIConfig("https://offline.example.invalid/v1", "offline-model", api_key="synthetic-only")
        with mock.patch.object(socket, "create_connection", side_effect=AssertionError("Offline test forbids network")):
            output = run_business_chat(question, self.database, self.source, config, transport=transport,
                allow_network=False, framework_reference_path="", policy=AgentPolicy(max_model_requests=1))
        self.assertEqual(len(requests), 1)
        self.assertEqual(output["agent_result"]["status"], "ANALYZED")
        investigation = output["agent_result"]["investigation_state"]["question_investigation"]
        self.assertEqual(investigation["open_gaps"], [])
        sent = {page["evidence_id"] for _, pages in requests for page in pages}
        for item in investigation["required_items"]:
            self.assertLessEqual(set(item["evidence_ids"]), sent)
        return output, requests

    def test_default_fake_answer_receives_original_select_fd_copy_and_both_io(self):
        self.build()
        _, requests = self.default_answer(self.question)
        text = "\n".join(page["source_text"] for _, pages in requests for page in pages)
        for literal in ("SELECT ACCOUNT-FILE", "FD ACCOUNT-FILE.", "COPY ACCOUNT-LAYOUT.",
                        "ACCOUNT-AMOUNT PIC 9(8)V99.", "READ ACCOUNT-FILE INTO ACCOUNT-RECORD.",
                        "REWRITE FD-ACCOUNT-RECORD FROM ACCOUNT-RECORD."):
            self.assertIn(literal, text)

    def test_actual_frozen_public_file_mapping_default_source_evidence_correct(self):
        configured = os.environ.get("PUBLIC_BUSINESS_SOURCE_ROOT")
        public = Path(configured or "/private/tmp/business-public-corpus")
        if not (public / "app/cbl/CBACT04C.cbl").is_file():
            if configured:
                self.fail("Configured frozen public source does not contain app/cbl/CBACT04C.cbl")
            self.skipTest("Frozen public source is not installed on this test host")
        manifest = json.loads((ROOT / "docs/eval/public-business-v1/manifest.json").read_text(encoding="utf-8"))
        for item in manifest["files"]:
            raw = (public / item["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), item["sha256"])
            target = self.source / item["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        build_business_index(self.source, self.database, source_format="auto", verify_content=True)
        ensure_repository_search(self.database, self.source)
        q = "CBACT04C 的 ACCOUNT-FILE、FD-ACCTFILE-REC 和 ACCOUNT-RECORD 如何对应？能否确认真实文件路径？"
        output, requests = self.default_answer(q)
        investigation = output["agent_result"]["investigation_state"]["question_investigation"]
        entity = investigation["analysis_plan"]["file_entities"][0]
        self.assertEqual([(span["start_line"], span["end_line"]) for span in entity["source_versions"]], [(41, 45), (84, 88)])
        self.assertEqual(self.item(investigation, "file_io")["candidate_count"], 2)
        pages = [page for _, pages in requests for page in pages]
        self.assertTrue(any(page["relative_path"] == "app/cpy/CVACT01Y.cpy" for page in pages))
        self.assertTrue(any("READ ACCOUNT-FILE INTO ACCOUNT-RECORD" in page["source_text"] for page in pages))
        self.assertTrue(any("REWRITE FD-ACCTFILE-REC FROM  ACCOUNT-RECORD" in page["source_text"] for page in pages))


if __name__ == "__main__":
    unittest.main()
