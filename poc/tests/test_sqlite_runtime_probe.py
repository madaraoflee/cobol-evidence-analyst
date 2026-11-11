"""Identity gates use neutral files; memory feature checks use no business data."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sqlite_runtime_probe as probe


class SQLiteRuntimeProbeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.dll = self.root / "sqlite3.dll"
        self.dll.write_bytes(b"neutral runtime identity fixture")
        self.extension = self.root / "_sqlite3.pyd"
        self.extension.write_bytes(b"neutral extension identity fixture")
        self.pin = {**probe.APPROVED_PIN, "dll_sha256": hashlib.sha256(self.dll.read_bytes()).hexdigest()}
        self.patch = mock.patch.object(probe, "APPROVED_PIN", self.pin)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def report(self, kind="source"):
        return {"schema_version": probe.SCHEMA, "status": "passed",
                "identity_kind": kind, "runtime_platform": "Windows", "pointer_bits": 64,
                "version": self.pin["version"], "source_id": self.pin["source_id"],
                "compile_options": sorted(probe.REQUIRED_OPTIONS),
                "checks": {name: True for name in probe.REQUIRED_CHECKS},
                "loaded_dll_path": str(self.dll), "loaded_dll_sha256": self.pin["dll_sha256"],
                "extension_path": str(self.extension), "extension_origin": "file"}

    def test_source_identity_accepts_external_venv_executable_only_when_DLL_and_extension_are_contained(self):
        value = {**self.report(), "executable": str(self.root.parent / "venv/python.exe")}
        self.assertIs(value, probe.validate_report(value, self.pin, self.root, identity_kind="source"))
        outside = self.root.parent / (self.root.name + "-outside.pyd")
        outside.write_bytes(b"outside identity fixture")
        self.addCleanup(outside.unlink)
        with self.assertRaises(RuntimeError):
            probe.validate_report({**value, "extension_path": str(outside)}, self.pin, self.root)

    def test_compiled_builtin_extension_requires_real_pinned_DLL_and_source_cannot_claim_builtin(self):
        value = {**self.report("compiled"), "extension_path": None, "extension_origin": "built_in"}
        self.assertIs(value, probe.validate_report(value, self.pin, self.root, identity_kind="compiled"))
        with self.assertRaises(RuntimeError):
            probe.validate_report({**value, "identity_kind": "source"}, self.pin, self.root)
        self.dll.unlink()
        with self.assertRaises(RuntimeError):
            probe.validate_report(value, self.pin, self.root)

    def test_changed_DLL_bytes_or_outside_loaded_path_cannot_pass(self):
        value = self.report()
        self.dll.write_bytes(b"changed neutral fixture")
        with self.assertRaises(RuntimeError):
            probe.validate_report(value, self.pin, self.root)
        outside = self.root.parent / (self.root.name + "-outside.dll")
        outside.write_bytes(b"neutral runtime identity fixture")
        self.addCleanup(outside.unlink)
        with self.assertRaises(RuntimeError):
            probe.validate_report({**value, "loaded_dll_path": str(outside)}, self.pin, self.root)

    def test_partial_checks_wrong_platform_version_source_and_kind_are_rejected(self):
        variants = [{"checks": {}}, {"checks": {**self.report()["checks"], "backup": 1}},
                    {"compile_options": []}, {"runtime_platform": "Darwin"},
                    {"pointer_bits": 32}, {"version": "3.50.4"},
                    {"source_id": "incorrect source"}, {"identity_kind": "interpreter"},
                    {"schema_version": "other"}, {"status": "incomplete"}]
        for variant in variants:
            with self.subTest(variant=variant), self.assertRaises(RuntimeError):
                probe.validate_report({**self.report(), **variant}, self.pin, self.root)
        with self.assertRaises(RuntimeError):
            probe.validate_report(self.report(), self.pin, self.root, identity_kind="compiled")

    def test_neutral_memory_features_complete_without_opening_any_business_file(self):
        self.assertEqual({name: True for name in probe.REQUIRED_CHECKS}, probe._memory_feature_checks())

    def test_run_probe_uses_shared_validator_for_collected_identity_and_features(self):
        identity = self.report()
        identity.pop("checks")
        with mock.patch.object(probe, "_is_windows", return_value=True), \
             mock.patch.object(probe, "_identity", return_value=identity), \
             mock.patch.object(probe, "_memory_feature_checks",
                               return_value={name: True for name in probe.REQUIRED_CHECKS}):
            result = probe.run_probe(self.pin, self.root)
            self.assertEqual("passed", result["status"])
            self.dll.write_bytes(b"controlled late component mismatch")
            with self.assertRaises(RuntimeError):
                probe.run_probe(self.pin, self.root)

    def test_unreviewed_pin_is_rejected_before_collecting_runtime_identity(self):
        with mock.patch.object(probe, "_identity") as identity:
            with self.assertRaises(RuntimeError):
                probe.run_probe({**self.pin, "version": "0.0.0"}, self.root)
        identity.assert_not_called()

    def test_CLI_reports_failed_JSON_and_nonzero_on_invalid_pin(self):
        pin = self.root / "unreviewed-pin.json"
        pin.write_text(json.dumps({**self.pin, "version": "0.0.0"}))
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = probe.main(["--pin-manifest", str(pin), "--allowed-root", str(self.root)])
        self.assertEqual(1, code)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("failed", json.loads(stderr.getvalue())["status"])


if __name__ == "__main__":
    unittest.main()
