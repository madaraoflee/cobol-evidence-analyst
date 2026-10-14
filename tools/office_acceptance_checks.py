"""Repeat all offline native checks on the actual office computer, without fees."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import sys

from desktop_acceptance import digest, main as run_native_suite, require


def office_environment():
    """Store a stable anonymous local identity, never a host or user name."""
    identity = "\0".join((socket.gethostname(), platform.system(), platform.machine()))
    return {"schema_version": "office-execution-environment/v1",
            "machine_sha256": hashlib.sha256(identity.encode()).hexdigest(),
            "system": platform.system(), "release": platform.release(),
            "version": platform.version(), "architecture": platform.machine(),
            "logical_processors": os.cpu_count(),
            "scope": "local_process_observation_not_gui_or_enterprise_dialect_attestation"}


def run_office_native(kit_root, output):
    """No model credentials needed. The real EXE sees a local synthetic provider."""
    require(sys.platform == "win32", "NATIVE_WINDOWS_EXECUTION_REQUIRED")
    kit_root = Path(kit_root).resolve(strict=True)
    from office_acceptance_app import load_kit, kit_file
    kit = load_kit(kit_root / "OFFICE-KIT.json")
    output = Path(output).resolve()
    require(not output.exists() and not output.is_relative_to(kit_root), "OFFICE_OUTPUT_INVALID")
    # Explicit data paths work in both the source runner and standalone helper.
    import tools.desktop_acceptance_runtime as runtime
    import tools.desktop_acceptance_cases as cases
    runtime.FIXTURES = kit_file(kit_root, "fixtures")
    cases.PUBLIC_MANIFEST = kit_file(kit_root, "contracts/public-manifest.json")
    cases.PUBLIC_CASES = kit_file(kit_root, "contracts/public-cases.json")
    cases.FROZEN_SOURCE_ROOT = kit["source"] / "synthetic"
    code = run_native_suite(["--archive", str(kit["inputs"]["archive"]),
        "--source-commit", kit["document"]["source_commit"],
        "--manifest", str(kit["inputs"]["manifest"]),
        "--public-source", str(kit["source"]), "--output", str(output)])
    report_path = output / "COMPILED-ACCEPTANCE.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    environment = office_environment()
    report["office_environment"] = environment
    report["office_source"] = "actual_target_computer_full_suite"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "OFFICE-ENVIRONMENT.json").write_text(json.dumps(environment, indent=2) + "\n", encoding="utf-8")
    from partial_acceptance_audit import audit_report
    audit = audit_report(report, observation_kind="target_native_repeat_not_original_ci_run")
    (output / "PARTIAL-DIAGNOSTICS.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    require(code == 0, "OFFICE_NATIVE_SUITE_FAILED")
    return report
