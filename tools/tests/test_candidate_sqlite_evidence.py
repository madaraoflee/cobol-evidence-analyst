"""Check compiled runtime report paths against the exact delivered ZIP layout."""
from copy import deepcopy
import hashlib
from pathlib import Path, PureWindowsPath
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import verify_desktop_candidate_evidence as validator


class CandidateSQLiteEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.package = Path(temporary.name) / "candidate.zip"
        self.executable = "COBOLWorkbench/COBOLWorkbench/COBOLWorkbench.exe"
        self.dll_member = "COBOLWorkbench/COBOLWorkbench/sqlite3.dll"
        self.dll_bytes = b"synthetic DLL identity bytes, never executed"
        pin = {**validator.SQLITE_PIN, "dll_sha256": hashlib.sha256(self.dll_bytes).hexdigest()}
        pin_patch = patch.object(validator, "SQLITE_PIN", pin)
        pin_patch.start()
        self.addCleanup(pin_patch.stop)
        self.write_package()
        native_root = PureWindowsPath(r"D:\acceptance\extracted\COBOLWorkbench\COBOLWorkbench")
        build_root = PureWindowsPath(r"D:\build\desktop_app.dist")
        self.identity = {"source_commit": "a" * 40, "archive_sha256": "b" * 64,
                         "binary_path": self.executable, "binary_sha256": "c" * 64,
                         "application_sha256": "d" * 64}
        native, recorded = self.probe(native_root), self.probe(build_root)
        self.metadata = {"dependencies": ["Nuitka==4.2.2"], "self_test": {"sqlite_runtime": recorded}}
        self.report = {**self.identity, "schema_version": "packaged-sqlite-runtime-acceptance/v1",
                       "status": "passed", "errors": [], "execution_surface": "native_windows_exe_self_test",
                       "runtime_platform": "Windows", "source_dirty": False, "model_calls": 0,
                       "external_model_calls": 0, "paid_model_calls": 0, "pin": pin, "pin_sha256": "e" * 64,
                       "sqlite_runtime": native, "self_test": {"ok": True, "model_calls": 0, "sqlite_runtime": native},
                       "packaged_dll": {"path": self.dll_member, "sha256": pin["dll_sha256"]},
                       "build_self_test_sqlite_runtime": deepcopy(recorded)}

    def write_package(self, *, with_python=False):
        with zipfile.ZipFile(self.package, "w") as archive:
            archive.writestr(self.executable, b"unit fixture executable identity")
            archive.writestr(self.dll_member, self.dll_bytes)
            if with_python:
                archive.writestr("COBOLWorkbench/COBOLWorkbench/python.exe", b"unapproved interpreter fixture")

    @staticmethod
    def probe(root):
        pin = validator.SQLITE_PIN
        return {"schema_version": "sqlite-runtime-probe/v1", "status": "passed", "identity_kind": "compiled",
                "runtime_platform": "Windows", "pointer_bits": 64, "version": pin["version"],
                "source_id": pin["source_id"], "loaded_dll_sha256": pin["dll_sha256"],
                "allowed_root": str(root), "prefix": str(root), "base_prefix": str(root),
                "executable": str(root / "python.exe"), "loaded_dll_path": str(root / "sqlite3.dll"),
                "extension_path": str(root / "_sqlite3.pyd"), "extension_origin": "file",
                "checks": {name: True for name in validator.SQLITE_FEATURES},
                "compile_options": sorted(validator.SQLITE_OPTIONS)}

    def verify(self):
        return validator.sqlite_report_check(self.report, self.identity, self.package, self.metadata)

    def test_pinned_compiled_alias_requires_the_same_root_and_no_shipped_interpreter(self):
        result = self.verify()
        self.assertTrue(result["sys_executable_is_nuitka_standalone_alias"])
        self.assertEqual(result["packaged_dll"], self.dll_member)
        self.assertTrue(result["reported_sys_executable"].endswith(r"\python.exe"))

    def test_canonical_executable_path_continues_to_pass(self):
        probe = self.report["sqlite_runtime"]
        probe["executable"] = str(PureWindowsPath(probe["allowed_root"]) / "COBOLWorkbench.exe")
        self.assertFalse(self.verify()["sys_executable_is_nuitka_standalone_alias"])

    def test_unrelated_executable_and_external_root_are_rejected(self):
        original = self.report["sqlite_runtime"]["executable"]
        for different in (r"D:\elsewhere\python.exe", original.replace("python.exe", "python3.exe")):
            with self.subTest(executable=different):
                self.report["sqlite_runtime"]["executable"] = different
                with self.assertRaisesRegex(ValueError, "executable is outside"):
                    self.verify()

    def test_alias_cannot_accept_wrong_compiler_prefix_or_shipped_interpreter(self):
        self.metadata["dependencies"] = ["Nuitka==0.0.0"]
        with self.assertRaisesRegex(ValueError, "Unproven compiled standalone executable alias"):
            self.verify()
        self.metadata["dependencies"] = ["Nuitka==4.2.2"]
        probe = self.report["sqlite_runtime"]
        probe["prefix"] = r"D:\elsewhere"
        with self.assertRaisesRegex(ValueError, "Unproven compiled standalone executable alias"):
            self.verify()
        probe["prefix"] = probe["allowed_root"]
        self.write_package(with_python=True)
        with self.assertRaisesRegex(ValueError, "Unproven compiled standalone executable alias"):
            self.verify()

    def test_compatible_alias_cannot_accept_an_external_loaded_dll(self):
        self.report["sqlite_runtime"]["loaded_dll_path"] = r"D:\elsewhere\sqlite3.dll"
        with self.assertRaisesRegex(ValueError, "loaded DLL is outside"):
            self.verify()
