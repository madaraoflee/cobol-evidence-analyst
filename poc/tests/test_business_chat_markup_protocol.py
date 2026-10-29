"""Generic machine-call markup is recovered without execution or display."""

import json
from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from agent_policy import AgentPolicy
import business_chat
from business_index import build_business_index
from company_api import TransportResponse
from repository_discovery import ensure_repository_search
import test_completion_driven_chat as fixtures


def machine_read():
    return ('<wire:calls>\n<wire:invoke name="read">'
            '<wire:parameter name="relative_path">helper-7.cbl</wire:parameter>'
            '<wire:parameter name="start_line">1</wire:parameter>'
            '<wire:parameter name="end_line">12</wire:parameter>'
            '</wire:invoke>\n</wire:calls>')


class MarkupProtocolRecognitionTests(unittest.TestCase):
    def test_neutral_namespaces_and_decorated_machine_containers_are_invalid_actions(self):
        packets = (
            machine_read(),
            '<calls><invoke name="search">["AMOUNT"]</invoke></calls>',
            '<tool_calls><tool_call name="unavailable_tool">{}</tool_call></tool_calls>',
            '<wire:function_calls><wire:function_call function="read">{}</wire:function_call>'
            '</wire:function_calls>',
            '<~~wire~~ calls><~~wire~~ invoke name="search">["AMOUNT"]'
            '</~~wire~~ invoke></~~wire~~ calls>',
        )
        for packet in packets:
            with self.subTest(packet=packet):
                self.assertTrue(business_chat._tool_markup_envelope(packet))
                self.assertIsNone(business_chat._actions(packet))
                self.assertTrue(business_chat._action_reply_invalid(packet, None))

    def test_bad_closing_tags_and_incomplete_bodies_are_still_machine_packets(self):
        for packet in (
            '<wire:calls><wire:invoke name="search">["AMOUNT"]</wire:parameter>'
            '</wire:invoke></wire:calls>',
            '<wire:calls><wire:invoke name="read">{"relative_path":"helper-7.cbl"',
        ):
            with self.subTest(packet=packet):
                self.assertTrue(business_chat._tool_markup_envelope(packet))
                self.assertTrue(business_chat._action_reply_invalid(packet, None))

    def test_standalone_terminal_machine_packet_after_prose_is_still_protocol(self):
        text = "还需核对业务金额的处理条件。\n\n" + machine_read()
        self.assertTrue(business_chat._tool_markup_envelope(text))
        self.assertTrue(business_chat._action_reply_invalid(text, None))
        start = text.index("<")
        for length in range(start + 1, len(text) + 1):
            with self.subTest(length=length):
                self.assertTrue(business_chat._tool_markup_envelope(text[:length], partial=True))

    def test_explicit_machine_containers_are_invalid_even_without_complete_calls(self):
        for packet in ('<tool_calls>', '<tool_calls></tool_calls>', '<wire:tool_calls',
                       '<function_calls>', '<function_calls></function_calls>', '<wire:function_calls'):
            with self.subTest(packet=packet):
                self.assertTrue(business_chat._tool_markup_envelope(packet))
                self.assertTrue(business_chat._action_reply_invalid(packet, None))

    def test_business_xml_html_and_explicit_code_examples_remain_explanations(self):
        samples = (
            '<p>余额不足时进入人工复核。</p>',
            '<div data-kind="tool_calls">业务处理结果</div>',
            '<payment><amount>100</amount><status>READY</status></payment>',
            '<calls><invoke name="telephone">客户回访记录</invoke></calls>',
            '<invoke name="read">业务字段示例</invoke>',
            '<calls><description>调用统计说明</description></calls>',
            '```xml\n' + machine_read() + '\n```',
            '下面是 XML 协议示例：\n' + machine_read(),
            'For example:\n' + machine_read(),
            '已有业务说明。\n```xml\n' + machine_read() + '\n```',
            '业务记录如下：\n<payment><amount>100</amount></payment>',
            '业务结论已确认；`<wire:calls>` 是示例标签。',
        )
        for text in samples:
            with self.subTest(text=text):
                self.assertFalse(business_chat._tool_markup_envelope(text))
                self.assertFalse(business_chat._action_reply_invalid(text, None))

    def test_stream_prefixes_wait_until_the_first_tag_can_be_classified(self):
        packet = machine_read()
        for length in range(1, len(packet) + 1):
            with self.subTest(length=length):
                self.assertTrue(business_chat._tool_markup_envelope(packet[:length], partial=True))
        for prefix in ('<', '<p', '<payment', '<wire:'):
            with self.subTest(prefix=prefix):
                self.assertTrue(business_chat._tool_markup_envelope(prefix, partial=True))
                self.assertFalse(business_chat._tool_markup_envelope(prefix))
        for text in ('<p>', '<p>业务结果</p>', '<payment>', '<payment><amount>100</amount></payment>'):
            with self.subTest(text=text):
                self.assertFalse(business_chat._tool_markup_envelope(text, partial=True))


class MarkupProtocolRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CompletionDrivenChatTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.requests = []
        self.events = []

    def ask(self, respond, *, request_guard, stream_first=False, policy=None):
        fixture = self.fixture
        build_business_index(fixture.source, fixture.database, source_format="free", verify_content=True,
                             framework_reference_path="", quiet=True)
        ensure_repository_search(fixture.database, fixture.source)

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            self.requests.append(payload)
            self.assertLessEqual(len(self.requests), request_guard, "Malformed markup did not converge")
            text = respond(payload, len(self.requests))
            if stream_first and len(self.requests) == 1:
                body = b"".join(('data: ' + json.dumps({"choices": [{"index": 0,
                    "delta": {"content": character}, "finish_reason": None}]}, ensure_ascii=False)
                    + '\n\n').encode() for character in text)
                body += ('data: ' + json.dumps({"choices": [{"index": 0, "delta": {},
                    "finish_reason": "stop"}]}) + '\n\ndata: [DONE]\n\n').encode()
                return TransportResponse(200, body, {"Content-Type": "text/event-stream"})
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": text},
                "finish_reason": "stop"}]}, ensure_ascii=False))

        result = business_chat.run_business_chat(fixture.question, fixture.database, fixture.source,
            fixture.config, framework_reference_path="", transport=transport, progress=self.events.append,
            **({"policy": policy} if policy is not None else {}))["agent_result"]
        self.assertLessEqual(len(self.requests), request_guard)
        self.assertTrue(all(payload["question"] == fixture.question for payload in self.requests))
        return result

    def test_machine_packet_then_valid_json_actually_reads_new_source_before_answering(self):
        observed = []

        def respond(payload, number):
            if number == 1:
                return machine_read()
            if number == 2:
                observed.append(payload)
                return self.fixture.read_helper(7)
            return self.fixture.cited_answer(payload)

        result = self.ask(respond, request_guard=3)
        self.assertEqual(len(self.requests), 3)
        self.assertGreater(observed[0]["investigation_budget"]["reads_per_turn"], 0)
        self.assertFalse(any(row.get("read", {}).get("relative_path") == "helper-7.cbl"
                             for row in observed[0]["completed_actions"]))
        third = self.requests[2]
        page = next(page for page in self.fixture.pages(third) if page["relative_path"] == "helper-7.cbl")
        self.assertEqual(page["source_text"], self.fixture.files["helper-7.cbl"].rstrip("\n"))
        self.assertTrue(any(row.get("read", {}).get("relative_path") == "helper-7.cbl"
                            for row in third["completed_actions"]))
        self.assertIn("金额按基础金额乘以二得到20", result["answer"])
        self.assertTrue(result["narrative"]["citations"])
        self.assertNotIn("<wire:", result["answer"])
        self.assertTrue(any(row.get("reason") == "investigation_action_recovery"
                            and row.get("next_step") == "correct_action" for row in result["boundaries"]))

    def test_repeated_machine_packets_stop_without_executing_their_requested_read(self):
        result = self.ask(lambda *_: machine_read(), request_guard=2)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result["stop_reason"], "INVALID_INVESTIGATION_ACTION")
        self.assertFalse(any(row.get("read", {}).get("relative_path") == "helper-7.cbl"
                             for payload in self.requests for row in payload["completed_actions"]))
        self.assertNotIn("<wire:", result["answer"])

    def test_discovery_recovery_explicitly_corrects_markup_and_keeps_json_read_available(self):
        self.fixture.question = "UnknownVioletFeature"

        def respond(payload, number):
            if number == 1:
                return machine_read()
            if number == 2:
                return json.dumps({"read": [{"relative_path": "root.cbl", "start_line": 1,
                    "end_line": len(self.fixture.files["root.cbl"].splitlines())}]})
            return self.fixture.cited_answer(payload)

        result = self.ask(respond, request_guard=3)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.fixture.pages(self.requests[0]), [])
        recovery = self.requests[1]
        self.assertEqual(self.fixture.pages(recovery), [])
        self.assertEqual(recovery["retrieval_status"]["state"], "needs_discovery")
        self.assertIn("上一轮返回的标签调用格式无效", recovery["task"])
        self.assertIn("JSON", recovery["task"])
        self.assertGreater(recovery["investigation_budget"]["reads_per_turn"], 0)
        self.assertGreater(recovery["investigation_budget"]["searches_per_turn"], 0)
        self.assertFalse(any(row.get("read", {}).get("relative_path") == "helper-7.cbl"
                             for row in recovery["completed_actions"]))
        supplied = next(page for page in self.fixture.pages(self.requests[2])
                        if page["relative_path"] == "root.cbl")
        self.assertEqual(supplied["source_text"], self.fixture.files["root.cbl"].rstrip("\n"))
        self.assertIn("金额按基础金额乘以二得到20", result["answer"])
        self.assertTrue(result["narrative"]["citations"])
        self.assertNotIn("<wire:", result["answer"])

    def test_final_answer_request_does_not_earn_a_markup_recovery_request(self):
        result = self.ask(lambda *_: machine_read(), request_guard=1,
                          policy=AgentPolicy(max_model_requests=1))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result["stop_reason"], "INVALID_INVESTIGATION_ACTION")
        self.assertNotIn("<wire:", result["answer"])

    def test_character_split_machine_packet_never_leaks_into_answer_previews(self):
        result = self.ask(lambda payload, number: machine_read() if number == 1
                          else self.fixture.cited_answer(payload), request_guard=2, stream_first=True)
        self.assertEqual(len(self.requests), 2)
        previews = [event["answer_preview"]["text"] for event in self.events if event.get("answer_preview")]
        self.assertTrue(previews, "The corrected business answer should still be displayed")
        self.assertTrue(all("<" not in text for text in previews), previews)
        self.assertIn("金额按基础金额乘以二得到20", result["answer"])
        self.assertNotIn("<wire:", result["answer"])

    def test_terminal_machine_packet_does_not_leak_after_a_prose_preview(self):
        text = "还需核对业务金额的处理条件。\n\n" + machine_read()
        result = self.ask(lambda payload, number: text if number == 1
                          else self.fixture.cited_answer(payload), request_guard=2, stream_first=True)
        previews = [event["answer_preview"]["text"] for event in self.events if event.get("answer_preview")]
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all("<wire:" not in preview for preview in previews), previews)
        self.assertNotIn("<wire:", result["answer"])
        self.assertIn("金额按基础金额乘以二得到20", result["answer"])


if __name__ == "__main__":
    unittest.main()
