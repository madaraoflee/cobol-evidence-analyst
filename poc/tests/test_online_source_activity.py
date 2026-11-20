"""Public online progress reports actual source operations without their payloads."""
import json
from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).parent)]
import test_online_investigation as investigation
import test_online_workspace as workspace
from online_investigation import run_investigation


class OnlineSourceActivityTests(unittest.TestCase):
    def test_real_search_location_and_read_survive_model_wait_and_completion(self):
        app_case = workspace.OnlineWorkspaceTests()
        app_case.setUp()
        self.addCleanup(app_case.doCleanups)
        model = investigation.OnlineInvestigationTests()
        model.setUp()
        self.addCleanup(model.doCleanups)
        states = []

        def run(query, source, question, **options):
            journal = options['progress']
            def record(event):
                journal(event)
                states.append(app_case.app.state()['active_job'])
            options.update(client=model.client([{'actions': [
                {'tool': 'search', 'text': 'PRIVATE-SEARCH-MARKER'},
                {'tool': 'locate_global', 'text': 'READY'},
                {'tool': 'relations', 'path': 'main.cbl'},
                investigation.read('main.cbl', 4, 5)]}, investigation.answer]), progress=record)
            return run_investigation(query, source, question, **options)
        app_case.app = app_case.make_app(investigator=run)
        connected = app_case.connect()
        conversation = app_case.conversation(connected['workspace_id'])
        final = app_case.ask(conversation['id'])
        self.assertEqual(final['status'], 'answered', final.get('error'))
        after_read = [state for state in states if state['progress']['phase'] == 'model_request_started'][-1]
        activity = after_read['source_activity']
        self.assertEqual([(step['tool'], step['count']) for step in activity],
                         [('search', 0), ('locate_global', 1), ('relations', 0), ('read', 2)])
        self.assertGreaterEqual(activity[1]['scanned_files'], 1)
        self.assertEqual(final['source_activity'], activity)
        self.assertTrue(any(s['progress'].get('next_tool') == 'locate_global' for s in states))
        for snapshot in states:
            visible = json.dumps([snapshot['progress'], snapshot.get('source_activity')])
            for private in ['PRIVATE-SEARCH-MARKER', 'STATUS-READY', 'main.cbl', 'checkpoint', 'source_context']:
                self.assertNotIn(private, visible)

    def test_failed_lookup_is_distinct_from_zero_matches_and_limited_scan(self):
        from online_workspace import _source_activity
        def observation(tool, result):
            return {'action': {'tool': tool, 'path': 'hidden'}, 'result': result}
        rows = [observation('search', {'matches': [], 'total_matches': 0}),
                observation('locate_global', {'matches': [], 'complete': False, 'next_cursor': None,
                            'search_mode': 'legacy_material_budget_exceeded'}),
                observation('read', {'error': 'MATERIAL_FILE_NOT_INDEXED', 'text': 'secret'}),
                observation('protocol', {'unknown': 'secret'})]
        limited = observation('locate_global', {'matches': [], 'complete': True, 'scanned_files': 2,
                              'search_mode': 'legacy_token_candidates_only'})
        self.assertEqual(_source_activity({'observations': [limited]}),
                         [{'tool': 'locate_global', 'count': 0, 'scanned_files': 2, 'partial': True}])
        self.assertEqual(_source_activity({'observations': rows}), [
            {'tool': 'search', 'count': 0}, {'tool': 'locate_global', 'count': 0, 'partial': True},
            {'tool': 'read', 'failed': True}])
