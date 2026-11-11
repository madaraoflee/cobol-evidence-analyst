"""Mock native execution while checking real ZIP, paths, hashes and probe policy."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'poc')]
from tools import check_packaged_sqlite_runtime as gate
from tools.desktop_acceptance import BINARY_MEMBER, BUILD_MEMBER, inspect_package
import sqlite_runtime_probe as probe_module


class PackagedSQLiteRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.commit = 'a' * 40
        self.dll = b'Neutral DLL bytes for mocked native execution only'
        self.pin = deepcopy(probe_module.APPROVED_PIN)
        # This fixture does not claim to contain or run the approved DLL.
        # Patch only the reviewed-pin constant, retaining the real validator.
        self.pin['dll_sha256'] = hashlib.sha256(self.dll).hexdigest()
        self.pin_path = self.root / 'pin.json'
        self.pin_path.write_text(json.dumps(self.pin), encoding='utf-8')
        self.archive = self.root / 'candidate.zip'
        self.recorded = {'version': self.pin['version'], 'source_id': self.pin['source_id'],
                         'loaded_dll_sha256': self.pin['dll_sha256'],
                         'loaded_dll_path': 'C:/original-build/product/sqlite3.dll'}
        self.native_mutator = None
        self.native_calls = []
        self.counter = 0
        self.write_package()

    def write_package(self, *, dll=None, recorded=None, extra=()):
        binary = bytearray(128)
        binary[:2] = b'MZ'
        struct.pack_into('<I', binary, 0x3C, 64)
        binary[64:68] = b'PE\0\0'
        struct.pack_into('<H', binary, 68, 0x8664)
        metadata = {'target': 'windows-x64', 'source_commit': self.commit, 'source_dirty': False,
                    'source_fingerprint': 'b' * 64,
                    'self_test': {'ok': True, 'json_accelerated': True, 'model_calls': 0,
                                  'sqlite_runtime': self.recorded if recorded is None else recorded}}
        with zipfile.ZipFile(self.archive, 'w') as package:
            package.writestr(BINARY_MEMBER, bytes(binary))
            package.writestr(BUILD_MEMBER, json.dumps(metadata))
            package.writestr('COBOLWorkbench/COBOLWorkbench/sqlite3.dll', self.dll if dll is None else dll)
            for name, content in extra:
                package.writestr(name, content)

    def native(self, command, **options):
        self.native_calls.append((command, options))
        binary = Path(command[0])
        value = {'schema_version': 'sqlite-runtime-probe/v1', 'status': 'passed',
                 'runtime_platform': 'Windows', 'pointer_bits': 64, 'identity_kind': 'compiled',
                 'version': self.pin['version'], 'source_id': self.pin['source_id'],
                 'loaded_dll_path': str(binary.parent / 'sqlite3.dll'),
                 'loaded_dll_sha256': self.pin['dll_sha256'], 'extension_path': None,
                 'extension_origin': 'built_in', 'compile_options': sorted(probe_module.REQUIRED_OPTIONS),
                 'checks': {name: True for name in probe_module.REQUIRED_CHECKS}}
        report = {'ok': True, 'model_calls': 0, 'sqlite_runtime': value, 'marker': 'actual native return fixture'}
        if self.native_mutator:
            self.native_mutator(report, command)
        Path(command[command.index('--self-test-result') + 1]).write_text(json.dumps(report), encoding='utf-8')
        return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')

    def run_gate(self, *, runner=None, platform='win32', source_commit=None, output=None):
        self.counter += 1
        output = output or self.root / ('check-' + str(self.counter))
        with mock.patch.object(gate.sys, 'platform', platform), \
                mock.patch.object(gate, 'APPROVED_PIN', self.pin), \
                mock.patch.object(probe_module, 'APPROVED_PIN', self.pin), \
                mock.patch.object(gate.subprocess, 'run', side_effect=runner or self.native), \
                mock.patch.object(gate, 'validate_report', wraps=probe_module.validate_report) as validate:
            report = gate.check_package(archive=self.archive, source_commit=source_commit or self.commit,
                                        pin=self.pin_path, output=output)
        self.last_output, self.last_validation = output.resolve(), validate
        self.assertEqual(json.loads((output / gate.REPORT_NAME).read_text()), report)
        return report

    def test_native_exe_identity_is_bound_to_one_zip_and_actual_packaged_dll_without_changing_joint_contract(self):
        expected = inspect_package(self.archive, self.commit)
        with mock.patch.dict(os.environ, {'COMPANY_API_KEY': 'private-token', 'PYTHONPATH': 'untrusted-python',
                'WORKBENCH_SETTINGS': 'private-settings', 'FRAMEWORK_REFERENCE_PATH': 'private-reference'}):
            report = self.run_gate()
        self.assertEqual(report['status'], 'passed', report)
        self.assertEqual(report['errors'], [])
        for key in ('archive_sha256', 'binary_path', 'binary_sha256', 'application_sha256', 'source_commit', 'source_dirty'):
            self.assertEqual(report[key], expected[key])
        self.assertEqual(inspect_package(self.archive, self.commit), expected)
        self.assertEqual(report['sqlite_runtime'], report['self_test']['sqlite_runtime'])
        self.assertEqual(report['build_self_test_sqlite_runtime'], self.recorded)
        self.assertEqual(report['packaged_dll']['sha256'], self.pin['dll_sha256'])
        self.assertEqual((report['model_calls'], report['external_model_calls'], report['paid_model_calls']), (0, 0, 0))
        self.last_validation.assert_called_once_with(report['sqlite_runtime'], self.pin,
            self.last_output / 'extracted/COBOLWorkbench/COBOLWorkbench', identity_kind='compiled')
        self.assertEqual(len(self.native_calls), 1)
        command, options = self.native_calls[0]
        self.assertEqual(Path(command[0]).name, 'COBOLWorkbench.exe')
        self.assertIn('--self-test', command)
        self.assertEqual(json.loads(Path(command[command.index('--sqlite-runtime-pin') + 1]).read_text()), self.pin)
        self.assertEqual(Path(command[command.index('--data-dir') + 1]), self.last_output / 'disposable-profile')
        self.assertEqual(options['timeout'], 120)
        self.assertTrue(options['check'])
        for key in ('COMPANY_API_KEY', 'PYTHONPATH', 'WORKBENCH_SETTINGS', 'FRAMEWORK_REFERENCE_PATH'):
            self.assertNotIn(key, options['env'])

    def test_source_mismatch_and_unsafe_zip_fail_before_execution_or_extraction_escape(self):
        wrong = self.run_gate(source_commit='c' * 40)
        self.assertEqual(wrong['status'], 'failed')
        self.assertEqual(wrong['errors'][0]['code'], 'DELIVERY_SOURCE_IDENTITY_MISMATCH')
        self.write_package(extra=[('../outside-marker', b'outside')])
        unsafe = self.run_gate()
        self.assertEqual(unsafe['status'], 'failed')
        self.assertEqual(unsafe['errors'][0]['code'], 'DELIVERY_UNSAFE_MEMBER')
        self.assertEqual(self.native_calls, [])
        self.assertFalse((self.root / 'outside-marker').exists())

    def test_exactly_one_matching_dll_must_exist_inside_the_extracted_application_before_execution(self):
        for wrong_bytes, extra in ((b'Wrong component', ()), (self.dll, [('COBOLWorkbench/extra/SQLite3.DLL', self.dll)])):
            with self.subTest(extra=bool(extra)):
                self.write_package(dll=wrong_bytes, extra=extra)
                report = self.run_gate()
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(report['errors'][0]['code'], 'PACKAGED_SQLITE_DLL_MISMATCH')
        self.assertEqual(self.native_calls, [])

    def test_real_probe_validation_rejects_outside_dll_wrong_identity_or_incomplete_checks_without_sdk_fallback(self):
        outside = self.root / 'sqlite3.dll'
        outside.write_bytes(self.dll)
        mutations = [
            lambda value: value.update(loaded_dll_path=str(outside)),
            lambda value: value.update(loaded_dll_sha256='0' * 64),
            lambda value: value.update(version='0.0.0'),
            lambda value: value.update(source_id='Different source identity'),
            lambda value: value.update(identity_kind='source'),
            lambda value: value['checks'].update(backup=False),
            lambda value: value.update(compile_options=[]),
        ]
        for mutate in mutations:
            self.native_mutator = lambda report, command, change=mutate: change(report['sqlite_runtime'])
            with self.subTest(mutation=mutations.index(mutate)):
                report = self.run_gate()
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(report['errors'][0]['stage'], 'runtime_report')
                self.assertIsNone(report['model_calls'])
        self.assertEqual(len(self.native_calls), len(mutations))
        self.assertTrue(all(Path(call[0][0]).name == 'COBOLWorkbench.exe' for call in self.native_calls))

    def test_build_info_source_version_and_hash_must_match_the_new_native_observation(self):
        for field in ('version', 'source_id', 'loaded_dll_sha256'):
            changed = {**self.recorded, field: 'different'}
            self.write_package(recorded=changed)
            with self.subTest(field=field):
                report = self.run_gate()
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(report['errors'][0]['code'], 'PACKAGED_SQLITE_BUILD_IDENTITY_MISMATCH')

    def test_timeout_and_failed_native_self_test_are_failed_reports_without_an_interpreter_retry(self):
        failures = [subprocess.TimeoutExpired(['native-exe'], 120),
                    subprocess.CalledProcessError(1, ['native-exe'], stderr=b'PRIVATE native error')]
        for failure in failures:
            calls = []
            def fail_once(command, **options):
                calls.append(command)
                raise failure
            with self.subTest(failure=type(failure).__name__):
                report = self.run_gate(runner=fail_once)
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(report['errors'][0]['stage'], 'native_self_test')
                self.assertEqual(len(calls), 1)
                self.assertNotIn('PRIVATE', json.dumps(report))
        self.native_mutator = lambda report, command: report.update(model_calls=1)
        report = self.run_gate()
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['errors'][0]['code'], 'PACKAGED_SQLITE_NATIVE_SELF_TEST_FAILED')

    def test_executable_or_archive_mutation_during_native_execution_cannot_pass_final_binding(self):
        for target in ('executable', 'archive', 'pin', 'pin_snapshot'):
            self.write_package()
            self.pin_path.write_text(json.dumps(self.pin), encoding='utf-8')
            def mutate(report, command):
                if target == 'executable':
                    Path(command[0]).write_bytes(b'Changed executable')
                elif target == 'archive':
                    self.archive.write_bytes(b'Changed archive')
                elif target == 'pin':
                    self.pin_path.write_text('{}', encoding='utf-8')
                else:
                    Path(command[command.index('--sqlite-runtime-pin') + 1]).write_text('{}', encoding='utf-8')
            self.native_mutator = mutate
            with self.subTest(target=target):
                report = self.run_gate()
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(report['errors'][0]['code'], 'PACKAGED_SQLITE_INPUT_CHANGED_DURING_CHECK')

    def test_non_windows_and_reused_output_never_execute_or_overwrite_existing_evidence(self):
        report = self.run_gate(platform='darwin')
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['errors'][0]['code'], 'PACKAGED_SQLITE_NATIVE_WINDOWS_REQUIRED')
        self.assertEqual(self.native_calls, [])
        existing = self.root / 'existing'
        existing.mkdir()
        marker = existing / 'retained-report.json'
        marker.write_bytes(b'Original evidence')
        with self.assertRaisesRegex(ValueError, 'PACKAGED_SQLITE_OUTPUT_MUST_BE_NEW'):
            self.run_gate(output=existing)
        self.assertEqual(marker.read_bytes(), b'Original evidence')


if __name__ == '__main__':
    unittest.main()
