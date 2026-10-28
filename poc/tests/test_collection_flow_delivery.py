"""A slow source-deferral answer must not end an available collection analysis."""

import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from question_budget import QuestionBudget
from repository_discovery import ensure_repository_search


class CollectionFlowDeliveryTests(unittest.TestCase):
    def test_slow_deferral_is_reviewed_with_all_steps_from_large_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            steps = [("CREDIT", 10), ("HOLDING", 20), ("REFUND", 30), ("REWARD", 40)]
            header = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. COLLECTION-RULE.\n"
                "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
                "01 BALANCE PIC 9(8)V99 VALUE 1000.\n"
                + "".join(f"01 {name}-AMOUNT PIC 9(6)V99 VALUE {amount}.\n"
                          for name, amount in steps)
                + "PROCEDURE DIVISION.\nMAIN.\n"
                + "".join(f"PERFORM APPLY-{name}.\n" for name, _ in steps)
                + "DISPLAY BALANCE.\nGOBACK.\n")
            body = "".join("*> neutral unrelated source context\n" * 1500
                + f"APPLY-{name}.\nIF BALANCE >= {name}-AMOUNT\n"
                + f"SUBTRACT {name}-AMOUNT FROM BALANCE\nEND-IF.\nEXIT.\n"
                for name, _ in steps)
            (source / "collection.cbl").write_text(header + body, encoding="utf-8")
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free")
            ensure_repository_search(database, source)
            requests = []
            now = [0.0]
            clock = SimpleNamespace(monotonic=lambda: now[0], time=lambda: 2000000000 + now[0])
            instructions = "先说明业务结论，再说明每项扣减条件和金额来源；不要只解释字段赋值。"

            def budget(*args, **kwargs):
                return QuestionBudget(*args, **{**kwargs, "clock": clock.monotonic,
                                               "wall_clock": clock.time})

            def transport(request):
                messages = json.loads(request.body)["messages"]
                self.assertIn(instructions, messages[0]["content"])
                payload = json.loads(messages[-1]["content"])
                requests.append(payload)
                self.assertLessEqual(len(requests), 2, "Useful review must remain bounded")
                pages = [page for bundle in payload["source_context"] for page in bundle["pages"]]
                supplied = "\n".join(page["source_text"] for page in pages)
                for name, amount in steps:
                    self.assertIn(f"PERFORM APPLY-{name}", supplied)
                    self.assertIn(f"IF BALANCE >= {name}-AMOUNT", supplied)
                    self.assertIn(f"SUBTRACT {name}-AMOUNT FROM BALANCE", supplied)
                    self.assertIn(f"{name}-AMOUNT PIC 9(6)V99 VALUE {amount}", supplied)
                if len(requests) == 1:
                    now[0] += 20
                    text = ("目前还不能从已提供的源码确认预存款、暂存款、退款、奖励款的扣减顺序，"
                        "也不能确认顺序按代码固定还是由配置决定。当前可见原文只有金额赋值；"
                        "没有展示各项如何进入计算、彼此先后或适用条件。"
                        "需要查看完整计算处理段落才能判断。")
                else:
                    flow = next(page for page in pages if "PERFORM APPLY-CREDIT" in page["source_text"])
                    text = ("收款先处理预存款，再处理暂存款，之后退款，最后奖励款。"
                        "主流程依次调用 APPLY-CREDIT、APPLY-HOLDING、APPLY-REFUND、APPLY-REWARD，"
                        f"所以顺序由程序固定。[{flow['evidence_id']}]\n")
                    for name, amount in steps:
                        page = next(page for page in pages
                                    if f"SUBTRACT {name}-AMOUNT" in page["source_text"])
                        declaration = next(page for page in pages
                            if f"{name}-AMOUNT PIC 9(6)V99 VALUE {amount}" in page["source_text"])
                        text += (f"当余额不少于{name}-AMOUNT时，从BALANCE扣除该金额；不足时跳过此项。"
                            f"[{page['evidence_id']}] {name}-AMOUNT初值为{amount}。"
                            f"[{declaration['evidence_id']}]\n")
                return TransportResponse(200, json.dumps({"choices": [{"message": {"content": text},
                    "finish_reason": "stop"}]}, ensure_ascii=False))

            with patch.object(business_chat, "time", clock), \
                    patch.object(business_chat, "QuestionBudget", side_effect=budget):
                result = business_chat.run_business_chat(
                    "COLLECTION-RULE中预存款、暂存款、退款、奖励款的扣减顺序是什么？"
                    "顺序按代码固定还是由配置决定？解释各项条件和金额来源。",
                    database, source, CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model",
                        api_key="offline-only"), transport=transport, framework_reference_path="",
                    answer_instructions=instructions)["agent_result"]
            self.assertEqual(len(requests), 2)
            self.assertEqual(result["status"], "ANALYZED")
            self.assertEqual(result["stop_reason"], "sufficient_material")
            self.assertIsNone(result["material_gap"])
            self.assertIn("顺序由程序固定", result["answer"])
            self.assertNotIn("需要查看完整计算处理段落", result["answer"])
            self.assertEqual(len(result["narrative"]["citations"]), 5)


if __name__ == "__main__":
    unittest.main()
