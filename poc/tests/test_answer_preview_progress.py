"""Prose must arrive during generation, including ordinary Markdown punctuation."""

import json
from pathlib import Path
import socket
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from answer_preview import visible_answer_prefix
import business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig
from repository_discovery import ensure_repository_search


class AnswerPreviewPrefixTests(unittest.TestCase):
    def test_citations_links_and_square_bracket_prose_are_not_protocol(self):
        for value in ("[ev_page_123] 已收到请求。", "[fw:123:1-4] 后续处理。",
                      "[业务说明](#details)\n请求已经受理。", "[受理] 后续步骤。",
                      "[1](#details) 请求已经受理。", "[2026] 年度结果说明。"):
            with self.subTest(value=value):
                self.assertEqual(visible_answer_prefix(value), value)

    def test_inline_braces_do_not_freeze_later_text(self):
        for value in ("输入 {KEY} 用于定位；之后返回结果。", "使用 `{KEY}` 标记，然后继续。"):
            self.assertEqual(visible_answer_prefix(value), value)

    def test_protocol_objects_arrays_and_partial_fences_stay_hidden(self):
        for value in ('{', '{"read":', '[', '[{"read":[]}]', '```', '```jso',
                      '```json\n{"search":["KEY"]}', '```\n{"search":[]}'):
            with self.subTest(value=value):
                self.assertEqual(visible_answer_prefix(value), "")
        self.assertEqual(visible_answer_prefix('需要补查。\n{"read":[{"relative_path":"entry.cbl"}]}'),
                         "需要补查。")
        self.assertEqual(visible_answer_prefix('需要补查：{"findings": [{"text":"protocol"}]}'), "需要补查：")

    def test_code_and_inline_examples_are_not_standalone_protocol(self):
        value = "```cobol\nMOVE 1 TO ACCEPTED-FLAG.\n```\n后续检查。"
        self.assertEqual(visible_answer_prefix(value), value)

    def test_credential_prefix_is_held_until_redaction_can_see_it(self):
        self.assertEqual(visible_answer_prefix("受理 test-cre", ("test-credential",)), "受理")


class AnswerPreviewDeliveryTests(unittest.TestCase):
    def test_real_chat_callbacks_deliver_markdown_and_braces_before_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "entry.cbl").write_bytes(
                b"IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRY-RULE.\nDATA DIVISION.\n"
                b"WORKING-STORAGE SECTION.\n01 ACCEPTED-FLAG PIC 9.\nPROCEDURE DIVISION.\n"
                b"MOVE 1 TO ACCEPTED-FLAG.\nGOBACK.\n")
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", quiet=True,
                                 framework_reference_path="")
            ensure_repository_search(database, source)
            events, before_complete = [], []
            elapsed = [0.0]
            clock = SimpleNamespace(monotonic=lambda: time.monotonic() + elapsed[0])

            def complete(*, messages, on_text_delta, **options):
                payload = json.loads(messages[-1]["content"])
                eid = payload["source_context"][0]["pages"][0]["evidence_id"]
                parts = [f"[{eid}] 请求进入后设置受理标志。", "输入标记 {KEY} 用于说明当前对象。",
                         "后续按受理状态继续处理。"]
                for part in parts:
                    elapsed[0] += .2
                    on_text_delta(part)
                    before_complete.append([event["answer_preview"]["text"] for event in events
                                            if event.get("answer_preview")])
                return {"choices": [{"message": {"content": "".join(parts)}, "finish_reason": "stop"}]}

            client = SimpleNamespace(complete=complete)
            config = CompanyAPIConfig("https://example.invalid/v1", "test-model", api_key="offline-credential")
            with mock.patch.object(business_chat, "OpenAICompatibleChatClient", return_value=client), \
                 mock.patch.object(business_chat, "time", clock), \
                 mock.patch.object(socket.socket, "connect", side_effect=AssertionError("Offline test")):
                business_chat.run_business_chat("entry.cbl 中受理状态如何设置？", database, source, config,
                    allow_network=False, framework_reference_path="", progress=events.append,
                    transport=mock.Mock(side_effect=AssertionError("Unexpected transport")),
                    policy=AgentPolicy(max_model_requests=1))
            self.assertTrue(before_complete[0], "Citation-first text was buffered until completion")
            self.assertIn("{KEY}", before_complete[1][-1])
            self.assertIn("后续按受理状态", before_complete[2][-1])
            self.assertGreater(len(set(before_complete[-1])), 1)


if __name__ == "__main__":
    unittest.main()
