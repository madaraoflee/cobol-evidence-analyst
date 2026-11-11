"""Bind an explicitly prepared private SQLite runtime to build products."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
PIN_ENV = "WORKBENCH_SQLITE_RUNTIME_PIN"
RECEIPT_ENV = "WORKBENCH_PRIVATE_SQLITE_RECEIPT"


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _probe_module():
    sys.path.insert(0, str(ROOT / "poc"))
    import sqlite_runtime_probe
    return sqlite_runtime_probe


def _same(left, right):
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def current_identity(*, required=False):
    """An opt-in build contract; ordinary source runs retain their behavior."""
    pin_name, receipt_name = os.environ.get(PIN_ENV), os.environ.get(RECEIPT_ENV)
    if not pin_name and not receipt_name and not required:
        return None
    if sys.platform != "win32" or not pin_name or not receipt_name:
        raise ValueError("PRIVATE_SQLITE_RUNTIME_REQUIRED")
    pin_path, receipt_path = Path(pin_name), Path(receipt_name)
    if any(not path.is_absolute() or path.is_symlink() or not path.is_file()
           for path in (pin_path, receipt_path)):
        raise ValueError("PRIVATE_SQLITE_INPUT_INVALID")
    pin = json.loads(pin_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if (receipt.get("status") != "completed_private_runtime" or receipt.get("exit_code") != 0
            or receipt.get("source_unchanged") is not True or receipt.get("pin") != pin
            or not isinstance(receipt.get("source_before"), dict)
            or receipt["source_before"] != receipt.get("source_after")):
        raise ValueError("PRIVATE_SQLITE_RECEIPT_INVALID")
    base = Path(receipt["output"]) / "python"
    if not base.is_absolute() or base.is_symlink() or not _same(sys.base_prefix, base):
        raise ValueError("PRIVATE_SQLITE_BASE_MISMATCH")
    probe_module = _probe_module()
    for relative in ("python.exe", "python313.dll", "DLLs/_sqlite3.pyd", "Lib/sqlite3/dbapi2.py"):
        if _digest(base / relative) != receipt["source_before"].get(relative):
            raise ValueError("PRIVATE_SQLITE_BASE_FILE_CHANGED")
    probe = probe_module.run_probe(pin, base)
    probe_module.validate_report(probe, pin, base, identity_kind="source")
    for field, expected in (("loaded_dll_path", base / "DLLs/sqlite3.dll"),
                            ("extension_path", base / "DLLs/_sqlite3.pyd")):
        if not probe.get(field) or not _same(probe[field], expected):
            raise ValueError("PRIVATE_SQLITE_LOADED_PATH_MISMATCH")
    inputs = {"base_prefix": str(base.resolve()), "pin": pin,
              "python_sha256": _digest(base / "python.exe"),
              "python_dll_sha256": _digest(base / "python313.dll"),
              "extension_sha256": _digest(base / "DLLs/_sqlite3.pyd"),
              "sqlite_sha256": _digest(base / "DLLs/sqlite3.dll"),
              "requirements_sha256": _digest(ROOT / "desktop-requirements.txt")}
    fingerprint = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    return {"schema_version": "private-sqlite-build/v1", "fingerprint": fingerprint,
            "inputs": inputs, "pin": pin, "pin_path": str(pin_path.resolve()),
            "receipt_sha256": _digest(receipt_path), "probe": probe}


def validate_product(result, product, identity):
    """Check the actual process evidence and the DLL that will enter the ZIP."""
    product = Path(product).resolve()
    value = result.get("sqlite_runtime") if isinstance(result, dict) else None
    _probe_module().validate_report(value, identity["pin"], product, identity_kind="compiled")
    candidates = [path for path in product.rglob("*") if path.name.casefold() == "sqlite3.dll"]
    if (len(candidates) != 1 or candidates[0].is_symlink() or not candidates[0].is_file()
            or _digest(candidates[0]) != identity["pin"]["dll_sha256"]
            or not _same(value["loaded_dll_path"], candidates[0])):
        raise ValueError("PACKAGED_SQLITE_DLL_MISMATCH")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-active", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("PRIVATE_SQLITE_ACTIVE_REPORT_MUST_BE_NEW")
    value = current_identity(required=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
