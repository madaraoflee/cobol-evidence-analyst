"""Offline draft checks: synthetic SDK/PE/ZIP and mocked Windows child only."""
from argparse import Namespace
import ast
import hashlib
import importlib.util
import io
import json
from pathlib import Path, PureWindowsPath
import stat
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import prepare_private_sqlite_runtime as draft


def fake_pe(names=None, source_id=None, machine=0x8664):
    """A non-executable synthetic PE export fixture, never launched."""
    names = sorted(draft.REQUIRED_EXPORTS if names is None else names)
    data = bytearray(0x2200)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HH", data, 0x84, machine, 1)
    struct.pack_into("<HH", data, 0x94, 0xF0, 0x2002)
    struct.pack_into("<H", data, 0x98, 0x20B)
    struct.pack_into("<I", data, 0x98 + 60, 0x200)
    struct.pack_into("<II", data, 0x98 + 112, 0x1000, 0x1000)
    struct.pack_into("<IIII", data, 0x188 + 8, 0x2000, 0x1000, 0x2000, 0x200)
    n = len(names)
    struct.pack_into("<IIIII", data, 0x200 + 20, n, n, 0x1040, 0x1080, 0x10C0)
    position = 0x300
    for index, name in enumerate(names):
        encoded = name.encode() + b"\0"
        struct.pack_into("<I", data, 0x240 + 4 * index, 0x1F00)
        struct.pack_into("<I", data, 0x280 + 4 * index, 0x1000 + position - 0x200)
        struct.pack_into("<H", data, 0x2C0 + 2 * index, index)
        data[position:position + len(encoded)] = encoded
        position += len(encoded)
    identity = (source_id or draft.APPROVED_PIN["source_id"]).encode()
    data[0x1800:0x1800 + len(identity)] = identity
    return bytes(data)


class PrivateRuntimeDraftTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="private-runtime-fixture-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "synthetic-base"
        self.source.mkdir()
        files = {
            "python.exe": fake_pe(), "python313.dll": fake_pe(),
            "DLLs/_sqlite3.pyd": fake_pe(), "DLLs/sqlite3.dll": b"old-neutral-fixture-core",
            "Lib/sqlite3/dbapi2.py": b"# neutral SDK fixture; no real interpreter\n",
        }
        for name, value in files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        (self.source / "neutral-empty-directory").mkdir()
        self.archive = self.root / "component.zip"
        self.pin_path = self.root / "pin.json"
        self.pin = self.bundle()

    def bundle(self, dll=None, extra=None, definition=None, linked=False):
        dll = fake_pe() if dll is None else dll
        definition = "EXPORTS\n" + "\n".join(sorted(draft.REQUIRED_EXPORTS)) if definition is None else definition
        with zipfile.ZipFile(self.archive, "w") as package:
            if linked:
                item = zipfile.ZipInfo("sqlite3.dll")
                item.create_system = 3
                item.external_attr = (stat.S_IFLNK | 0o777) << 16
                package.writestr(item, dll)
            else:
                package.writestr("sqlite3.dll", dll)
            package.writestr("sqlite3.def", definition)
            if extra:
                package.writestr(extra, b"unexpected")
        pin = {**draft.APPROVED_PIN, "archive_sha3_256": hashlib.sha3_256(self.archive.read_bytes()).hexdigest(),
               "dll_sha256": draft.digest(dll)}
        self.pin_path.write_text(json.dumps(pin))
        return pin

    def arguments(self, name):
        return Namespace(source_base=str(self.source), output=str(self.root / name),
                         pin_manifest=str(self.pin_path), archive=str(self.archive),
                         receipt=str(self.root / (name + ".receipt.json")))

    def good_probe(self, private, output, pin):
        return {"status": "passed", "version": pin["version"], "source_id": pin["source_id"],
                "schema_version": "sqlite-runtime-probe/v1", "identity_kind": "source",
                "runtime_platform": "Windows", "extension_origin": "file",
                "python_major_minor": [3, 13], "pointer_bits": 64,
                "loaded_dll_sha256": pin["dll_sha256"], "compile_options": sorted(draft.REQUIRED_OPTIONS),
                "checks": {name: True for name in draft.REQUIRED_CHECKS},
                "executable": str(private / "python.exe"), "prefix": str(private),
                "base_prefix": str(private), "extension_path": str(private / "DLLs/_sqlite3.pyd"),
                "loaded_dll_path": str(private / "DLLs/sqlite3.dll")}

    def invoke(self, args, probe=None, pin=None):
        with mock.patch.object(draft, "APPROVED_PIN", pin or self.pin), \
             mock.patch.object(draft.runtime_probe, "APPROVED_PIN", pin or self.pin), \
             mock.patch.object(draft, "is_windows", return_value=True), \
             mock.patch.object(draft, "run_probe", side_effect=probe or self.good_probe) as child:
            code = draft.prepare(args)
        return code, json.loads(Path(args.receipt).read_text()), child

    def test_success_copies_fixture_only_and_preserves_every_source_byte(self):
        before = draft.tree_manifest(self.source)
        args = self.arguments("fresh")
        code, receipt, child = self.invoke(args)
        self.assertEqual(0, code)
        self.assertEqual("completed_private_runtime", receipt["status"])
        self.assertIs(receipt["source_unchanged"], True)
        self.assertEqual(before, draft.tree_manifest(self.source))
        expected = {**before, "DLLs/sqlite3.dll": self.pin["dll_sha256"]}
        self.assertEqual(expected, draft.tree_manifest(Path(args.output) / "python"))
        self.assertFalse(Path(args.output).joinpath("python/pyvenv.cfg").exists())
        self.assertEqual(1, child.call_count)
        ast.parse(draft.PROBE)

    def test_existing_output_and_path_overlap_and_traversal_are_refused(self):
        for name, output in [
            ("existing", self.root / "old-output"),
            ("overlap", self.source / "copy"),
            ("same-source", self.source),
        ]:
            with self.subTest(name=name):
                args = self.arguments(name)
                args.output = str(output)
                if name == "existing":
                    output.mkdir()
                    (output / "keep").write_bytes(b"unchanged")
                code, receipt, child = self.invoke(args)
                self.assertEqual(2, code)
                self.assertEqual("failed", receipt["status"])
                self.assertFalse(child.called)
                if name == "existing":
                    self.assertEqual(b"unchanged", (output / "keep").read_bytes())

    def test_source_venv_and_nested_symlink_are_refused(self):
        (self.source / "pyvenv.cfg").write_text("home = neutral-fixture")
        code, receipt, child = self.invoke(self.arguments("venv"))
        self.assertEqual(2, code)
        self.assertFalse(child.called)
        (self.source / "pyvenv.cfg").unlink()
        (self.source / "DLLs/alias.dll").symlink_to(self.source / "DLLs/sqlite3.dll")
        code, receipt, child = self.invoke(self.arguments("linked-source"))
        self.assertEqual(2, code)
        self.assertFalse(child.called)

    def test_output_parent_symlink_and_simulated_windows_reparse_are_refused(self):
        real = self.root / "real-parent"
        real.mkdir()
        link = self.root / "linked-parent"
        link.symlink_to(real, target_is_directory=True)
        args = self.arguments("linked-parent-case")
        args.output = str(link / "copy")
        code, receipt, child = self.invoke(args)
        self.assertEqual(2, code)
        self.assertFalse(child.called)
        original = Path.lstat
        def reparse(path, *a, **kw):
            result = original(path, *a, **kw)
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400) if path == self.source / "DLLs" else result
        with mock.patch.object(Path, "lstat", reparse):
            code, receipt, child = self.invoke(self.arguments("reparse"))
        self.assertEqual(2, code)
        self.assertFalse(child.called)

    def test_unapproved_pin_and_archive_hash_mismatch_are_refused(self):
        self.pin_path.write_text(json.dumps({**self.pin, "version": "0.0.0"}))
        code, receipt, child = self.invoke(self.arguments("pin-tamper"))
        self.assertEqual(2, code)
        self.assertFalse(child.called)
        self.pin_path.write_text(json.dumps(self.pin))
        self.archive.write_bytes(self.archive.read_bytes() + b"changed")
        code, receipt, child = self.invoke(self.arguments("archive-tamper"))
        self.assertEqual(2, code)
        self.assertFalse(child.called)

    def test_archive_member_PE_identity_and_export_failures_are_refused(self):
        variants = [
            {"extra": "../outside"},
            {"linked": True},
            {"dll": fake_pe(machine=0x14C)},
            {"dll": fake_pe(source_id="wrong source identity")},
            {"dll": fake_pe(names=sorted(draft.REQUIRED_EXPORTS)[1:])},
            {"definition": "EXPORTS\nsqlite3_not_in_binary\n"},
        ]
        for number, options in enumerate(variants):
            with self.subTest(options=number):
                pin = self.bundle(**options)
                code, receipt, child = self.invoke(self.arguments("bad-archive-" + str(number)), pin=pin)
                self.assertEqual(2, code)
                self.assertEqual("failed", receipt["status"])
                self.assertFalse(child.called)

    def test_probe_mismatched_loaded_path_and_missing_check_never_pass(self):
        for number, field in enumerate(("loaded_dll_path", "checks")):
            def wrong(private, output, pin):
                value = self.good_probe(private, output, pin)
                value[field] = str(self.source / "DLLs/sqlite3.dll") if field == "loaded_dll_path" else {}
                return value
            code, receipt, child = self.invoke(self.arguments("bad-probe-" + str(number)), probe=wrong)
            self.assertEqual(1, code)
            self.assertEqual("failed", receipt["status"])
            self.assertIs(receipt["source_unchanged"], True)

    def test_probe_failure_and_interrupt_keep_receipt_and_nonzero_exit(self):
        for name, error, expected in [
            ("probe-failed", RuntimeError("controlled probe failure"), 1),
            ("probe-interrupted", KeyboardInterrupt(), 130),
        ]:
            def fail(*args):
                raise error
            code, receipt, child = self.invoke(self.arguments(name), probe=fail)
            self.assertEqual(expected, code)
            self.assertEqual(expected, receipt["exit_code"])
            self.assertEqual("interrupted" if expected == 130 else "failed", receipt["status"])
            self.assertIs(receipt["source_unchanged"], True)

    def test_last_source_change_blocks_success_after_good_probe(self):
        def change_fake_source(private, output, pin):
            value = self.good_probe(private, output, pin)
            (self.source / "DLLs/sqlite3.dll").write_bytes(b"controlled late fixture mutation")
            return value
        code, receipt, child = self.invoke(self.arguments("late-source-change"), probe=change_fake_source)
        self.assertEqual(1, code)
        self.assertEqual("SourceChanged", receipt["error_type"])
        self.assertEqual("failed", receipt["status"])
        self.assertIs(receipt["source_unchanged"], False)

    def test_last_source_guard_read_error_or_interrupt_never_report_success(self):
        original = draft.tree_manifest
        for name, error, expected in [("guard-failed", OSError("controlled read error"), 1),
                                       ("guard-interrupted", KeyboardInterrupt(), 130)]:
            source_calls = 0
            def guard(path):
                nonlocal source_calls
                if path == self.source:
                    source_calls += 1
                    if source_calls == 2:
                        raise error
                return original(path)
            with mock.patch.object(draft, "tree_manifest", side_effect=guard):
                code, receipt, child = self.invoke(self.arguments(name))
            self.assertEqual(expected, code)
            self.assertNotEqual("completed_private_runtime", receipt["status"])
            self.assertIs(receipt["source_unchanged"], False)

    def test_existing_or_unsafe_receipt_is_never_overwritten(self):
        args = self.arguments("receipt")
        Path(args.receipt).write_bytes(b"keep existing receipt")
        with self.assertRaises(draft.InputError):
            draft.prepare(args)
        self.assertEqual(b"keep existing receipt", Path(args.receipt).read_bytes())
        args.receipt = str(self.source / "inside.receipt.json")
        with self.assertRaises(draft.InputError):
            draft.prepare(args)
        self.assertFalse(Path(args.receipt).exists())

    def test_windows_device_UNC_ADS_and_trailing_aliases_are_refused_offline(self):
        with mock.patch.object(draft, "Path", PureWindowsPath):
            for value in (r"\\server\share\copy", r"\\?\C:\copy", r"C:\copy:stream",
                          "C:\\copy.\\child", "C:\\copy \\child", r"C:\parent\..\copy",
                          r"C:\temp\NUL", r"C:\temp\CON.txt", r"C:\temp\COM1"):
                with self.subTest(value=value), self.assertRaises(draft.InputError):
                    draft.absolute_path(value)
            self.assertEqual(PureWindowsPath(r"C:\neutral\copy"), draft.absolute_path(r"C:\neutral\copy"))

    def test_namespace_or_traversal_input_is_rejected_before_path_resolution(self):
        args = self.arguments("early-reject")
        args.output = str(self.root / "intermediate" / ".." / "new-output")
        with mock.patch.object(Path, "resolve", side_effect=AssertionError("must not resolve invalid path")):
            with self.assertRaises(draft.InputError):
                draft.prepare(args)
        self.assertFalse(Path(args.receipt).exists())

    def test_unreadable_tree_scan_cannot_be_silently_accepted(self):
        def denied(root, **options):
            options["onerror"](PermissionError("controlled SDK scan denial"))
            yield
        with mock.patch.object(draft.os, "walk", side_effect=denied):
            with self.assertRaises(PermissionError):
                draft.tree_manifest(self.source)

    def test_final_empty_directory_change_blocks_success(self):
        def add_empty_directory(private, output, pin):
            value = self.good_probe(private, output, pin)
            (self.source / "controlled-late-empty-directory").mkdir()
            return value
        code, receipt, child = self.invoke(self.arguments("late-directory"), probe=add_empty_directory)
        self.assertEqual(1, code)
        self.assertEqual("SourceChanged", receipt["error_type"])
        self.assertIs(receipt["source_unchanged"], False)

    def test_missing_required_export_fails_even_when_PE_and_DEF_match(self):
        remaining = sorted(draft.REQUIRED_EXPORTS)[1:]
        pin = self.bundle(dll=fake_pe(names=remaining), definition="EXPORTS\n" + "\n".join(remaining))
        code, receipt, child = self.invoke(self.arguments("missing-required-export"), pin=pin)
        self.assertEqual(2, code)
        self.assertEqual("failed", receipt["status"])
        self.assertFalse(child.called)


if __name__ == "__main__":
    unittest.main()
