"""Repeat only seven synthetic quota scenarios; never contact a real endpoint."""
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import traceback
import unittest
import urllib.error
from unittest.mock import Mock, patch

SOURCE = Path('/private/tmp/cobol-open-defect-audit-20261109')
OUTPUT = Path('/private/tmp/cobol-quota-evidence-20261110')
EXPECTED_COMMIT = '9dae4b3e756eeaea5cd822d5fa8cc18d0ec16eae'
TEST_ID = ('test_workbench_failed_handoff.WorkbenchFailedHandoffTests.'
           'test_actual_http500_diagnostic_does_not_hide_two_generations_from_next_question')
FILES = ('poc/company_api.py', 'poc/api_error_details.py', 'poc/answer_diagnostics.py',
         'poc/business_chat.py', 'poc/web_app.py', 'poc/conversation_analysis_handoff.py',
         'poc/tests/test_business_chat_api_errors.py', 'poc/tests/test_workbench_failed_handoff.py')
sys.path[:0] = [str(SOURCE / 'poc/tests'), str(SOURCE / 'poc')]
from company_api import (APIClientError, CompanyAPIConfig, OpenAICompatibleChatClient,
                         TransportResponse, UrllibTransport)
from test_business_chat_api_errors import BusinessChatAPIErrorsTests
import test_workbench_failed_handoff as handoff


def git(*args):
    return subprocess.check_output(['git', *args], cwd=SOURCE, text=True).strip()


def file_hashes():
    return {name: hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() for name in FILES}


report = {
    'schema_version': 'offline-quota-evidence/v1',
    'started_at_utc': datetime.now(timezone.utc).isoformat(),
    'source_root': str(SOURCE), 'source_commit': git('rev-parse', 'HEAD'),
    'source_status_before': git('status', '--porcelain'),
    'source_files_before': file_hashes(),
    'scope': 'Only seven supplemental synthetic scenarios; the previous 54-test batch is not rerun.',
    'external_api_calls': 0, 'real_model_calls': 0, 'repository_files_modified_by_this_run': False,
    'transport_boundary': 'Mocked urllib opener errors and injected responses; network connections are blocked.',
    'scenarios': [], 'status': 'running',
}


def record(row):
    row['status'] = 'passed'
    report['scenarios'].append(row)
    print(json.dumps(row, ensure_ascii=False), flush=True)


try:
    assert report['source_commit'] == EXPECTED_COMMIT
    assert report['source_status_before'] == ''
    with patch('socket.create_connection', side_effect=AssertionError('external network forbidden')) as create_connection, \
            patch.object(socket.socket, 'connect', side_effect=AssertionError('external network forbidden')) as socket_connect:
        config = CompanyAPIConfig('https://neutral.example.invalid/v1', 'offline-model',
                                  api_key='synthetic-credential')
        forms = [
            {'code': 'insufficient_quota'},
            {'code': 'quota_exceeded'},
            {'code': 'billing_hard_limit_reached'},
            {'code': 'internal_error', 'type': 'insufficient_quota'},
            {'code': 'internal_error', 'message': 'insufficient quota'},
        ]
        for index, fields in enumerate(forms, 1):
            opener = Mock()
            opener.open.side_effect = urllib.error.HTTPError(
                'https://neutral.example.invalid/v1', 500, 'synthetic response', {},
                io.BytesIO(json.dumps({'error': fields}).encode()))
            with patch('company_api.urllib.request.build_opener', return_value=opener):
                client = OpenAICompatibleChatClient(config, transport=UrllibTransport())
                try:
                    client.complete(messages=[{'role': 'user', 'content': 'Synthetic question.'}])
                except APIClientError as error:
                    diagnostic = error.diagnostic
                    assert diagnostic['category'] == 'quota_exhausted', diagnostic
                    assert diagnostic['http_status'] == 500
                    expected_basis = 'provider_message' if 'message' in fields else 'provider_code'
                    assert diagnostic['evidence_source'] == expected_basis
                    if expected_basis == 'provider_code':
                        assert diagnostic['provider_code'] == fields.get('type', fields['code'])
                    else:
                        assert diagnostic.get('provider_code') is None
                    assert opener.open.call_count == 1
                    assert client.last_request_outcome == 'response_received'
                    record({'id': f'quota-format-{index}', 'injected_http_status': 500,
                            'injected_response_body': {'error': fields},
                            'category': diagnostic['category'],
                            'provider_code': diagnostic.get('provider_code'),
                            'evidence_source': diagnostic['evidence_source'],
                            'mocked_opener_calls': opener.open.call_count,
                            'request_outcome': client.last_request_outcome})
                else:
                    raise AssertionError('Expected quota failure')

        case = BusinessChatAPIErrorsTests(methodName='runTest')
        case.setUp()
        try:
            original = '扣减金额为基础金额减费用，剩余条件尚未写完。'
            first = TransportResponse(200, json.dumps({'choices': [{'message': {
                'role': 'assistant', 'content': original}, 'finish_reason': 'length'}]}))
            second = TransportResponse(500, json.dumps({'error': {'code': 'insufficient_quota'}}))
            result = case.run_chat([first, second])['agent_result']
            assert len(case.calls) == 2
            assert result['answer'] == original and result['narrative']['text'] == original
            assert result['metrics']['provider_retries'] == []
            for diagnostic in (result['diagnostic'], result['diagnostics'][-1]['diagnostic'],
                               result['diagnostic_summary']['api_failures'][-1]):
                assert diagnostic['category'] == 'quota_exhausted'
                assert diagnostic['provider_code'] == 'insufficient_quota'
                assert diagnostic['http_status'] == 500
            record({'id': 'quota-continuation-preserves-answer',
                    'injected_response_sequence': [
                        {'http_status': 200, 'finish_reason': 'length'},
                        {'http_status': 500, 'body': {'error': {'code': 'insufficient_quota'}}}],
                    'injected_transport_calls': 2, 'automatic_retries': 0,
                    'original_answer_unchanged': True,
                    'provider_code_retained_in_result_and_summary': True})
        finally:
            case.doCleanups()

        real_response = handoff.TransportResponse
        overridden = []

        def quota_response(status, *args, **kwargs):
            if status == 500:
                overridden.append({'original_http_status': 500,
                                   'original_body': args[0] if args else kwargs.get('body'),
                                   'replacement_body': {'error': {'code': 'insufficient_quota'}}})
                return real_response(500, json.dumps({'error': {'code': 'insufficient_quota'}}))
            return real_response(status, *args, **kwargs)

        with patch.object(handoff, 'TransportResponse', side_effect=quota_response):
            suite = unittest.defaultTestLoader.loadTestsFromName(TEST_ID)
            check = unittest.TextTestRunner(verbosity=2).run(suite)
        assert check.wasSuccessful() and check.testsRun == 1
        assert not check.skipped and not check.expectedFailures
        assert len(overridden) == 1
        assert overridden[0]['original_body'] == '{}'
        record({'id': 'quota-failed-turn-preserves-two-generations',
                'existing_test_id': TEST_ID, 'existing_test_file_modified': False,
                'synthetic_response_overrides': overridden,
                'existing_assertions_preserved': True, 'executed_existing_test_count': 1,
                'generation_two_in_next_question': True,
                'early_condition_and_archived_citation_preserved': True,
                'previous_saved_generation_unchanged': True})
        assert create_connection.call_count == 0 and socket_connect.call_count == 0
        report['blocked_network_function_calls'] = 0

    assert len(report['scenarios']) == 7
    report['source_commit_after'] = git('rev-parse', 'HEAD')
    report['source_status_after'] = git('status', '--porcelain')
    report['source_files_after'] = file_hashes()
    assert report['source_commit_after'] == EXPECTED_COMMIT
    assert report['source_status_after'] == report['source_status_before']
    assert report['source_files_after'] == report['source_files_before']
    report['source_unchanged_during_run'] = True
    report['status'] = 'passed'
except BaseException as error:
    report['status'] = 'failed'
    report['failure_type'] = type(error).__name__
    traceback.print_exc()
finally:
    report['completed_at_utc'] = datetime.now(timezone.utc).isoformat()
    report['scenario_count'] = len(report['scenarios'])
    report['script_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    with (OUTPUT / 'QUOTA-SCENARIO-RESULTS.json').open('x', encoding='utf-8') as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    print(json.dumps({'status': report['status'], 'scenario_count': report['scenario_count'],
                      'external_api_calls': 0, 'real_model_calls': 0}, ensure_ascii=False), flush=True)
raise SystemExit(report['status'] != 'passed')
