"""Adversarial provenance and citation visibility checks for prior source memory."""

from copy import deepcopy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from analysis_continuation import AnalysisContinuation
from agent_policy import AgentPolicy
from business_chat import _action_reply_invalid, _actions, _fit_request, _request_manifest
from company_api import CompanyAPIConfig
from prior_source_memory import PriorSourceMemory, source_binding_sha256
from run_store import RunStore
from source_reading import _identify_page
import test_completion_driven_chat as chat_fixtures


class PriorSourceMemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "index.sqlite"
        self.question = "说明金额的输入与计算条件。"
        self.context = {"source_format": "free", "compilation_context": {"dialect": "neutral"}}
        self.memory = self.new_memory()
        self.page = self.make_page()
        self.payload = {"question": self.question, "source_context": [{"pages": [self.page]}]}
        self.record = self.round_record([self.page])

    def new_memory(self, **overrides):
        return PriorSourceMemory(self.database, **{
            "question": self.question, "run_id": "neutral-run", "context": self.context, **overrides})

    @staticmethod
    def make_page(**overrides):
        text = "IF INPUT-AMOUNT > ZERO\nCOMPUTE RESULT-AMOUNT = INPUT-AMOUNT * 2\nEND-IF."
        page = {"relative_path": "rule.cbl", "start_line": 1, "end_line": 3,
                "source_sha256": hashlib.sha256(text.encode()).hexdigest(), "source_text": text,
                "span_truncated": False, "include_chain": [], **overrides}
        page["evidence_id"] = _identify_page(page)
        return page

    @staticmethod
    def round_record(pages, number=1):
        return {"round_id": f"round-{number}", "request_body_sha256": hashlib.sha256(str(number).encode()).hexdigest(),
                "sources": [{"evidence_id": page["evidence_id"], "path": page["relative_path"],
                    "source_sha256": page["source_sha256"], "start_line": page["start_line"],
                    "end_line": page["end_line"], "excerpt_sha256": hashlib.sha256(page["source_text"].encode()).hexdigest(),
                    "binding_sha256": source_binding_sha256(page),
                    "include_chain": deepcopy(page.get("include_chain", [])),
                    **{key: deepcopy(page[key]) for key in ("replacement_trace", "copy_instance") if key in page}}
                    for page in pages]}

    def note(self, page=None, text="正数输入参与金额计算。"):
        identifier = (page or self.page)["evidence_id"]
        return {"text": text + f" [{identifier}]", "evidence_ids": [identifier]}

    def accept(self, note=None, **overrides):
        return self.memory.accept_findings([note or self.note()], **{
            "payload": self.payload, "round_record": self.record, **overrides})

    def inherited_payload(self):
        return {"question": self.question, "source_context": [{"pages": []}],
                "prior_source_memory": self.memory.prompt()}

    def test_only_exact_actual_round_sources_can_establish_memory(self):
        other = self.make_page(relative_path="unseen.cbl")
        forged = {"question": self.question, "source_context": [{"pages": [other]}]}
        self.assertEqual(self.accept(self.note(other), payload=forged), 0)
        for key, value in (("excerpt_sha256", "f" * 64), ("source_sha256", "e" * 64),
                           ("end_line", 20), ("path", "other.cbl")):
            with self.subTest(key=key):
                record = deepcopy(self.record)
                record["sources"][0][key] = value
                self.assertEqual(self.accept(round_record=record), 0)
        for key, value in (("request_body_sha256", None), ("request_body_sha256", ""),
                           ("request_body_sha256", "g" * 64), ("round_id", None)):
            with self.subTest(key=key, value=value):
                record = deepcopy(self.record)
                record[key] = value
                self.assertEqual(self.accept(round_record=record), 0)
        self.assertEqual(self.memory.prompt()["blocks"], [])
        self.assertEqual(self.accept(), 1)

    def test_invalid_or_framework_reference_rejects_the_entire_note(self):
        identifier = self.page["evidence_id"]
        invalid = [
            {"text": "混合结论", "evidence_ids": [identifier, "ev_page_unknown"]},
            {"text": f"结论 [{identifier}] [ev_page_unknown]", "evidence_ids": [identifier]},
            {"text": f"混合结论 [{identifier}] [fw:manual]", "evidence_ids": [identifier, "fw:manual"]},
        ]
        for note in invalid:
            with self.subTest(note=note):
                self.assertEqual(self.accept(note), 0)
                self.assertEqual(self.memory.prompt()["blocks"], [])
                self.assertEqual(self.memory.checkpoint()["pages"], [])

    def test_malformed_findings_are_not_repaired_into_memory(self):
        note = self.note()
        for findings in (note, "finding", [None], [{**note, "extra": True}],
                         [{**note, "evidence_ids": note["evidence_ids"][0]}],
                         [{**note, "evidence_ids": [None]}], [{**note, "text": " "}],
                         [note, {"text": "missing IDs"}]):
            with self.subTest(findings=findings):
                self.assertEqual(self.memory.accept_findings(findings, payload=self.payload,
                                                            round_record=self.record), 0)
                self.assertEqual(self.memory.prompt()["blocks"], [])

    def test_memory_block_is_atomic_and_its_removal_revokes_citation_permission(self):
        self.assertEqual(self.accept(), 1)
        payload = self.inherited_payload()
        self.assertEqual(self.memory.visible_ids(payload), {self.page["evidence_id"]})
        self.assertEqual(self.memory.effective_pages(payload, verify_archive=True), [self.page])
        for key, replacement in (("text", "改写结论"), ("evidence_ids", []), ("origins", [])):
            changed = deepcopy(payload)
            changed["prior_source_memory"]["blocks"][0][key] = replacement
            self.assertEqual(self.memory.visible_ids(changed), set())
            self.assertEqual(self.memory.effective_pages(changed), [])
        payload["prior_source_memory"]["blocks"] = []
        self.assertEqual(self.memory.visible_ids(payload), set())
        self.assertEqual(self.memory.effective_pages(payload), [])
        self.assertTrue(self.memory.checkpoint()["pages"], "Wire trimming must not erase the local archive")

    def test_draft_and_tool_metadata_cannot_authorize_unsupplied_source(self):
        note = self.note()
        payload = {"question": self.question, "source_context": [{"pages": []}],
                   "draft_answer": note["text"], "findings": [note],
                   "evidence_groups": [{"semantic_analysis": {"tool_results": [{
                       "evidence_ids": note["evidence_ids"], "source_text": self.page["source_text"]}]}}]}
        self.assertEqual(self.accept(note, payload=payload), 0)
        self.assertEqual(self.memory.accept_draft(note["text"], payload=payload, round_record=self.record), 0)
        self.assertEqual(self.memory.prompt()["blocks"], [])

    def test_actual_request_fitting_removes_whole_blocks_and_recomputes_visibility(self):
        pages = [self.page, self.make_page(relative_path="other.cbl")]
        for number, page in enumerate(pages, 1):
            self.assertEqual(self.accept(self.note(page, text="文" * 3900),
                payload={"source_context": [{"pages": [page]}]},
                round_record=self.round_record([page], number)), 1)
        original_blocks = self.memory.prompt()["blocks"]
        payload = {"question": self.question, "repository": {}, "business_map": {},
                   "framework_references": [], "prior_source_memory": self.memory.prompt(),
                   "retrieval_status": {"state": "needs_discovery"},
                   "investigation_budget": {"searches_per_turn": 3},
                   "source_context": [{"pages": [], "call_chain": {"links": [], "omitted_links": 0},
                                       "outline": [], "notices": []}]}
        trims = []
        config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model", api_key="offline-only")
        messages, size = _fit_request(config, payload, [], AgentPolicy(max_request_bytes=32768),
                                     source_memory=self.memory, trim_events=trims)
        self.assertLessEqual(size, 32768)
        wire = json.loads(messages[-1]["content"])
        retained = wire.get("prior_source_memory", {}).get("blocks", [])
        self.assertLess(len(retained), len(original_blocks))
        self.assertTrue(retained)
        self.assertTrue(all(block in original_blocks for block in retained))
        retained_ids = {identifier for block in retained for identifier in block["evidence_ids"]}
        self.assertEqual(wire["response_mode"], "business_analysis")
        self.assertEqual(wire["source_selection"]["prior_memory_page_count"], len(retained_ids))
        self.assertEqual(set(_request_manifest(payload, self.memory)["memory_source_ids"]), retained_ids)
        self.assertEqual({page["evidence_id"] for page in self.memory.effective_pages(payload)}, retained_ids)
        self.assertEqual(len(self.memory.checkpoint()["pages"]), 2)
        self.assertTrue(any(item["reason"] == "prior_source_memory_request_bytes" for item in trims))

    def test_restore_rejects_other_question_run_context_and_changed_round_proof(self):
        self.assertEqual(self.accept(), 1)
        saved = self.memory.checkpoint()
        restored = self.new_memory()
        restored.restore(saved, [self.record])
        self.assertEqual(restored.prompt(), self.memory.prompt())
        for overrides in ({"question": "另一个问题"}, {"run_id": "other-run"},
                          {"context": {"source_format": "fixed"}}):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(ValueError, "PRIOR_SOURCE_MEMORY_INVALID"):
                self.new_memory(**overrides).restore(saved, [self.record])
        for field in ("request_body_sha256", "sources"):
            changed = deepcopy(self.record)
            changed[field] = "0" * 64 if field == "request_body_sha256" else []
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "PRIOR_SOURCE_MEMORY_INVALID"):
                self.new_memory().restore(saved, [changed])

    def test_copy_and_replacement_bindings_must_match_the_actual_round(self):
        original = self.make_page(include_chain=[{"relative_path": "root.cbl", "line": 8}],
                                  replacement_trace=[{"from": "OLD-FIELD", "to": "INPUT-AMOUNT"}],
                                  copy_instance="instance-a")
        record = self.round_record([original])
        mutations = [dict(include_chain=[{"relative_path": "other.cbl", "line": 8}]),
                     dict(replacement_trace=[{"from": "OLD-FIELD", "to": "OTHER-AMOUNT"}]),
                     dict(copy_instance="instance-b")]
        for mutation in mutations:
            self.memory = self.new_memory()
            changed = self.make_page(**{**original, **mutation})
            payload = {"source_context": [{"pages": [changed]}]}
            with self.subTest(mutation=mutation):
                self.assertEqual(self.accept(self.note(changed), payload=payload, round_record=record), 0)
                self.assertEqual(self.memory.prompt()["blocks"], [])

    def test_restore_rejects_a_changed_or_missing_archived_excerpt(self):
        self.assertEqual(self.accept(), 1)
        saved = self.memory.checkpoint()
        archive = self.database.parent / "versioned-evidence.sqlite"
        with closing(sqlite3.connect(archive)) as connection, connection:
            connection.execute("UPDATE excerpts SET text=? WHERE evidence_id=?",
                               ("MOVE ZERO TO RESULT-AMOUNT.", self.page["evidence_id"]))
        with self.assertRaisesRegex(ValueError, "PRIOR_SOURCE_MEMORY_INVALID"):
            self.new_memory().restore(saved, [self.record])
        archive.unlink()
        with self.assertRaisesRegex(ValueError, "PRIOR_SOURCE_MEMORY_INVALID"):
            self.new_memory().restore(saved, [self.record])

    def test_resupply_keeps_original_provenance_and_draft_can_use_inherited_source(self):
        self.assertEqual(self.accept(), 1)
        original_prompt = self.memory.prompt()
        later_record = self.round_record([self.page], number=2)
        self.assertEqual(self.accept(self.note(text="只有正数输入参与计算。"), round_record=later_record), 1)
        self.assertEqual(self.memory.visible_ids({"prior_source_memory": original_prompt}),
                         {self.page["evidence_id"]})
        restored = self.new_memory()
        restored.restore(self.memory.checkpoint(), [self.record, later_record])
        self.assertEqual(restored.prompt(), self.memory.prompt())
        inherited = self.inherited_payload()
        self.assertEqual(self.memory.accept_draft(self.note(text="输入须为正数。 ")["text"],
            payload=inherited, round_record=self.round_record([], number=3)), 1)
        updated = self.inherited_payload()
        self.assertEqual(self.memory.accept_draft(self.note(text="正数输入按公式计算。 ")["text"],
            payload=updated, round_record=self.round_record([], number=4)), 1)
        self.assertEqual(self.memory.visible_ids(self.inherited_payload()), {self.page["evidence_id"]})

    def test_rephrased_memory_does_not_reset_evidence_no_progress(self):
        tracker = AnalysisContinuation(self.question)
        self.assertTrue(tracker.observe([self.page], [], {}))
        for text in ("正数输入参与计算。", "该计算使用正数输入。"):
            self.assertEqual(self.accept(self.note(text=text)), 1)
            tracker.awaiting_tools = True
            self.assertFalse(tracker.observe(self.memory.effective_pages(self.inherited_payload()), [], {}))
        self.assertEqual(tracker.stop_reason, "no_evidence_progress")

    def test_findings_protocol_never_becomes_a_business_answer(self):
        for value in ({"findings": [self.note()]}, {"findings": "malformed"},
                      {"read": [], "findings": [self.note()]},
                      [{"findings": [self.note()]}],
                      {"findings": [self.note()], "answer": "不能直接把协议对象当答案"}):
            with self.subTest(value=value):
                text = json.dumps(value, ensure_ascii=False)
                self.assertTrue(_action_reply_invalid(text, _actions(text)))
        for text in ('{"findings": [', '```json\n{"findings": [\n```'):
            with self.subTest(text=text):
                self.assertTrue(_action_reply_invalid(text, _actions(text)))
        explanation = '程序输出的 JSON 字段为 {"findings": "本次检查结果"}，随后返回。'
        self.assertFalse(_action_reply_invalid(explanation, _actions(explanation)))
        text = json.dumps({"read": [{"relative_path": "other.cbl", "start_line": 1, "end_line": 4}],
                           "findings": [self.note()]})
        action = _actions(text)
        self.assertTrue(action["read"])
        self.assertNotIn("findings", action)
        self.assertFalse(_action_reply_invalid(text, action))


class PriorSourceMemoryChatSafetyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = chat_fixtures.CompletionDrivenChatTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_provider_echoed_credential_is_redacted_before_memory_and_next_request(self):
        fixture = self.fixture
        protected = fixture.config.resolve_api_key()

        def respond(payload, number):
            if number == 1:
                page = next(page for page in fixture.pages(payload) if page["relative_path"] == "root.cbl")
                action = json.loads(fixture.read_helper(1))
                action["findings"] = [{"text": f"已读取金额公式 {protected} [{page['evidence_id']}]",
                                       "evidence_ids": [page["evidence_id"]]}]
                return json.dumps(action, ensure_ascii=False)
            return fixture.cited_answer(payload)

        result = fixture.ask(respond, request_guard=2)
        self.assertEqual(len(fixture.requests), 2)
        self.assertTrue(result["model_answer_recorded"])
        memory = fixture.requests[-1]["prior_source_memory"]
        self.assertTrue(memory["blocks"])
        self.assertNotIn(protected, json.dumps(memory, ensure_ascii=False))
        saved = RunStore(fixture.root / "investigation-runs.sqlite").load(result["run_id"])["checkpoint"]
        self.assertNotIn(protected, json.dumps(saved["prior_source_memory"], ensure_ascii=False))

    def test_findings_envelope_answer_field_cannot_skip_protocol_recovery(self):
        fixture = self.fixture

        def respond(payload, number):
            if number == 1:
                page = next(page for page in fixture.pages(payload) if page["relative_path"] == "root.cbl")
                return json.dumps({"answer": "错误的协议内答案不应显示。", "findings": [{
                    "text": f"金额公式 [{page['evidence_id']}]", "evidence_ids": [page["evidence_id"]]}]},
                    ensure_ascii=False)
            return fixture.cited_answer(payload)

        result = fixture.ask(respond, request_guard=2)
        self.assertEqual(len(fixture.requests), 2)
        self.assertTrue(result["model_answer_recorded"])
        self.assertNotIn("错误的协议内答案", result["answer"])
        self.assertNotIn("findings", result["answer"])
        self.assertEqual(fixture.requests[-1].get("prior_source_memory", {}).get("blocks", []), [])
        self.assertTrue(any(item.get("reason") == "investigation_action_recovery" for item in result["boundaries"]))


if __name__ == "__main__":
    unittest.main()
