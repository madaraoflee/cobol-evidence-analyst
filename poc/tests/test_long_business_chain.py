from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyze_source import analyze_source
from company_api import CompanyAPIConfig, TransportResponse


class LongBusinessChainTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.output = self.root / 'output'
        self.config = CompanyAPIConfig('https://gateway.example.invalid/v1', 'test-model', api_key='test-key')

    def test_large_entry_tail_call_reaches_seven_levels_and_keeps_local_business_result(self):
        names = [f'FLOW-{index}' for index in range(8)]
        for index, name in enumerate(names):
            body = f'CALL "{names[index + 1]}" USING REQUEST-AMOUNT.\n' if index + 1 < len(names) else (
                'IF REQUEST-AMOUNT > 5000\n    MOVE 7 TO REQUEST-AMOUNT\nEND-IF.\nCALL "UNAVAILABLE-STORE".\n')
            filler = ('*> source padding for a distant call\n' * 80000) if index == 0 else ''
            data = ('WORKING-STORAGE SECTION.\n' if index == 0 else 'LINKAGE SECTION.\n') + '01 REQUEST-AMOUNT PIC 9(8).\n'
            signature = 'PROCEDURE DIVISION' + ('.\n' if index == 0 else ' USING REQUEST-AMOUNT.\n')
            (self.source / f'{index}.cbl').write_text(
                f'IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n' + data + signature + filler + body + 'GOBACK.\n', encoding='utf-8')
        requests = []

        def chat(request):
            payload = json.loads(request.body)
            requests.append(payload)
            supplied = json.loads(payload['messages'][-1]['content'])
            refs = supplied.get('source_pages') or supplied.get('page_summaries') or []
            text = '已收到当前业务链的来源资料。' + (f"[{refs[0]['evidence_id']}]" if refs else '')
            return TransportResponse(200, json.dumps({'choices': [{'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}]}))

        report = analyze_source(self.source, self.output, entry='FLOW-0', question='最终金额由哪些业务条件决定？',
                                index_mode='catalog', analysis_mode='business', source_format='free',
                                config=self.config, transport=chat, allow_network=True,
                                framework_reference_path='', max_source_pages=12, quiet=True)
        self.assertTrue(report['source_manifest_verified'])
        self.assertEqual(report['scope']['selected_file_count'], 8)
        self.assertGreaterEqual(max(item['depth'] for item in report['scope']['dependency_scan']), 7)
        self.assertNotIn('DEPTH_LIMIT', {item['status'] for item in report['scope']['boundaries']})
        self.assertIn('MISSING_SOURCE', {item['status'] for item in report['scope']['boundaries']})
        self.assertTrue(requests)
        sent = json.dumps(requests, ensure_ascii=False)
        self.assertIn('REQUEST-AMOUNT > 5000', sent)
        for name in names:
            self.assertIn(name, sent)
        result = json.loads((self.output / 'agent-result.json').read_text(encoding='utf-8'))
        self.assertEqual(result['runner_status'], 'COMPLETED')
        self.assertTrue(result['agent_result']['narrative']['text'])
        self.assertFalse(result['agent_result']['reading_coverage']['complete'])

    def test_source_change_after_response_keeps_unaccepted_text_without_api_capture(self):
        entry = self.source / 'entry.cbl'
        entry.write_text('IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRY.\nPROCEDURE DIVISION.\nGOBACK.\n', encoding='utf-8')

        def chat(request):
            entry.write_text(entry.read_text(encoding='utf-8') + '*> changed\n', encoding='utf-8')
            return TransportResponse(200, json.dumps({'choices': [{'message': {'role': 'assistant', 'content': '这笔申请满足当前可见条件，但保存效果需要核对。'}, 'finish_reason': 'stop'}]}))

        report = analyze_source(self.source, self.output, entry='ENTRY', question='申请如何处理？',
                                index_mode='catalog', analysis_mode='business', source_format='free',
                                config=self.config, transport=chat, allow_network=True,
                                framework_reference_path='', capture_api_responses=False, quiet=True)
        result = json.loads((self.output / 'agent-result.json').read_text(encoding='utf-8'))
        self.assertFalse(report['source_manifest_verified'])
        self.assertIsNone(result['agent_result'])
        self.assertNotIn('api_diagnostics', result)
        self.assertEqual(result['unaccepted_response']['reason_code'], 'SOURCE_ANALYSIS_FAILED')
        self.assertIn('这笔申请', result['unaccepted_response']['text'])


if __name__ == '__main__':
    unittest.main()
