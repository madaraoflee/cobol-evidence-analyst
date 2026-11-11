"""CI bootstrap never activates an unverified or reused private runtime."""
from contextlib import ExitStack
from io import BytesIO
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import setup_private_sqlite_workflow as setup_tool


class DownloadResponse(BytesIO):
    def __init__(self, content, url):
        super().__init__(content)
        self.url = url
    def geturl(self):
        return self.url


class PrivateSQLiteWorkflowSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / 'original-python'
        self.base.mkdir()
        (self.base / 'python.exe').write_bytes(b'Original interpreter')
        self.base_bytes = (self.base / 'python.exe').read_bytes()
        self.path_file, self.env_file = self.root / 'github-path', self.root / 'github-env'
        self.path_file.write_text('existing-path\n')
        self.env_file.write_text('EXISTING=value\n')
        self.content = b'Frozen official archive fixture'
        self.pin = {'version': '3.54.1', 'url': 'https://sqlite.org/2026/sqlite-dll-win-x64-3540100.zip',
            'archive_sha3_256': hashlib.sha3_256(self.content).hexdigest(),
            'dll_sha256': 'a' * 64, 'source_id': 'Synthetic frozen engine identity'}
        self.pin_file = self.root / 'pin.json'
        self.pin_file.write_text(json.dumps(self.pin))
        self.commands = []
        self.prepare_change = None
        self.probe_change = None
        self.validate = mock.Mock()

    def fake_run(self, command, *, stdout, stderr, timeout):
        command = [str(value) for value in command]
        self.commands.append((command, timeout))
        Path(stderr).write_text('')
        def arg(name):
            return Path(command[command.index(name) + 1])
        if '--source-base' in command:
            output = arg('--output')
            interpreter = output / 'python' / 'python.exe'
            interpreter.parent.mkdir(parents=True)
            interpreter.write_bytes(b'Private interpreter')
            (interpreter.parent / 'Scripts').mkdir()
            receipt = {'status': 'completed_private_runtime', 'exit_code': 0,
                'source_unchanged': True, 'source_base': str(arg('--source-base')),
                'output': str(output), 'pin_manifest': str(arg('--pin-manifest')),
                'pin': self.pin, 'source_before': {'python.exe': 'original'},
                'source_after': {'python.exe': 'original'}, 'source_manifest_sha256': 'b' * 64,
                'private_python': str(interpreter)}
            if self.prepare_change:
                self.prepare_change(receipt)
            arg('--receipt').write_text(json.dumps(receipt))
            Path(stdout).write_text('{}')
        else:
            report = {'schema_version': 'sqlite-runtime-probe/v1', 'status': 'passed',
                'version': self.pin['version'], 'source_id': self.pin['source_id'],
                'loaded_dll_sha256': self.pin['dll_sha256']}
            if self.probe_change:
                self.probe_change(report)
            Path(stdout).write_text(json.dumps(report))

    def run_setup(self, **changes):
        arguments = dict(output=self.root / 'fresh-setup', pin_manifest=self.pin_file,
            source_base=self.base, github_path=self.path_file, github_env=self.env_file)
        arguments.update(changes)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(setup_tool.sys, 'platform', 'win32'))
            stack.enter_context(mock.patch.object(setup_tool, '_run', side_effect=self.fake_run))
            stack.enter_context(mock.patch.object(setup_tool, 'urlopen',
                side_effect=lambda request, timeout: DownloadResponse(self.content, request.full_url)))
            stack.enter_context(mock.patch.dict(sys.modules, {
                'sqlite_runtime_probe': SimpleNamespace(validate_report=self.validate)}))
            return setup_tool.setup(**arguments)

    def assert_not_activated(self):
        self.assertEqual(self.path_file.read_text(), 'existing-path\n')
        self.assertEqual(self.env_file.read_text(), 'EXISTING=value\n')
        self.assertEqual((self.base / 'python.exe').read_bytes(), self.base_bytes)

    def test_verified_fresh_private_interpreter_is_probed_before_actions_environment_activation(self):
        result = self.run_setup()
        self.assertEqual(result['status'], 'completed_private_workflow')
        self.assertEqual(len(self.commands), 2)
        prepare, probe = (row[0] for row in self.commands)
        self.assertIn('--source-base', prepare)
        self.assertEqual(probe[1:4], ['-I', '-S', '-B'])
        self.assertEqual(Path(probe[0]), Path(result['private_python']))
        self.validate.assert_called_once()
        self.assertEqual(self.validate.call_args.kwargs, {'identity_kind': 'source'})
        self.assertEqual(self.validate.call_args.args[2], Path(result['private_base']))
        lines = self.path_file.read_text().splitlines()
        self.assertEqual(lines, ['existing-path', result['private_base']])
        self.assertNotIn(str(Path(result['private_base']) / 'Scripts'), lines)
        # Actions reverses prepends: private Python precedes the old interpreter.
        self.assertEqual(list(reversed(lines))[0], result['private_base'])
        env = self.env_file.read_text()
        self.assertIn('PYTHONDONTWRITEBYTECODE=1\n', env)
        self.assertIn('WORKBENCH_PRIVATE_SQLITE_RECEIPT=' + result['preparation_receipt'], env)
        self.assertIn('WORKBENCH_PRIVATE_SQLITE_FINGERPRINT=' + result['private_base_fingerprint'], env)
        self.assertIn('WORKBENCH_SQLITE_RUNTIME_PIN=' + str(self.pin_file.resolve()), env)
        self.assertEqual((self.base / 'python.exe').read_bytes(), self.base_bytes)
        self.assertEqual(json.loads((self.root / 'fresh-setup/WORKFLOW-SQLITE-SETUP.json').read_text()), result)

    def test_private_fingerprint_changes_with_the_pinned_engine_even_for_the_same_source_base(self):
        first = self.run_setup()
        self.pin['dll_sha256'] = 'c' * 64
        self.pin_file.write_text(json.dumps(self.pin))
        second = self.run_setup(output=self.root / 'different-fresh-setup')
        self.assertEqual(first['source_manifest_sha256'], second['source_manifest_sha256'])
        self.assertNotEqual(first['private_base_fingerprint'], second['private_base_fingerprint'])

    def test_reused_output_and_non_windows_host_are_rejected_before_network_or_activation(self):
        occupied = self.root / 'occupied'
        occupied.mkdir()
        with self.assertRaisesRegex(ValueError, 'FRESH_OUTPUT'):
            self.run_setup(output=occupied)
        with mock.patch.object(setup_tool.sys, 'platform', 'darwin'), mock.patch.object(setup_tool, 'urlopen') as fetch:
            with self.assertRaisesRegex(ValueError, 'WINDOWS_ONLY'):
                setup_tool.setup(output=self.root / 'not-windows')
            fetch.assert_not_called()
        self.assertEqual(self.commands, [])
        self.assert_not_activated()

    def test_bad_archive_digest_keeps_download_and_failure_receipt_without_activating_python(self):
        self.pin['archive_sha3_256'] = '0' * 64
        self.pin_file.write_text(json.dumps(self.pin))
        with self.assertRaisesRegex(ValueError, 'HASH_MISMATCH'):
            self.run_setup()
        output = self.root / 'fresh-setup'
        self.assertEqual((output / 'official-sqlite.zip').read_bytes(), self.content)
        receipt = json.loads((output / 'WORKFLOW-SQLITE-SETUP.json').read_text())
        self.assertEqual(receipt['status'], 'failed_private_workflow')
        self.assertEqual(receipt['stages'], ['download'])
        self.assertEqual(self.commands, [])
        self.assert_not_activated()

    def test_source_guard_identity_and_private_interpreter_must_all_be_verified(self):
        changes = (
            lambda r: r.update(source_unchanged=False),
            lambda r: r.update(source_after={'python.exe': 'changed'}),
            lambda r: r.update(source_manifest_sha256='invalid'),
            lambda r: r.update(private_python=str(self.base / 'python.exe')),
            lambda r: r.update(pin={}),
            lambda r: r.update(source_base=str(self.root)),
        )
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                self.prepare_change = change
                self.commands.clear()
                with self.assertRaises(ValueError):
                    self.run_setup(output=self.root / ('rejected-' + str(index)))
                self.assertEqual(len(self.commands), 1)
                self.assert_not_activated()
        self.validate.assert_not_called()

    def test_probe_validation_failure_retains_diagnostics_and_never_appends_actions_paths(self):
        self.validate.side_effect = ValueError('PINNED_DLL_NOT_ACTUALLY_LOADED')
        with self.assertRaisesRegex(ValueError, 'PINNED_DLL'):
            self.run_setup()
        output = self.root / 'fresh-setup'
        self.assertTrue((output / 'PRIVATE-SQLITE-PREPARATION.json').is_file())
        self.assertTrue((output / 'PRIVATE-SQLITE-PROBE.json').is_file())
        self.assertEqual(json.loads((output / 'WORKFLOW-SQLITE-SETUP.json').read_text())['status'], 'failed_private_workflow')
        self.assert_not_activated()

    def test_child_prepare_failure_is_not_hidden_or_followed_by_a_private_probe(self):
        original = self.fake_run
        def failure(command, **options):
            original(command, **options)
            raise subprocess.CalledProcessError(2, command)
        with mock.patch.object(self, 'fake_run', side_effect=failure):
            with self.assertRaises(subprocess.CalledProcessError):
                self.run_setup()
        self.assertEqual(len(self.commands), 1)
        self.assert_not_activated()

    def test_download_rejects_redirect_http_untrusted_hosts_and_excessive_bytes(self):
        for url in ('http://sqlite.org/2026/sqlite-dll-win-x64-3540100.zip',
                    'https://untrusted.invalid/2026/sqlite-dll-win-x64-3540100.zip'):
            with self.subTest(url=url):
                self.pin['url'] = url
                self.pin_file.write_text(json.dumps(self.pin))
                with self.assertRaisesRegex(ValueError, 'URL_INVALID'):
                    setup_tool._pin(self.pin_file)
        self.pin['url'] = 'https://sqlite.org/2026/sqlite-dll-win-x64-3540100.zip'
        with mock.patch.object(setup_tool, 'urlopen', return_value=DownloadResponse(self.content, 'http://sqlite.org/insecure.zip')):
            with self.assertRaisesRegex(ValueError, 'REDIRECT_REJECTED'):
                setup_tool.download_archive(self.pin, self.root / 'redirect.zip')
        with mock.patch.object(setup_tool, 'MAX_ARCHIVE_BYTES', 4), \
                mock.patch.object(setup_tool, 'urlopen', return_value=DownloadResponse(self.content, self.pin['url'])):
            with self.assertRaisesRegex(ValueError, 'TOO_LARGE'):
                setup_tool.download_archive(self.pin, self.root / 'oversized.zip')


if __name__ == '__main__':
    unittest.main()
