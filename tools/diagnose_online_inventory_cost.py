#!/usr/bin/env python3
"""Compare four neutral pipeline persistence cases, always synchronous FULL.
Sequence: DELETE, PERSIST, TRUNCATE, DELETE. Only external diagnostic connections
change their mode; repository code and production defaults are left unchanged.
No real model or remote business-source repository is accessed; network is blocked.
"""
import sys
import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
from unittest import mock

BINS = (0.0001, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 5.0)

class Observer:
    def __init__(self, output, mode, journal_mode=None):
        self.output, self.mode, self.journal_mode = output, mode, journal_mode
        self.started, self.cpu_started = time.perf_counter(), time.process_time()
        self.lock, self.write_lock, self.stop = threading.RLock(), threading.Lock(), threading.Event()
        self.counts, self.times, self.distributions = Counter(), Counter(), {}
        self.phase, self.status = 'setup', 'running'
        self.reviewed, self.verified, self.last_action = 0, set(), None
        self.reviewed_paths = set()
        self.config, self.seen_schema, self.driver_statistics = {}, set(), None
        self.driver_returned, self.tests = False, {}
        self.thread = threading.Thread(target=self.heartbeat, daemon=True)
    def measure(self, kind, duration):
        with self.lock:
            self.counts[kind] += 1
            self.times[kind] += duration
            value = self.distributions.setdefault(kind, {'max': 0.0, 'buckets': [0] * (len(BINS) + 1)})
            value['max'] = max(value['max'], duration)
            index = next((i for i, bound in enumerate(BINS) if duration <= bound), len(BINS))
            value['buckets'][index] += 1
    def progress(self, value):
        with self.lock:
            self.phase = 'source_review'
            self.reviewed = value['reviewed_files']
        self.snapshot(announce=True)
    def metadata(self, parameters):
        try:
            value = json.loads(parameters[0])
            verification = value.get('verification', {})
            after = verification.get('after')
            if verification.get('active') and after:
                with self.lock:
                    self.phase = 'final_source_verification'
                    before = len(self.verified)
                    self.verified.add(after)
                if len(self.verified) != before and len(self.verified) % 250 == 0:
                    self.snapshot(announce=True)
        except (IndexError, TypeError, ValueError, AttributeError):
            pass
    def inspect_schema(self, db, key):
        if key in self.seen_schema:
            return
        queries = {
            'path': ('SELECT key,data FROM records INDEXED BY record_path_key WHERE session=? AND kind=? AND path=? ORDER BY key LIMIT ? OFFSET ?', ('diagnostic', 'findings', 'unit-00000.cbl', 100, 0)),
            'occurrences': ("SELECT coalesce(sum(json_extract(data,'$.occurrence_count')),0) FROM records INDEXED BY summary_candidate_occurrences WHERE session=? AND kind='candidates'", ('diagnostic',)),
            'inventoried': ("SELECT count(*) FROM records INDEXED BY summary_inventoried_files WHERE session=? AND kind='files' AND json_extract(data,'$.inventoried')=1", ('diagnostic',))}
        config = {'sqlite_version': sqlite3.sqlite_version,
            'journal_mode': sqlite3.Connection.execute(db, 'PRAGMA journal_mode').fetchone()[0],
            'synchronous': sqlite3.Connection.execute(db, 'PRAGMA synchronous').fetchone()[0],
            'query_plans': {}}
        for label, (query, parameters) in queries.items():
            try:
                config['query_plans'][label] = sqlite3.Connection.execute(db, 'EXPLAIN QUERY PLAN ' + query, parameters).fetchall()
            except sqlite3.Error as error:
                config['query_plans'][label] = {'error': str(error)[:200]}
        with self.lock:
            self.config[key] = config
            self.seen_schema.add(key)
        self.snapshot()
    def snapshot(self, announce=False):
        with self.write_lock:
            with self.lock:
                elapsed, cpu = time.perf_counter() - self.started, time.process_time() - self.cpu_started
                data = {'schema': 'external-online-inventory-diagnostic/v1', 'diagnostic_only': True,
                    'official_acceptance': False, 'mode': self.mode, 'status': self.status,
                    'phase': self.phase, 'requested_journal_mode': self.journal_mode, 'elapsed_seconds': elapsed, 'process_cpu_seconds': cpu,
                    'cpu_to_wall_ratio': cpu / elapsed if elapsed else 0,
                    'reviewed_files': self.reviewed, 'verified_source_files': len(self.verified),
                    'last_action_kind': self.last_action, 'counts': dict(self.counts), 'seconds': dict(self.times),
                    'latency_buckets_upper_seconds': list(BINS) + ['over_5_seconds'],
                    'latency_distributions': json.loads(json.dumps(self.distributions)),
                    'sqlite_configuration': dict(self.config), 'tests': dict(self.tests),
                    'driver_statistics': self.driver_statistics,
                    'limits': ['Row changes do not prove VFS fsync calls; identical SQL updates can count as changed rows.',
                               'Instrumentation adds overhead; this is not a clean Windows speed baseline.',
                               'An interrupted/running snapshot must never be counted as a passed test.']}
            temporary = self.output / ('snapshot-' + str(threading.get_ident()) + '.tmp')
            with temporary.open('w', encoding='utf-8', newline='\n') as stream:
                json.dump(data, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.output / 'diagnostic.json')
        if announce:
            print(json.dumps({key: data[key] for key in ('status', 'phase', 'elapsed_seconds',
                'process_cpu_seconds', 'reviewed_files', 'verified_source_files')}), flush=True)
    def heartbeat(self):
        while not self.stop.wait(10):
            self.snapshot(announce=True)

@contextmanager
def instrument(observer, driver, inventory, investigation):
    original_connect, original_driver = sqlite3.connect, driver.exercise_online_store
    original_store_init = inventory.OnlineInventoryStore.__init__
    supports_journal_mode = 'journal_mode' in inspect.signature(original_store_init).parameters
    opened_stores = []
    original_page, original_export = inventory.InventoryPublication.page, inventory.InventoryPublication.export_data
    original_execute = investigation._execute
    def store_init(store, *args, **options):
        if supports_journal_mode and observer.journal_mode is not None:
            options['journal_mode'] = observer.journal_mode
        original_store_init(store, *args, **options)
        opened_stores.append(store)
    class Connection(sqlite3.Connection):
        def __init__(self, *args, **options):
            super().__init__(*args, **options)
            self.changed_before_commit = 0
            self.key = hashlib.sha256(str(args[0]).encode()).hexdigest()[:12]
            if observer.journal_mode is not None:
                actual = sqlite3.Connection.execute(self, 'PRAGMA journal_mode=' + observer.journal_mode).fetchone()[0]
                if actual.upper() != observer.journal_mode:
                    raise RuntimeError('Requested diagnostic journal mode was not applied')
                sqlite3.Connection.execute(self, 'PRAGMA synchronous=FULL')
                if sqlite3.Connection.execute(self, 'PRAGMA synchronous').fetchone()[0] != 2:
                    raise RuntimeError('Diagnostic connection must keep synchronous FULL')
                observer.counts['connections_verified_FULL'] += 1
                observer.counts['connections_verified_' + observer.journal_mode] += 1
        def execute(self, sql, parameters=(), /):
            kind = sql.lstrip().split(' ', 1)[0].upper()
            started = time.perf_counter()
            try:
                value = super().execute(sql, parameters)
            finally:
                observer.measure('sql_' + kind, time.perf_counter() - started)
            if sql.strip().upper().startswith('PRAGMA SYNCHRONOUS'):
                if sqlite3.Connection.execute(self, 'PRAGMA synchronous').fetchone()[0] != 2:
                    raise RuntimeError('Production diagnostic connection lowered synchronous')
            if sql.startswith('UPDATE sessions SET meta='):
                observer.metadata(parameters)
            return value
        def executescript(self, sql, /):
            value = super().executescript(sql)
            observer.inspect_schema(self, self.key)
            return value
        def commit(self):
            transaction = self.in_transaction
            changed = self.total_changes - self.changed_before_commit
            label = ('commit_row_changes' if changed else 'commit_no_row_changes') if transaction else 'commit_no_transaction'
            started, completed = time.perf_counter(), False
            try:
                value = super().commit()
                completed = True
                return value
            finally:
                observer.measure(label + ('_completed' if completed else '_interrupted_or_error'), time.perf_counter() - started)
                self.changed_before_commit = self.total_changes
        def close(self):
            started = time.perf_counter()
            try:
                return super().close()
            finally:
                observer.measure('close', time.perf_counter() - started)
    def connect(*args, **options):
        path = str(args[0] if args else options.get('database', '')).replace('\\', '/').split('?', 1)[0]
        if not path.endswith('/online-inventories.sqlite'):
            return original_connect(*args, **options)
        options['factory'] = Connection
        started = time.perf_counter()
        try:
            return original_connect(*args, **options)
        finally:
            observer.measure('connect', time.perf_counter() - started)
    def execute(*args, **options):
        action = args[2]
        with observer.lock:
            observer.last_action = action.get('tool')
            if action.get('tool') == 'inventory':
                observer.phase = 'file_inventory'
            elif action.get('tool') in {'arithmetic_candidates', 'read', 'review', 'review_file'}:
                observer.phase = 'source_review'
        value = original_execute(*args, **options)
        if action.get('tool') == 'review_file':
            with observer.lock:
                observer.reviewed_paths.add(action['path'])
                observer.reviewed = len(observer.reviewed_paths)
                observer.phase = 'source_review'
        return value
    def exercise(*args, **options):
        original_progress = options.get('progress')
        def progress(value):
            observer.progress(value)
            if original_progress is not None:
                original_progress(value)
        options['progress'] = progress
        value = original_driver(*args, **options)
        with observer.lock:
            observer.driver_statistics = value['statistics']
            observer.reviewed = value['statistics']['reviewed_files']
            observer.driver_returned = True
            observer.phase = 'original_test_assertions'
        observer.snapshot(announce=True)
        return value
    def page(*args, **options):
        if observer.driver_returned:
            observer.phase = 'complete_inventory_pagination'
        return original_page(*args, **options)
    def export(*args, **options):
        if observer.driver_returned:
            observer.phase = 'complete_export'
        return original_export(*args, **options)
    with mock.patch('sqlite3.connect', side_effect=connect), \
         mock.patch.object(inventory.OnlineInventoryStore, '__init__', store_init), \
         mock.patch.object(driver, 'exercise_online_store', exercise), \
         mock.patch.object(inventory.InventoryPublication, 'page', page), mock.patch.object(inventory.InventoryPublication, 'export_data', export), \
         mock.patch.object(investigation, '_execute', execute), \
         mock.patch('socket.create_connection', side_effect=AssertionError('No network allowed in diagnostic')), \
         mock.patch('socket.socket.connect', side_effect=AssertionError('No network allowed in diagnostic')):
        try:
            yield
        finally:
            primary_failure = sys.exc_info()[0] is not None
            cleanup_errors = []
            for store in reversed(opened_stores):
                close = getattr(store, 'close', None)
                if close is not None:
                    try:
                        close()
                    except Exception as error:
                        cleanup_errors.append(type(error).__name__)
            if cleanup_errors:
                observer.tests['resource_cleanup'] = 'failed: ' + ', '.join(cleanup_errors)
                if not primary_failure:
                    raise RuntimeError('Diagnostic stores did not close cleanly')

def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)

def binding(repo):
    paths = ['poc/online_inventory_store.py', 'poc/online_sqlite_store.py', 'poc/online_investigation.py',
        'poc/online_material_reader.py', 'poc/online_material_query.py', 'poc/online_material_format.py',
        'poc/online_material_export.py', 'poc/business_index.py', 'poc/tests/large_repository_fixture.py',
        'poc/tests/large_repository_online_driver.py', 'poc/tests/test_large_repository_inventory_acceptance.py']
    return {name: hashlib.sha256((repo / name).read_bytes()).hexdigest() for name in paths}

def verify_delivery(value, gold, count):
    from large_repository_fixture import evaluate_delivery
    result, publication, stats = value['result'], value['publication'], value['statistics']
    assert result['status'] == 'answered' and result['coverage_ledger']['complete']
    assert stats['inventory_files'] == stats['reviewed_files'] == stats['source_reads_before_delivery'] == count
    assert stats['older_handle_unchanged'] and stats['initial_completed_action_retained']
    assert stats['final_model_requests'] == 2 and stats['durable_completed_actions'] > 4 * count
    rows, offset, pages = [], 0, 0
    while True:
        page = publication.page(offset, 73)
        assert page['complete'] and page['total'] == count
        rows.extend(page['findings']);pages += 1
        if page['next_offset'] is None:
            break
        assert page['next_offset'] > offset
        offset = page['next_offset']
    page_delivery = evaluate_delivery(gold, rows)
    exported = publication.export_data()
    export_delivery = evaluate_delivery(gold, exported['findings'])
    assert page_delivery['complete'] and export_delivery['complete']
    assert len(exported['source_reviews']) == count
    original = publication.export_data()
    store = value['store']
    from online_inventory_store import InventorySession
    with store.connection() as db:
        before_meta, raw_owner = db.execute('SELECT meta,owner FROM sessions WHERE id=?', (publication.session,)).fetchone()
    session = InventorySession(store, publication.session, json.loads(raw_owner))
    before_summary = session.summary()
    try:
        with store.connection() as db:
            db.execute('UPDATE sessions SET meta=? WHERE id=?', (b'{}', publication.session))
            raise RuntimeError('controlled diagnostic rollback')
    except RuntimeError as error:
        assert str(error) == 'controlled diagnostic rollback'
    with store.connection() as db:
        after_meta = db.execute('SELECT meta FROM sessions WHERE id=?', (publication.session,)).fetchone()[0]
    assert after_meta == before_meta, 'Controlled rollback did not restore original session metadata bytes'
    assert session.summary() == before_summary, 'Controlled rollback changed the inventory summary'
    assert publication.export_data() == original
    fields = ('inventory_files', 'reviewed_files', 'source_reads_before_delivery', 'durable_completed_actions',
              'initial_completed_action_retained', 'older_handle_unchanged', 'final_model_requests', 'restores')
    return {'page_delivery': page_delivery, 'export_delivery': export_delivery, 'pages': pages,
        'source_reviews': len(exported['source_reviews']), 'rollback_kept_publication': True,
        'rollback_kept_session_metadata_bytes': True, 'rollback_kept_summary': True,
        'statistics': {key: stats[key] for key in fields}}

def main():
    parser = argparse.ArgumentParser(description='Offline diagnostic only: DELETE/PERSIST/TRUNCATE/DELETE with synchronous FULL; production defaults are not changed.')
    parser.add_argument('--repo-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--files', type=int, default=200, choices=(200, 1000))
    args = parser.parse_args()
    repo, output = args.repo_root.resolve(), args.output.resolve()
    allowed = {'WORKFLOW-EXECUTION.json', 'diagnostic.log'}
    if output.is_relative_to(repo) or (output.exists() and any(p.name not in allowed or not p.is_file() for p in output.iterdir())):
        parser.error('Output must be fresh, except pre-existing WORKFLOW-EXECUTION.json/diagnostic.log')
    output.mkdir(parents=True, exist_ok=True)
    sys.path[:0] = [str(repo / 'poc'), str(repo / 'poc/tests')]
    import large_repository_online_driver as driver
    import online_inventory_store as inventory
    import online_investigation as investigation
    from large_repository_fixture import create_large_fixture
    from business_index import build_business_index
    from repository_discovery import ensure_repository_search
    from online_material_export import export_material
    initial = binding(repo)
    report = {'schema': 'online-inventory-cost-diagnostic/v1', 'status': 'incomplete',
        'started_at': datetime.now(timezone.utc).isoformat(), 'files': args.files,
        'mode_sequence': ['DELETE', 'PERSIST', 'TRUNCATE', 'DELETE'], 'synchronous': 'FULL',
        'production_defaults_changed': False, 'real_model_calls': False, 'real_source_access': False,
        'source_regression_acceptance': False, 'executable_acceptance': False,
        'source_binding_scope': list(initial), 'source_binding_before': initial,
        'observer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), 'cases': [],
        'limits': ['This diagnostic does not replace the original 10001-file test, complete regressions, final EXE or office acceptance.',
                   'Only the listed repository files are checked unchanged; this is not an assertion that the entire checkout is clean.',
                   'Row-changing commits are not direct VFS fsync counts.',
                   'No fastest journal mode is selected automatically.']}
    atomic_json(output / 'DIAGNOSTIC-REPORT.json', report)
    observer = None
    try:
        with mock.patch('socket.create_connection', side_effect=AssertionError('No diagnostic network')), \
             mock.patch('socket.socket.connect', side_effect=AssertionError('No diagnostic network')):
            manifest = create_large_fixture(output / 'fixture', args.files)
            source, database, material = Path(manifest['source']), output / 'index.sqlite', output / 'material'
            built = build_business_index(source, database, source_format='free', verify_content=True, quiet=True, framework_reference_path='')
            ensure_repository_search(database, source)
            export_material(database, source, material, repository_id='example/neutral-source', commit='d' * 40, build_report=built)
            expected = None
            for index, mode in enumerate(report['mode_sequence'], 1):
                case_dir = output / f'case-{index:02d}-{mode.lower()}'
                case_dir.mkdir()
                item = {'case': index, 'mode': mode, 'status': 'running'}
                report['cases'].append(item)
                atomic_json(output / 'DIAGNOSTIC-REPORT.json', report)
                observer = Observer(case_dir, 'same-machine-neutral-pipeline', journal_mode=mode)
                observer.snapshot();observer.thread.start()
                started = time.perf_counter()
                try:
                    with instrument(observer, driver, inventory, investigation):
                        value = driver.exercise_online_store(material, source, case_dir / 'inventory')
                        checked = verify_delivery(value, manifest['gold'], args.files)
                    if expected is None:
                        expected = checked
                    else:
                        assert checked == expected, 'Journal cases changed semantic results or required work'
                    assert observer.counts['connections_verified_FULL'] == observer.counts['connect']
                    assert observer.config, 'Missing observed SQLite configuration'
                    assert all(config['synchronous'] == 2 and config['journal_mode'].upper() == mode for config in observer.config.values())
                    observer.status = 'diagnostic_case_passed'
                    item.update(status='passed', seconds=time.perf_counter() - started, verification=checked)
                    atomic_json(case_dir / 'result.json', item)
                except BaseException as error:
                    observer.status = 'interrupted' if isinstance(error, KeyboardInterrupt) else 'failed'
                    item.update(status=observer.status, error_type=type(error).__name__)
                    atomic_json(case_dir / 'result.json', item)
                    raise
                finally:
                    observer.stop.set();observer.thread.join(timeout=1)
                    observer.snapshot(announce=True)
                    atomic_json(case_dir / 'observations.json', json.loads((case_dir / 'diagnostic.json').read_text()))
                atomic_json(output / 'DIAGNOSTIC-REPORT.json', report)
                observer = None
        report['source_binding_after'] = binding(repo)
        assert report['source_binding_after'] == initial, 'Observed repository source bytes changed during diagnostic'
        report['source_unchanged_in_declared_scope'] = True
        assert len(report['cases']) == 4 and all(case['status'] == 'passed' for case in report['cases'])
        report['status'] = 'completed_diagnostic'
    except KeyboardInterrupt:
        report['status'] = 'interrupted'
        if report['cases'] and report['cases'][-1]['status'] == 'running':
            report['cases'][-1]['status'] = 'interrupted'
        if observer is not None:
            observer.status = 'interrupted'
        return 130
    except BaseException as error:
        report.update(status='failed', error_type=type(error).__name__, error_message=str(error)[:300])
        if report['cases'] and report['cases'][-1]['status'] == 'running':
            report['cases'][-1]['status'] = 'failed'
        if observer is not None:
            observer.status = 'failed'
        raise
    finally:
        if observer is not None:
            observer.stop.set();observer.thread.join(timeout=1);observer.snapshot(announce=True)
            atomic_json(observer.output / 'observations.json', json.loads((observer.output / 'diagnostic.json').read_text()))
            if report['cases']:
                atomic_json(observer.output / 'result.json', report['cases'][-1])
        report['source_binding_after'] = binding(repo)
        report['source_unchanged_in_declared_scope'] = report['source_binding_after'] == initial
        if not report['source_unchanged_in_declared_scope'] and report['status'] == 'completed_diagnostic':
            report.update(status='failed', error_type='SourceBindingChanged',
                          error_message='Final source observation changed during diagnostic')
        report['completed_at'] = datetime.now(timezone.utc).isoformat()
        atomic_json(output / 'DIAGNOSTIC-REPORT.json', report)
    return 0 if report['status'] == 'completed_diagnostic' else 1

if __name__ == '__main__':
    sys.dont_write_bytecode = True
    raise SystemExit(main())
