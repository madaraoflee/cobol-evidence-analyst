"""Build provenance and Windows workflow must bind acceptance to the final EXE."""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("desktop_build_contract", ROOT / "build_app.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
from tools import desktop_acceptance


class DesktopBuildIdentityTests(unittest.TestCase):
    def test_git_identity_preserves_dirty_status_and_rejects_missing_or_invalid_commit(self):
        for output, expected in ((["a" * 40 + "\n", ""], {"source_commit": "a" * 40, "source_dirty": False}),
                (["a" * 40 + "\n", " M poc/module.py\n"], {"source_commit": "a" * 40, "source_dirty": True}),
                (["not-a-commit\n", ""], {"source_commit": None, "source_dirty": True})):
            with self.subTest(output=output), mock.patch.object(builder.subprocess, "check_output", side_effect=output):
                self.assertEqual(builder.source_identity(ROOT), expected)
        for error in (OSError("git unavailable"), subprocess.CalledProcessError(128, "git")):
            with self.subTest(error=type(error)), mock.patch.object(builder.subprocess, "check_output", side_effect=error):
                self.assertEqual(builder.source_identity(ROOT), {"source_commit": None, "source_dirty": True})

    def test_build_records_initial_identity_before_staging_or_compiling(self):
        identity = {"source_commit": "a" * 40, "source_dirty": False}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def stage(destination):
                self.assertEqual(json.loads((destination.parent / "source-identity.json").read_text()), identity)
                destination.mkdir()
                return destination
            with mock.patch.object(builder, "BUILD_ROOT", root), \
                    mock.patch.object(builder, "source_identity", return_value=identity), \
                    mock.patch.object(builder, "target_label", return_value="windows-x64"), \
                    mock.patch.object(builder, "stage_sources", side_effect=stage) as staged, \
                    mock.patch.object(builder, "collect_licenses"), \
                    mock.patch.object(builder, "run_checked", side_effect=RuntimeError("stop before compilation")), \
                    mock.patch("sys.stdout", new_callable=io.StringIO):
                with self.assertRaisesRegex(RuntimeError, "stop before compilation"):
                    builder.build(argparse.Namespace(jobs=1, output_dir=root / "delivery"))
            staged.assert_called_once()

    def _finish(self, start, end):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "run"
            source = run / "source"
            product = run / "compiled/desktop_app.dist"
            product.mkdir(parents=True)
            source.mkdir()
            (product / "COBOLWorkbench.exe").write_bytes(b"synthetic native binary")
            (source / "module.py").write_text("pass\n")
            if start is not None:
                (run / "source-identity.json").write_text(json.dumps(start))
            certificate = root / "certificate.txt"
            certificate.write_text("public certificate fixture")
            def self_test(command, **options):
                self.assertIn("--self-test", command)
                Path(command[command.index("--self-test-result") + 1]).write_text(json.dumps({"ok": True}))
            with mock.patch.object(builder.sys, "platform", "win32"), \
                    mock.patch.object(builder, "source_identity", return_value=end), \
                    mock.patch.object(builder, "minimum_macos_version", return_value=None), \
                    mock.patch.object(builder, "run_checked", side_effect=self_test), \
                    mock.patch.object(builder.importlib.metadata, "distributions", return_value=[]), \
                    mock.patch.dict(sys.modules, {"certifi": SimpleNamespace(where=lambda: str(certificate))}), \
                    mock.patch("sys.stdout", new_callable=io.StringIO):
                archive = builder.finish_delivery(argparse.Namespace(output_dir=root / "delivery"),
                    source, run / "compiled", run, "windows-x64")
            with zipfile.ZipFile(archive) as package:
                return json.loads(package.read("COBOLWorkbench/BUILD-INFO.json"))

    def test_changed_commit_or_dirty_state_at_end_cannot_look_like_clean_start(self):
        initial = {"source_commit": "a" * 40, "source_dirty": False}
        for final in ({"source_commit": "b" * 40, "source_dirty": False},
                {"source_commit": "a" * 40, "source_dirty": True},
                {"source_commit": None, "source_dirty": True}):
            with self.subTest(final=final):
                metadata = self._finish(initial, final)
                self.assertEqual(metadata["source_commit"], initial["source_commit"])
                self.assertTrue(metadata["source_dirty"])
                self.assertTrue(metadata["source_identity_changed_during_build"])
                self.assertEqual(metadata["release_status"], "candidate_unaccepted")
        unchanged = self._finish(initial, initial)
        self.assertFalse(unchanged["source_dirty"])
        self.assertNotIn("source_identity_changed_during_build", unchanged)

    def test_missing_build_start_record_cannot_borrow_current_clean_identity(self):
        metadata = self._finish(None, {"source_commit": "a" * 40, "source_dirty": False})
        self.assertIsNone(metadata["source_commit"])
        self.assertTrue(metadata["source_dirty"])
        self.assertTrue(metadata["source_identity_missing_at_build_start"])


class WindowsAcceptanceWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / ".github/workflows/windows-desktop.yml").read_text(encoding="utf-8")
        starts = list(re.finditer(r"^      - name: (.+)$", self.text, re.M))
        self.steps = [(match.group(1), self.text[match.end():starts[index + 1].start() if index + 1 < len(starts) else None])
            for index, match in enumerate(starts)]

    def step(self, phrase):
        return next((index, body) for index, (name, body) in enumerate(self.steps) if phrase in name)

    def test_mandatory_public_corpus_precedes_tests_and_final_exe_gate_precedes_upload(self):
        public, public_body = self.step("mandatory frozen public corpus")
        tests, _ = self.step("offline Python regressions")
        compile_index, compile_body = self.step("Compile with MSVC")
        gate, gate_body = self.step("Exercise final EXE")
        artifact, artifact_body = self.step("Upload only the accepted candidate")
        self.assertLess(public, tests)
        self.assertLess(tests, compile_index)
        self.assertLess(compile_index, gate)
        self.assertLess(gate, artifact)
        self.assertIn("manifest['files']", public_body)
        self.assertIn("hashlib.sha256", public_body)
        self.assertIn("check=True", public_body)
        self.assertNotIn("continue-on-error", public_body + gate_body + artifact_body)
        self.assertNotRegex(artifact_body, r"\bif:\s*(?:always\(\)|failure\(\))")
        self.assertIn("python build_app.py --skip-install", compile_body)
        self.assertIn("'--archive', str(archive)", gate_body)
        self.assertNotIn("--source-root", gate_body)
        self.assertLess(gate_body.index("tools/desktop_acceptance.py"), gate_body.index("tools/collect_desktop_candidate.py"))
        self.assertEqual(gate_body.count("check=True"), 2)
        for artifact_name in ("COMPILED-ACCEPTANCE.json", "CANDIDATE-GATE.json", "ACCEPTANCE-MANIFEST.json", "CANDIDATE-POLICY.json"):
            self.assertIn("windows-delivery/" + artifact_name, artifact_body)
        self.assertIn("if-no-files-found: error", artifact_body)

    def test_workflow_has_no_formal_release_or_unconditional_binary_upload(self):
        self.assertRegex(self.text, r"permissions:\s*\n\s+contents: read")
        self.assertNotRegex(self.text, r"(?i)gh\s+release|(?<![A-Za-z0-9_])publish_desktop_release\.py|action-gh-release|contents:\s*write")
        _, failure = self.step("failure diagnostics")
        self.assertIn("if: failure()", failure)
        self.assertNotIn(".zip", failure)
        self.assertNotIn("windows-delivery", failure)

    def test_context_lint_precedes_native_build_and_corpus_path_is_step_runtime(self):
        self.assertIn("needs: workflow_lint", self.text)
        self.assertIn("actionlint/cmd/actionlint@v1.7.7", self.text)
        self.assertIn("Path(os.environ['RUNNER_TEMP']) / 'public-business-source'", self.text)
        self.assertIn("PUBLIC_BUSINESS_SOURCE_ROOT={target}", self.text)
        self.assertNotIn("PUBLIC_BUSINESS_SOURCE_ROOT: ${{ runner.temp }}", self.text)
        first, fast = self.step("Fail fast")
        full, _ = self.step("offline Python regressions")
        self.assertLess(first, full)
        self.assertIn("test_desktop_acceptance_runtime.py", fast)
        self.assertIn("test_publish_desktop_release.py", fast)

    def test_python_workflow_blocks_compile_without_shell_expansion_or_posix_tools(self):
        for name, body in self.steps:
            if "shell: python" not in body:
                continue
            code = body.split("run: |\n", 1)[1]
            source = "\n".join(line[10:] if line.startswith("          ") else line for line in code.splitlines())
            ast.parse(source, filename=name)
            self.assertNotIn("/usr/bin/", source)
            self.assertNotIn("os.killpg", source)

    def test_direct_tools_entrypoint_imports_in_isolated_python(self):
        script = ("import sys;sys.path.insert(0,sys.argv[1]);import desktop_acceptance;"
            "import tools.desktop_acceptance_scenarios;import tools.desktop_acceptance_cases")
        environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")}
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-I", "-c", script, str(ROOT / "tools")],
                cwd=directory, env=environment, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_runner_dispatches_extracted_exe_not_source_python(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive, manifest = root / "candidate.zip", root / "manifest.json"
            archive.write_bytes(b"package inspected by separate package-contract tests")
            manifest.write_text(json.dumps({"cases": [{"id": "claim_repair", "owner": "runtime",
                "repeats": 1, "assertions": ["quality"], "limits": {"question_seconds": 5}}]}))
            native = root / "extracted/COBOLWorkbench.exe"
            native.parent.mkdir()
            native.write_bytes(b"native dispatch fixture")
            seen = []
            def exercise(command, output, selected):
                seen.append(command)
                return [{"id": "claim_repair", "turn_id": "1", "sample_index": 1,
                    "run_id": "native-run", "elapsed_seconds": 1, "index_reused": False, "question_index_reused": True,
                    "answer_sha256": "a" * 64, "checks": {"quality": True}, "timings": {"question_seconds": 1}}]
            scenarios = SimpleNamespace(run_scenarios=exercise)
            cases = SimpleNamespace(run_cases=lambda *args, **kwargs: self.fail("Unexpected case runner"))
            provenance = {"binary_sha256": hashlib.sha256(native.read_bytes()).hexdigest(),
                "execution_surface": "native_windows_http", "source_commit": "a" * 40, "source_dirty": False,
                "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "binary_path": "COBOLWorkbench/COBOLWorkbench/COBOLWorkbench.exe",
                "application_sha256": "b" * 64, "runtime_platform": "Windows"}
            with mock.patch.object(desktop_acceptance.sys, "platform", "win32"), \
                    mock.patch.object(desktop_acceptance, "inspect_package", return_value=provenance), \
                    mock.patch.object(desktop_acceptance, "extract_package", return_value=native), \
                    mock.patch.dict(sys.modules, {"desktop_acceptance_scenarios": scenarios,
                        "desktop_acceptance_cases": cases}), mock.patch("sys.stdout", new_callable=io.StringIO):
                result = desktop_acceptance.main(["--archive", str(archive), "--manifest", str(manifest),
                    "--source-commit", "a" * 40, "--public-source", str(root), "--output", str(root / "report")])
            report = json.loads((root / "report/COMPILED-ACCEPTANCE.json").read_text())
            self.assertEqual(result, 0, report)
            self.assertEqual(seen, [[str(native)]])
            self.assertEqual(report["execution_surface"], "native_windows_http")
            self.assertEqual(report["binary_sha256"], provenance["binary_sha256"])


if __name__ == "__main__":
    unittest.main()
