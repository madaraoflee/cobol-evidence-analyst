from __future__ import annotations

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
from online_analysis import main
from online_material_format import MaterialError, digest
from online_material_query import MaterialQuery
from online_material_reader import DirectoryObjectStore, MaterialReader


def program(name, body="GOBACK."):
    return ("IDENTIFICATION DIVISION.\nPROGRAM-ID. " + name + ".\n"
            "PROCEDURE DIVISION.\nMAIN-PARA.\n" + body + "\n")


class OnlineAnalysisIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.output = self.root / "materials"
        self.commit = "b" * 40

    def invoke(self, *args):
        out, error = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(error):
            status = main(list(args))
        return status, json.loads(out.getvalue() or error.getvalue())

    def build(self, *extra, unverified=True):
        return self.invoke("build", "--source", str(self.source), "--output", str(self.output),
                           "--repository", "example/source", "--commit", self.commit,
                           "--source-format", "free", *(["--unverified-source"] if unverified else []), *extra)

    def test_build_search_relations_and_verified_remote_evidence_without_source_directory(self):
        main_source = program("MAIN-ENTRY", 'CALL "WORKER-ENTRY".\nGOBACK.')
        (self.source / "main.cbl").write_text(main_source)
        (self.source / "worker.cbl").write_text(program("WORKER-ENTRY", 'DISPLAY "SPECIAL-MARKER".'))
        for index in range(60):
            (self.source / f"extra-{index}.cbl").write_text(program(f"EXTRA-{index}"))
        status, result = self.build()
        self.assertEqual(status, 0, result)
        manifest = result["manifest"]
        self.assertEqual(result["publication"], "not_published")
        self.assertEqual(manifest["analysis"]["commit_verification"], "caller_declared")
        revision = manifest["revision"]
        source_bytes = (self.source / "main.cbl").read_bytes()
        shutil.rmtree(self.source)

        reader = MaterialReader(DirectoryObjectStore(self.output), expected_revision=revision,
                                expected_repository="example/source", expected_commit=self.commit)
        self.assertEqual(reader.fetched_objects, 0)
        query = MaterialQuery(reader)
        matches = query.search("SPECIAL-MARKER")
        self.assertEqual([row["relative_path"] for row in matches["matches"]], ["worker.cbl"])
        incoming = query.incoming("WORKER-ENTRY")
        self.assertIn("main.cbl", [row["relative_path"] for row in incoming["matches"]])
        reads = []

        def remote_read(path, *, max_bytes):
            reads.append((path, max_bytes))
            return source_bytes

        evidence = query.evidence("main.cbl", 4, 6, remote_read)
        self.assertIn('CALL "WORKER-ENTRY".', evidence["text"])
        self.assertEqual(reads[0][0], "main.cbl")
        self.assertLess(reader.fetched_objects, len(list((self.output / "objects").iterdir())))
        self.assertFalse(list(self.root.rglob("*.sqlite")))
        with self.assertRaises(MaterialError):
            query.evidence("main.cbl", 4, 6, lambda *a, **kw: source_bytes + b" ")
        status, response = self.invoke("search", "--material", str(self.output),
                                       "--expected-revision", revision, "SPECIAL-MARKER")
        self.assertEqual(status, 0, response)
        self.assertEqual(len(response["matches"]), 1)
        self.assertGreater(response["transfer"]["fetched_objects"], 0)

    def test_file_larger_than_native_search_limit_is_searchable(self):
        source = program("LONG-ENTRY", "*> padding\n" * 60000 + 'DISPLAY "TAIL-SIGNAL".')
        self.assertGreater(len(source.encode()), 512 * 1024)
        (self.source / "long.cbl").write_text(source)
        status, result = self.build()
        self.assertEqual(status, 0, result)
        shutil.rmtree(self.source)
        status, result = self.invoke("search", "--material", str(self.output), "TAIL-SIGNAL")
        self.assertEqual(status, 0, result)
        self.assertEqual(result["matches"][0]["relative_path"], "long.cbl")

    def test_default_build_requires_actual_git_commit_and_does_not_overwrite(self):
        (self.source / "main.cbl").write_text(program("MAIN-ENTRY"))
        status, result = self.build(unverified=False)
        self.assertEqual(status, 2)
        self.assertFalse(self.output.exists())
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        subprocess.run(["git", "-C", str(self.source), "config", "core.autocrlf", "false"], check=True)
        subprocess.run(["git", "-C", str(self.source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.source), "-c", "user.name=Test User",
                        "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
                        "commit", "-qm", "fixture"], check=True)
        self.commit = subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"]).decode().strip()
        status, result = self.build(unverified=False)
        self.assertEqual(status, 0, result)
        self.assertEqual(result["manifest"]["analysis"]["commit_verification"], "git_checked")
        before = digest((self.output / "material.json").read_bytes())
        status, result = self.build(unverified=False)
        self.assertEqual(status, 2)
        self.assertEqual(result["error"], "MATERIAL_DESTINATION_EXISTS")
        self.assertEqual(before, digest((self.output / "material.json").read_bytes()))
        shutil.rmtree(self.output)
        (self.source / "extra.cbl").write_text(program("UNTRACKED-ENTRY"))
        status, result = self.build(unverified=False)
        self.assertEqual(status, 2)
        self.assertEqual(result["error"], "MATERIAL_UNTRACKED_SOURCE")
        self.assertFalse(self.output.exists())
        (self.source / "extra.cbl").unlink()
        (self.source / "main.cbl").write_text(program("CHANGED-ENTRY"))
        status, result = self.build(unverified=False)
        self.assertEqual(status, 2)
        self.assertEqual(result["error"], "MATERIAL_CHECKOUT_MISMATCH")
        self.assertFalse(self.output.exists())

    def test_evidence_cli_reads_pinned_subdirectory_and_keeps_credentials_out_of_output(self):
        source = program("MAIN-ENTRY")
        (self.source / "main.cbl").write_bytes(source.encode("utf-8"))
        status, result = self.build("--subdirectory", "src")
        self.assertEqual(status, 0, result)
        with mock.patch.dict("os.environ", {"ONLINE_SOURCE_TOKEN": "private-test-token"}), \
                mock.patch("online_source_client.PinnedSourceClient") as client:
            client.return_value.read_file.return_value = source.encode()
            status, result = self.invoke("evidence", "--material", str(self.output), "main.cbl", "1", "2")
        self.assertEqual(status, 0, result)
        self.assertNotIn("private-test-token", json.dumps(result))
        self.assertEqual(client.call_args.args, ("example", "source", self.commit))
        self.assertEqual(client.return_value.read_file.call_args.args, ("src/main.cbl",))
        self.assertEqual(result["transfer"]["source_requests"], 1)
        self.assertEqual(result["transfer"]["source_bytes"], len(source.encode()))


if __name__ == "__main__":
    unittest.main()
