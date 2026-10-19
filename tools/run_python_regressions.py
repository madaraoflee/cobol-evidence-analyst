#!/usr/bin/env python3
"""Run a deterministic, module-atomic share of the complete unittest suite."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import faulthandler
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
import traceback
import unittest


ROOT = Path(__file__).resolve().parents[1]
_timer = time.perf_counter


def json_digest(value):
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def flatten_suite(suite):
    """Keep discovery order, including failed-import placeholder cases."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten_suite(item)
        elif isinstance(item, unittest.TestCase):
            yield item
        else:
            raise TypeError(f"Unsupported discovered test object: {type(item).__name__}")


def partition_tests(tests, shard_count):
    """Balance whole modules by test count; never change their execution order."""
    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    by_module = defaultdict(list)
    for test in tests:
        by_module[type(test).__module__].append(test)
    loads = [0] * shard_count
    assignments = {}
    for module in sorted(by_module, key=lambda name: (-len(by_module[name]), name)):
        shard = min(range(shard_count), key=lambda index: (loads[index], index))
        assignments[module] = shard
        loads[shard] += len(by_module[module])
    return [[test for test in tests if assignments[type(test).__module__] == index]
            for index in range(shard_count)]


def source_manifest(tests_dir):
    return [{"path": path.relative_to(tests_dir).as_posix(),
             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in sorted(tests_dir.rglob("*.py"),
                               key=lambda path: path.relative_to(tests_dir).as_posix())
            if "__pycache__" not in path.relative_to(tests_dir).parts]


def git_provenance():
    def git(*arguments):
        return subprocess.run(["git", "-C", str(ROOT), *arguments], check=True,
                              capture_output=True, text=True, timeout=10).stdout.strip()
    return git("rev-parse", "HEAD"), bool(git("status", "--porcelain", "--untracked-files=normal"))


class TimedResult(unittest.TextTestResult):
    """Emit full IDs immediately and retain one timing per root TestCase."""

    def __init__(self, stream, descriptions, verbosity):
        super().__init__(stream, descriptions, verbosity)
        self.started_ids = []
        self.test_results = []
        self._started = {}
        self._outcomes = {}

    def startTest(self, test):
        unittest.TestResult.startTest(self, test)
        identifier = test.id()
        self.started_ids.append(identifier)
        self._started[identifier] = _timer()
        self._outcomes[identifier] = "passed"
        self.stream.write(f"START {identifier}\n")
        self.stream.flush()

    def stopTest(self, test):
        identifier = test.id()
        seconds = round(max(0.0, _timer() - self._started.pop(identifier)), 6)
        outcome = self._outcomes.pop(identifier)
        self.test_results.append({"id": identifier, "seconds": seconds, "outcome": outcome})
        self.stream.write(f"END {identifier} {outcome} ({seconds:.6f}s)\n")
        self.stream.flush()
        unittest.TestResult.stopTest(self, test)

    def _mark(self, test, outcome):
        if test.id() in self._outcomes:
            self._outcomes[test.id()] = outcome

    def addSuccess(self, test):
        unittest.TestResult.addSuccess(self, test)

    def addFailure(self, test, err):
        self._mark(test, "failed")
        unittest.TestResult.addFailure(self, test, err)

    def addError(self, test, err):
        self._mark(test, "error")
        unittest.TestResult.addError(self, test, err)

    def addSkip(self, test, reason):
        self._mark(test, "skipped")
        unittest.TestResult.addSkip(self, test, reason)

    def addExpectedFailure(self, test, err):
        self._mark(test, "expected_failure")
        unittest.TestResult.addExpectedFailure(self, test, err)

    def addUnexpectedSuccess(self, test):
        self._mark(test, "unexpected_success")
        unittest.TestResult.addUnexpectedSuccess(self, test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._mark(test, "failed" if issubclass(err[0], test.failureException) else "error")
        unittest.TestResult.addSubTest(self, test, subtest, err)


def write_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix=path.name + ".", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_regressions(*, tests_dir, shard_index, shard_count, output,
                    expected_commit=None, stack_dump_interval=120, stream=None):
    stream = stream or sys.stderr
    started = _timer()
    report = {"schema_version": 1, "source_commit": None, "source_dirty": None,
              "runtime_platform": platform.system(), "status": "runner_error",
              "shard_index": shard_index, "shard_count": shard_count,
              "discovery": {"count": 0, "ids": [], "ids_sha256": json_digest([]),
                            "test_sources": [], "test_sources_sha256": json_digest([])},
              "selected_ids": [], "selected_count": 0, "tests_run": 0,
              "started_ids": [], "test_results": [], "failures": [], "errors": [],
              "skipped": [], "unexpected_successes": [], "expected_failures": [],
              "discovery_errors": [], "runner_errors": [], "elapsed_seconds": 0.0}
    result = None
    dump_started = False
    try:
        if shard_count < 1 or not 0 <= shard_index < shard_count:
            raise ValueError("shard-index must be in [0, shard-count)")
        if stack_dump_interval < 0:
            raise ValueError("stack-dump-interval must not be negative")
        report["source_commit"], report["source_dirty"] = git_provenance()
        if expected_commit is not None and report["source_commit"] != expected_commit:
            raise ValueError("Actual git commit does not match --source-commit")
        tests_dir = Path(tests_dir).resolve(strict=True)
        sources = source_manifest(tests_dir)
        # Direct script execution puts tools/, not the repository root, on
        # sys.path. Match the original `python -m unittest` import boundary.
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        loader = unittest.TestLoader()
        suite = loader.discover(str(tests_dir), pattern="test_*.py")
        tests = list(flatten_suite(suite))
        ids = [test.id() for test in tests]
        report["discovery"] = {"count": len(ids), "ids": ids,
                               "ids_sha256": json_digest(sorted(ids)),
                               "test_sources": sources, "test_sources_sha256": json_digest(sources)}
        report["discovery_errors"] = list(loader.errors)
        failed_imports = [test.id() for test in tests if isinstance(test, unittest.loader._FailedTest)]
        if failed_imports and not report["discovery_errors"]:
            report["discovery_errors"].append("Failed import placeholders: " + ", ".join(failed_imports))
        duplicates = [identifier for identifier, count in Counter(ids).items() if count > 1]
        if duplicates:
            report["discovery_errors"].append("Duplicate discovered test IDs: " + ", ".join(duplicates))
        if not tests:
            report["discovery_errors"].append("Discovery found no tests")
        if report["discovery_errors"]:
            report["status"] = "discovery_error"
            for error in report["discovery_errors"]:
                stream.write(error + "\n")
            stream.flush()
        else:
            selected = partition_tests(tests, shard_count)[shard_index]
            report["selected_ids"] = [test.id() for test in selected]
            report["selected_count"] = len(selected)
            if not selected:
                raise ValueError("The selected shard contains no tests")
            stream.write(f"Discovered {len(tests)} tests; shard {shard_index}/{shard_count} "
                         f"selects {len(selected)} tests\n")
            stream.flush()
            if stack_dump_interval:
                faulthandler.dump_traceback_later(stack_dump_interval, repeat=True, file=stream)
                dump_started = True
            runner = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=TimedResult)
            # Retain the result even when a test interrupts the runner.
            result = runner._makeResult()
            runner._makeResult = lambda: result
            runner.run(unittest.TestSuite(selected))
            ran_ids = [row["id"] for row in result.test_results]
            complete = (result.testsRun == len(selected)
                        and result.started_ids == report["selected_ids"]
                        and ran_ids == report["selected_ids"])
            if not complete:
                report["runner_errors"].append("Executed test IDs/count do not match selected tests")
            report["status"] = "passed" if result.wasSuccessful() and complete else "failed"
    except (Exception, KeyboardInterrupt, SystemExit):
        report["status"] = "runner_error"
        report["runner_errors"].append(traceback.format_exc())
        stream.write(report["runner_errors"][-1])
        stream.flush()
    finally:
        if dump_started:
            faulthandler.cancel_dump_traceback_later()
        if result is not None:
            report.update(tests_run=result.testsRun, started_ids=result.started_ids,
                          test_results=result.test_results,
                          failures=[{"id": test.id(), "traceback": detail} for test, detail in result.failures],
                          errors=[{"id": test.id(), "traceback": detail} for test, detail in result.errors],
                          skipped=[{"id": test.id(), "reason": reason} for test, reason in result.skipped],
                          unexpected_successes=[test.id() for test in result.unexpectedSuccesses],
                          expected_failures=[{"id": test.id(), "traceback": detail}
                                             for test, detail in result.expectedFailures])
        if report["source_commit"] is not None:
            try:
                final_commit, final_dirty = git_provenance()
                report["source_dirty"] = report["source_dirty"] or final_dirty
                if final_commit != report["source_commit"]:
                    raise ValueError("Git commit changed during the test run")
            except Exception:
                report["status"] = "runner_error"
                report["runner_errors"].append(traceback.format_exc())
        report["elapsed_seconds"] = round(max(0.0, _timer() - started), 6)
        write_report(output, report)
    return 0 if report["status"] == "passed" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tests-dir", type=Path, default=ROOT / "poc" / "tests")
    parser.add_argument("--source-commit", help="Expected git HEAD; actual provenance is always read from git")
    parser.add_argument("--stack-dump-interval", type=int, default=120,
                        help="Seconds between diagnostic thread stacks; 0 disables them")
    args = parser.parse_args(argv)
    return run_regressions(tests_dir=args.tests_dir, shard_index=args.shard_index,
                           shard_count=args.shard_count, output=args.output,
                           expected_commit=args.source_commit,
                           stack_dump_interval=args.stack_dump_interval)


if __name__ == "__main__":
    raise SystemExit(main())
