"""The default question path supplies indexed CL facts without a new gate."""
import hashlib
import json
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import batch_context_evidence
from batch_context_evidence import investigate_batch_context
from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from business_map import build_business_map
from company_api import CompanyAPIConfig, TransportResponse
from question_investigation import build_question_investigation
from repository_discovery import ensure_repository_search
from tools.desktop_acceptance_runtime import decoded_navigation

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/cl-batch-v1"
QUESTION = "BATCHPAY 批处理怎么触发？"


class BatchContextEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="batch-context-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        shutil.copytree(FIXTURE, self.source)
        self.database = self.root / "index.sqlite"
        build_business_index(self.source, self.database, source_format="free", verify_content=True,
            framework_reference_path="")
        ensure_repository_search(self.database, self.source)
        self.mapping = build_business_map(self.database, self.source, QUESTION)
        for target, name in ((socket, "create_connection"), (socket.socket, "connect"),
                             (socket.socket, "connect_ex")):
            patch = mock.patch.object(target, name, side_effect=AssertionError("offline test forbids network"))
            patch.start()
            self.addCleanup(patch.stop)

    def page(self, name):
        raw = (self.source / name).read_bytes()
        lines = raw.decode().splitlines()
        return {"relative_path": name, "source_sha256": hashlib.sha256(raw).hexdigest(),
            "start_line": 1, "end_line": len(lines), "source_text": "\n".join(lines),
            "evidence_id": "ev_test_" + name, "span_truncated": False}

    def all_pages(self):
        return [self.page(path.name) for path in self.source.iterdir()]

    def test_indexed_upstream_commands_produce_two_reads_then_bound_supplied_facts(self):
        before = investigate_batch_context(self.mapping, [self.page("batchpay.cbl")],
            database_path=self.database)
        self.assertEqual({action["arguments"]["relative_path"] for action in before["planned_actions"]},
            {"launcher.clle", "nightjob.clle"})
        after = investigate_batch_context(self.mapping, self.all_pages(), database_path=self.database)
        self.assertFalse(after["planned_actions"])
        context = after["cl_context"]
        self.assertEqual(context["unread_candidate_count"], 0)
        self.assertTrue({"SBMJOB", "CALL", "OVRDBF", "DLTOVR", "CHGLIBL", "ADDLIBLE", "MONMSG"}
            <= {row["command"] for row in context["observations"]})
        for row in context["observations"]:
            self.assertEqual(row["source_sha256"], self.page(row["relative_path"])["source_sha256"])
            self.assertTrue(row["evidence_ids"])
            self.assertFalse(row["runtime_execution_verified"])
        self.assertFalse(context["scope"]["scheduler_verified"])
        self.assertFalse(context["scope"]["effective_overrides_verified"])

    def test_cached_nomination_rechecks_current_evidence_visibility(self):
        cache = {}
        with mock.patch.object(batch_context_evidence, "nominate_cl_evidence",
                wraps=batch_context_evidence.nominate_cl_evidence) as nominate:
            before = investigate_batch_context(self.mapping, [], database_path=self.database, candidate_cache=cache)
            after = investigate_batch_context(self.mapping, self.all_pages(), database_path=self.database,
                candidate_cache=cache)
        self.assertEqual(nominate.call_count, 1)
        self.assertTrue(before["planned_actions"])
        self.assertFalse(after["planned_actions"])

    def test_wrong_versions_and_truncated_pages_do_not_bind_commands(self):
        for change in ({"source_sha256": "0" * 64}, {"span_truncated": True}):
            pages = [{**self.page(name), **change} for name in ("launcher.clle", "nightjob.clle")]
            result = investigate_batch_context(self.mapping, pages, database_path=self.database)
            self.assertEqual(result["cl_context"]["observations"], [])
            self.assertTrue(result["planned_actions"])

    def test_frontier_retains_unread_commands_without_new_blocking_obligation(self):
        result = investigate_batch_context(self.mapping, [], database_path=self.database, max_actions=0)
        self.assertFalse(result["planned_actions"])
        self.assertTrue(result["cl_context"]["frontier"])
        investigation = build_question_investigation(QUESTION, self.mapping, database_path=self.database,
            source_pages=self.all_pages())
        self.assertTrue(investigation["cl_context"]["observations"])
        self.assertTrue(investigation["can_answer"])
        self.assertFalse(any(item["kind"] == "cl_batch_context" for item in investigation["required_items"]))

    def test_default_brief_chat_supplies_commands_before_single_fake_response(self):
        requests = []
        def transport(request):
            envelope = json.loads(request.body)
            payload = decoded_navigation(json.loads(envelope["messages"][-1]["content"]))
            requests.append(payload)
            context = payload["question_investigation"]["cl_context"]
            observations = context["observations"]
            calls = [row for row in observations if row["command"] == "CALL"]
            self.assertTrue(any(row.get("target_name") == "NIGHTJOB" for row in calls))
            self.assertEqual(sum(row.get("target_name") == "BATCHPAY" for row in calls), 2)
            self.assertFalse({"FAKEJOB", "COMMENTJOB", "STRINGJOB"} & {row.get("target_name") for row in calls})
            submit = next(row for row in observations if row["command"] == "SBMJOB")
            override = next(row for row in observations if row["command"] == "OVRDBF")
            ids = {page["evidence_id"] for bundle in payload["source_context"] for page in bundle["pages"]}
            for row in observations:
                self.assertLessEqual(set(row["evidence_ids"]), ids)
            answer = (f"CL 源码提交 NIGHTJOB，再调用 BATCHPAY；声明库列表 RUNLIB DATALIB。"
                f"[{submit['evidence_ids'][0]}] ACCOUNTLF 的覆盖声明指向 DATALIB/ACCOUNTPF，"
                f"成员使用 &DAY，作用域为 *CALLLVL。[{override['evidence_ids'][0]}] "
                "这说明静态命令关系，实际定时调度与运行对象尚未确认。")
            return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant",
                "content": answer}, "finish_reason": "stop"}]}, ensure_ascii=False))
        config = CompanyAPIConfig("https://offline.example.invalid/v1", "neutral-model", api_key="offline-only")
        result = run_business_chat(QUESTION, self.database, self.source, config, transport=transport,
            allow_network=False, capture_context=True, framework_reference_path="", answer_detail="brief",
            policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0))
        self.assertEqual(result["runner_status"], "COMPLETED")
        self.assertEqual(len(requests), 1)
        self.assertIn("DATALIB/ACCOUNTPF", result["agent_result"]["answer"])
        self.assertIn("尚未确认", result["agent_result"]["answer"])


if __name__ == "__main__":
    unittest.main()
