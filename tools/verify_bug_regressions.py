"""Run the full source regression suite and account for every recorded defect.

This maintenance command does not build applications, call model services or
certify native packages. A passing source run is not field acceptance.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = "docs/bugs/registry.json"
BASELINE = "docs/bugs/regression-baseline.json"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def snapshot(root):
    """Bind reports to application, fixture, tool and contract bytes, even before commit."""
    paths = set()
    for name in ("poc", "tools", "docs/bugs"):
        paths.update(p for p in (root / name).rglob("*") if p.is_file()
                     and "__pycache__" not in p.parts and p.suffix not in {".pyc", ".pyo"})
    paths.update(root.glob("*.py"))
    paths.update(root.glob("*requirements*.txt"))
    rows = [{"path": p.relative_to(root).as_posix(), "sha256": digest(p.read_bytes())}
            for p in sorted(paths)]
    return {"sha256": digest(json.dumps(rows, sort_keys=True).encode()), "files": rows}


def discover(root, tests_dir="poc/tests"):
    sys.path.insert(0, str(root))
    loader = unittest.TestLoader()
    suite = loader.discover(str(root / tests_dir), pattern="test_*.py")

    def flatten(value):
        for item in value:
            if isinstance(item, unittest.TestSuite):
                yield from flatten(item)
            else:
                yield item.id()

    ids = list(flatten(suite))
    if loader.errors or not ids or len(ids) != len(set(ids)):
        raise ValueError("Test discovery failed or contains duplicate IDs: " + "\n".join(loader.errors))
    return ids


def regression_key(row):
    return (row.get("kind"), row.get("test_id") if row.get("kind") == "python" else row.get("path"))


def validate_contract(registry, baseline, python_ids, browser_files, previous_registry=None, previous_baseline=None):
    errors = []
    if not isinstance(registry, dict) or registry.get("schema_version") != 1 or not isinstance(registry.get("bugs"), list):
        return ["Invalid defect registry"]
    if not isinstance(baseline, dict) or baseline.get("schema_version") != 1:
        return ["Invalid regression baseline"]
    for key in ("python_ids", "browser_ids"):
        values = baseline.get(key)
        if not isinstance(values, list) or not values or any(not isinstance(x, str) or not x for x in values):
            errors.append(f"Invalid baseline {key}")
        elif len(values) != len(set(values)):
            errors.append(f"Duplicate baseline {key}")
    if errors:
        return errors
    for identifier in sorted(set(baseline["python_ids"]) - set(python_ids)):
        errors.append(f"Historical Python test missing: {identifier}")
    ids = []
    for bug in registry["bugs"]:
        if not isinstance(bug, dict) or not isinstance(bug.get("id"), str):
            errors.append("Invalid defect entry")
            continue
        ids.append(bug["id"])
        for key in ("title", "symptom", "root_cause", "verification_scope", "limitations"):
            if not isinstance(bug.get(key), str) or not bug[key].strip():
                errors.append(f"{bug['id']}: missing {key}")
        if bug.get("status") not in {"fixed", "open"}:
            errors.append(f"{bug['id']}: invalid status")
        if bug.get("status") == "fixed" and bug.get("verification_scope") != "source_offline":
            errors.append(f"{bug['id']}: this gate can only verify source_offline fixes")
        if not isinstance(bug.get("evidence"), list) or not bug["evidence"]:
            errors.append(f"{bug['id']}: missing evidence")
        regressions = bug.get("regressions")
        if not isinstance(regressions, list) or (bug.get("status") == "fixed" and not regressions):
            errors.append(f"{bug['id']}: fixed defect needs a regression")
            continue
        for row in regressions:
            if not isinstance(row, dict):
                errors.append(f"{bug['id']}: invalid regression")
            elif row.get("kind") == "python":
                if row.get("test_id") not in python_ids:
                    errors.append(f"{bug['id']}: missing test {row.get('test_id')}")
            elif row.get("kind") == "browser":
                if row.get("path") not in browser_files:
                    errors.append(f"{bug['id']}: missing browser file {row.get('path')}")
            else:
                errors.append(f"{bug['id']}: unsupported regression kind")
    if not ids or len(ids) != len(set(ids)):
        errors.append("Defect IDs must be nonempty and unique")
    if previous_registry:
        current = {b["id"]: b for b in registry["bugs"] if isinstance(b, dict) and "id" in b}
        for old in previous_registry["bugs"]:
            new = current.get(old["id"])
            if new is None:
                errors.append(f"Historical defect removed: {old['id']}")
            elif not {regression_key(r) for r in old["regressions"]}.issubset(
                    {regression_key(r) for r in new["regressions"]}):
                errors.append(f"Historical regression mapping removed: {old['id']}")
    if previous_baseline:
        for key in ("python_ids", "browser_ids"):
            if not set(previous_baseline[key]).issubset(baseline[key]):
                errors.append(f"Historical baseline reduced: {key}")
    return errors


def browser_results(events, root, files):
    rows, errors, summaries = [], [], []
    for event in events:
        kind, data = event.get("type"), event.get("data", {})
        if kind == "test:summary":
            summaries.append(data)
        elif kind in {"test:pass", "test:fail"}:
            if data.get("details", {}).get("type") == "suite":
                continue
            try:
                path = Path(data["file"]).resolve().relative_to(root.resolve()).as_posix()
                name = data["name"]
                if not isinstance(name, str) or not name:
                    raise ValueError("empty test name")
            except (KeyError, TypeError, ValueError):
                errors.append("Browser result lacks a valid source file and name")
                continue
            outcome = "passed" if kind == "test:pass" else "failed"
            if data.get("skip") or data.get("todo"):
                outcome = "skipped"
            rows.append({"id": path + "::" + name, "path": path, "outcome": outcome})
    # The final summary is the aggregate emitted by Node, not a per-file summary.
    if not summaries or summaries[-1].get("success") is not True:
        errors.append("Missing successful browser summary")
    else:
        counts = summaries[-1].get("counts", {})
        if (counts.get("tests") != len(rows) or counts.get("passed") != len(rows)
                or any(counts.get(k) != 0 for k in ("failed", "cancelled", "skipped", "todo"))):
            errors.append("Browser summary is incomplete or contains nonpassing tests")
    if Counter(row["id"] for row in rows) != Counter(set(row["id"] for row in rows)):
        errors.append("Duplicate browser test identity")
    if {r["path"] for r in rows} != set(files):
        errors.append("Browser results do not cover exactly all current test files")
    return rows, errors


def evaluate(registry, baseline, reports, browser, python_ids, browser_files, browser_errors=()):
    errors = list(browser_errors)
    outcomes, seen, discoveries, groups = {}, [], {}, {}
    if not reports:
        errors.append("No Python reports")
    for report in reports:
        key = tuple(sorted(report.get("discovery", {}).get("ids", [])))
        groups.setdefault(key, []).append(report.get("shard_index"))
    for report in reports:
        key = tuple(sorted(report.get("discovery", {}).get("ids", [])))
        if report.get("status") != "passed" or report.get("shard_count") != len(groups[key]):
            errors.append("Python shard did not complete successfully")
        for field in ("failures", "errors", "skipped", "expected_failures", "unexpected_successes",
                      "discovery_errors", "runner_errors", "diagnostic_errors"):
            if report.get(field) != []:
                errors.append(f"Python shard {report.get('shard_index')}: {field} is not empty")
        current_discovery = report.get("discovery")
        reference = discoveries.setdefault(key, current_discovery)
        if current_discovery != reference or not isinstance(current_discovery, dict):
            errors.append("Python shards have different discovery manifests")
        elif not set(current_discovery.get("ids", [])).issubset(python_ids):
            errors.append("Python discovery contains tests outside the current checkout")
        selected = report.get("selected_ids", [])
        results = report.get("test_results", [])
        result_ids = [row["id"] for row in results]
        if (report.get("started_ids") != selected or result_ids != selected
                or report.get("tests_run") != len(selected) or report.get("selected_count") != len(selected)):
            errors.append("Python selected, started and completed tests differ")
        seen.extend(result_ids)
        outcomes.update({row["id"]: row["outcome"] for row in results})
        if any(row["outcome"] != "passed" for row in results):
            errors.append("Python test did not pass")
    if any(Counter(indices) != Counter(range(len(indices))) for indices in groups.values()):
        errors.append("Missing or duplicate Python shard")
    if Counter(seen) != Counter(python_ids):
        errors.append("Python results do not cover every test exactly once")
    browser_outcomes = {row["id"]: row["outcome"] for row in browser}
    if any(row["outcome"] != "passed" for row in browser):
        errors.append("Browser test did not pass")
    if set(baseline["browser_ids"]) - set(browser_outcomes):
        errors.append("Historical browser tests missing")
    bugs = []
    for bug in registry["bugs"]:
        checks = []
        for row in bug["regressions"]:
            if row["kind"] == "python":
                checks.append({"test": row["test_id"], "outcome": outcomes.get(row["test_id"], "missing")})
            else:
                matched = [r for r in browser if r["path"] == row["path"]]
                checks.append({"test": row["path"], "outcome": "passed" if matched and all(
                    r["outcome"] == "passed" for r in matched) else "missing_or_failed"})
        verified = bug["status"] == "fixed" and bool(checks) and all(c["outcome"] == "passed" for c in checks)
        bugs.append({"id": bug["id"], "title": bug["title"], "status": "passed" if verified else "unverified",
                     "scope": bug["verification_scope"], "checks": checks})
    source_passed = not errors
    all_verified = source_passed and all(b["status"] == "passed" for b in bugs)
    return {"schema_version": 1, "status": "passed" if all_verified else "blocked",
            "source_regressions_passed": source_passed, "all_recorded_bugs_verified": all_verified,
            "errors": errors, "python_tests": len(seen), "browser_tests": len(browser), "bugs": bugs,
            "unverified_bug_ids": [b["id"] for b in bugs if b["status"] != "passed"],
            "scope": "source_offline_only", "native_or_real_model_acceptance": False}


def previous_json(root, path):
    # Include every committed version: committing a deletion before running
    # the gate must not erase the minimum accepted regression contract.
    commits = subprocess.run(["git", "log", "--format=%H", "--diff-filter=AM", "HEAD", "--", path],
                             cwd=root, check=True, capture_output=True, text=True).stdout.splitlines()
    if not commits:
        return None
    versions = [json.loads(subprocess.run(["git", "show", commit + ":" + path], cwd=root,
                    check=True, capture_output=True, text=True).stdout) for commit in commits]
    if path == BASELINE:
        return {key: sorted({item for version in versions for item in version[key]})
                for key in ("python_ids", "browser_ids")}
    bugs = {}
    for version in versions:
        for row in version["bugs"]:
            bug = bugs.setdefault(row["id"], {"id": row["id"], "regressions": []})
            existing = {regression_key(r) for r in bug["regressions"]}
            bug["regressions"].extend(r for r in row["regressions"] if regression_key(r) not in existing)
    return {"bugs": list(bugs.values())}


def require_full_history(root):
    shallow = subprocess.check_output(["git", "rev-parse", "--is-shallow-repository"], cwd=root, text=True).strip()
    if shallow != "false":
        raise ValueError("Full Git history is required; shallow history cannot prove the historical defect contract")


def run(command, log, root):
    with log.open("w", encoding="utf-8") as stream:
        try:
            return subprocess.run(command, cwd=root, stdout=stream, stderr=subprocess.STDOUT, timeout=3600).returncode
        except subprocess.TimeoutExpired:
            stream.write("\nMaintenance regression process exceeded one hour. Verification incomplete.\n")
            return 124


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="New directory outside the checkout")
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--check-only", action="store_true", help="Validate inventory only; never counts as passing regression")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if output.is_relative_to(ROOT) or output.exists() or not 1 <= args.shards <= 16:
        parser.error("Use a new output directory outside the checkout and 1-16 shards")
    output.mkdir(parents=True)
    report = {"schema_version": 1, "status": "blocked", "all_recorded_bugs_verified": False,
              "scope": "source_offline_only", "native_or_real_model_acceptance": False, "errors": []}
    try:
        require_full_history(ROOT)
        before = snapshot(ROOT)
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        report.update(source_commit=commit, source_snapshot=before["sha256"],
                      started_at=datetime.now(timezone.utc).isoformat())
        registry, baseline = read_json(ROOT / REGISTRY), read_json(ROOT / BASELINE)
        python_ids = discover(ROOT) + discover(ROOT, "tools/tests")
        if len(python_ids) != len(set(python_ids)):
            raise ValueError("Test IDs collide across source and maintenance suites")
        files = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "poc/tests").glob("*.test.cjs"))
        errors = validate_contract(registry, baseline, python_ids, files,
                                   previous_json(ROOT, REGISTRY), previous_json(ROOT, BASELINE))
        if errors:
            report["errors"] = errors
        elif args.check_only:
            report.update(status="inventory_only", python_tests=len(python_ids), browser_files=len(files))
        else:
            print(f"Running all {len(python_ids)} Python tests and {len(files)} browser test files", flush=True)
            with ThreadPoolExecutor(max_workers=args.shards) as pool:
                futures = [pool.submit(run, [sys.executable, "tools/run_python_regressions.py", "--shard-index", str(i),
                    "--shard-count", str(args.shards), "--source-commit", commit, "--output", str(output / f"python-{i}.json")],
                    output / f"python-{i}.log", ROOT) for i in range(args.shards)]
                codes = [future.result() for future in futures]
            codes.append(run([sys.executable, "tools/run_python_regressions.py", "--tests-dir", "tools/tests",
                "--shard-index", "0", "--shard-count", "1", "--source-commit", commit,
                "--output", str(output / "python-tools.json")], output / "python-tools.log", ROOT))
            browser_log = output / "browser.jsonl"
            browser_code = run(["node", "--test", "--test-reporter=./tools/bug_browser_reporter.cjs", *files], browser_log, ROOT)
            events = [json.loads(line) for line in browser_log.read_text(encoding="utf-8").splitlines() if line.strip()]
            browser, browser_errors = browser_results(events, ROOT, files)
            reports = [read_json(output / f"python-{i}.json") for i in range(args.shards)]
            reports.append(read_json(output / "python-tools.json"))
            if any(code != 0 for code in codes) or browser_code:
                browser_errors.append("Regression process returned a nonzero exit code")
            if any(r.get("source_commit") != commit for r in reports):
                browser_errors.append("Python report source commit mismatch")
            report.update(evaluate(registry, baseline, reports, browser, python_ids, files, browser_errors))
            report["report_sha256"] = {p.name: digest(p.read_bytes()) for p in sorted(output.glob("python-*.json"))}
            report["report_sha256"]["browser.jsonl"] = digest(browser_log.read_bytes())
        after = snapshot(ROOT)
        if after != before or subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip() != commit:
            report["errors"].append("Source or regression contract changed during verification")
            report.update(status="blocked", all_recorded_bugs_verified=False, source_regressions_passed=False)
            for bug in report.get("bugs", []):
                bug["status"] = "unverified"
            report["unverified_bug_ids"] = [b["id"] for b in report.get("bugs", [])]
        write_json(output / "SOURCE-SNAPSHOT.json", before)
    except (Exception, KeyboardInterrupt) as error:
        report.update(status="blocked", all_recorded_bugs_verified=False, source_regressions_passed=False)
        report["errors"].append(f"Verification interrupted: {type(error).__name__}: {error}")
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_json(output / "BUG-REGRESSION-REPORT.json", report)
    print(json.dumps({k: v for k, v in report.items() if k in {
        "status", "all_recorded_bugs_verified", "python_tests", "browser_tests", "errors"}}, ensure_ascii=False), flush=True)
    return 0 if report["status"] in {"passed", "inventory_only"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
