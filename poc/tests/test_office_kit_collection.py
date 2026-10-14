"""Packaging contract fixtures only: no fixture EXE is executed or native-tested."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import stat
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import collect_office_acceptance_kit as collector
import office_acceptance_app as assistant


def pe_header(*, machine=0x8664, payload=b"unit packaging fixture"):
    """A complete header allows architecture checks, not an executable program."""
    binary = bytearray(256)
    binary[:2] = b"MZ"
    struct.pack_into("<I", binary, 0x3C, 128)
    binary[128:132] = b"PE\0\0"
    struct.pack_into("<H", binary, 132, machine)
    return bytes(binary) + payload


class OfficeKitCollectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.candidate = self.root / "candidate"
        self.candidate.mkdir()
        self.source_commit, self.helper_commit = "a" * 40, "d" * 40
        self.build_info = {"target": "windows-x64", "source_commit": self.source_commit, "source_dirty": False,
            "source_fingerprint": hashlib.sha256(b"unit application source fingerprint").hexdigest(),
            "self_test": {"ok": True, "json_accelerated": True, "desktop_dependencies": True, "model_calls": 0}}
        self.write_candidate(self.candidate / collector.CANDIDATE_FILES[0])
        for name in collector.CANDIDATE_FILES[1:]:
            (self.candidate / name).write_text(json.dumps(self.build_info if name == "BUILD-INFO.json" else {}), encoding="utf-8")
        self.original_archive = (self.candidate / collector.CANDIDATE_FILES[0]).read_bytes()
        self.original_artifact = collector.inspect_package(self.candidate / collector.CANDIDATE_FILES[0], self.source_commit)
        self.helper_binary = pe_header(payload=b"unit helper header only, never executed")
        self.helper_metadata = {"schema_version": "office-helper-build/v1", "source_commit": self.helper_commit,
            "source_dirty": False, "helper_source_sha256": hashlib.sha256(b"unit helper source fingerprint").hexdigest(),
            "target": "windows-x64", "workbench_rebuilt": False,
            "self_test": {"ok": True, "gui_started": True, "native_platform": "Windows", "model_calls": 0}}
        self.helper_archive = self.root / "helper.zip"
        self.write_helper()
        self.public_source = self.root / "public"
        self.public_source.mkdir()
        accepted = patch.object(collector, "evaluate_release", return_value={"candidate_allowed": True})
        self.accepted = accepted.start()
        self.addCleanup(accepted.stop)
        generated = patch.object(collector, "generate_suite", side_effect=self.generate_small_suite)
        self.generated = generated.start()
        self.addCleanup(generated.stop)

    def write_candidate(self, path, *, payload=b"unit application header only, never executed"):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("COBOLWorkbench/COBOLWorkbench/COBOLWorkbench.exe", pe_header(payload=payload))
            archive.writestr("COBOLWorkbench/BUILD-INFO.json", json.dumps(self.build_info))

    def write_helper(self, *, extra=(), binary=None, metadata=None):
        with zipfile.ZipFile(self.helper_archive, "w") as archive:
            archive.writestr("OfficeAcceptance/OfficeAcceptance.exe", binary if binary is not None else self.helper_binary)
            archive.writestr("OfficeAcceptance/HELPER-BUILD-INFO.json", json.dumps(metadata or self.helper_metadata))
            archive.writestr("OfficeAcceptance/runtime.dll", b"unit DLL fixture, never loaded")
            for name, value in extra:
                if isinstance(name, str) and "\\" in name:
                    # ZipInfo normalizes native separators on Windows. Write
                    # the deliberately malformed header bytes on every host.
                    malformed = zipfile.ZipInfo(name)
                    malformed.filename = malformed.orig_filename = name
                    name = malformed
                archive.writestr(name, value)

    def generate_small_suite(self, destination, public_source, *, include_scale):
        """Use genuine source-bound case schema without generating 12,041 files."""
        destination = Path(destination)
        source = destination / "sources"
        source.mkdir(parents=True)
        path = source / "entry.cbl"
        path.write_bytes(b"COMPUTE RESULT = INPUT * 2.\n")
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        common = {"coverage": list(assistant.OFFICE_REGRESSION_BASELINE),
            "gold": {"required_findings": [{"id": "formula", "description": "RESULT uses INPUT times two.",
                "supporting_ranges": [{"path": "entry.cbl", "source_sha256": sha, "start_line": 1, "end_line": 1}]}],
                "forbidden_claims": [{"id": "invented", "description": "No times-999 formula."}]},
            "reviewed_context": {"source_ranges": [{"path": "entry.cbl", "sha256": sha, "start_line": 1, "end_line": 1}]}}
        items = [{**copy.deepcopy(common), "id": "regression", "split": "regression",
            "turns": [{"id": str(index), "question": f"Frozen question {index}?"} for index in range(1, 4)]},
            {**copy.deepcopy(common), "id": "reserved", "split": "holdout", "question": "How is RESULT computed?"}]
        collector.write_json(destination / "LIVE-CASES.json", {"schema_version": "live-business-cases/v1", "cases": items})
        collector.write_json(destination / "SOURCE-LOCK.json", {"schema_version": "office-source-lock/v1", "files": [
            {"path": "entry.cbl", "sha256": sha, "bytes": path.stat().st_size}]})
        collector.write_json(destination / "WINDOWS-MEASUREMENT-PLAN.json", {"status": "planned_not_executed", "result": None})
        (destination / "PUBLIC-SOURCE-LICENSE.txt").write_text("Unit packaging license fixture.", encoding="utf-8")
        return {"source": source, "source_files": 1}

    def assemble(self, destination=None, **options):
        return collector.assemble(candidate=self.candidate, helper_archive=self.helper_archive,
            public_source=self.public_source, destination=destination or self.root / "delivery",
            source_commit=self.source_commit, helper_commit=self.helper_commit, **options)

    def test_assembly_roundtrip_preserves_exact_application_and_is_no_model(self):
        import desktop_live_acceptance as live
        with patch.object(live, "NativeWorkbench") as workbench, patch.object(live, "evaluate_native") as model:
            archive = self.assemble()
            extracted = self.root / "checked"
            with zipfile.ZipFile(archive) as handle:
                handle.extractall(extracted)
            kit = assistant.load_kit(extracted / "OfficeAcceptanceKit/OFFICE-KIT.json")
        model.assert_not_called()
        workbench.assert_not_called()
        self.assertEqual(kit["artifact"], self.original_artifact)
        self.assertEqual(kit["inputs"]["archive"].read_bytes(), self.original_archive)
        self.assertEqual(kit["request_budget"], 12)
        self.assertEqual(kit["turns"], 4)
        self.assertFalse(kit["document"]["publication_performed"])
        self.assertFalse(kit["document"]["company_office_acceptance_completed"])
        self.assertEqual(kit["document"]["model_calls_during_assembly"], 0)
        self.assertTrue(any(path.endswith("runtime.dll") for path in kit["document"]["files"]))
        receipt = json.loads((archive.parent / "KIT-BUILD-RECEIPT.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["artifact"], self.original_artifact)
        self.assertEqual(receipt["model_calls"], 0)

    def test_unaccepted_candidate_fails_before_generation(self):
        self.accepted.return_value = {"candidate_allowed": False}
        with self.assertRaisesRegex(ValueError, "UNACCEPTED_CANDIDATE"):
            self.assemble()
        self.generated.assert_not_called()
        self.assertFalse((self.root / "delivery").exists())

    def test_helper_zip_rejects_paths_and_symbolic_links(self):
        symbolic = zipfile.ZipInfo("OfficeAcceptance/link")
        symbolic.create_system = 3
        symbolic.external_attr = (stat.S_IFLNK | 0o777) << 16
        for index, name in enumerate(("../escape.dll", "/absolute.dll", "C:/drive.dll", "OfficeAcceptance\\bad.dll", symbolic)):
            with self.subTest(name=name):
                self.write_helper(extra=[(name, b"../escape")])
                with self.assertRaisesRegex(ValueError, "ARCHIVE_UNSAFE"):
                    self.assemble(destination=self.root / f"unsafe-{index}")
                self.assertFalse((self.root / f"unsafe-{index}").exists())

    def test_helper_zip_rejects_windows_case_collision(self):
        self.write_helper(extra=[("officeacceptance/OFFICEACCEPTANCE.EXE", self.helper_binary)])
        with self.assertRaisesRegex(ValueError, "ARCHIVE_DUPLICATES"):
            self.assemble()

    def test_helper_must_have_real_x64_pe_header_not_just_mz(self):
        for index, binary in enumerate((b"MZ", b"MZ" + bytes(200), pe_header(machine=0x14C), b"XX" + self.helper_binary[2:])):
            with self.subTest(index=index):
                self.write_helper(binary=binary)
                with self.assertRaisesRegex(ValueError, "EXE_NOT_X64"):
                    self.assemble(destination=self.root / f"pe-{index}")

    def test_helper_metadata_cannot_claim_another_commit_or_unknown_source(self):
        for index, changes in enumerate(({"source_commit": "e" * 40}, {"schema_version": "unbound/v0"},
                                         {"helper_source_sha256": "missing"}, {"source_dirty": True},
                                         {"workbench_rebuilt": True}, {"target": "windows-x86"})):
            with self.subTest(changes=changes):
                self.write_helper(metadata={**self.helper_metadata, **changes})
                with self.assertRaisesRegex(ValueError, "NOT_NATIVE_TESTED"):
                    self.assemble(destination=self.root / f"metadata-{index}")

    def test_helper_self_test_cannot_be_other_platform_skipped_gui_or_charged(self):
        for index, changes in enumerate(({"native_platform": "macOS"}, {"gui_started": False},
                                         {"ok": False}, {"model_calls": 1})):
            with self.subTest(changes=changes):
                self.write_helper(metadata={**self.helper_metadata, "self_test": {**self.helper_metadata["self_test"], **changes}})
                with self.assertRaisesRegex(ValueError, "NOT_NATIVE_TESTED"):
                    self.assemble(destination=self.root / f"self-test-{index}")

    def test_application_copy_changed_after_acceptance_cannot_become_deliverable(self):
        real_copy = collector.shutil.copyfile
        def tampered_copy(source, destination, *args, **kwargs):
            result = real_copy(source, destination, *args, **kwargs)
            if Path(source) == self.candidate / collector.CANDIDATE_FILES[0]:
                self.write_candidate(Path(destination), payload=b"different unit application bytes")
            return result
        with patch.object(collector.shutil, "copyfile", side_effect=tampered_copy), \
             self.assertRaisesRegex(ValueError, "ASSEMBLED_ARTIFACT_CHANGED"):
            self.assemble()
        self.assertFalse((self.root / "delivery").exists())

    def test_existing_destination_is_never_overwritten(self):
        destination = self.root / "existing"
        destination.mkdir()
        protected = destination / "existing.txt"
        protected.write_text("keep archived evidence", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "DESTINATION_EXISTS"):
            self.assemble(destination=destination)
        self.assertEqual(protected.read_text(encoding="utf-8"), "keep archived evidence")
        self.generated.assert_not_called()


if __name__ == "__main__":
    unittest.main()
