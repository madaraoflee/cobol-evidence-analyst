from pathlib import Path
import hashlib
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run_store import RunStore
from business_index import build_business_index
from business_chat import run_business_chat
from company_api import CompanyAPIConfig, TransportResponse
from agent_policy import AgentPolicy
from repository_discovery import ensure_repository_search


class RunStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = RunStore(self.root / "runs.sqlite")

    def test_remote_request_start_is_uncertain_until_completion(self):
        self.store.create("run", {"question_id": "question"})
        self.store.start_request("run", "request", "hash")
        self.store.checkpoint("run", {"frontier": [{"tool": "read", "arguments": {"start_line": 20}}]})
        restored = RunStore(self.store.path).load("run")
        self.assertEqual(len(restored["uncertain_requests"]), 1)
        self.assertEqual(restored["checkpoint"]["frontier"][0]["arguments"]["start_line"], 20)
        self.store.finish_request("run", "request", {"remote_response": {"content": "done"}})
        self.assertFalse(self.store.load("run")["uncertain_requests"])

    def test_summary_change_invalidates_callers_transitively(self):
        self.store.cache("parse", "leaf", {}, ["copy:common"])
        self.store.cache("summary", "leaf", {}, ["parse:leaf"])
        self.store.cache("summary", "middle", {}, ["summary:leaf"])
        self.store.cache("summary", "entry", {}, ["summary:middle"])
        self.store.cache("parse", "independent", {}, ["copy:other"])
        self.assertEqual(len(self.store.invalidate("copy:common")), 4)
        self.assertIsNotNone(self.store.cached("parse", "independent"))
        self.assertIsNone(self.store.cached("summary", "entry"))

    def test_unfinished_lookup_keeps_older_matching_context(self):
        self.store.create("original", {"question_id": "same", "analysis_identity": "entry-a",
            "configuration": {"model_fingerprint": "model"}})
        for index in range(40):
            self.store.create("later-" + str(index), {"question_id": "same", "analysis_identity": "entry-b",
                "configuration": {"model_fingerprint": "model"}})
        self.assertEqual(self.store.unfinished("same", "model", "entry-a")["run_id"], "original")

    def setup_chat(self):
        self.source = self.root / "source"
        self.source.mkdir()
        self.source.joinpath("entry.cbl").write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRYPOINT.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 IN-AMOUNT PIC 9(5) VALUE 4.\n"
            "01 OUT-AMOUNT PIC 9(5).\nPROCEDURE DIVISION.\nMAIN.\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nGOBACK.\n")
        self.database = self.root / "facts.sqlite"
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "offline-model", api_key="offline-only")

    def test_process_interruption_after_response_resumes_without_another_remote_request(self):
        self.setup_chat()
        received, calls = False, []
        def transport(request):
            nonlocal received
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            calls.append(payload)
            evidence = payload["source_context"][0]["pages"][0]["evidence_id"]
            received = True
            return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant",
                "content": f"OUT-AMOUNT = IN-AMOUNT * 2。[{evidence}]"}, "finish_reason": "stop"}]}))
        def interrupt():
            if received:
                raise RuntimeError("simulated_process_interruption")
        policy = AgentPolicy(max_model_requests=1)
        with self.assertRaisesRegex(RuntimeError, "simulated_process"):
            run_business_chat("ENTRYPOINT OUT-AMOUNT 怎么计算？", self.database, self.source,
                self.config, transport=transport, check_cancel=interrupt, policy=policy)
        resumed = run_business_chat("ENTRYPOINT OUT-AMOUNT 怎么计算？", self.database, self.source,
            self.config, transport=transport, policy=policy)
        self.assertEqual(len(calls), 1)
        self.assertIn("IN-AMOUNT * 2", resumed["agent_result"]["answer"])
        journal = RunStore(self.root / "investigation-runs.sqlite").load(resumed["agent_result"]["run_id"])
        self.assertEqual(journal["state"], "completed")
        self.assertFalse(journal["checkpoint"]["exactly_once"])

    def test_interrupted_remote_request_does_not_blindly_repeat(self):
        self.setup_chat()
        calls = []
        def transport(request):
            calls.append(request)
            raise KeyboardInterrupt("process_lost_during_request")
        with self.assertRaises(KeyboardInterrupt):
            run_business_chat("ENTRYPOINT OUT-AMOUNT 怎么计算？", self.database, self.source, self.config, transport=transport)
        resumed = run_business_chat("ENTRYPOINT OUT-AMOUNT 怎么计算？", self.database, self.source, self.config, transport=transport)
        self.assertEqual(resumed["reason_code"], "REMOTE_REQUEST_OUTCOME_UNKNOWN")
        self.assertEqual(len(calls), 1)
        self.assertTrue(resumed["agent_result"]["resume_state"]["uncertain_requests"])

    def test_changed_entry_uses_its_own_run_and_older_entry_can_resume(self):
        self.setup_chat()
        self.source.joinpath("other.cbl").write_text((self.source / "entry.cbl").read_text()
            .replace("ENTRYPOINT", "OTHERPOINT").replace("IN-AMOUNT * 2", "IN-AMOUNT * 3"))
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        received, calls = False, []
        def transport(request):
            nonlocal received
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            calls.append(payload)
            selected = payload["business_map"]["direct_paths"]
            page = next(page for page in payload["source_context"][0]["pages"]
                        if page["relative_path"] in selected and "COMPUTE OUT-AMOUNT" in page["source_text"])
            factor = 3 if page["relative_path"] == "other.cbl" else 2
            received = True
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content":
                f"OUT-AMOUNT = IN-AMOUNT * {factor}。[{page['evidence_id']}]"}, "finish_reason": "stop"}]}))
        def interrupt():
            if received:
                raise RuntimeError("simulated_process_interruption")
        question, policy = "OUT-AMOUNT 怎么计算？", AgentPolicy(max_model_requests=1)
        for entry in ("ENTRYPOINT", "OTHERPOINT"):
            received = False
            with self.assertRaisesRegex(RuntimeError, "simulated_process_interruption"):
                run_business_chat(question, self.database, self.source, self.config, entry_program=entry,
                                  transport=transport, check_cancel=interrupt, policy=policy)
        self.assertEqual(len(calls), 2)
        older = run_business_chat(question, self.database, self.source, self.config, entry_program="ENTRYPOINT",
                                  transport=transport, policy=policy)
        newer = run_business_chat(question, self.database, self.source, self.config, entry_program="OTHERPOINT",
                                  transport=transport, policy=policy)
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(older["agent_result"]["run_id"], newer["agent_result"]["run_id"])
        self.assertIn("IN-AMOUNT * 2", older["agent_result"]["answer"])
        self.assertIn("IN-AMOUNT * 3", newer["agent_result"]["answer"])
        self.assertEqual(calls[-1]["business_map"]["direct_paths"], ["other.cbl"])
        self.assertTrue(any(ref["relative_path"] == "other.cbl"
                            for ref in newer["agent_result"]["evidence_refs"]))

    def test_explicit_resume_rejects_changed_conversation_or_framework_target(self):
        self.setup_chat()
        calls = []
        def transport(request):
            calls.append(request)
            raise KeyboardInterrupt("process_lost_during_request")
        question = "ENTRYPOINT OUT-AMOUNT 怎么计算？"
        with self.assertRaises(KeyboardInterrupt):
            run_business_chat(question, self.database, self.source, self.config, transport=transport)
        prior = RunStore(self.root / "investigation-runs.sqlite").unfinished(
            "sha256:" + hashlib.sha256(question.encode()).hexdigest(), hashlib.sha256(self.config.chat_model.encode()).hexdigest())
        for changed in ({"history": [{"role": "user", "content": "刚才是在另一种条件下。"}]},
                        {"framework_reference_path": self.root / "different-manual.md"},
                        {"answer_detail": "brief"}):
            with self.subTest(changed=changed):
                result = run_business_chat(question, self.database, self.source, self.config, transport=transport,
                                          resume_run_id=prior["run_id"], **changed)
                self.assertEqual(result["reason_code"], "RESUME_IDENTITY_MISMATCH")
        self.assertEqual(len(calls), 1)

    def test_pending_run_does_not_replay_under_changed_pipeline_version(self):
        from unittest import mock
        self.setup_chat()
        calls = []
        def transport(request):
            calls.append(request)
            raise KeyboardInterrupt("process_lost_during_request")
        question = "ENTRYPOINT OUT-AMOUNT 怎么计算？"
        with self.assertRaises(KeyboardInterrupt):
            run_business_chat(question, self.database, self.source, self.config, transport=transport)
        with mock.patch("business_chat.PIPELINE_VERSION", "different-pipeline"):
            result = run_business_chat(question, self.database, self.source, self.config, transport=transport)
        self.assertEqual(result["reason_code"], "RESUME_PIPELINE_VERSION_CHANGED")
        self.assertEqual(len(calls), 1)

    def test_unchanged_source_hash_with_changed_compilation_context_cannot_resume(self):
        from compilation_context import CompilationContext
        self.setup_chat()
        calls = []
        def transport(request):
            calls.append(request)
            raise KeyboardInterrupt("process_lost_during_request")
        question = "ENTRYPOINT OUT-AMOUNT 怎么计算？"
        with self.assertRaises(KeyboardInterrupt):
            run_business_chat(question, self.database, self.source, self.config, transport=transport)
        journal = RunStore(self.root / "investigation-runs.sqlite")
        prior = journal.unfinished("sha256:" + hashlib.sha256(question.encode()).hexdigest(),
                                  hashlib.sha256(self.config.chat_model.encode()).hexdigest())
        original_checkpoint = prior["checkpoint"]
        original_hash = hashlib.sha256((self.source / "entry.cbl").read_bytes()).hexdigest()
        build_business_index(self.source, self.database, source_format="free", verify_content=True,
            compilation_context=CompilationContext(source_format="free", framework_version="R2.0",
                compile_conditions=(("FEATURE", True),)))
        self.assertEqual(hashlib.sha256((self.source / "entry.cbl").read_bytes()).hexdigest(), original_hash)
        result = run_business_chat(question, self.database, self.source, self.config, transport=transport,
                                   resume_run_id=prior["run_id"])
        self.assertEqual(result["reason_code"], "RESUME_COMPILATION_CONTEXT_CHANGED")
        self.assertEqual(len(calls), 1)
        retained = journal.load(prior["run_id"])
        self.assertEqual(retained["checkpoint"], original_checkpoint)
        self.assertEqual(len(retained["uncertain_requests"]), 1)

    def test_completed_archive_keeps_original_context_and_does_not_rerun(self):
        from compilation_context import CompilationContext
        self.setup_chat()
        calls = []
        def transport(request):
            calls.append(request)
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            evidence = next(page for page in payload["source_context"][0]["pages"] if "IN-AMOUNT * 2" in page["source_text"])
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content":
                f"OUT-AMOUNT = IN-AMOUNT * 2。[{evidence['evidence_id']}]"}, "finish_reason": "stop"}]}))
        question = "ENTRYPOINT OUT-AMOUNT 怎么计算？"
        policy = AgentPolicy(max_model_requests=1)
        original = run_business_chat(question, self.database, self.source, self.config, transport=transport, policy=policy)
        build_business_index(self.source, self.database, source_format="free", verify_content=True,
                             compilation_context=CompilationContext(source_format="free", framework_version="R2.0"))
        restored = run_business_chat(question, self.database, self.source, self.config, transport=transport,
                                    policy=policy, resume_run_id=original["agent_result"]["run_id"])
        self.assertEqual(restored, json.loads(json.dumps(original)))
        self.assertEqual(len(calls), 1)
