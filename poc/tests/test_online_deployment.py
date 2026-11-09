"""Producer source staging and local readiness checks use synthetic inputs only."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import online_deployment as deployment
from online_material_format import MaterialError, canonical_json, digest


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.output = self.root / "producer"

    def checkout(self):
        checkout = self.root / "checkout"
        source = checkout / "src"
        source.mkdir(parents=True)
        (source / "entry.cbl").write_text(
            'IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRY.\nPROCEDURE DIVISION.\n'
            'DISPLAY "ISOLATED-MARKER".\nGOBACK.\n', encoding="utf-8")
        (source / "NOTES").write_text("selection fixture", encoding="utf-8")
        (source / "readme.md").write_text("fixture", encoding="utf-8")
        ignored = source / "node_modules"
        ignored.mkdir()
        (ignored / "ignored.cbl").write_text("fixture", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
        subprocess.run(["git", "-C", str(checkout), "-c", "user.name=Test User",
                        "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
                        "commit", "-qm", "fixture"], check=True)
        commit = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"]).decode().strip()
        return checkout, source, commit

    def test_stage_has_exact_reproducible_source_files_and_no_runtime_data(self):
        first = deployment.stage(self.output)
        second = self.root / "second"
        self.assertEqual(first, deployment.stage(second))
        manifest = json.loads((self.output / deployment.MANIFEST).read_bytes())
        self.assertGreater(first["files"], 20)
        self.assertIn("business_index.py", manifest["files"])
        self.assertIn("company_api.py", manifest["files"])
        self.assertIn("online_preflight.py", manifest["files"])
        self.assertFalse(manifest["dynamic_imports_inferred"])
        self.assertEqual({path.name for path in self.output.iterdir()}, set(manifest["files"]) | {deployment.MANIFEST})
        self.assertFalse(any(path.suffix in {".exe", ".pyc", ".sqlite"} for path in self.output.rglob("*")))
        self.assertEqual(deployment.verify(self.output)["manifest_sha256"], first["manifest_sha256"])
        self.assertNotIn(str(self.root), json.dumps(first))

    def test_plain_staged_cli_verifies_without_creating_bytecode(self):
        deployment.stage(self.output)
        completed = subprocess.run([sys.executable, str(self.output / "online_deployment.py"),
                                    "verify", "--directory", str(self.output)],
                                   cwd=self.root, capture_output=True, timeout=30, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "sources_verified")
        self.assertFalse(list(self.output.rglob("*.pyc")))
        self.assertFalse((self.output / "__pycache__").exists())

    def test_staged_producer_builds_with_original_code_unavailable(self):
        deployment.stage(self.output)
        _, source, commit = self.checkout()
        material = self.root / "material"
        # Isolated mode removes PYTHONPATH and the original project. The only
        # explicit non-stdlib search location is the staged source directory.
        script = ("import sys; sys.path.insert(0,sys.argv.pop(1)); "
                  "import socket; socket.create_connection=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('network forbidden')); "
                  "from online_publish import main; raise SystemExit(main())")
        completed = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(self.output),
            "prepare", "--repository-snapshot", "--source-repository", "example/source", "--commit", commit,
            "--subdirectory", "src", "--source", str(source), "--source-format", "free", "--output", str(material)],
            cwd=self.root, capture_output=True, timeout=30, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["manifest"]["repository"]["commit"], commit)
        self.assertEqual(result["manifest"]["analysis"]["commit_verification"], "git_checked")
        self.assertEqual(result["source_snapshot"]["sync_completeness"], "not_verified")
        self.assertFalse(list(self.root.rglob("*.sqlite")))
        self.assertEqual(deployment.verify(self.output)["status"], "sources_verified")

    def test_verify_rejects_tamper_extra_missing_and_symlink(self):
        deployment.stage(self.output)
        path = self.output / "online_publish.py"
        original = path.read_bytes()
        path.write_bytes(original + b"\n# changed\n")
        with self.assertRaisesRegex(MaterialError, "SOURCE_HASH_MISMATCH"):
            deployment.verify(self.output)
        path.write_bytes(original)
        extra = self.output / ".env"
        extra.write_text("fixture", encoding="utf-8")
        with self.assertRaisesRegex(MaterialError, "FILE_SET_MISMATCH"):
            deployment.verify(self.output)
        extra.unlink()
        path.unlink()
        with self.assertRaisesRegex(MaterialError, "FILE_SET_MISMATCH"):
            deployment.verify(self.output)
        path.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(MaterialError, "SYMLINK_REJECTED"):
            deployment.verify(self.output)

    def test_manifest_cannot_hide_missing_static_dependency_or_add_unreachable_code(self):
        deployment.stage(self.output)
        path = self.output / deployment.MANIFEST
        manifest = json.loads(path.read_bytes())
        del manifest["files"]["business_index.py"]
        (self.output / "business_index.py").unlink()
        path.write_bytes(canonical_json(manifest))
        with self.assertRaisesRegex(MaterialError, "DEPENDENCY_MISSING"):
            deployment.verify(self.output)
        shutil.rmtree(self.output)
        deployment.stage(self.output)
        manifest = json.loads(path.read_bytes())
        raw = b"# unreferenced code\n"
        (self.output / "unreferenced.py").write_bytes(raw)
        manifest["files"]["unreferenced.py"] = {"size": len(raw), "sha256": digest(raw)}
        path.write_bytes(canonical_json(manifest))
        with self.assertRaisesRegex(MaterialError, "CLOSURE_MISMATCH"):
            deployment.verify(self.output)

    def test_stage_refuses_existing_overlapping_symlink_and_incomplete_inputs(self):
        self.output.mkdir()
        with self.assertRaisesRegex(MaterialError, "DESTINATION_EXISTS"):
            deployment.stage(self.output)
        self.output.rmdir()
        self.output.symlink_to(self.root / "absent")
        with self.assertRaisesRegex(MaterialError, "SYMLINK_REJECTED"):
            deployment.stage(self.output)
        self.output.unlink()
        source = Path(deployment.__file__).resolve().parent
        with self.assertRaisesRegex(MaterialError, "OVERLAPS_SOURCE"):
            deployment.stage(source / "never-created")
        missing = self.root / "incomplete"
        missing.mkdir()
        with self.assertRaisesRegex(MaterialError, "SOURCE_MISSING"):
            deployment.stage(self.output, source_directory=missing)
        self.assertFalse(self.output.exists())

    def test_stage_failure_removes_only_its_new_directory(self):
        marker = self.root / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        with mock.patch("online_deployment.verify", side_effect=MaterialError("DEPLOYMENT_FIXTURE_FAILURE")):
            with self.assertRaisesRegex(MaterialError, "FIXTURE_FAILURE"):
                deployment.stage(self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(marker.read_text(), "keep")

    def test_runtime_checks_both_storage_locations_and_states_floor_is_not_guarantee(self):
        second = self.root / "output"
        second.mkdir()
        before = sorted(self.root.iterdir())
        result = deployment.runtime(self.root, second, 0)
        self.assertEqual(result["status"], "runtime_checks_passed", result)
        self.assertFalse(result["resource_floor_is_capacity_guarantee"])
        self.assertIn("free_bytes", result["checks"]["temporary_storage"])
        self.assertIn("free_bytes", result["checks"]["output_storage"])
        self.assertEqual(before, sorted(self.root.iterdir()))
        self.assertNotIn(str(self.root), json.dumps(result))
        result = deployment.runtime(self.root, second, 2**63 - 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["checks"]["temporary_storage"]["error"], "DEPLOYMENT_FREE_SPACE_BELOW_FLOOR")
        self.assertEqual(result["checks"]["output_storage"]["error"], "DEPLOYMENT_FREE_SPACE_BELOW_FLOOR")

    def test_runtime_reports_missing_git_fts_and_storage_without_raw_paths(self):
        with mock.patch("online_deployment.subprocess.run", side_effect=FileNotFoundError("private path")), \
                mock.patch("online_deployment.sqlite3.connect", side_effect=sqlite3.OperationalError("private error")), \
                mock.patch("online_deployment.tempfile.TemporaryFile", side_effect=PermissionError("private path")):
            result = deployment.runtime(self.root, self.root, 0)
        self.assertEqual(result["status"], "failed")
        for name in ("git", "sqlite_fts5", "temporary_storage", "output_storage"):
            self.assertEqual(result["checks"][name]["status"], "failed")
        self.assertNotIn("private", json.dumps(result))

    def test_inventory_counts_selection_without_parsing_source_or_including_raw_text(self):
        _, source, commit = self.checkout()
        with mock.patch("pathlib.Path.read_bytes", side_effect=AssertionError("source body read")):
            result = deployment.inventory(source, commit, "src")
        self.assertEqual(result["git_tracked_files"], 4)
        self.assertEqual(result["selected_files"], 1)
        self.assertEqual(result["tracked_not_selected"], 3)
        self.assertEqual(result["excluded_extensionless_file_count"], 1)
        self.assertEqual(result["excluded_extensions"], {".md": 1})
        self.assertEqual(result["coverage_scope"], "discovery_selection_only")
        self.assertFalse(result["source_body_in_report"])
        self.assertFalse(result["source_content_parsed"])
        self.assertTrue(result["git_worktree_content_check"])
        self.assertEqual(result["sync_completeness"], "not_verified")
        self.assertNotIn(str(source), json.dumps(result))
        self.assertEqual(deployment.inventory(source, commit, "src", include_extensionless=True)["selected_files"], 2)

    def test_inventory_rejects_wrong_commit_dirty_files_and_untracked_scope(self):
        _, source, commit = self.checkout()
        with self.assertRaisesRegex(MaterialError, "CHECKOUT_MISMATCH"):
            deployment.inventory(source, "b" * 40, "src")
        path = source / "readme.md"
        path.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(MaterialError, "CHECKOUT_MISMATCH"):
            deployment.inventory(source, commit, "src")
        path.write_text("fixture", encoding="utf-8")
        (source / "untracked.txt").write_text("fixture", encoding="utf-8")
        with self.assertRaisesRegex(MaterialError, "UNTRACKED_SOURCE"):
            deployment.inventory(source, commit, "src")

    def test_cli_failure_uses_bounded_json_without_private_path(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = deployment.main(["verify", "--directory", str(self.root / "private-directory")])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue()), {"status": "failed", "error": "DEPLOYMENT_DIRECTORY_MISSING"})


if __name__ == "__main__":
    unittest.main()
