"""Exercise saved writing requirements across actual source/history workflows.

Repository downloads and model responses are local test doubles; indexing,
conversation persistence, source restoration and request construction are real.
"""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from analyze_source import analyze_source
from company_api import CompanyAPIConfig, TransportResponse
from test_conversation_workbench import _WorkbenchWorkerGuard
from web_app import WorkbenchState


class AnswerInstructionsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.state_path = self.root / "state.json"
        self.requests = []
        self.truncate_first = False
        self.workers = _WorkbenchWorkerGuard(lambda: len(self.requests))
        self.addCleanup(self.cleanup)
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model", api_key="offline-only")
        self.local_source = self.root / "local-source"
        self.local_source.mkdir()
        self.write_source(self.local_source, 11)
        self.local_output = self.root / "local-output"
        self.instructions = '  请使用「业务目的」和「处理结果」。\n保留 "格式" 与换行。\n'
        self.app = self.new_app()
        self.app.configure_answer_instructions({"answer_instructions": self.instructions})
        self.finish(self.app.start({"source": str(self.local_source), "output": str(self.local_output),
                                    "source_format": "free"}))
        self.local_workspace = self.app.project["workspace_id"]

    def cleanup(self):
        self.workers.shutdown()
        self.temporary.cleanup()

    @staticmethod
    def write_source(directory, amount):
        # Identical names deliberately make source selection, not symbols,
        # responsible for keeping the two business rules separate.
        (directory / "decision.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. DECISION.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 RESULT-AMOUNT PIC 99.\n"
            f"PROCEDURE DIVISION.\nMOVE {amount} TO RESULT-AMOUNT.\nGOBACK.\n", encoding="utf-8")

    def transport(self, request):
        envelope = json.loads(request.body)
        self.requests.append(envelope)
        payload = json.loads(envelope["messages"][-1]["content"])
        pages = [page for bundle in payload["source_context"] for page in bundle["pages"]]
        page = next(page for page in pages if " TO RESULT-AMOUNT" in page["source_text"])
        amount = 11 if "MOVE 11 " in page["source_text"] else 29
        content = f"业务结果为{amount}：程序将结果金额设置为{amount}后返回调用方。[{page['evidence_id']}]"
        return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": content},
            "finish_reason": "length" if self.truncate_first and len(self.requests) == 1 else "stop"}]}, ensure_ascii=False))

    def new_app(self):
        app = self.workers.track(WorkbenchState(
            state_path=self.state_path, remote_source_root=self.root / "remote-sources",
            analyzer=lambda source, output, **options: analyze_source(source, output, transport=self.transport, **options),
            config_provider=lambda: self.config,
            policy_provider=lambda: AgentPolicy(max_model_requests=1, max_answer_revisions=0)))
        if app.framework_reference_path is None:
            app.framework_reference_path = str(self.root / "no-framework-reference")
        return app

    def finish(self, accepted):
        job = self.workers.wait(self.app, accepted["job_id"])
        self.assertEqual(job["status"], "COMPLETED", job.get("error"))
        return job["result"]

    def ask(self, question="DECISION 返回的 RESULT-AMOUNT 是多少？", **extra):
        return self.finish(self.app.start({"source": self.app.project["source"], "output": self.app.project["output"],
            "source_format": "free", "reading_strategy": "retrieval", "allow_network": True,
            "question": question, **extra}))

    def import_remote(self):
        def fetch(spec, destination, **unused):
            destination.mkdir()
            self.write_source(destination, 29)
            return {**spec.public_metadata(), "commit": "a" * 40}
        with patch("web_app.fetch_remote_source", side_effect=fetch):
            self.finish(self.app.start_remote({
                "url": "https://code.example.invalid/projects/DEMO/repos/business-source/browse/stage",
                "source_format": "free"}))
        self.remote_workspace = self.app.project["workspace_id"]

    def open(self, workspace, conversation):
        before = len(self.requests)
        self.app.open_source_conversation({"workspace_id": workspace, "conversation_id": conversation})
        self.assertEqual(len(self.requests), before)

    def assert_request(self, number, amount, instructions, *, history_amount=None):
        envelope = self.requests[number]
        self.assertEqual(envelope["messages"][0]["content"].count(instructions), 1)
        payload = json.loads(envelope["messages"][-1]["content"])
        source = "\n".join(page["source_text"] for bundle in payload["source_context"] for page in bundle["pages"])
        other = 29 if amount == 11 else 11
        self.assertIn(f"MOVE {amount} TO RESULT-AMOUNT", source)
        self.assertNotIn(f"MOVE {other} TO RESULT-AMOUNT", source)
        history = "\n".join(message["content"] for message in envelope["messages"][1:-1])
        self.assertNotIn(f"业务结果为{other}", history)
        if history_amount is not None:
            self.assertIn(f"业务结果为{history_amount}", history)
        return payload

    def test_restart_and_source_history_keep_global_requirements_and_separate_source_evidence(self):
        local = self.ask()
        local_conversation = local["conversation"]["id"]
        local_answer = local["conversation"]["messages"][-1]
        self.import_remote()
        remote = self.ask()
        remote_conversation = remote["conversation"]["id"]
        self.assert_request(0, 11, self.instructions)
        self.assert_request(1, 29, self.instructions)
        self.assertNotEqual(local["snapshot_id"], remote["snapshot_id"])

        changed = "请按新人能理解的业务流程解释，并给出来源。"
        self.app.configure_answer_instructions({"answer_instructions": changed})
        self.app = self.new_app()
        self.assertEqual(self.app.answer_instructions, changed)
        self.open(self.local_workspace, local_conversation)
        self.assertEqual(self.app.answer_instructions, changed)
        self.ask("这个金额是在哪里设定的？", conversation_id=local_conversation)
        self.assert_request(2, 11, changed, history_amount=11)
        reference = local_answer["evidence_refs"][0]
        evidence = self.app.evidence(reference["evidence_id"], local_conversation, local_answer["id"])
        self.assertEqual(evidence["spans"][0]["integrity"], "VALID")

        self.open(self.remote_workspace, remote_conversation)
        self.ask("这个金额是在哪里设定的？", conversation_id=remote_conversation)
        self.assert_request(3, 29, changed, history_amount=29)
        self.assertEqual(len(self.requests), 4)

    def test_retry_after_cross_source_switch_uses_new_prompt_and_original_question_source(self):
        self.truncate_first = True
        local = self.ask()
        local_conversation = local["conversation"]["id"]
        original_user, original_answer = local["conversation"]["messages"]
        self.assertEqual(original_answer["status"], "partial")
        self.import_remote()
        remote = self.ask()
        self.assertEqual(len(self.requests), 2)
        changed = "本次使用「新的业务说明」作为栏目，说明处理结果和依据。"
        self.app.configure_answer_instructions({"answer_instructions": changed})
        self.open(self.local_workspace, local_conversation)
        retried = self.ask(question="", conversation_id=local_conversation, retry_message_id=original_user["id"])
        payload = self.assert_request(2, 11, changed)
        self.assertEqual(payload["question"], original_user["content"])
        self.assertNotIn("draft_answer", payload)
        self.assertEqual(len(self.requests[2]["messages"]), 2)
        self.assertNotIn(self.instructions, self.requests[2]["messages"][0]["content"])
        self.assertEqual(retried["conversation"]["id"], local_conversation)
        self.assertEqual(len(retried["conversation"]["messages"]), 2)
        self.assertIn("业务结果为11", retried["conversation"]["messages"][-1]["content"])
        self.open(self.remote_workspace, remote["conversation"]["id"])
        self.assertIn("业务结果为29", self.app.conversation["messages"][-1]["content"])
        self.assertEqual(len(self.requests), 3)

    def test_continue_after_restart_uses_new_and_then_cleared_global_requirements(self):
        self.truncate_first = True
        local = self.ask()
        conversation = local["conversation"]["id"]
        original_user, original_answer = local["conversation"]["messages"]
        self.assertEqual(original_answer["status"], "partial")
        changed = "用「补充业务结果」说明新发现并保留引用。"
        self.app.configure_answer_instructions({"answer_instructions": changed})
        self.app = self.new_app()
        self.open(self.local_workspace, conversation)
        self.ask(question="", conversation_id=conversation, continue_message_id=original_user["id"])
        payload = self.assert_request(1, 11, changed)
        self.assertTrue(payload["draft_continuation"])
        self.assertEqual(payload["draft_answer"], original_answer["content"])
        self.assertNotIn(self.instructions, self.requests[1]["messages"][0]["content"])

        self.app.configure_answer_instructions({"answer_instructions": ""})
        self.app = self.new_app()
        self.open(self.local_workspace, conversation)
        self.ask(question="", conversation_id=conversation, continue_message_id=original_user["id"])
        system = self.requests[2]["messages"][0]["content"]
        self.assertNotIn(changed, system)
        self.assertNotIn(self.instructions, system)
        self.assertNotIn("用户在系统配置中保存的回答要求（原文）", system)
        self.assertIn("业务概览默认按以下方式组织", system)
        self.assertEqual(len(self.requests), 3)


if __name__ == "__main__":
    unittest.main()
