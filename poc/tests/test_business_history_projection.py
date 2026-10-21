"""Provider history is navigation context, not prior-answer factual authority."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import _history_messages, _history_projection_mode, run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search, read_repository_context


class BusinessHistoryProjectionTests(unittest.TestCase):
    def history(self):
        return [
            {"role": "user", "content": "Explain the input file."},
            {"role": "assistant", "content": "Earlier unverified conclusion: all failures return 77.",
             "status": "ANALYZED", "claims_semantically_verified": False,
             "evidence_refs": [{"evidence_id": "old", "relative_path": "worker.cbl"}]},
            {"role": "user", "content": "Which conditions change the result?"},
            {"role": "assistant", "content": "Not every failure returns 77; the earlier answer was wrong.",
             "status": "PARTIAL"}]

    def test_standalone_source_questions_omit_even_analyzed_assistant_prose(self):
        history = self.history()
        original = deepcopy(history)
        questions = (
            "继续核对 RECORD-RULE 业务边界：空输入时文件怎样处理，查询异常是否总能返回？",
            "請解釋 RECORD-RULE 的輸入輸出及例外處理。",
            "Explain RECORD-RULE's inputs, output files and exception paths.",
            "What does the CALL in worker.cbl do?",
            "RESULT-RULE", "Explain CAPACITY.", "Explain the `rate` field.",
        )
        for question in questions:
            with self.subTest(question=question):
                self.assertEqual(_history_projection_mode(question), "source_question")
                projected = _history_messages(history, question=question)
                self.assertEqual(projected, [item for item in history if item["role"] == "user"])
                self.assertNotIn("77", json.dumps(projected))
        self.assertEqual(history, original)

    def test_explicit_prior_answer_requests_preserve_wrong_and_negated_originals(self):
        history = self.history()
        for question in (
            "请引用上一答中关于异常的结论并更正。",
            "請把上一段關於程式的說明改寫得簡短。",
            "你刚才说所有异常都返回77，请检查这句话。",
            "Review your previous answer about the program.",
            "Quote what you said about the output.",
            "Rewrite the previous response without removing the caveat.",
            "第二点的程序解释是什么意思？",
            "Explain the exception in that answer.",
            "Explain the exception in this response.",
            "解释那条回答中的程序异常。",
            "請解釋這條回答裡的例外。",
        ):
            with self.subTest(question=question):
                projected = _history_messages(history, question=question)
                self.assertEqual(projected, [{"role": x["role"], "content": x["content"]} for x in history])
                self.assertIn("Not every failure", projected[-1]["content"])

    def test_ambiguous_followups_unknown_language_and_legacy_keep_context(self):
        history = self.history()
        for question in (None, "", "继续", "再详细一点", "那超过上限会怎样？",
                         "这个程序为什么这样处理？", "那这个字段呢？",
                         "这个字段为什么是 00？", "这个公式为什么要乘 3？", "Explain that formula.",
                         "Explain the exception.", "Explain the formula.", "Explain the rule.",
                         "Explain this program.", "解释源码。", "这个程序呢？",
                         "Why does this program do that?", "What about this field?",
                         "Why?", "Step-by-step please.", "それはどういう意味ですか？"):
            with self.subTest(question=question):
                self.assertEqual(_history_messages(history, question=question),
                    [{"role": x["role"], "content": x["content"]} for x in history])
        self.assertEqual(_history_messages(history, AgentPolicy(max_history_characters=0),
                                          question="Explain this program."), [])

    def test_ordinary_source_projection_retains_real_user_text_within_budget(self):
        history = self.history()
        history[0]["content"] *= 100
        projected = _history_messages(history, AgentPolicy(max_history_characters=200),
                                      question="Explain RECORD-RULE in the source.")
        self.assertLessEqual(sum(len(x["content"]) for x in projected), 200)
        self.assertTrue(all(x["role"] == "user" for x in projected))
        self.assertEqual(projected[-1]["content"], history[2]["content"])

    def _check_actual_wire(self, question, *, preserve=False):
        source = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. RECORD-RULE.\nDATA DIVISION.\n"
                  "WORKING-STORAGE SECTION.\n01 INPUT-COUNT PIC 99.\n01 OUTPUT-COUNT PIC 99.\n"
                  "PROCEDURE DIVISION.\nCOMPUTE OUTPUT-COUNT = INPUT-COUNT * 2.\nGOBACK.\n")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source_root = root / "source"; source_root.mkdir()
            (source_root / "worker.cbl").write_bytes(source.encode())
            database = root / "index.sqlite"
            build_business_index(source_root, database, source_format="free", verify_content=True)
            ensure_repository_search(database, source_root)
            page = read_repository_context(database, source_root, relative_path="worker.cbl",
                                           start_line=1, end_line=9, max_chars=10000)["pages"][0]
            ref = {key: page[key] for key in ("evidence_id", "relative_path", "source_sha256",
                                             "start_line", "end_line")}
            history = [{"role": "user", "content": "Explain RECORD-RULE."},
                       {"role": "assistant", "content": "POISON_OLD_EXPLANATION: source multiplies by 999.",
                        "status": "ANALYZED", "claims_semantically_verified": False,
                        "evidence_refs": [ref]}]
            before = deepcopy(history); observed = []
            def transport(request):
                wire = json.loads(request.body); observed.append(wire)
                expected = ([{"role": x["role"], "content": x["content"]} for x in history]
                            if preserve else [history[0]])
                self.assertEqual(wire["messages"][1:-1], expected)
                if not preserve:
                    self.assertNotIn("POISON_OLD_EXPLANATION", request.body.decode())
                payload = json.loads(wire["messages"][-1]["content"])
                pages = payload["source_context"][0]["pages"]
                actual = next(p for p in pages if p["relative_path"] == "worker.cbl")
                self.assertEqual(actual["source_sha256"], hashlib.sha256(source.encode()).hexdigest())
                self.assertIn("COMPUTE OUTPUT-COUNT = INPUT-COUNT * 2", actual["source_text"])
                return TransportResponse(200, json.dumps({"choices": [{
                    "message": {"role": "assistant", "content":
                                f"输出数量按输入数量的两倍计算。[{actual['evidence_id']}]"},
                    "finish_reason": "stop"}]}))
            result = run_business_chat(question, database, source_root,
                CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-view", api_key="unused"),
                history=history, transport=transport, framework_reference_path="",
                policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0))["agent_result"]
            self.assertEqual(len(observed), 1)
            self.assertEqual(history, before)
            self.assertIn("两倍", result["answer"])
            metrics = result["metrics"]
            self.assertEqual(metrics["history_messages"], 2)
            self.assertEqual(metrics["wire_history_messages"], 2 if preserve else 1)
            self.assertEqual(metrics["wire_history_characters"],
                             sum(len(x["content"]) for x in history) if preserve else len(history[0]["content"]))
            self.assertEqual(metrics["wire_history_assistant_messages"], int(preserve))
            self.assertEqual(metrics["wire_history_projection_mode"], _history_projection_mode(question))

    def test_actual_wire_omits_poison_but_preserves_prior_source_navigation(self):
        self._check_actual_wire("RECORD-RULE 的输出公式是什么？")

    def test_actual_wire_keeps_ambiguous_deictic_followups_and_explicit_rewrite(self):
        for question in ("这个程序为什么这样处理？", "那这个字段呢？",
                         "Why does this program do that?",
                         "这个字段为什么是 00？", "这个公式为什么要乘 3？", "Explain that formula.",
                         "Explain the exception in that answer.", "Explain the exception.",
                         "请重写上一答关于程序的内容，保留原先的否定条件。"):
            with self.subTest(question=question):
                self._check_actual_wire(question, preserve=True)


if __name__ == "__main__":
    unittest.main()
