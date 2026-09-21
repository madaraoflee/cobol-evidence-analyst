from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import tracemalloc
import unittest

POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))
from report_view import DIRECT_REPORT_BYTES, project_report, write_report_view
from web_app import _read_json


class ReportViewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'agent-result.json'

    def large_report(self):
        report = {'runner_status': 'COMPLETED', 'agent_result': {
            'page_summaries': [{'text': 'Repeated page explanation ' * 12000}] * 65,
            'snapshot_id': 'sha256:current-source', 'status': 'ANALYZED',
            'narrative': {'text': 'Business output: "approved" \\ saved.\n业务说明。'},
            'answer': 'The business rule applies to approved requests.',
            'reading_coverage': {'sent_pages': 1400, 'summarized_pages': 1400, 'complete': True},
            'evidence_refs': [{'evidence_id': 'ev_page_1', 'relative_path': 'entry.cbl', 'start_line': 1}],
        }, 'api_diagnostics': {'exchanges': [{'sequence': i, 'body_text': 'reply'} for i in range(150)]}}
        self.path.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
        self.assertGreater(self.path.stat().st_size, DIRECT_REPORT_BYTES)
        write_report_view(self.path, report)
        return report

    def test_large_report_preserves_answer_snapshot_coverage_and_reports_display_omissions(self):
        original = self.large_report()
        tracemalloc.start()
        loaded = _read_json(self.path)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        self.assertLess(peak, 4_000_000, "browser read must not load the complete large report")
        self.assertEqual(loaded['agent_result']['narrative'], original['agent_result']['narrative'])
        self.assertEqual(loaded['agent_result']['snapshot_id'], 'sha256:current-source')
        self.assertEqual(loaded['agent_result']['reading_coverage'], original['agent_result']['reading_coverage'])
        self.assertEqual(loaded['agent_result']['evidence_refs'], original['agent_result']['evidence_refs'])
        self.assertEqual(loaded['agent_result']['page_summaries'], [])
        self.assertEqual([x['sequence'] for x in loaded['api_diagnostics']['exchanges']], list(range(50, 150)))
        self.assertTrue(loaded['display_projection']['complete_report_on_disk'])
        self.assertTrue(any(item['total_items'] == 65 for item in loaded['display_projection']['omitted']))
        self.assertEqual(json.loads(self.path.read_text())['agent_result']['page_summaries'], original['agent_result']['page_summaries'])
        self.assertLess(self.path.with_name('agent-result-view.json').stat().st_size, 100000)

    def test_modified_or_partial_original_never_uses_a_stale_view(self):
        self.large_report()
        with self.path.open('r+b') as stream:
            stream.seek(-1, 2)
            stream.write(b'!')
        with self.assertRaisesRegex(ValueError, 'does not match'):
            _read_json(self.path)
        with self.path.open('r+b') as stream:
            stream.truncate(DIRECT_REPORT_BYTES + 1)
        with self.assertRaisesRegex(ValueError, 'does not match'):
            _read_json(self.path)

    def test_malformed_view_or_small_report_is_rejected_by_standard_json_parser(self):
        self.large_report()
        view = self.path.with_name('agent-result-view.json')
        for bad in ('{"agent_result":', '{"text":"bad\\escape"}', '{"text":"unterminated}'):
            view.write_text(bad, encoding='utf-8')
            with self.assertRaises(ValueError):
                _read_json(self.path)
        self.path.write_text('{"text":"unterminated}', encoding='utf-8')
        with self.assertRaises(ValueError):
            _read_json(self.path)

    def test_small_reports_remain_byte_equivalent_and_need_no_companion(self):
        report = {'agent_result': {'narrative': {'text': '"quoted" \\ tab\t\n业务'}}}
        self.path.write_text(json.dumps(report), encoding='utf-8')
        write_report_view(self.path, report)
        self.assertEqual(_read_json(self.path), report)
        self.assertFalse(self.path.with_name('agent-result-view.json').exists())

    def test_large_single_fields_are_bounded_without_changing_coverage_counts(self):
        report = {'agent_result': {'answer': '业务' * 700000,
                  'reading_coverage': {'total_pages': 100000, 'sent_pages': 99999},
                  'tool_trace': [{'arguments': {'query': '<script>x</script>'}}] * 2000}}
        view = project_report(report)
        self.assertEqual(len(view['agent_result']['answer']), 1000000)
        self.assertEqual(view['agent_result']['reading_coverage']['sent_pages'], 99999)
        self.assertEqual(len(view['agent_result']['tool_trace']), 300)
        self.assertGreater(view['display_projection']['omitted_field_count'], 0)


if __name__ == '__main__':
    unittest.main()
