"""Explicit materialization preserves one verified SDK launcher and all guards.

Neutral SDK/PE fixtures and child probes are synthetic; no Windows executable
or model is run by these tests. Real symlink metadata is retained, with only
Windows tag/attribute fields supplied when this runs on another platform.
"""
from contextlib import contextmanager, redirect_stderr
import hashlib
import io
import json
from pathlib import Path, PureWindowsPath
import stat
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tools'), str(Path(__file__).parent)]
from tools import prepare_private_sqlite_runtime as preparation
import setup_private_sqlite_workflow as setup_tool
import test_prepare_private_sqlite_runtime as prepare_fixture
import test_private_sqlite_workflow_setup as setup_fixture


class TaggedStat:
    def __init__(self, original, tag, attributes):
        self.original = original
        self.st_reparse_tag, self.st_file_attributes = tag, attributes
    def __getattr__(self, name):
        return getattr(self.original, name)


class PrivateSDKLauncherAliasTests(unittest.TestCase):
    def setUp(self):
        self.fixture = prepare_fixture.PrivateRuntimeDraftTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.alias = self.fixture.source / 'python3.exe'
        self.alias.symlink_to(self.fixture.source / 'python.exe')

    @contextmanager
    def windows_alias(self, *, path=None, tag=0xA000000C, attributes=0x420):
        selected, original = path or self.alias, Path.lstat
        def lstat(path, *args, **options):
            value = original(path, *args, **options)
            return TaggedStat(value, tag, attributes) if path == selected else value
        with mock.patch.object(Path, 'lstat', new=lstat):
            yield

    def args(self, name, *, enabled=True):
        args = self.fixture.arguments(name)
        args.materialize_python3_alias = enabled
        return args

    def test_explicit_materialization_preserves_complete_content_host_alias_and_source_identity(self):
        original = Path.read_bytes
        def no_alias_read(path):
            if path == self.alias:
                raise AssertionError('The launcher link must never be read as file content')
            return original(path)
        with self.windows_alias(), mock.patch.object(Path, 'read_bytes', new=no_alias_read):
            before = preparation.source_manifest(self.fixture.source, True)
            args = self.args('verified-launcher')
            code, receipt, child = self.fixture.invoke(args)
            self.assertEqual(code, 0)
            self.assertEqual(receipt['source_before'], before)
            self.assertEqual(receipt['source_after'], before)
            self.assertIs(receipt['source_unchanged'], True)
            self.assertEqual(receipt['source_manifest_sha256'], hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest())
            private = Path(args.output) / 'python'
            expected = {**before, 'python3.exe': before['python.exe'], 'DLLs/sqlite3.dll': self.fixture.pin['dll_sha256']}
            self.assertEqual(preparation.tree_manifest(private), expected)
            self.assertFalse((private / 'python3.exe').is_symlink())
            self.assertFalse((private / 'python3.exe').samefile(self.fixture.source / 'python.exe'))
            self.assertFalse((private / 'python3.exe').samefile(private / 'python.exe'))
            self.assertEqual((private / 'python3.exe').read_bytes(), (private / 'python.exe').read_bytes())
            self.assertTrue(self.alias.is_symlink())
            self.assertEqual(str(self.alias.readlink()), before['python3.exe']['link_target'])
            self.assertEqual(preparation.python3_alias(self.fixture.source), before['python3.exe'])
            self.assertEqual(child.call_count, 1)

    def test_default_policy_still_rejects_the_launcher_and_preserves_the_old_error_text(self):
        with self.windows_alias():
            code, receipt, child = self.fixture.invoke(self.args('default-rejection', enabled=False))
            self.assertEqual(code, 2)
            self.assertFalse(child.called)
            self.assertEqual(receipt['error'], 'Symbolic links and Windows reparse points are refused')
            self.assertEqual(receipt['path_guard']['component_path'], str(self.alias))
            self.assertEqual(receipt['path_guard']['reparse_tag'], 0xA000000C)
            self.assertTrue(self.alias.is_symlink())

    def test_wrong_and_outside_targets_are_rejected_without_reading_their_bytes(self):
        outside = self.fixture.root / 'outside-python.exe'
        outside.write_bytes(b'Outside content must never be read')
        outside_same_file = self.fixture.root / 'outside-same-file.exe'
        outside_same_file.hardlink_to(self.fixture.source / 'python.exe')
        original = Path.read_bytes
        def no_outside_read(path):
            if path in (outside, outside_same_file, self.alias):
                raise AssertionError('An unapproved alias target must not be read')
            return original(path)
        for index, target in enumerate((outside, outside_same_file, self.fixture.source / 'python313.dll')):
            with self.subTest(target=target):
                self.alias.unlink()
                self.alias.symlink_to(target)
                with self.windows_alias(), mock.patch.object(Path, 'read_bytes', new=no_outside_read):
                    code, receipt, child = self.fixture.invoke(self.args('wrong-target-' + str(index)))
                self.assertEqual(code, 2)
                self.assertFalse(child.called)
                self.assertEqual(receipt['path_guard']['component_path'], str(self.alias))
                self.assertFalse(Path(self.args('wrong-target-' + str(index)).output).exists())

    def test_nested_other_named_links_and_junction_tags_are_not_exceptions(self):
        for index, (relative, tag) in enumerate((('DLLs/python3.exe', 0xA000000C),
                ('python3.13.exe', 0xA000000C), ('python3.exe', 0xA0000003))):
            with self.subTest(relative=relative, tag=tag):
                extra = self.fixture.source / relative
                if extra != self.alias:
                    extra.symlink_to(self.fixture.source / 'python.exe')
                try:
                    with self.windows_alias(), self.windows_alias(path=extra, tag=tag):
                        code, receipt, child = self.fixture.invoke(self.args('other-link-' + str(index)))
                    self.assertEqual(code, 2)
                    self.assertFalse(child.called)
                    self.assertEqual(receipt['path_guard']['component_path'], str(extra))
                finally:
                    if extra != self.alias:
                        extra.unlink()

    def test_late_alias_target_change_fails_source_guard_after_private_probe(self):
        outside = self.fixture.root / 'late-outside.exe'
        outside.write_bytes(b'Unrelated late target')
        def change(private, output, pin):
            value = self.fixture.good_probe(private, output, pin)
            self.alias.unlink()
            self.alias.symlink_to(outside)
            return value
        with self.windows_alias():
            code, receipt, child = self.fixture.invoke(self.args('late-target'), probe=change)
        self.assertEqual(code, 1)
        self.assertEqual(receipt['status'], 'failed')
        self.assertIs(receipt['source_unchanged'], False)
        self.assertEqual(receipt['source_guard_path']['component_path'], str(self.alias))
        self.assertEqual(child.call_count, 1)

    def test_installer_device_prefix_is_limited_to_an_ordinary_drive_target(self):
        source = PureWindowsPath(r'C:\neutral\sdk')
        target = source / 'python.exe'
        with mock.patch.object(preparation, 'Path', PureWindowsPath):
            self.assertEqual(preparation.launcher_target(source, r'\\?\C:\neutral\sdk\python.exe'), target)
            self.assertEqual(preparation.launcher_target(source, r'C:\neutral\sdk\python.exe'), target)
            self.assertEqual(preparation.launcher_target(source, 'python.exe'), target)
            for value in (r'\\?\UNC\server\share\python.exe', r'\\server\share\python.exe',
                          r'C:\neutral\sdk\..\python.exe', r'C:\outside\python.exe',
                          r'\\?\C:python.exe', 'other.exe'):
                with self.subTest(value=value), self.assertRaises(preparation.InputError):
                    preparation.launcher_target(source, value)

    def test_bootstrap_enables_materialization_only_for_its_bound_automatic_sdk(self):
        fixture = setup_fixture.PrivateSQLiteWorkflowSetupTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.base = fixture.base.resolve()
        with mock.patch.dict(sys.modules, {'prepare_private_sqlite_runtime': preparation}), \
                mock.patch.object(setup_tool.sys, 'base_prefix', str(fixture.base)), \
                mock.patch.object(setup_tool.sys, 'executable', str(fixture.base / 'python.exe')):
            result = fixture.run_setup(source_base=None)
        self.assertEqual(result['status'], 'completed_private_workflow')
        self.assertIn('--materialize-python3-alias', fixture.commands[0][0])
        self.assertEqual(result['automatic_sdk_before'], result['automatic_sdk_after'])
        fixture.commands.clear()
        result = fixture.run_setup(output=fixture.root / 'explicit-selection')
        self.assertNotIn('--materialize-python3-alias', fixture.commands[0][0])
        self.assertNotIn('automatic_sdk_before', result)
        fixture.commands.clear()
        other = fixture.root / 'other-python.exe'
        other.write_bytes(b'Unselected interpreter')
        previous_path, previous_env = fixture.path_file.read_bytes(), fixture.env_file.read_bytes()
        with mock.patch.dict(sys.modules, {'prepare_private_sqlite_runtime': preparation}), \
                mock.patch.object(setup_tool.sys, 'base_prefix', str(fixture.base)), \
                mock.patch.object(setup_tool.sys, 'executable', str(other.resolve())):
            with self.assertRaisesRegex(ValueError, 'AUTOMATIC_BASE_MISMATCH'):
                fixture.run_setup(source_base=None, output=fixture.root / 'unbound-selection')
        self.assertEqual(fixture.commands, [])
        self.assertEqual(fixture.path_file.read_bytes(), previous_path)
        self.assertEqual(fixture.env_file.read_bytes(), previous_env)

    def test_bootstrap_rejects_a_changed_automatic_sdk_without_activating_environment(self):
        fixture = setup_fixture.PrivateSQLiteWorkflowSetupTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.base = fixture.base.resolve()
        original = fixture.fake_run
        def change_after_probe(command, **options):
            original(command, **options)
            if '--source-base' not in [str(value) for value in command]:
                (fixture.base / 'python.exe').write_bytes(b'Changed selected interpreter')
        with mock.patch.dict(sys.modules, {'prepare_private_sqlite_runtime': preparation}), \
                mock.patch.object(setup_tool.sys, 'base_prefix', str(fixture.base)), \
                mock.patch.object(setup_tool.sys, 'executable', str(fixture.base / 'python.exe')), \
                mock.patch.object(fixture, 'fake_run', new=change_after_probe):
            with self.assertRaisesRegex(ValueError, 'AUTOMATIC_BASE_CHANGED'):
                fixture.run_setup(source_base=None)
        self.assertEqual(fixture.path_file.read_text(), 'existing-path\n')
        self.assertEqual(fixture.env_file.read_text(), 'EXISTING=value\n')

    def test_bootstrap_late_launcher_target_change_never_activates_or_reads_outside(self):
        fixture = setup_fixture.PrivateSQLiteWorkflowSetupTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.base = fixture.base.resolve()
        alias = fixture.base / 'python3.exe'
        alias.symlink_to(fixture.base / 'python.exe')
        outside = fixture.root / 'outside-bootstrap.exe'
        outside.write_bytes(b'Outside target is never runtime input')
        original_run, original_read = fixture.fake_run, Path.read_bytes
        def change_after_probe(command, **options):
            original_run(command, **options)
            if '--source-base' not in [str(value) for value in command]:
                alias.unlink()
                alias.symlink_to(outside)
        def refuse_outside(path):
            if path in (outside, alias):
                raise AssertionError('No byte reads through the changed source launcher')
            return original_read(path)
        with mock.patch.dict(sys.modules, {'prepare_private_sqlite_runtime': preparation}), \
                mock.patch.object(setup_tool.sys, 'base_prefix', str(fixture.base)), \
                mock.patch.object(setup_tool.sys, 'executable', str(fixture.base / 'python.exe')), \
                mock.patch.object(fixture, 'fake_run', new=change_after_probe), \
                mock.patch.object(Path, 'read_bytes', new=refuse_outside), self.windows_alias(path=alias):
            with self.assertRaises(preparation.InputError):
                fixture.run_setup(source_base=None)
        fixture.assert_not_activated()
        details = json.loads((fixture.root / 'fresh-setup/WORKFLOW-SQLITE-SETUP.json').read_text())
        self.assertEqual(details['status'], 'failed_private_workflow')
        self.assertEqual(Path(details['path_guard']['component_path']), alias)

    def test_early_guard_diagnostic_and_child_failure_remain_available_without_an_unsafe_receipt(self):
        real = self.fixture.root / 'ordinary-parent'
        real.mkdir()
        link = self.fixture.root / 'linked-parent'
        link.symlink_to(real, target_is_directory=True)
        args = self.args('early-diagnostic')
        args.receipt = str(link / 'receipt.json')
        options = []
        for key in ('source_base', 'output', 'pin_manifest', 'archive', 'receipt'):
            options.extend(('--' + key.replace('_', '-'), getattr(args, key)))
        error = io.StringIO()
        with redirect_stderr(error):
            code = preparation.main(options)
        self.assertEqual(code, 2)
        details = json.loads(error.getvalue())['path_guard']
        self.assertEqual(details['component_path'], str(link))
        self.assertFalse(Path(args.receipt).exists())
        failure = self.fixture.root / 'workflow-failure'
        failure.mkdir()
        (failure / 'prepare-stderr.txt').write_text(error.getvalue())
        self.assertEqual(setup_tool._failure_path_guard(failure, subprocess.CalledProcessError(2, [])), details)


if __name__ == '__main__':
    unittest.main()
