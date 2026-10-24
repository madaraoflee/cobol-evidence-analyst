"""Offline recovery contracts; all provider responses are local test transports."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search
from run_store import RunStore, fingerprint


class ExplicitRetryRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.source_file = self.source / 'entry.cbl'
        self.source_file.write_text('IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRYPOINT.\n'
            'DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 IN-AMOUNT PIC 9(5) VALUE 4.\n'
            '01 OUT-AMOUNT PIC 9(5).\nPROCEDURE DIVISION.\nMAIN.\n'
            'COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nGOBACK.\n', encoding='utf-8')
        self.database = self.root / 'facts.sqlite'
        self.rebuild()
        self.store = RunStore(self.root / 'investigation-runs.sqlite')
        self.config = CompanyAPIConfig('https://offline.example.invalid/v1', 'offline-model', api_key='offline-only')
        self.question = 'ENTRYPOINT OUT-AMOUNT'
        self.policy = AgentPolicy(max_model_requests=1)
        self.calls = []
        guard = patch('socket.socket.connect', side_effect=AssertionError('Real network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)

    def rebuild(self):
        build_business_index(self.source, self.database, source_format='free', verify_content=True)
        ensure_repository_search(self.database, self.source)

    def invoke(self, **kwargs):
        return run_business_chat(self.question, self.database, self.source, self.config,
                                 transport=kwargs.pop('transport', self.success), policy=self.policy,
                                 conversation_id=kwargs.pop('conversation_id', 'conversation-a'), **kwargs)

    def capture(self, request):
        payload = json.loads(json.loads(request.body)['messages'][-1]['content'])
        self.calls.append(payload)
        return payload

    def interrupted(self, request):
        self.capture(request)
        raise KeyboardInterrupt('simulated provider interruption')

    def success(self, request):
        payload = self.capture(request)
        page = next(page for bundle in payload['source_context'] for page in bundle['pages']
                    if 'COMPUTE OUT-AMOUNT' in page['source_text'])
        factor = '3' if 'IN-AMOUNT * 3' in page['source_text'] else '2'
        return TransportResponse(200, json.dumps({'choices': [{'message': {'role': 'assistant',
            'content': f"OUT-AMOUNT = IN-AMOUNT * {factor}。[{page['evidence_id']}]"}, 'finish_reason': 'stop'}]}))

    def interrupt_run(self, **kwargs):
        with self.assertRaisesRegex(KeyboardInterrupt, 'simulated provider interruption'):
            self.invoke(transport=self.interrupted, **kwargs)
        with self.store.connect() as db:
            identifier = db.execute('SELECT run_id FROM investigation_runs ORDER BY created_at DESC LIMIT 1').fetchone()[0]
        return self.store.load(identifier)

    def identity(self, *, language='zh-CN', conversation_id='conversation-a'):
        scope = {'database_path': str(self.database.resolve()), 'source_root': str(self.source.resolve()),
                 'entry_program': None, 'history': [], 'framework_reference_path': None,
                 'answer_detail': 'detailed', 'conversation_id': conversation_id}
        return fingerprint({**scope, 'answer_language': language}) if language else fingerprint(scope)

    def clone_pending(self, original, identifier, *, language='zh-CN', conversation_id='conversation-a'):
        metadata = deepcopy(original['metadata'])
        metadata['analysis_identity'] = self.identity(language=language, conversation_id=conversation_id)
        self.store.create(identifier, metadata)
        self.store.checkpoint(identifier, deepcopy(original['checkpoint']), status='attention')
        self.store.start_request(identifier, 'unknown-' + identifier, 'request-hash-' + identifier)
        return self.store.load(identifier)

    def assert_retained_unknown(self, before, replacement):
        after = self.store.load(before['run_id'])
        self.assertEqual(after['state'], 'superseded')
        self.assertEqual(after['metadata'], before['metadata'])
        self.assertEqual(after['requests'], before['requests'])
        self.assertEqual(after['uncertain_requests'], before['uncertain_requests'])
        self.assertEqual(after['completed_steps'], before['completed_steps'])
        self.assertEqual({key: after['checkpoint'][key] for key in before['checkpoint']}, before['checkpoint'])
        self.assertTrue(after['checkpoint']['superseded_by_explicit_retry'])
        self.assertEqual(after['checkpoint']['replacement_run_id'], replacement)

    def test_old_pipeline_and_language_require_explicit_retry_then_use_fresh_run(self):
        with patch('business_chat.PIPELINE_VERSION', 'older-pipeline'):
            prior = self.interrupt_run(locale='zh-CN')
        # Make the actual saved metadata old, independently of the live policy.
        metadata = {**prior['metadata'], 'pipeline_version': 'older-pipeline', 'prompt_version': 'older-prompt'}
        with self.store.connect() as db:
            db.execute('UPDATE investigation_runs SET metadata_json=? WHERE run_id=?',
                       (json.dumps(metadata), prior['run_id']))
        prior = self.store.load(prior['run_id'])
        blocked = self.invoke(locale='en', resume_run_id=prior['run_id'])
        self.assertEqual(blocked['reason_code'], 'REMOTE_REQUEST_OUTCOME_UNKNOWN')
        self.assertEqual(len(self.calls), 1)
        result = self.invoke(locale='en', resume_run_id=prior['run_id'], start_new_investigation=True)
        self.assertTrue(result['agent_result']['model_answer_recorded'])
        replacement = result['agent_result']['run_id']
        self.assertNotEqual(replacement, prior['run_id'])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result['agent_result']['answer_language'], 'en')
        self.assert_retained_unknown(prior, replacement)

    def test_all_matching_pending_languages_retire_without_touching_another_conversation(self):
        original = self.interrupt_run(locale='zh-CN')
        english = self.clone_pending(original, 'older-english', language='en')
        legacy = self.clone_pending(original, 'older-language-absent', language=None)
        unrelated = self.clone_pending(original, 'other-conversation', conversation_id='conversation-b')
        result = self.invoke(locale='zh-CN', start_new_investigation=True)
        replacement = result['agent_result']['run_id']
        for prior in (original, english, legacy):
            self.assert_retained_unknown(prior, replacement)
        self.assertEqual(self.store.load(unrelated['run_id']), unrelated)
        self.assertEqual(len(self.calls), 2)

    def test_explicit_retry_from_another_conversation_is_rejected(self):
        prior = self.interrupt_run()
        rejected = self.invoke(conversation_id='conversation-b', resume_run_id=prior['run_id'],
                               start_new_investigation=True)
        self.assertEqual(rejected['reason_code'], 'RESUME_IDENTITY_MISMATCH')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.load(prior['run_id']), prior)

    def test_new_source_is_reread_and_old_evidence_remains_immutable(self):
        prior = self.interrupt_run()
        old_hash = hashlib.sha256(self.source_file.read_bytes()).hexdigest()
        self.source_file.write_text(self.source_file.read_text(encoding='utf-8').replace('IN-AMOUNT * 2', 'IN-AMOUNT * 3'), encoding='utf-8')
        self.rebuild()
        new_hash = hashlib.sha256(self.source_file.read_bytes()).hexdigest()
        self.assertNotEqual(old_hash, new_hash)
        result = self.invoke(start_new_investigation=True)
        self.assertTrue(result['agent_result']['model_answer_recorded'])
        self.assertIn('IN-AMOUNT * 3', result['agent_result']['answer'])
        self.assert_retained_unknown(prior, result['agent_result']['run_id'])
        pages = [page for bundle in self.calls[-1]['source_context'] for page in bundle['pages']]
        self.assertTrue(pages)
        self.assertTrue(all(page['source_sha256'] == new_hash for page in pages))
        self.assertFalse(any('IN-AMOUNT * 2' in page['source_text'] for page in pages))
        self.assertEqual(len(self.calls), 2)

    def test_changed_source_after_received_response_starts_fresh_only_on_explicit_retry(self):
        for allowance in (1, 2):
            with self.subTest(request_allowance=allowance):
                self.policy = AgentPolicy(max_model_requests=allowance)
                self.question = f'ENTRYPOINT OUT-AMOUNT，调查 {allowance}'
                self.source_file.write_text(self.source_file.read_text(encoding='utf-8')
                    .replace('IN-AMOUNT * 3', 'IN-AMOUNT * 2'), encoding='utf-8')
                self.rebuild()
                received = False

                def transport(request):
                    nonlocal received
                    response = self.success(request)
                    received = True
                    return response

                def interrupt_after_response():
                    if received:
                        raise RuntimeError('interrupted after saved response')

                with self.assertRaisesRegex(RuntimeError, 'interrupted after saved response'):
                    self.invoke(transport=transport, check_cancel=interrupt_after_response)
                prior = self.store.unfinished('sha256:' + hashlib.sha256(self.question.encode()).hexdigest(),
                    hashlib.sha256(self.config.chat_model.encode()).hexdigest())
                self.assertIsNotNone(prior)
                self.assertEqual(len(prior['requests']), 1)
                self.assertEqual(prior['requests'][0]['state'], 'completed')
                self.assertFalse(prior['uncertain_requests'])
                self.source_file.write_text(self.source_file.read_text(encoding='utf-8')
                    .replace('IN-AMOUNT * 2', 'IN-AMOUNT * 3'), encoding='utf-8')
                self.rebuild()
                before_calls = len(self.calls)
                blocked = self.invoke()
                self.assertEqual(blocked['reason_code'], 'RESUME_SOURCE_VERSION_CHANGED')
                self.assertEqual(len(self.calls), before_calls)
                self.assertEqual(self.store.load(prior['run_id']), prior)

                retired = None
                for explicit_id in (None, prior['run_id']):
                    result = self.invoke(start_new_investigation=True,
                        **({'resume_run_id': explicit_id} if explicit_id else {}))
                    self.assertTrue(result['agent_result']['model_answer_recorded'])
                    self.assertIn('IN-AMOUNT * 3', result['agent_result']['answer'])
                    self.assertNotEqual(result['agent_result']['run_id'], prior['run_id'])
                    pages = [page for bundle in self.calls[-1]['source_context'] for page in bundle['pages']]
                    self.assertFalse(any('IN-AMOUNT * 2' in page['source_text'] for page in pages))
                    after = self.store.load(prior['run_id'])
                    self.assertEqual(after['state'], 'superseded')
                    for field in ('metadata', 'requests', 'completed_steps', 'uncertain_requests'):
                        self.assertEqual(after[field], prior[field])
                    self.assertEqual({key: after['checkpoint'][key] for key in prior['checkpoint']}, prior['checkpoint'])
                    self.assertTrue(after['checkpoint']['superseded_by_explicit_retry'])
                    if retired is None:
                        retired = after
                    else:
                        self.assertEqual(after, retired)
                    pending = self.store.unfinished(prior['metadata']['question_id'],
                        prior['metadata']['configuration']['model_fingerprint'])
                    self.assertTrue(pending is None or pending['run_id'] != prior['run_id'])
                self.assertEqual(len(self.calls), before_calls + 2)

    def test_retry_using_a_superseded_original_id_can_start_a_new_explicit_run(self):
        original = self.interrupt_run()
        first_answer = self.invoke(start_new_investigation=True)
        first_replacement = first_answer['agent_result']['run_id']
        retired = self.store.load(original['run_id'])
        replayed = self.invoke(resume_run_id=first_replacement)
        self.assertEqual(replayed, json.loads(json.dumps(first_answer)))
        blocked = self.invoke(resume_run_id=original['run_id'])
        self.assertEqual(blocked['reason_code'], 'REMOTE_REQUEST_OUTCOME_UNKNOWN')
        self.assertEqual(len(self.calls), 2)
        second_answer = self.invoke(resume_run_id=original['run_id'], start_new_investigation=True)
        self.assertTrue(second_answer['agent_result']['model_answer_recorded'])
        self.assertNotEqual(second_answer['agent_result']['run_id'], first_replacement)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.store.load(original['run_id']), retired)

    def test_repeated_interruptions_never_trigger_automatic_resubmission(self):
        first = self.interrupt_run()
        second = self.interrupt_run(start_new_investigation=True)
        self.assert_retained_unknown(first, second['run_id'])
        for _ in range(3):
            blocked = self.invoke()
            self.assertEqual(blocked['reason_code'], 'REMOTE_REQUEST_OUTCOME_UNKNOWN')
            self.assertFalse(blocked['agent_result']['resume_state']['automatic_resend'])
        self.assertEqual(len(self.calls), 2)
        third = self.interrupt_run(start_new_investigation=True)
        self.assert_retained_unknown(second, third['run_id'])
        blocked = self.invoke()
        self.assertEqual(blocked['reason_code'], 'REMOTE_REQUEST_OUTCOME_UNKNOWN')
        self.assertEqual(len(self.calls), 3)


if __name__ == '__main__':
    unittest.main()
