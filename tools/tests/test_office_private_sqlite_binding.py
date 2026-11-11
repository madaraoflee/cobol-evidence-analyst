"""Exercise the office collector with neutral app and helper archive fixtures."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "poc/tests")]
import collect_office_acceptance_kit as collector
from desktop_acceptance import BINARY_MEMBER, BUILD_MEMBER
import test_office_kit_collection as fixtures


class OfficePrivateSQLiteBindingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.OfficeKitCollectionTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.dll = b"Neutral component fixture. This file is never loaded."
        self.pin = {**collector.APPROVED_PIN, "dll_sha256": hashlib.sha256(self.dll).hexdigest()}
        pinned = patch.object(collector, "APPROVED_PIN", self.pin)
        pinned.start()
        self.addCleanup(pinned.stop)
        self.app_dll = str(Path(BINARY_MEMBER).parent.as_posix()) + "/sqlite3.dll"
        self.helper_dll = "OfficeAcceptance/sqlite3.dll"

    def private_metadata(self, legacy, *, runner):
        result = deepcopy(legacy)
        directory = f"C:/neutral-{runner}/product"
        result["private_sqlite_runtime"] = {"schema_version": "private-sqlite-build/v1",
            "fingerprint": ("a" if runner == "app" else "b") * 64,
            "receipt_sha256": ("c" if runner == "app" else "d") * 64,
            "inputs": {"pin": deepcopy(self.pin), "sqlite_sha256": self.pin["dll_sha256"],
                "base_prefix": f"C:/neutral-{runner}/interpreter"}}
        result["self_test"]["sqlite_runtime"] = {"schema_version": collector.SQLITE_PROBE_SCHEMA,
            "status": "passed", "identity_kind": "compiled", "runtime_platform": "Windows", "pointer_bits": 64,
            "version": self.pin["version"], "source_id": self.pin["source_id"],
            "loaded_dll_sha256": self.pin["dll_sha256"], "loaded_dll_path": directory + "/sqlite3.dll",
            "allowed_root": directory, "extension_path": None, "extension_origin": "built_in",
            "checks": {key: True for key in collector.REQUIRED_CHECKS},
            "compile_options": sorted(collector.REQUIRED_OPTIONS)}
        return result

    def write_pair(self, *, app=None, helper=None, app_dlls=None, helper_dlls=None):
        if app is None:
            app = self.private_metadata(self.fixture.build_info, runner="app")
        if helper is None:
            helper = self.private_metadata(self.fixture.helper_metadata, runner="helper")
        if app_dlls is None:
            app_dlls = [(self.app_dll, self.dll)]
        if helper_dlls is None:
            helper_dlls = [(self.helper_dll, self.dll)]
        candidate = self.fixture.candidate / collector.CANDIDATE_FILES[0]
        with zipfile.ZipFile(candidate, "w") as archive:
            archive.writestr(BINARY_MEMBER, fixtures.pe_header())
            archive.writestr(BUILD_MEMBER, json.dumps(app))
            for name, content in app_dlls:
                archive.writestr(name, content)
        self.fixture.write_helper(extra=helper_dlls, metadata=helper)
        return candidate

    def reject(self, code):
        destination = self.fixture.root / "rejected"
        with self.assertRaisesRegex(ValueError, code):
            self.fixture.assemble(destination=destination)
        self.assertFalse(destination.exists())

    def test_matching_pinned_dll_bytes_and_independent_build_paths_assemble(self):
        candidate = self.write_pair()
        result = self.fixture.assemble()
        with zipfile.ZipFile(result) as archive:
            self.assertEqual(archive.read("OfficeAcceptanceKit/candidate/" + candidate.name), candidate.read_bytes())
            self.assertEqual(archive.read("OfficeAcceptanceKit/helper/" + self.helper_dll), self.dll)

    def test_legacy_pair_without_runtime_declarations_still_assembles(self):
        candidate = self.write_pair(app=self.fixture.build_info, helper=self.fixture.helper_metadata)
        result = self.fixture.assemble()
        with zipfile.ZipFile(result) as archive:
            self.assertEqual(archive.read("OfficeAcceptanceKit/candidate/" + candidate.name), candidate.read_bytes())

    def test_asymmetric_declarations_including_native_probe_only_are_rejected(self):
        app_probe_only = self.private_metadata(self.fixture.build_info, runner="app")
        app_probe_only.pop("private_sqlite_runtime")
        for app, helper in ((None, self.fixture.helper_metadata), (self.fixture.build_info, None),
                            (app_probe_only, self.fixture.helper_metadata)):
            with self.subTest(app=app is not None, helper=helper is not None):
                self.write_pair(app=app, helper=helper)
                self.reject("OFFICE_PRIVATE_SQLITE_PAIR_REQUIRED")

    def test_malformed_or_unapproved_runtime_declarations_are_rejected(self):
        for product in ("app", "helper"):
            legacy = self.fixture.build_info if product == "app" else self.fixture.helper_metadata
            for issue in ("null", "wrong_pin", "wrong_input_dll_hash"):
                metadata = self.private_metadata(legacy, runner=product)
                if issue == "null":
                    metadata["private_sqlite_runtime"] = None
                elif issue == "wrong_pin":
                    metadata["private_sqlite_runtime"]["inputs"]["pin"]["version"] = "0.0.0"
                else:
                    metadata["private_sqlite_runtime"]["inputs"]["sqlite_sha256"] = "0" * 64
                with self.subTest(product=product, issue=issue):
                    self.write_pair(**{product: metadata})
                    self.reject("OFFICE_PRIVATE_SQLITE_PIN_INVALID")

    def test_both_compiled_probes_must_match_approved_component_and_pass_checks(self):
        for product in ("app", "helper"):
            legacy = self.fixture.build_info if product == "app" else self.fixture.helper_metadata
            variants = {"version": "0.0.0", "source_id": "other source", "loaded_dll_sha256": "0" * 64,
                "identity_kind": "source", "pointer_bits": 32, "checks": {}, "compile_options": []}
            for field, wrong in variants.items():
                metadata = self.private_metadata(legacy, runner=product)
                metadata["self_test"]["sqlite_runtime"][field] = wrong
                with self.subTest(product=product, field=field):
                    self.write_pair(**{product: metadata})
                    self.reject("OFFICE_PRIVATE_SQLITE_PROBE_INVALID")

    def test_tampered_dll_bytes_in_either_product_are_rejected(self):
        for product, name in (("app", self.app_dll), ("helper", self.helper_dll)):
            with self.subTest(product=product):
                self.write_pair(**{product + "_dlls": [(name, b"Wrong component bytes")]})
                self.reject("OFFICE_" + product.upper() + "_SQLITE_DLL_INVALID")

    def test_missing_duplicate_or_outside_product_dll_is_rejected(self):
        for product, name, outside in (("app", self.app_dll, "COBOLWorkbench/sqlite3.dll"),
                                       ("helper", self.helper_dll, "other/sqlite3.dll")):
            for rows in ([], [(outside, self.dll)], [(name, self.dll), (outside, self.dll)]):
                with self.subTest(product=product, members=rows):
                    self.write_pair(**{product + "_dlls": rows})
                    self.reject("OFFICE_" + product.upper() + "_SQLITE_DLL_INVALID")


if __name__ == "__main__":
    unittest.main()
