"""Publication CLI checks using a synthetic Git repository and in-memory storage."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from online_distribution import DownloadsObjectStore, load_release
from online_material_format import canonical_json
from online_material_reader import MaterialReader
from online_publish import main, parser
from test_online_distribution import MemoryClient


class SnapshotPublishTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.checkout = self.root / "checkout"
        self.source = self.checkout / "src"
        self.source.mkdir(parents=True)
        (self.source / "entry.cbl").write_text(
            'IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRY.\nPROCEDURE DIVISION.\n'
            'DISPLAY "SNAPSHOT-MARKER".\nGOBACK.\n', encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.checkout)], check=True)
        subprocess.run(["git", "-C", str(self.checkout), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.checkout), "-c", "user.name=Test User",
                        "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
                        "commit", "-qm", "fixture"], check=True)
        self.commit = subprocess.check_output(
            ["git", "-C", str(self.checkout), "rev-parse", "HEAD"]).decode().strip()
        self.output = self.root / "material"

    def invoke(self, *arguments):
        out, error = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(error):
            status = main(list(arguments))
        return status, json.loads(out.getvalue() or error.getvalue())

    def prepare(self, commit=None, *extra):
        return self.invoke("prepare", "--source", str(self.source), "--output", str(self.output),
            "--repository-snapshot", "--source-repository", "example/source", "--commit", commit or self.commit,
            "--subdirectory", "src", "--source-format", "free", *extra)

    def test_prepare_publish_and_read_snapshot_without_sync_integration(self):
        status, result = self.prepare()
        self.assertEqual(status, 0, result)
        self.assertEqual(result["manifest"]["analysis"]["commit_verification"], "git_checked")
        self.assertEqual(result["source_snapshot"]["sync_completeness"], "not_verified")
        self.assertNotIn("sync_receipt", result)
        client = MemoryClient()
        shutil.rmtree(self.checkout)
        with mock.patch("online_publish._client", return_value=client):
            status, published = self.invoke("publish", "--material", str(self.output), "--repository-snapshot",
                "--workspace", "example", "--repository", "materials")
        self.assertEqual(status, 0, published)
        descriptor = load_release(client, published["release_name"])
        self.assertEqual(descriptor["repository"]["commit"], self.commit)
        reader = MaterialReader(DownloadsObjectStore(client, descriptor["revision"], release=descriptor))
        self.assertEqual(reader.postings("snapshot-marker"), ["entry.cbl"])
        self.assertFalse(list(self.root.rglob("*.sqlite")))

    def test_snapshot_prepare_rejects_wrong_commit_dirty_scope_and_untracked_source(self):
        status, result = self.prepare("b" * 40)
        self.assertEqual((status, result["error"]), (2, "MATERIAL_CHECKOUT_MISMATCH"))
        self.assertFalse(self.output.exists())
        path = self.source / "entry.cbl"
        original = path.read_bytes()
        path.write_bytes(original + b"*> changed\n")
        status, result = self.prepare()
        self.assertEqual((status, result["error"]), (2, "MATERIAL_CHECKOUT_MISMATCH"))
        self.assertFalse(self.output.exists())
        path.write_bytes(original)
        (self.source / "untracked.cbl").write_bytes(original.replace(b"ENTRY", b"UNTRACKED"))
        status, result = self.prepare()
        self.assertEqual((status, result["error"]), (2, "MATERIAL_UNTRACKED_SOURCE"))
        self.assertFalse(self.output.exists())

    def test_snapshot_identity_is_required_before_build(self):
        for missing in ("--source-repository", "--commit"):
            arguments = ["prepare", "--source", str(self.source), "--output", str(self.output),
                         "--repository-snapshot", "--source-repository", "example/source", "--commit", self.commit]
            index = arguments.index(missing)
            del arguments[index:index + 2]
            status, result = self.invoke(*arguments)
            self.assertEqual((status, result["error"]), (2, "DISTRIBUTION_SNAPSHOT_IDENTITY_REQUIRED"))
            self.assertFalse(self.output.exists())

    def test_prepare_legacy_receipt_remains_supported_and_never_ignores_snapshot_identity(self):
        receipt = {"schema_version": "source-sync/v1", "repository": "example/source", "commit": self.commit,
            "subdirectory": "src", "complete": True, "completed_at": "2026-11-07T00:00:00Z", "sync_id": "weekly-001"}
        path = self.root / "receipt.json"
        path.write_bytes(canonical_json(receipt))
        arguments = ["prepare", "--source", str(self.source), "--output", str(self.output),
                     "--sync-receipt", str(path), "--source-format", "free"]
        status, result = self.invoke(*arguments, "--commit", "b" * 40)
        self.assertEqual((status, result["error"]), (2, "DISTRIBUTION_INVALID_PUBLICATION_SOURCE"))
        status, result = self.invoke(*arguments)
        self.assertEqual(status, 0, result)
        self.assertEqual(result["sync_receipt"], receipt)
        self.assertNotIn("source_snapshot", result)

    def test_cli_requires_exactly_one_publication_mode(self):
        for arguments in (["prepare", "--source", str(self.source), "--output", str(self.output)],
                          ["publish", "--material", str(self.output), "--workspace", "example", "--repository", "materials"]):
            for flags in ([], ["--repository-snapshot", "--sync-receipt", "receipt.json"]):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    parser().parse_args(arguments + flags)
                self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
