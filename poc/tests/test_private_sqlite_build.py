"""The approved private component must reach the build and actual product."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import private_sqlite_build as runtime

spec = importlib.util.spec_from_file_location("private_build_contract", ROOT / "build_app.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class PrivateSQLiteBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pin = {"version": "3.54.1", "source_id": "neutral source", "dll_sha256": hashlib.sha256(b"fixed DLL").hexdigest()}
        self.base = self.root / "prepared/python"
        self.before = {}
        for relative in ("python.exe", "python313.dll", "DLLs/_sqlite3.pyd", "Lib/sqlite3/dbapi2.py"):
            path = self.base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(relative.encode())
            self.before[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        (self.base / "DLLs/sqlite3.dll").write_bytes(b"fixed DLL")
        self.pin_path = self.root / "pin.json"
        self.pin_path.write_text(json.dumps(self.pin))
        self.receipt_path = self.root / "receipt.json"
        self.receipt = {"status": "completed_private_runtime", "exit_code": 0, "source_unchanged": True,
                        "pin": self.pin, "output": str(self.base.parent), "source_before": self.before,
                        "source_after": self.before.copy()}
        self.receipt_path.write_text(json.dumps(self.receipt))
        self.probe = {"loaded_dll_path": str(self.base / "DLLs/sqlite3.dll"),
                      "extension_path": str(self.base / "DLLs/_sqlite3.pyd")}
        self.module = SimpleNamespace(run_probe=mock.Mock(return_value=self.probe), validate_report=mock.Mock())

    def active(self):
        stack = ExitStack()
        stack.enter_context(mock.patch.dict(os.environ, {runtime.PIN_ENV: str(self.pin_path), runtime.RECEIPT_ENV: str(self.receipt_path)}))
        stack.enter_context(mock.patch.object(runtime.sys, "platform", "win32"))
        stack.enter_context(mock.patch.object(runtime.sys, "base_prefix", str(self.base)))
        stack.enter_context(mock.patch.object(runtime, "_probe_module", return_value=self.module))
        return stack

    def test_opt_in_is_required_and_half_configured_runtime_cannot_silently_fall_back(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(runtime.current_identity())
            with self.assertRaisesRegex(ValueError, "PRIVATE_SQLITE_RUNTIME_REQUIRED"):
                runtime.current_identity(required=True)
        with mock.patch.dict(os.environ, {runtime.PIN_ENV: str(self.pin_path)}, clear=True):
            with self.assertRaisesRegex(ValueError, "PRIVATE_SQLITE_RUNTIME_REQUIRED"):
                runtime.current_identity()

    def test_verified_identity_stays_stable_across_base_and_venv_interpreters(self):
        with self.active():
            first = runtime.current_identity()
            with mock.patch.object(sys, "executable", str(self.root / "venv/Scripts/python.exe")):
                second = runtime.current_identity()
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(first["inputs"]["sqlite_sha256"], self.pin["dll_sha256"])
        self.module.validate_report.assert_called_with(self.probe, self.pin, self.base, identity_kind="source")

    def test_failed_or_inconsistent_source_guard_receipts_are_rejected_before_probing(self):
        for patch in ({"status": "failed"}, {"exit_code": 1}, {"source_unchanged": False},
                      {"source_after": {}}, {"pin": {}}):
            with self.subTest(patch=patch):
                self.receipt_path.write_text(json.dumps({**self.receipt, **patch}))
                with self.active(), self.assertRaisesRegex(ValueError, "PRIVATE_SQLITE_RECEIPT_INVALID"):
                    runtime.current_identity()
        self.module.run_probe.assert_not_called()

    def test_wrong_base_or_changed_original_extension_cannot_reuse_a_successful_receipt(self):
        with self.active(), mock.patch.object(sys, "base_prefix", str(self.root / "host")):
            with self.assertRaisesRegex(ValueError, "PRIVATE_SQLITE_BASE_MISMATCH"):
                runtime.current_identity()
        (self.base / "DLLs/_sqlite3.pyd").write_bytes(b"changed extension")
        with self.active(), self.assertRaisesRegex(ValueError, "PRIVATE_SQLITE_BASE_FILE_CHANGED"):
            runtime.current_identity()
        self.module.run_probe.assert_not_called()

    def test_loaded_extension_and_dll_must_be_the_prepared_sdk_files(self):
        for field in ("extension_path", "loaded_dll_path"):
            with self.subTest(field=field):
                self.module.run_probe.return_value = {**self.probe, field: str(self.root / "host/file.dll")}
                with self.active(), self.assertRaisesRegex(ValueError, "PRIVATE_SQLITE_LOADED_PATH_MISMATCH"):
                    runtime.current_identity()

    def test_new_runtime_uses_a_distinct_environment_and_rejects_unmarked_reuse(self):
        info = {"fingerprint": "a" * 64, "inputs": {"component": "fixed"}}
        build_root = self.root / "build"
        environment = build_root / ("venv-" + "a" * 20)
        interpreter = environment / "Scripts/python.exe"
        with mock.patch.object(runtime, "current_identity", return_value=info), \
                mock.patch.object(builder, "BUILD_ROOT", build_root), mock.patch.object(builder.sys, "platform", "win32"), \
                mock.patch.object(builder.venv, "EnvBuilder") as factory:
            factory.return_value.create.side_effect = lambda path: path.mkdir(parents=True)
            actual = builder.prepare_environment(argparse.Namespace(skip_install=True, private_sqlite_runtime=True))
            self.assertEqual(actual, interpreter)
            factory.return_value.create.assert_called_once_with(environment)
            self.assertEqual(json.loads((environment / "private-runtime.json").read_text()), info["inputs"])
            interpreter.parent.mkdir(parents=True, exist_ok=True)
            interpreter.write_bytes(b"private interpreter")
            (environment / "private-runtime.json").write_text('{}')
            with self.assertRaisesRegex(ValueError, "PRIVATE_SQLITE_BUILD_ENVIRONMENT_MISMATCH"):
                builder.prepare_environment(argparse.Namespace(skip_install=True, private_sqlite_runtime=True))

    def test_ordinary_build_fixture_does_not_consume_ambient_private_release_settings(self):
        build_root = self.root / "ordinary"
        interpreter = build_root / ("venv/Scripts/python.exe" if sys.platform == "win32" else "venv/bin/python")
        interpreter.parent.mkdir(parents=True)
        interpreter.write_bytes(b"ordinary interpreter")
        with mock.patch.object(builder, "BUILD_ROOT", build_root), \
                mock.patch.object(runtime, "current_identity", side_effect=AssertionError("Explicit release opt-in is required")):
            self.assertEqual(builder.prepare_environment(argparse.Namespace(skip_install=True)), interpreter)

    def test_product_requires_one_pinned_dll_and_actual_loaded_path_inside_that_product(self):
        product = self.root / "app"
        product.mkdir()
        dll = product / "sqlite3.dll"
        dll.write_bytes(b"fixed DLL")
        value = {"loaded_dll_path": str(dll)}
        identity = {"pin": self.pin}
        with mock.patch.object(runtime, "_probe_module", return_value=self.module):
            self.assertEqual(runtime.validate_product({"sqlite_runtime": value}, product, identity), value)
            with self.assertRaisesRegex(ValueError, "PACKAGED_SQLITE_DLL_MISMATCH"):
                runtime.validate_product({"sqlite_runtime": {"loaded_dll_path": str(self.base / 'DLLs/sqlite3.dll')}}, product, identity)
            dll.write_bytes(b"old DLL")
            with self.assertRaisesRegex(ValueError, "PACKAGED_SQLITE_DLL_MISMATCH"):
                runtime.validate_product({"sqlite_runtime": value}, product, identity)
            dll.write_bytes(b"fixed DLL")
            (product / "other").mkdir()
            (product / "other/sqlite3.dll").write_bytes(b"fixed DLL")
            with self.assertRaisesRegex(ValueError, "PACKAGED_SQLITE_DLL_MISMATCH"):
                runtime.validate_product({"sqlite_runtime": value}, product, identity)

    def test_workflows_verify_active_private_interpreter_and_final_exe_before_candidate_upload(self):
        text = (ROOT / ".github/workflows/windows-desktop.yml").read_text()
        self.assertEqual(text.count('python tools/setup_private_sqlite_workflow.py'), 2)
        self.assertEqual(text.count('python tools/private_sqlite_build.py --verify-active'), 2)
        self.assertLess(text.index('python tools/check_packaged_sqlite_runtime.py'), text.index('name: Upload only the accepted candidate'))
        self.assertIn('timeout-minutes: 45', text)
        office = (ROOT / ".github/workflows/windows-office-acceptance.yml").read_text()
        self.assertLess(office.index('python tools/private_sqlite_build.py --verify-active'), office.index('name: Validate office contracts'))


if __name__ == "__main__":
    unittest.main()
