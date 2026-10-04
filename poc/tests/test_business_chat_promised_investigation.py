"""Offline regressions for investigation promises presented as final answers."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from business_synthesis import assess_answer_completion
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search


PLAN = (
    '我先确认一下这里的“出 bill”具体指哪段流程：目前材料显示 BILL-RULE '
    '涉及账单日期校验、账单金额累计和账单明细处理，但还不足以串出完整的出单规则；'
    'CHARGE-RULE 里也有收费收取相关逻辑，未必是同一条流程。\n\n'
    '我会继续核对 BILL-RULE 的账单生成与输出步骤，再按“何时出单、账单金额怎么算、'
    '哪些情况会跳过或调整”说明。'
)
ANSWER = '账单日期已到且金额为正时，将本金加费用生成账单金额；否则本次不出账。'


class PromisedInvestigationCompletionTests(unittest.TestCase):
    def test_overview_and_future_plan_are_not_a_finished_answer(self):
        for reply in (PLAN, '我会继续核对账单生成步骤，再按何时出单、如何计算说明。',
                      '我將繼續核對帳單生成步驟，再說明適用條件。',
                      "I will continue checking the billing rules, then explain when billing runs."):
            with self.subTest(reply=reply):
                result = assess_answer_completion(reply)
                self.assertEqual(result['status'], 'incomplete')
                self.assertTrue(result['pending_investigation'])

    def test_known_rules_with_a_promise_remain_unfinished_without_discarding_text(self):
        result = assess_answer_completion(ANSWER + '\n我会继续核对外部打印步骤。')
        self.assertTrue(result['pending_investigation'])
        self.assertEqual(result['status'], 'incomplete')

    def test_program_behavior_and_quoted_examples_are_not_analyst_promises(self):
        for reply in (ANSWER, '程序将继续累计金额并写入明细。',
                      '程序输出“我会继续核对”并返回。',
                      '程序输出“步骤未完成。我会继续核对账单。”并返回。',
                      '先核对条件：基础值大于零时乘以系数，否则归零。',
                      '该字段保存提示文本。\n```text\n我会继续核对。\n```',
                      '该字段保存提示文本。\n> 我会继续核对。',
                      'The program will check the date and write the billing record.'):
            with self.subTest(reply=reply):
                result = assess_answer_completion(reply)
                self.assertFalse(result['pending_investigation'])
                self.assertNotEqual(result['status'], 'incomplete')


class PromisedInvestigationChatTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.database = self.root / 'index.sqlite'
        (self.source / 'billing.cbl').write_text(
            'IDENTIFICATION DIVISION.\nPROGRAM-ID. BILL-RULE.\n'
            'DATA DIVISION.\nWORKING-STORAGE SECTION.\n'
            '01 BILL-DATE PIC 9(8) VALUE 20300101.\n01 TODAY-DATE PIC 9(8) VALUE 20300102.\n'
            '01 PRINCIPAL PIC 9(7)V99 VALUE 10.\n01 FEE PIC 9(5)V99 VALUE 2.\n'
            '01 BILL-AMOUNT PIC 9(8)V99 VALUE ZERO.\n'
            'PROCEDURE DIVISION.\nMAIN.\n'
            '*> bill billing statement generation\n'
            'IF BILL-DATE <= TODAY-DATE AND PRINCIPAL > ZERO\n'
            'COMPUTE BILL-AMOUNT = PRINCIPAL + FEE\n'
            'DISPLAY BILL-AMOUNT\nEND-IF.\nGOBACK.\n', encoding='utf-8')
        build_business_index(self.source, self.database, source_format='free', quiet=True,
                             framework_reference_path='')
        ensure_repository_search(self.database, self.source)
        self.config = CompanyAPIConfig('https://gateway.example.invalid/v1', 'synthetic-model',
                                       api_key='synthetic-key')
        self.requests = []
        self.replies = []

    def ask(self, replies, *, limit=5, policy=None):
        def transport(request):
            payload = json.loads(json.loads(request.body)['messages'][-1]['content'])
            self.requests.append(payload)
            reply = replies[min(len(self.requests) - 1, len(replies) - 1)]
            if isinstance(reply, int):
                return TransportResponse(reply, '{"error":{"code":"server_error"}}')
            if callable(reply):
                reply = reply(payload)
            self.replies.append(reply)
            return TransportResponse(200, json.dumps({'choices': [{'message': {
                'role': 'assistant', 'content': reply}, 'finish_reason': 'stop'}]}, ensure_ascii=False))

        return run_business_chat('出 bill 的逻辑是什么？', self.database, self.source, self.config,
            transport=transport, framework_reference_path='',
            policy=policy or AgentPolicy(max_model_requests=limit))['agent_result']

    @staticmethod
    def cited(text):
        def respond(payload):
            page = next(page for page in payload['source_context'][0]['pages']
                        if 'COMPUTE BILL-AMOUNT' in page['source_text'])
            return f"{text}[{page['evidence_id']}]"
        return respond

    def test_plan_gets_actual_tool_opportunity_before_final_business_answer(self):
        result = self.ask([self.cited(PLAN),
            '{"read":[{"relative_path":"billing.cbl","start_line":1,"end_line":17}]}',
            self.cited(ANSWER)])
        self.assertEqual(len(self.requests), 3)
        self.assertGreater(self.requests[1]['investigation_budget']['reads_per_turn'], 0)
        self.assertNotIn('answer_review', self.requests[1])
        self.assertGreaterEqual(result['metrics']['tool_calls']['read'], 1)
        self.assertIn(ANSWER, result['answer'])
        self.assertFalse(result['business_review']['answer_completion']['pending_investigation'])
        self.assertEqual(result['status'], 'ANALYZED')

    def test_repeated_promises_do_not_become_success(self):
        result = self.ask([self.cited(PLAN)], limit=3)
        self.assertGreaterEqual(len(self.requests), 2)
        self.assertLessEqual(len(self.requests), 3)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['stop_reason'], 'answer_incomplete')
        self.assertTrue(result['business_review']['answer_completion']['pending_investigation'])

    def test_one_request_budget_does_not_promote_promise(self):
        result = self.ask([self.cited(PLAN)], limit=1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['stop_reason'], 'answer_incomplete')

    def test_followup_500_keeps_known_rules_and_original_citations_without_retry(self):
        draft = ANSWER + '\n我会继续核对外部打印步骤。'
        result = self.ask([self.cited(draft), 500])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result['answer'], self.replies[0])
        self.assertTrue(result['model_answer_recorded'])
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['diagnostics'][-1]['http_status'], 500)
        self.assertEqual(result['metrics']['provider_retries'], [])
        self.assertTrue(result['narrative']['citations'])
        first_ids = {page['evidence_id'] for page in self.requests[0]['source_context'][0]['pages']}
        self.assertLessEqual({ref['evidence_id'] for ref in result['narrative']['citations']}, first_ids)
        trace = json.loads(Path(result['metrics']['quality_trace_path']).read_text(encoding='utf-8'))
        self.assertEqual(trace['final']['final_answer_round_id'], 'round-1')

    def test_concrete_short_answer_finishes_without_extra_requests(self):
        result = self.ask([self.cited(ANSWER)])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result['status'], 'ANALYZED')

    def test_repeated_promise_does_not_replace_known_rules_before_failure(self):
        draft = ANSWER + '\n我会继续核对外部打印步骤。'
        result = self.ask([self.cited(draft), '我会继续核对账单生成步骤，再说明。', 500])
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(result['answer'], self.replies[0])
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertTrue(result['narrative']['citations'])
        self.assertEqual(result['diagnostics'][-1]['http_status'], 500)
        trace = json.loads(Path(result['metrics']['quality_trace_path']).read_text(encoding='utf-8'))
        self.assertEqual(trace['final']['final_answer_round_id'], 'round-1')

    def test_final_revision_promise_does_not_replace_partial_rules_or_citations(self):
        draft = ANSWER + '\n我会继续核对外部打印步骤。'
        promise = '我会继续核对账单生成步骤，再说明。'
        result = self.ask([self.cited(draft), promise, promise])
        self.assertEqual(len(self.requests), 3)
        self.assertIn('answer_review', self.requests[-1])
        self.assertEqual(result['answer'], self.replies[0])
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['stop_reason'], 'answer_incomplete')
        self.assertTrue(result['narrative']['citations'])
        trace = json.loads(Path(result['metrics']['quality_trace_path']).read_text(encoding='utf-8'))
        self.assertEqual(trace['final']['final_answer_round_id'], 'round-1')

    def test_promise_after_real_source_progress_keeps_first_draft_on_followup_failure(self):
        source = (
            'IDENTIFICATION DIVISION.\nPROGRAM-ID. BILL-RULE.\nDATA DIVISION.\n'
            'WORKING-STORAGE SECTION.\n'
            + ''.join(f'01 BILL-AMOUNT-{index} PIC 9(7) VALUE 1.\n' for index in range(10))
            + 'PROCEDURE DIVISION.\nMAIN.\n'
            + ''.join(f'PERFORM BILL-STEP-{index}.\n' for index in range(10)) + 'GOBACK.\n'
            + ''.join('*> neutral source separation\n' * 600
                + f'BILL-STEP-{index}.\nCOMPUTE BILL-AMOUNT-{index} = BILL-AMOUNT-{index} * 2.\nEXIT.\n'
                for index in range(10))
        )
        (self.source / 'billing.cbl').write_text(source, encoding='utf-8')
        build_business_index(self.source, self.database, source_format='free', quiet=True,
                             framework_reference_path='')
        ensure_repository_search(self.database, self.source)
        policy = AgentPolicy(max_model_requests=5, max_business_context_actions_per_turn=0,
                             max_evidence_groups=0)
        draft = '已读金额按原值乘以二。\n我会继续核对其余账单处理步骤。'
        result = self.ask([self.cited(draft), '我会继续核对账单生成步骤，再说明。', 500],
                          policy=policy)
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(self.requests[0]['question_investigation']['planned_actions'])
        first_reads = sum('read' in action for action in self.requests[0]['completed_actions'])
        next_reads = sum('read' in action for action in self.requests[1]['completed_actions'])
        self.assertGreater(next_reads, first_reads)
        self.assertNotIn('回复承诺继续调查，但尚未执行补查', self.requests[1]['task'])
        self.assertEqual(result['answer'], self.replies[0])
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['diagnostics'][-1]['http_status'], 500)
        self.assertEqual(result['metrics']['provider_retries'], [])
        first_ids = {page['evidence_id'] for page in self.requests[0]['source_context'][0]['pages']}
        self.assertTrue(result['narrative']['citations'])
        self.assertLessEqual({ref['evidence_id'] for ref in result['narrative']['citations']}, first_ids)
        trace = json.loads(Path(result['metrics']['quality_trace_path']).read_text(encoding='utf-8'))
        self.assertEqual(trace['final']['final_answer_round_id'], 'round-1')


if __name__ == '__main__':
    unittest.main()
