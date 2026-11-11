"""Read downloaded candidate bytes; never execute, publish or call a model."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys
import zipfile

ASSETS = {
    "COBOLWorkbench-windows-x64.zip", "COBOLWorkbench-windows-x64.zip.sha256",
    "BUILD-INFO.json", "COMPILED-ACCEPTANCE.json", "CANDIDATE-GATE.json",
    "ACCEPTANCE-MANIFEST.json", "CANDIDATE-POLICY.json", "INVESTIGATION-CONTINUITY.json"}
GLOBAL_CHECKS = {
    "failure_restart_and_explicit_continuation_preserve_completed_work",
    "every_original_and_business_position_classified_once", "strict_review_completed_after_analysis",
    "all_pages_deliver_every_business_formula", "complete_export_retains_formulas_and_business_explanations",
    "all_exported_business_explanations_passed_strict_review", "first_middle_last_originals_are_accessible",
    "earlier_published_result_does_not_follow_later_progress", "only_the_planned_failure_occurred",
    "invalid_lookup_stops_after_two_requests_without_deployment_ceiling", "invalid_lookup_preserves_every_pending_item",
    "empty_lookup_stops_after_two_requests_without_deployment_ceiling", "empty_lookup_preserves_every_pending_item"}


def require(value, reason):
    if not value:
        raise ValueError(reason)


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    require(isinstance(value, dict), "Expected a JSON object: " + Path(path).name)
    return value


def native_report_identity(report, identity, label):
    require(report.get("execution_surface") == "native_windows_http", label + " report is not final-EXE HTTP execution")
    require(report.get("runtime_platform") == "Windows", label + " report is not from Windows")
    require(report.get("source_dirty") is False, label + " report source is not explicitly frozen")
    require(all(report[key] == value for key, value in identity.items()), label + " report does not bind the same native ZIP/EXE/source")


def artifact_check(archive, metadata, expected_name, expected_files, *, commit, run_id):
    require(bool(archive) == bool(metadata), "Artifact ZIP and GitHub metadata must be supplied together")
    if not archive:
        return None
    info = read(metadata)
    require(info["name"] == expected_name and info["expired"] is False, "Wrong or expired Actions artifact")
    require(info["digest"] == "sha256:" + sha(archive), "Actions artifact digest differs from downloaded bytes")
    owner = info["workflow_run"]
    require(owner["head_sha"] == commit and owner["head_branch"] == "main" and owner["id"] == run_id,
            "Actions artifact belongs to a different source/run")
    with zipfile.ZipFile(archive) as bundle:
        require(bundle.testzip() is None, "Actions artifact CRC failure")
        members = [entry.filename for entry in bundle.infolist() if not entry.is_dir()]
        require(len(members) == len(set(members)) and set(members) == set(expected_files), "Actions artifact asset whitelist differs")
        for name, path in expected_files.items():
            require(bundle.read(name) == Path(path).read_bytes(), "Extracted bytes differ from Actions artifact: " + name)
    return {"artifact_id": info["id"], "artifact_name": expected_name, "sha256": sha(archive), "run_id": run_id}

SQLITE_PIN = {
    "version": "3.54.1",
    "url": "https://sqlite.org/2026/sqlite-dll-win-x64-3540100.zip",
    "archive_sha3_256": "c2d09c8babb9399277055c057c97a53f1ffd7c5f11d0c1f2118ffdfa39f2daa4",
    "dll_sha256": "4b59d3cded9dc1608544160f3b4b10ac974c6ac0d6ffc23064e5f4c8dbd05aea",
    "source_id": "2026-10-16 14:45:20 555e07ef33f69a0957d24d8e555b702464a131d6f1607f4c3c1e4551e8e6ca76",
}
SQLITE_FEATURES = {"json", "fts4", "fts5", "rtree", "math", "serialize", "backup", "config"}
SQLITE_OPTIONS = {"ENABLE_FTS5", "ENABLE_RTREE", "ENABLE_MATH_FUNCTIONS", "THREADSAFE=1"}


def windows_report_path(value):
    require(isinstance(value, str) and value, "Native runtime path is missing")
    path = PureWindowsPath(value)
    require(path.is_absolute() and re.fullmatch(r"[a-zA-Z]:", path.drive)
        and ".." not in path.parts and not any(":" in part for part in path.parts[1:]),
        "Native runtime path is not an ordinary absolute Windows path")
    return path


def static_sqlite_probe(probe, label):
    require(isinstance(probe, dict) and probe.get("schema_version") == "sqlite-runtime-probe/v1"
        and probe.get("status") == "passed", label + " SQLite probe did not pass")
    require(probe.get("identity_kind") == "compiled" and probe.get("runtime_platform") == "Windows"
        and probe.get("pointer_bits") == 64, label + " SQLite identity is not native Windows x64 compiled")
    require(all(probe.get(key) == SQLITE_PIN[key] for key in ("version", "source_id"))
        and probe.get("loaded_dll_sha256") == SQLITE_PIN["dll_sha256"], label + " SQLite identity differs from the exact pin")
    checks, options = probe.get("checks"), probe.get("compile_options")
    require(isinstance(checks, dict) and set(checks) == SQLITE_FEATURES
        and all(value is True for value in checks.values()), label + " SQLite feature checks incomplete")
    require(isinstance(options, list) and all(isinstance(item, str) for item in options)
        and SQLITE_OPTIONS <= set(options), label + " SQLite compile options incomplete")


def sqlite_report_check(report, identity, package, build_info):
    """Check controlled native evidence on a download host without opening DLLs.

    Windows paths belong to the recorded runner, not this machine. The actual
    DLL bytes are read from the same digest-bound ZIP and the original native
    report is separately checked against its downloaded Actions artifact.
    """
    require(report.get("schema_version") == "packaged-sqlite-runtime-acceptance/v1"
        and report.get("status") == "passed" and report.get("errors") == [], "SQLite acceptance report did not pass")
    require(report.get("execution_surface") == "native_windows_exe_self_test"
        and report.get("runtime_platform") == "Windows", "SQLite check was not the actual Windows EXE self-test")
    require(report.get("source_dirty") is False, "SQLite report source is not frozen")
    for key in ("source_commit", "archive_sha256", "binary_path", "binary_sha256", "application_sha256"):
        require(report.get(key) == identity[key], "SQLite report does not bind the same package: " + key)
    require(all(type(report.get(key)) is int and report[key] == 0
        for key in ("model_calls", "external_model_calls", "paid_model_calls")), "SQLite component check called a model")
    require(report.get("pin") == SQLITE_PIN, "SQLite report pin is not the reviewed official component")
    require(isinstance(report.get("pin_sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", report["pin_sha256"]),
        "SQLite native pin content hash is missing")
    probe = report.get("sqlite_runtime")
    static_sqlite_probe(probe, "native")
    self_test = report.get("self_test")
    require(isinstance(self_test, dict) and self_test.get("ok") is True
        and type(self_test.get("model_calls")) is int and self_test["model_calls"] == 0
        and self_test.get("sqlite_runtime") == probe, "SQLite native self-test/result differs")
    with zipfile.ZipFile(package) as bundle:
        dlls = [entry for entry in bundle.infolist() if not entry.is_dir()
            and PurePosixPath(entry.filename).name.casefold() == "sqlite3.dll"]
        require(len(dlls) == 1, "Same ZIP must contain exactly one SQLite DLL")
        member = PurePosixPath(dlls[0].filename)
        executables = [entry.filename for entry in bundle.infolist()
                       if not entry.is_dir() and PurePosixPath(entry.filename).suffix.casefold() == ".exe"]
        require(member.parts[:2] == ("COBOLWorkbench", "COBOLWorkbench")
            and not member.is_absolute() and ".." not in member.parts,
            "SQLite DLL is outside the actual packaged product")
        require(hashlib.sha256(bundle.read(dlls[0])).hexdigest() == SQLITE_PIN["dll_sha256"],
            "Actual packaged SQLite DLL bytes differ from the pin")
    packaged = report.get("packaged_dll")
    require(isinstance(packaged, dict) and packaged.get("path") == member.as_posix()
        and packaged.get("sha256") == SQLITE_PIN["dll_sha256"], "SQLite native DLL/member record differs")
    loaded = windows_report_path(probe.get("loaded_dll_path"))
    root = windows_report_path(probe.get("allowed_root"))
    executable = windows_report_path(probe.get("executable"))
    require(root.parts[-3:] == ("extracted", "COBOLWorkbench", "COBOLWorkbench"),
        "SQLite native root is not the checker's fresh extracted product")
    expected_executable = root / "COBOLWorkbench.exe"
    require(loaded == root.joinpath(*member.parts[2:]), "SQLite actually loaded DLL is outside/different from the extracted product")
    extension = probe.get("extension_path")
    if extension is None:
        require(probe.get("extension_origin") == "built_in", "SQLite extension origin is invalid")
    else:
        extension = windows_report_path(extension)
        require(probe.get("extension_origin") == "file" and extension.is_relative_to(root),
            "SQLite extension was loaded outside the extracted product")
    recorded = build_info.get("self_test", {}).get("sqlite_runtime")
    static_sqlite_probe(recorded, "BUILD-INFO")
    alias = executable == root / "python.exe"
    if alias:
        # Nuitka 4.2.2 standalone reports the compiler Python basename under
        # the product root. The trusted native checker launches the exact EXE.
        dependencies = build_info.get("dependencies")
        build_root = windows_report_path(recorded.get("allowed_root"))
        require(isinstance(dependencies, list) and dependencies.count("Nuitka==4.2.2") == 1
            and executables == [identity["binary_path"]]
            and windows_report_path(recorded.get("executable")) == build_root / "python.exe"
            and all(windows_report_path(row.get(field)) == product
                    for row, product in ((probe, root), (recorded, build_root))
                    for field in ("prefix", "base_prefix")),
            "Unproven compiled standalone executable alias")
    else:
        require(executable == expected_executable, "SQLite probe executable is outside the extracted product")
    require(report.get("build_self_test_sqlite_runtime") == recorded,
        "SQLite acceptance BUILD-INFO snapshot differs from the package")
    require(all(recorded.get(key) == probe.get(key) for key in ("version", "source_id", "loaded_dll_sha256")),
        "Native and BUILD-INFO SQLite identities differ")
    return {"version": probe["version"], "source_id": probe["source_id"],
        "loaded_dll_sha256": probe["loaded_dll_sha256"], "packaged_dll": member.as_posix(),
        "native_loaded_dll_path": str(loaded), "feature_checks": len(SQLITE_FEATURES),
        "reported_sys_executable": str(executable),
        "sys_executable_is_nuitka_standalone_alias": alias,
        "download_host_loaded_dll_or_ran_exe": False}


def verify(args):
    sys.path[:0] = [str(args.repo_root.resolve()), str(args.repo_root.resolve() / "poc")]
    from release_acceptance import evaluate_release
    from tools.desktop_acceptance import inspect_package
    require(args.run_id > 0 and args.run_attempt > 0, "Explicit positive actual run ID and attempt are required")
    require(args.sqlite_artifact is not None and args.sqlite_metadata is not None,
        "SQLite native report must include the raw Actions artifact and API metadata")
    require(read(args.repo_root / "tools/sqlite-runtime-pin.json") == SQLITE_PIN,
        "Verifier checkout does not contain the reviewed SQLite pin")
    directory = args.candidate_dir.resolve(strict=True)
    require(set(path.name for path in directory.iterdir()) == ASSETS, "Candidate directory must contain exactly the original eight assets")
    require(all((directory / name).is_file() and not (directory / name).is_symlink() for name in ASSETS), "Candidate assets must be regular files")
    require(re.fullmatch(r"[0-9a-f]{40}", args.source_commit), "Expected full source commit")
    package = directory / "COBOLWorkbench-windows-x64.zip"
    checksum = (directory / "COBOLWorkbench-windows-x64.zip.sha256").read_text(encoding="ascii").strip()
    require(checksum == sha(package) + "  " + package.name, "Delivery checksum does not match ZIP")
    identity = inspect_package(package, args.source_commit)
    with zipfile.ZipFile(package) as bundle:
        require(bundle.read("COBOLWorkbench/BUILD-INFO.json") == (directory / "BUILD-INFO.json").read_bytes(), "External BUILD-INFO differs from package")
    build_info = read(directory / "BUILD-INFO.json")
    sqlite_report = read(args.sqlite_report)
    require(sqlite_report.get("pin_sha256") == sha(args.repo_root / "tools/sqlite-runtime-pin.json"),
        "SQLite native pin bytes differ from the exact source checkout pin")
    sqlite_identity = sqlite_report_check(sqlite_report, identity, package, build_info)
    compiled = read(directory / "COMPILED-ACCEPTANCE.json")
    manifest = read(directory / "ACCEPTANCE-MANIFEST.json")
    policy = read(directory / "CANDIDATE-POLICY.json")
    require(policy["source_commit"] == args.source_commit, "Candidate policy commit differs")
    gate = evaluate_release(package=package, compiled_report=directory / "COMPILED-ACCEPTANCE.json",
        manifest=directory / "ACCEPTANCE-MANIFEST.json", policy=directory / "CANDIDATE-POLICY.json")
    require(gate == read(directory / "CANDIDATE-GATE.json"), "Candidate gate cannot be independently reproduced")
    require(gate["candidate_allowed"] is True and gate["candidate_errors"] == [] and gate["publish_allowed"] is False,
            "Candidate must pass offline acceptance and remain blocked for formal publication")
    require(gate["publish_errors"] == [{"code": "REAL_MODEL_AND_OFFICE_ACCEPTANCE_REQUIRED"}], "Unexpected formal gate reason")
    require(len(manifest["cases"]) == 25 and len(compiled["cases"]) == len(gate["compiled_cases"]) == 79
        and all(row["passed"] is True for row in gate["compiled_cases"]), "Native 25-case/79-sample contract incomplete")
    continuity = read(directory / "INVESTIGATION-CONTINUITY.json")
    global_report = read(args.global_report)
    for name, report in (("compiled", compiled), ("continuity", continuity), ("global", global_report)):
        require(report["status"] == "passed" and report["errors"] == [], name + " report did not pass")
        native_report_identity(report, identity, name)
        require(report["transport_kind"] == "synthetic", name + " report is not the isolated offline check")
    require(continuity["checks"] and all(value is True for value in continuity["checks"].values()), "Original continuity check failure")
    require(global_report["schema_version"] == "global-inventory-continuity/v1", "Global report schema differs")
    require(set(global_report["checks"]) == GLOBAL_CHECKS and all(value is True for value in global_report["checks"].values()), "All thirteen global checks are required")
    require(global_report["external_model_calls"] == global_report["paid_model_calls"] == 0
        and global_report["formal_acceptance"] is False and global_report["real_model_accuracy"] is None,
        "Global report changed synthetic acceptance boundaries")
    require(global_report["source_files"] == global_report["formula_positions"] == 257
        and global_report["strict_review_parts"] > 1 and 4 < global_report["synthetic_model_requests"] < 8 * 48 + 2,
        "Global review/retention fixture incomplete")
    expected_fixture = {f"charge-{ordinal:03}.cbl" for ordinal in range(257)}
    require(set(global_report["fixture_sha256"]) == expected_fixture and all(re.fullmatch(r"[0-9a-f]{64}", digest)
        for digest in global_report["fixture_sha256"].values()), "Global fixture source binding incomplete")
    phases = global_report["observations"]
    for phase in ("invalid_lookup", "empty_lookup"):
        observed = [row for row in phases if row.get("phase") == phase]
        require(len(observed) == 1, "Missing/duplicate lookup termination phase: " + phase)
        row = observed[0]
        require(row["status"] == "PARTIAL" and row["convergence_stop_reason"] == "no_evidence_progress"
            and row["model_requests"] == 2 and row["request_limit"] is None and row["hard_seconds"] is None,
            "Lookup did not terminate after two requests under the default unbounded policy")
        counts = row["counts"]
        require(counts["files"] == 257 and counts["pending"] == counts["candidates"] == counts["review_pending"] == 514
            and counts["reviewed"] == counts["included"] == counts["excluded"] == 0, "Lookup lost unfinished global work")
    candidate_artifact = artifact_check(args.candidate_artifact, args.candidate_metadata,
        f"COBOLWorkbench-windows-x64-{args.source_commit}-{args.run_attempt}",
        {name: directory / name for name in ASSETS}, commit=args.source_commit, run_id=args.run_id)
    global_artifact = artifact_check(args.global_artifact, args.global_metadata,
        f"global-inventory-continuity-{args.source_commit}-{args.run_attempt}",
        {"GLOBAL-INVENTORY-CONTINUITY.json": args.global_report}, commit=args.source_commit, run_id=args.run_id)
    sqlite_artifact = artifact_check(args.sqlite_artifact, args.sqlite_metadata,
        f"sqlite-runtime-acceptance-{args.source_commit}-{args.run_attempt}",
        {"SQLITE-RUNTIME-ACCEPTANCE.json": args.sqlite_report}, commit=args.source_commit, run_id=args.run_id)
    return {"status": "passed", "verified_at_utc": datetime.now(timezone.utc).isoformat(), "identity": identity,
        "candidate_allowed": True, "formal_publish_allowed": False, "formal_gate_reasons": gate["publish_errors"],
        "native_cases": 25, "native_samples": 79, "continuity_checks": len(continuity["checks"]),
        "global_checks": len(GLOBAL_CHECKS), "global_formula_positions": 257,
        "global_lookup_phases": [row for row in phases if row.get("phase")],
        "candidate_artifact": candidate_artifact, "global_artifact": global_artifact,
        "sqlite_artifact": sqlite_artifact, "sqlite_runtime": sqlite_identity,
        "sqlite_report_sha256": sha(args.sqlite_report),
        "assets": {name: {"bytes": (directory / name).stat().st_size, "sha256": sha(directory / name)} for name in sorted(ASSETS)},
        "global_report_sha256": sha(args.global_report), "executables_or_models_run_by_this_verifier": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--candidate-dir", type=Path, required=True)
    parser.add_argument("--global-report", type=Path, required=True)
    parser.add_argument("--sqlite-report", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--candidate-artifact", type=Path)
    parser.add_argument("--candidate-metadata", type=Path)
    parser.add_argument("--global-artifact", type=Path)
    parser.add_argument("--global-metadata", type=Path)
    parser.add_argument("--sqlite-artifact", type=Path, required=True)
    parser.add_argument("--sqlite-metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), "Use a new verification result path")
    try:
        result = verify(args)
    except Exception as error:
        result = {"status": "failed", "errors": [{"type": type(error).__name__, "message": str(error)}]}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("status", "errors") if key in result}))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
