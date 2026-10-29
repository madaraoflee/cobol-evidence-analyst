"""Known source plus a syntax inventory must not look like a business answer."""

import unittest

import test_completion_driven_chat as fixtures
from business_synthesis import assess_answer_completion


class BusinessRefusalRecoveryTests(unittest.TestCase):
    def test_blanket_refusal_with_bare_assignment_observation_is_revisited(self):
        for observation in (
            "当前可见的是一个赋值语句。",
            "目前只能看到一条赋值语句。",
            "目前只能看到一条 MOVE 语句。",
            "当前可见的是一个 `MOVE` 语句。",
            "當前可見的是一個賦值語句。",
            "The current excerpt only contains a MOVE statement.",
        ):
            with self.subTest(observation=observation):
                fixture = fixtures.CompletionDrivenChatTests()
                fixture.setUp()
                try:
                    denied = "当前源码不足以确认计算规则。" + observation

                    def reply(payload, number):
                        pages = fixture.pages(payload)
                        self.assertTrue(any("COMPUTE TOTAL-AMOUNT = BASE-AMOUNT * 2" in
                                            page["source_text"] for page in pages))
                        return fixture.cited_answer(payload, denied) if number == 1 else fixture.cited_answer(payload)

                    result = fixture.ask(reply, request_guard=2,
                        answer_instructions="用业务语言解释计算结果，不要介绍框架。")
                    # Assert outside the transport: adapter error handling must
                    # not swallow a failing request-content assertion.
                    self.assertEqual(len(fixture.requests), 2)
                    self.assertTrue(all(any("COMPUTE TOTAL-AMOUNT = BASE-AMOUNT * 2" in p["source_text"]
                                            for p in fixture.pages(request)) for request in fixture.requests))
                    self.assertIn("基础金额初值为10", result["answer"])
                    self.assertIn("得到20", result["answer"])
                    self.assertEqual(result["status"], "ANALYZED")
                    self.assertFalse(result["claims_semantically_verified"])
                finally:
                    fixture.doCleanups()

    def test_actual_business_explanation_with_runtime_limit_does_not_regenerate(self):
        fixture = fixtures.CompletionDrivenChatTests()
        fixture.setUp()
        try:
            explanation = "基础金额初值为10，金额按基础金额乘以二得到20。当前源码不能确认生产当天实际传入的金额。"
            result = fixture.ask(lambda payload, number: fixture.cited_answer(payload, explanation),
                request_guard=1, answer_instructions="只解释已知业务规则，简短说明与本题有关的限制。")
            self.assertEqual(len(fixture.requests), 1)
            self.assertIn(explanation, result["answer"])
        finally:
            fixture.doCleanups()

    def test_actual_execution_wording_does_not_hide_a_source_rule_refusal(self):
        self.assertEqual(assess_answer_completion(
            "当前可见的是一个赋值语句。当前源码不足以确认实际执行的扣减顺序。"
        )["status"], "incomplete")
        self.assertEqual(assess_answer_completion(
            "The current excerpt only contains a MOVE statement. The current source cannot confirm the actual execution order."
        )["status"], "incomplete")

    def test_runtime_only_limit_with_syntax_observation_does_not_promise_impossible_reading(self):
        self.assertEqual(assess_answer_completion(
            "当前可见的是一个调用语句。当前源码不能确认生产当天是否实际执行。"
        )["status"], "not_assessed")

    def test_observation_without_deferral_is_not_automatically_rejected(self):
        self.assertEqual(assess_answer_completion("当前可见的是一个赋值语句。")["status"], "not_assessed")
        self.assertEqual(assess_answer_completion(
            "当前可见的语句把基础金额乘以二得到最终金额。当前源码不足以确认实际输入。")["status"], "not_assessed")


if __name__ == "__main__":
    unittest.main()
