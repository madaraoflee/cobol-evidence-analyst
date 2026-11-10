import os
"""Read-only online enumeration audit using neutral, synthetic sources only."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[4]
OUT = Path(os.environ["FORMULA_AUDIT_OUTPUT"]) / "online"
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(REPO / 'poc'))
from business_index import build_business_index
from online_material_export import export_material
from online_material_format import MaterialError
from online_material_reader import DirectoryObjectStore, MaterialReader
from online_material_query import MaterialQuery
from online_investigation import run_investigation, _action
from online_workspace import OnlineWorkbench


def program(name, variable, operator='COMPUTE', calls=''):
    calculation = {'COMPUTE': f'COMPUTE {variable} = BASE-AMOUNT * RATE-VALUE',
                   'ADD': f'ADD BASE-AMOUNT TO {variable}',
                   'SUBTRACT': f'SUBTRACT BASE-AMOUNT FROM {variable}',
                   'MULTIPLY': f'MULTIPLY RATE-VALUE BY {variable}',
                   'DIVIDE': f'DIVIDE RATE-VALUE INTO {variable}'}[operator]
    return ('IDENTIFICATION DIVISION.\nPROGRAM-ID. ' + name + '.\nDATA DIVISION.\n'
            'WORKING-STORAGE SECTION.\n01 BASE-AMOUNT PIC 9(5)V99.\n01 RATE-VALUE PIC 9V99.\n'
            f'01 {variable} PIC 9(7)V99.\nPROCEDURE DIVISION.\nCALCULATE-AMOUNT.\n'
            + calculation + '.\n' + calls + 'GOBACK.\n').encode()


class ScriptedClient:
    last_request_outcome = 'received'
    def __init__(self, replies):
        self.replies = iter(replies)
        self.contexts, self.systems = [], []
    def complete(self, *, messages, check_cancel):
        check_cancel()
        context = json.loads(messages[-1]['content'])
        self.contexts.append(context)
        self.systems.append(messages[0]['content'])
        item = next(self.replies)
        if callable(item):
            item = item(context)
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(item)}}]}


def read(number):
    return {'tool': 'read', 'path': f'premium_{number:02}.cbl', 'start_line': 1, 'end_line': 11}


def scripted_final(context):
    identifier = context['source_context'][0]['id']
    return {'answer': f'The supplied formula multiplies the base amount by its rate [{identifier}].',
            'citations': [identifier], 'complete': True, 'remaining': []}


def main():
    output = {'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip(),
              'boundary': 'Offline synthetic parser/export/query execution and scripted responses; no real model or company network.'}
    with tempfile.TemporaryDirectory(prefix='formula-audit-', dir=OUT) as temporary:
        root = Path(temporary)
        source = root / 'source'
        source.mkdir()
        sources = {f'premium_{number:02}.cbl': program(f'PREMIUM-{number:02}', 'PREMIUM-AMOUNT') for number in range(25)}
        sources['abbreviated.cbl'] = program('RATE-ENGINE', 'PRM-AMT', calls="CALL 'ROUNDING-ENGINE'.\n")
        sources['rounding.cbl'] = program('ROUNDING-ENGINE', 'ROUNDED-AMOUNT', 'ADD')
        for path, raw in sources.items():
            (source / path).write_bytes(raw)
        database = root / 'business.sqlite'
        report = build_business_index(source, database, source_format='free', verify_content=True, framework_reference_path='')
        destination = root / 'material'
        manifest = export_material(database, source, destination, repository_id='example/source', commit='c' * 40, build_report=report)
        def query():
            return MaterialQuery(MaterialReader(DirectoryObjectStore(destination)))
        source_requests = []
        def source_read(path, *, max_bytes):
            source_requests.append(path)
            return sources[path]
        def run(replies, **options):
            client = ScriptedClient(replies)
            result = run_investigation(query(), source_read,
                'List every place in the system that calculates premium formulas and explain all formulas.',
                client=client, allow_source_transfer=True, **options)
            return result, client
        material = query()
        first, second = material.search('premium'), material.search('premium', offset=20)
        output['lexical_paging'] = {key: first[key] for key in ('total_matches', 'truncated', 'offset', 'match_mode', 'terms')}
        output['lexical_paging'].update(first_count=len(first['matches']), second_count=len(second['matches']),
            second_truncated=second['truncated'], total_unique_paths=len({x['relative_path'] for x in first['matches'] + second['matches']}))
        output['candidate_discovery'] = {
            'manifest_indexed_files': manifest['coverage']['indexed_files'],
            'premium_lexical_paths': [x['relative_path'] for x in material.search('premium', limit=100)['matches']],
            'compute_lexical_paths': [x['relative_path'] for x in material.search('compute', limit=100)['matches']],
            'abbreviated_exported_rules': material.reader.facts('abbreviated.cbl')['rules'],
            'abbreviated_relations': material.relations('abbreviated.cbl'),
            'relations_return_rules': 'rules' in material.relations('abbreviated.cbl')}
        unsupported = {}
        for action in ({'tool': 'files'}, {'tool': 'rules', 'path': 'abbreviated.cbl'}, {'tool': 'formula_candidates'}):
            try:
                _action(action)
            except MaterialError as error:
                unsupported[action['tool']] = error.code
        output['unsupported_inventory_tools'] = unsupported
        result, client = run([
            {'actions': [{'tool': 'search', 'text': 'premium'}], 'remaining': ['Visit every search page and each formula']},
            {'actions': [read(0)], 'remaining': ['24 candidate files remain unverified']},
            scripted_final,
        ])
        search = client.contexts[1]['observations'][0]['result']
        output['premature_completion'] = {'status': result['status'], 'error': result['error'],
            'model_calls': result['model_calls'], 'actions': result['actions'],
            'evidence_paths': [item['citation']['path'] for item in result['evidence']],
            'last_context_remaining': client.contexts[-1]['remaining'], 'result_remaining': result['remaining'],
            'search_total': search['total_matches'], 'search_rows': len(search['matches']),
            'search_truncated': search['truncated'], 'search_next_offset': search['next_offset'],
            'coverage': result['coverage']}
        controller = OnlineWorkbench(root / 'online-state',
            material_factory=lambda *args: MaterialReader(DirectoryObjectStore(destination)),
            source_factory=lambda *args: source_read,
            investigator=lambda *args, **kwargs: deepcopy(result))
        try:
            _, connection = controller.dispatch('POST', '/api/online/connect', {
                'location': {'kind': 'https', 'base_url': 'https://materials.invalid/source/'},
                'repository': 'example/source', 'commit': 'c' * 40, 'revision': manifest['revision']})
            for thread in list(controller._threads.values()):
                thread.join(2)
            workspace_id = controller.state()['current_workspace_id']
            _, conversation = controller.dispatch('POST', '/api/online/conversations', {'workspace_id': workspace_id})
            controller.dispatch('POST', '/api/online/question', {'conversation_id': conversation['conversation']['id'],
                'question': 'List every premium formula in the system.', 'allow_source_transfer': True})
            for thread in list(controller._threads.values()):
                thread.join(2)
            public = controller.state()['conversation']
            output['premature_completion']['workspace_status'] = public['status']
            output['premature_completion']['workspace_pending'] = public['pending']
        finally:
            controller.close()
        result, client = run([
            {'actions': [{'tool': 'search', 'text': 'premium'}], 'remaining': ['Visit the remaining search page']},
            {'actions': [read(i) for i in range(8)]},
            {'actions': [read(i) for i in range(8, 13)]},
            scripted_final,
        ])
        final_context = client.contexts[-1]
        output['search_frontier_eviction'] = {'status': result['status'], 'model_calls': result['model_calls'],
            'actions': result['actions'], 'durable_observations_count': len(result['checkpoint']['observations']),
            'durable_search_retained': any(row['action']['tool'] == 'search' for row in result['checkpoint']['observations']),
            'model_observations_count': len(final_context['observations']),
            'model_observations_omitted': final_context['observations_omitted'],
            'model_has_search_or_cursor': any(row['action']['tool'] == 'search' for row in final_context['observations']),
            'model_remaining': final_context['remaining'], 'evidence_count': len(result['evidence']),
            'model_system_has_only_six_tools': all(name in client.systems[-1] for name in ['"tool":"search"', '"tool":"evidence_catalog"'])}
        narrow_reads = [{'tool': 'read', 'path': f'premium_{number // 11:02}.cbl',
                         'start_line': number % 11 + 1, 'end_line': number % 11 + 1} for number in range(97)]
        continuing_client = ScriptedClient([
            {'actions': [{'tool': 'search', 'text': 'premium'}], 'remaining': ['Visit remaining search page']},
            *[{'actions': narrow_reads[start:start + 8]} for start in range(0, 97, 8)], scripted_final])
        checkpoint, statuses = None, []
        for _ in range(8):
            durable = run_investigation(query(), source_read, 'List every premium formula in the system.',
                client=continuing_client, allow_source_transfer=True, checkpoint=checkpoint)
            statuses.append({'status': durable['status'], 'error': durable['error'], 'actions': durable['actions'],
                             'model_calls': durable['model_calls']})
            checkpoint = durable['checkpoint']
            if durable['status'] != 'needs_continuation':
                break
        output['durable_frontier_eviction_after_explicit_continuations'] = {
            'runs': statuses, 'actions_total': checkpoint['actions_total'],
            'observations_total': checkpoint['observations_total'], 'retained_observations': len(checkpoint['observations']),
            'search_observation_retained': any(row['action']['tool'] == 'search' for row in checkpoint['observations']),
            'evidence_count': len(durable['evidence']), 'remaining': durable['remaining'],
            'note': 'Each invocation uses the default budgets; the harness explicitly resumes the saved checkpoint.'}
        result, client = run([{'actions': [read(i)], 'remaining': ['Continue the complete formula inventory']} for i in range(6)])
        output['default_model_budget'] = {'status': result['status'], 'error': result['error'], 'model_calls': result['model_calls'],
            'actions': result['actions'], 'evidence_count': len(result['evidence']), 'pending_actions': result['checkpoint']['pending_actions'], 'remaining': result['remaining']}
        result, client = run([{'actions': [read(i) for i in range(start, start + 8)], 'remaining': ['Continue the complete formula inventory']}
                              for start in range(0, 24, 8)] + [{'actions': [read(24)], 'remaining': ['Continue the complete formula inventory']}])
        output['default_action_budget'] = {'status': result['status'], 'error': result['error'], 'model_calls': result['model_calls'],
            'actions': result['actions'], 'evidence_count': len(result['evidence']), 'pending_actions': result['checkpoint']['pending_actions'], 'remaining': result['remaining']}
    paths = ['poc/online_investigation.py', 'poc/online_material_query.py', 'poc/online_material_export.py', 'poc/online_material_reader.py', 'poc/online_workspace.py']
    output['sha256'] = {path: hashlib.sha256((REPO / path).read_bytes()).hexdigest() for path in paths}
    (OUT / 'result.json').write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: value for key, value in output.items() if key not in {'candidate_discovery', 'sha256'}}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
