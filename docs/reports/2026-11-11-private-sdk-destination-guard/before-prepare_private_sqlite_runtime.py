"""Prepare a fresh private Windows CPython base, never its host base.

Exit codes: 0 verified private copy; 1 copy/probe/source-guard failure;
2 invalid inputs; 130 interrupted. A valid fresh external receipt is required.
No download, registry/PATH change, venv, Nuitka or application build is performed.
"""
import argparse
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "poc"))
from sqlite_runtime_probe import APPROVED_PIN, REQUIRED_OPTIONS, REQUIRED_CHECKS
import sqlite_runtime_probe as runtime_probe

REQUIRED_EXPORTS = {
    "sqlite3_libversion", "sqlite3_sourceid", "sqlite3_db_config",
    "sqlite3_backup_init", "sqlite3_backup_step", "sqlite3_backup_finish",
    "sqlite3_serialize", "sqlite3_deserialize",
}


class InputError(ValueError):
    def __init__(self, message, *, component_path=None, checked_path=None, info=None):
        super().__init__(message)
        self.path_guard = None if component_path is None else {
            "component_path": str(component_path), "checked_path": str(checked_path),
            "file_attributes": getattr(info, "st_file_attributes", None),
            "reparse_tag": getattr(info, "st_reparse_tag", None),
        }


def is_windows():
    return os.name == "nt"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def absolute_path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise InputError("Paths must be absolute and must not contain '..'")
    # This draft accepts ordinary local drive paths, not Windows device/UNC/ADS aliases.
    if path.drive and (len(path.drive) != 2 or path.drive[1] != ":"):
        raise InputError("Windows UNC/device path aliases are refused")
    if path.drive and any(":" in part or part.endswith((".", " ")) for part in path.parts[1:]):
        raise InputError("Windows alternate streams and trailing-dot/space aliases are refused")
    if path.drive and ntpath.isreserved(str(path)):
        raise InputError("Windows reserved device names/characters are refused")
    return path


def no_links(path):
    """Check existing path components before any resolving/copying."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise InputError("Symbolic links and Windows reparse points are refused",
                             component_path=current, checked_path=path, info=info)


def inside(path, root):
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _file_identity(info):
    return {name: getattr(info, name, None) for name in (
        "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns",
        "st_file_attributes", "st_reparse_tag")}


def launcher_target(root, original):
    """Parse the installer's exact interpreter target without dereferencing it."""
    interpreter, spelling = root / "python.exe", original
    if spelling.startswith("\\\\?\\"):
        spelling = spelling[4:]
        if not ntpath.isabs(spelling) or not re.fullmatch(r"[A-Za-z]:", ntpath.splitdrive(spelling)[0]):
            raise InputError("Interpreter alias target is not an ordinary SDK drive path")
    target = Path(spelling)
    if not target.is_absolute():
        if spelling != "python.exe":
            raise InputError("Interpreter alias target is not the SDK interpreter")
        target = interpreter
    absolute_path(target)
    if os.path.normcase(str(target)) != os.path.normcase(str(interpreter)):
        raise InputError("Interpreter alias target is not the SDK interpreter")
    return target


def python3_alias(root):
    """Recognize only the selected SDK's top-level interpreter symlink.

    Read the link as metadata, never read or copy through its target. Even an
    equivalent hard link outside this SDK is not an accepted target spelling.
    """
    alias, interpreter = root / "python3.exe", root / "python.exe"
    info = alias.lstat()
    def invalid():
        return InputError("Only the verified top-level python3.exe interpreter alias is permitted",
                          component_path=alias, checked_path=root, info=info)
    if (not stat.S_ISLNK(info.st_mode)
            or not getattr(info, "st_file_attributes", 0) & 0x400
            or getattr(info, "st_reparse_tag", None) != 0xA000000C):
        raise invalid()
    original = str(alias.readlink())
    # Windows readlink preserves this ordinary drive-path prefix in the
    # official interpreter installer. UNC/device targets remain invalid.
    try:
        launcher_target(root, original)
    except InputError:
        raise invalid() from None
    no_links(interpreter)
    target_info = interpreter.lstat()
    if not stat.S_ISREG(target_info.st_mode) or not os.path.samefile(alias, interpreter):
        raise invalid()
    target_hash = digest(interpreter.read_bytes())
    if (_file_identity(alias.lstat()) != _file_identity(info)
            or str(alias.readlink()) != original
            or _file_identity(interpreter.lstat()) != _file_identity(target_info)):
        raise invalid()
    return {"kind": "verified_python3_launcher_alias", "alias_identity": _file_identity(info),
            "link_target": original, "target_relative_path": "python.exe",
            "target_identity": _file_identity(target_info), "target_sha256": target_hash}


def tree_manifest(root, *, materialize_python3_alias=False):
    no_links(root)
    result = {}
    def fail_scan(error):
        raise error
    for directory, folders, names in os.walk(root, followlinks=False, onerror=fail_scan):
        for name in folders + names:
            path = Path(directory) / name
            info = path.lstat()
            if (materialize_python3_alias and path == root / "python3.exe"
                    and (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400)):
                result["python3.exe"] = python3_alias(root)
                continue
            no_links(path)
            if path.is_dir():
                result[path.relative_to(root).as_posix() + "/"] = "directory"
                continue
            if not stat.S_ISREG(info.st_mode):
                raise InputError("Only ordinary SDK files/directories are allowed")
            result[path.relative_to(root).as_posix()] = digest(path.read_bytes())
    alias = result.get("python3.exe")
    if isinstance(alias, dict) and alias["target_sha256"] != result.get("python.exe"):
        raise InputError("Interpreter alias target changed while reading the source manifest")
    return result


def source_manifest(root, materialize):
    # Preserve the original call contract and strict behavior by default.
    return tree_manifest(root, materialize_python3_alias=True) if materialize else tree_manifest(root)


def copy_private_sdk(source, private, before):
    alias = before.get("python3.exe")
    if not isinstance(alias, dict):
        shutil.copytree(source, private, symlinks=False)
        return before
    if python3_alias(source) != alias:
        raise InputError("Interpreter alias changed before private copying")
    def omit_launcher(directory, names):
        return ["python3.exe"] if Path(directory) == source and "python3.exe" in names else []
    # Never dereference the allowed alias. Unexpected links remain links and
    # fail the unchanged private manifest guard, rather than copying a target.
    shutil.copytree(source, private, symlinks=True, ignore=omit_launcher)
    no_links(private / "python.exe")
    if digest((private / "python.exe").read_bytes()) != alias["target_sha256"]:
        raise RuntimeError("Private interpreter differs before launcher materialization")
    shutil.copy2(private / "python.exe", private / "python3.exe", follow_symlinks=False)
    return {**before, "python3.exe": alias["target_sha256"]}


def write_receipt(path, value):
    no_links(path)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def pe_info(data, exports=False):
    """Read only PE32+ x64 headers and, when requested, named exports."""
    def unpack(fmt, offset):
        size = struct.calcsize(fmt)
        if offset < 0 or offset + size > len(data):
            raise InputError("Truncated PE")
        return struct.unpack_from(fmt, data, offset)
    if data[:2] != b"MZ":
        raise InputError("Missing PE DOS header")
    offset = unpack("<I", 0x3C)[0]
    if data[offset:offset + 4] != b"PE\0\0":
        raise InputError("Missing PE signature")
    machine, sections = unpack("<HH", offset + 4)
    optional_size = unpack("<H", offset + 20)[0]
    optional = offset + 24
    if machine != 0x8664 or not 0 < sections <= 96 or optional_size < 120:
        raise InputError("Windows x64 PE32+ is required")
    if unpack("<H", optional)[0] != 0x20B:
        raise InputError("Windows x64 PE32+ is required")
    if not exports:
        return {"machine": "0x8664"}
    if not unpack("<H", offset + 22)[0] & 0x2000:
        raise InputError("SQLite must be a DLL")
    section_offset = optional + optional_size
    ranges = []
    for index in range(sections):
        position = section_offset + 40 * index
        virtual_size, address, raw_size, raw_offset = unpack("<IIII", position + 8)
        ranges.append((address, virtual_size, raw_offset, raw_size))
    def rva(address, length):
        for start, virtual_size, raw_offset, raw_size in ranges:
            delta = address - start
            if 0 <= delta and delta + length <= raw_size and delta < max(virtual_size, raw_size):
                position = raw_offset + delta
                if position + length <= len(data):
                    return position
        raise InputError("Invalid or unbacked PE export RVA")
    export_rva, export_size = unpack("<II", optional + 112)
    table = rva(export_rva, 40)
    functions, names, function_rva, names_rva, ordinals_rva = unpack("<IIIII", table + 20)
    if not 0 < names <= functions <= 10000 or export_size < 40:
        raise InputError("Invalid PE export table")
    name_table = rva(names_rva, names * 4)
    ordinal_table = rva(ordinals_rva, names * 2)
    function_table = rva(function_rva, functions * 4)
    found = set()
    for index in range(names):
        position = rva(unpack("<I", name_table + index * 4)[0], 1)
        end = data.find(b"\0", position, min(position + 256, len(data)))
        if end < 0:
            raise InputError("Unterminated PE export")
        try:
            name = data[position:end].decode("ascii")
        except UnicodeError as error:
            raise InputError("Invalid export name") from error
        ordinal = unpack("<H", ordinal_table + index * 2)[0]
        if ordinal >= functions or not unpack("<I", function_table + ordinal * 4)[0] or name in found:
            raise InputError("Invalid PE export ordinal or duplicate")
        found.add(name)
    return {"machine": "0x8664", "exports": sorted(found)}


def verified_dll(archive, manifest):
    pin = json.loads(manifest.read_text(encoding="utf-8"))
    if pin != APPROVED_PIN:
        raise InputError("Pin manifest differs from the reviewed official component")
    raw = archive.read_bytes()
    if hashlib.sha3_256(raw).hexdigest() != pin["archive_sha3_256"]:
        raise InputError("Official archive SHA3 mismatch")
    with zipfile.ZipFile(archive) as package:
        members = package.infolist()
        if len(members) != 2 or sorted(item.filename for item in members) != ["sqlite3.def", "sqlite3.dll"]:
            raise InputError("Archive must contain exactly the two official root members")
        for item in members:
            mode = (item.external_attr >> 16) & 0o170000
            if item.is_dir() or mode not in (0, stat.S_IFREG) or item.file_size > 16 * 1024 * 1024:
                raise InputError("Archive links/directories/oversized members are refused")
        dll, definitions = package.read("sqlite3.dll"), package.read("sqlite3.def").decode("ascii")
    if digest(dll) != pin["dll_sha256"] or pin["source_id"].encode("ascii") not in dll:
        raise InputError("DLL digest/source identity mismatch")
    exported = set(pe_info(dll, exports=True)["exports"])
    declared = {line.split()[0] for line in definitions.splitlines() if line.strip().startswith("sqlite3")}
    if exported != declared or not REQUIRED_EXPORTS.issubset(exported):
        raise InputError("DLL named exports do not match the official definition/required API")
    return dll, pin


PROBE = r'''
import json, os, pathlib, runpy, struct, sys
root = pathlib.Path(sys.argv[1]).resolve()
expected = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
def same(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
assert sys.version_info[:2] == (3, 13) and struct.calcsize("P") == 8
assert same(sys.executable, root / "python.exe")
assert same(sys.prefix, root) and same(sys.base_prefix, root)
probe = runpy.run_path(sys.argv[3])
result = probe["run_probe"](expected, root)
assert same(result["extension_path"], root / "DLLs" / "_sqlite3.pyd")
print(json.dumps(result))
'''


def run_probe(private, output, pin):
    if os.name != "nt":
        raise RuntimeError("The real private interpreter probe requires Windows")
    script, expected = output / "probe.py", output / "probe-pin.json"
    script.write_text(PROBE, encoding="utf-8")
    expected.write_text(json.dumps(pin), encoding="utf-8")
    module = output / "sqlite_runtime_probe.py"
    module.write_bytes((ROOT / "poc/sqlite_runtime_probe.py").read_bytes())
    process = subprocess.run([str(private / "python.exe"), "-I", "-S", "-B", str(script),
                              str(private), str(expected), str(module)], cwd=output, capture_output=True,
                             encoding="utf-8", timeout=120)
    evidence = {"exit_code": process.returncode, "stdout": process.stdout[-16384:], "stderr": process.stderr[-16384:]}
    (output / "probe-process.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    if process.returncode != 0:
        raise RuntimeError("Private interpreter compatibility probe failed")
    return json.loads(process.stdout)


def validate_probe(value, private, pin):
    runtime_probe.validate_report(value, pin, private, identity_kind="source")
    required_paths = {"executable": private / "python.exe", "prefix": private, "base_prefix": private,
                      "extension_path": private / "DLLs/_sqlite3.pyd",
                      "loaded_dll_path": private / "DLLs/sqlite3.dll"}
    if value.get("python_major_minor") != [3, 13]:
        raise RuntimeError("Incomplete or mismatched private interpreter evidence")
    for name, expected in required_paths.items():
        if os.path.normcase(str(Path(value.get(name, "")).resolve())) != os.path.normcase(str(expected.resolve())):
            raise RuntimeError("Probe loaded outside the private interpreter")


def prepare(args):
    # Reject namespace/traversal aliases before resolve() can inspect them.
    source, output = absolute_path(args.source_base), absolute_path(args.output)
    archive, manifest = absolute_path(args.archive), absolute_path(args.pin_manifest)
    receipt = absolute_path(args.receipt)
    no_links(receipt)
    if (not receipt.parent.is_dir() or receipt.exists() or receipt.is_symlink()
            or inside(receipt, source) or inside(receipt, output)
            or receipt in (Path(args.pin_manifest), Path(args.archive))):
        raise InputError("Receipt must be a fresh external file outside source/output/inputs")
    # Exclusive reservation prevents clobbering an existing receipt.
    with receipt.open("x", encoding="utf-8"):
        pass
    record = {"status": "incomplete", "exit_code": 1, "source_base": str(source),
              "output": str(output), "receipt": str(receipt), "stages": [],
              "archive": str(args.archive), "pin_manifest": str(args.pin_manifest),
              "source_unchanged": None, "application_built": False}
    write_receipt(receipt, record)
    before = None
    materialize = getattr(args, "materialize_python3_alias", False) is True
    code = 1
    try:
        source, output = absolute_path(source), absolute_path(output)
        for path in (source, output, archive, manifest):
            no_links(path)
        if (not source.is_dir() or not output.parent.is_dir() or output.exists()
                or inside(output, source) or inside(source, output)
                or any(inside(path, source) or inside(path, output) for path in (archive, manifest))
                or not archive.is_file() or not manifest.is_file()
                or (source / "pyvenv.cfg").exists()):
            raise InputError("Use a real base SDK, fresh disjoint output and external regular inputs")
        before = source_manifest(source, materialize)
        required = {"python.exe", "python313.dll", "DLLs/_sqlite3.pyd", "DLLs/sqlite3.dll", "Lib/sqlite3/dbapi2.py"}
        if not required.issubset(before):
            raise InputError("Expected ordinary Windows CPython 3.13 base layout is missing")
        for name in ("python.exe", "python313.dll", "DLLs/_sqlite3.pyd"):
            pe_info((source / name).read_bytes())
        record["source_before"] = before
        record["source_manifest_sha256"] = digest(json.dumps(before, sort_keys=True).encode())
        dll, pin = verified_dll(archive, manifest)
        record["pin"] = pin
        record["stages"].append("archive_and_base_verified")
        if not is_windows():
            raise InputError("Private SDK preparation/probe must run on Windows")
        output.mkdir(mode=0o700)
        private = output / "python"
        logical_before = copy_private_sdk(source, private, before)
        if tree_manifest(private) != logical_before:
            raise RuntimeError("Private SDK differs before component replacement")
        target = private / "DLLs/sqlite3.dll"
        if os.path.samefile(target, source / "DLLs/sqlite3.dll"):
            raise RuntimeError("Private component must not alias the host file")
        target.write_bytes(dll)
        expected = {**logical_before, "DLLs/sqlite3.dll": pin["dll_sha256"]}
        if tree_manifest(private) != expected:
            raise RuntimeError("Unexpected private SDK file changes")
        record["stages"].append("private_copy_prepared")
        record["private_python"] = str(private / "python.exe")
        probe = run_probe(private, output, pin)
        record["probe"] = probe
        validate_probe(probe, private, pin)
        if tree_manifest(private) != expected:
            raise RuntimeError("Probe changed private SDK files")
        record["stages"].append("private_interpreter_probe_verified")
        record["status"], code = "completed_private_runtime", 0
    except KeyboardInterrupt:
        record["status"], code = "interrupted", 130
    except Exception as error:
        record.update(status="failed", error_type=type(error).__name__, error=str(error))
        if getattr(error, "path_guard", None) is not None:
            record["path_guard"] = error.path_guard
        code = 2 if isinstance(error, InputError) else 1
    finally:
        if before is not None:
            try:
                after = source_manifest(source, materialize)
                record["source_after"] = after
                record["source_unchanged"] = after == before
            except Exception as error:
                record["source_unchanged"] = False
                record["source_guard_error"] = str(error)
                if getattr(error, "path_guard", None) is not None:
                    record["source_guard_path"] = error.path_guard
            except KeyboardInterrupt:
                record["source_unchanged"] = False
                record["status"], code = "interrupted", 130
            if record["source_unchanged"] is not True and code == 0:
                record.update(status="failed", error_type="SourceChanged",
                              error="Host SDK changed during preparation")
                code = 1
        record["exit_code"] = code
        write_receipt(receipt, record)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source-base", "output", "pin-manifest", "archive", "receipt"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--materialize-python3-alias", action="store_true")
    args = parser.parse_args(argv)
    try:
        return prepare(args)
    except InputError as error:
        value = {"status": "invalid_input", "exit_code": 2, "error": str(error)}
        if error.path_guard is not None:
            value["path_guard"] = error.path_guard
        print(json.dumps(value), file=sys.stderr)
        return 2
    except Exception as error:
        print(json.dumps({"status": "failed", "exit_code": 1, "error": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
