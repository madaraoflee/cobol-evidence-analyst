"""A clean checkout must still match the immutable repository's raw blobs."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import online_analysis
from online_material_format import MaterialError
from online_material_query import MaterialQuery
from online_material_reader import DirectoryObjectStore, MaterialReader


SOURCE = b"IDENTIFICATION DIVISION.\nPROGRAM-ID. MAIN-ENTRY.\nPROCEDURE DIVISION.\nDISPLAY 'LOCAL'.\nGOBACK.\n"


class OnlineGitBlobIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def git(self, root, *arguments):
        result = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        return result.stdout

    def repository(self, name, *, object_format="sha1", raw=SOURCE, subdirectory=""):
        root = self.root / name
        root.mkdir()
        self.git(root, "init", "-q", "--object-format=" + object_format)
        self.git(root, "config", "core.autocrlf", "false")
        self.git(root, "config", "core.safecrlf", "false")
        self.git(root, "config", "commit.gpgsign", "false")
        self.git(root, "config", "user.name", "Test User")
        self.git(root, "config", "user.email", "test@example.invalid")
        source = root / subdirectory
        source.mkdir(parents=True, exist_ok=True)
        (source / "main.cbl").write_bytes(raw)
        self.commit(root)
        return root, source

    def commit(self, root):
        self.git(root, "add", ".")
        self.git(root, "commit", "-qm", "fixture")
        return self.git(root, "rev-parse", "HEAD").decode("ascii").strip()

    def build(self, root, source, destination, *, subdirectory=""):
        commit = self.git(root, "rev-parse", "HEAD").decode("ascii").strip()
        out, error = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(error):
            status = online_analysis.main(["build", "--source", str(source), "--output", str(destination),
                "--repository", "example/source", "--commit", commit, "--subdirectory", subdirectory,
                "--source-format", "free"])
        return status, json.loads(out.getvalue() or error.getvalue())

    def test_autocrlf_clean_worktree_is_rejected_when_commit_blob_has_different_bytes(self):
        root, source = self.repository("autocrlf")
        self.git(root, "config", "core.autocrlf", "true")
        path = source / "main.cbl"
        path.write_bytes(SOURCE.replace(b"\n", b"\r\n"))
        self.git(root, "diff", "--quiet", "HEAD")
        before = path.read_bytes()
        destination = self.root / "rejected-material"
        status, result = self.build(root, source, destination)
        self.assertEqual((status, result.get("error")), (2, "MATERIAL_CHECKOUT_CONTENT_MISMATCH"))
        self.assertFalse(destination.exists())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.git(root, "show", "HEAD:main.cbl"), SOURCE)

    def test_custom_clean_filter_cannot_certify_transformed_worktree_bytes(self):
        root, source = self.repository("filtered")
        transform = self.root / "normalize_fixture.py"
        transform.write_bytes(b"import sys\nsys.stdout.buffer.write(sys.stdin.buffer.read().replace(b'LOCAL', b'CANON'))\n")
        command = '"' + Path(sys.executable).as_posix() + '" "' + transform.as_posix() + '"'
        self.git(root, "config", "filter.fixture.clean", command)
        (root / ".gitattributes").write_bytes(b"*.cbl filter=fixture\n")
        self.commit(root)
        self.git(root, "diff", "--quiet", "HEAD")
        committed = self.git(root, "show", "HEAD:main.cbl")
        self.assertEqual(committed, SOURCE.replace(b"LOCAL", b"CANON"))
        self.assertEqual((source / "main.cbl").read_bytes(), SOURCE)
        destination = self.root / "filtered-material"
        status, result = self.build(root, source, destination)
        self.assertEqual((status, result.get("error")), (2, "MATERIAL_CHECKOUT_CONTENT_MISMATCH"))
        self.assertFalse(destination.exists())

    def test_matching_lf_and_crlf_blobs_build_and_verify_for_sha1_and_sha256(self):
        for object_format in ("sha1", "sha256"):
            for newlines in ("lf", "crlf"):
                with self.subTest(object_format=object_format, newlines=newlines):
                    raw = SOURCE if newlines == "lf" else SOURCE.replace(b"\n", b"\r\n")
                    name = object_format + "-" + newlines
                    root, source = self.repository(name, object_format=object_format, raw=raw,
                                                   subdirectory="selected scope")
                    destination = self.root / (name + "-material")
                    status, result = self.build(root, source, destination, subdirectory="selected scope")
                    self.assertEqual(status, 0, result)
                    self.assertEqual(result["manifest"]["analysis"]["commit_verification"], "git_checked")
                    blob = self.git(root, "show", "HEAD:selected scope/main.cbl")
                    self.assertEqual(blob, raw)
                    query = MaterialQuery(MaterialReader(DirectoryObjectStore(destination)))
                    evidence = query.evidence("main.cbl", 1, 5, lambda path, **options: blob)
                    self.assertEqual(evidence["text"], SOURCE.decode("utf-8").rstrip("\n"))

    def test_verification_uses_one_tree_query_for_many_selected_files(self):
        root, source = self.repository("many")
        paths = ["main.cbl"]
        for number in range(1000):
            name = f"record-{number:04}.cpy"
            (source / name).write_bytes(b"01 SHARED-AMOUNT PIC 9(5).\n")
            paths.append(name)
        commit = self.commit(root)
        with mock.patch.object(online_analysis, "_git", wraps=online_analysis._git) as commands:
            online_analysis._verify_checkout(source, commit, "", paths)
        calls = [call.args[1:] for call in commands.call_args_list]
        self.assertLessEqual(len(calls), 4)
        self.assertEqual(sum(call[0] == "ls-tree" for call in calls), 1)
        self.assertFalse(any(call[0] in {"show", "cat-file", "hash-object"} for call in calls))

    def test_large_source_blob_is_hashed_with_bounded_reads(self):
        raw = SOURCE + b"*> source padding\n" * 150000
        root, source = self.repository("large", raw=raw)
        commit = self.git(root, "rev-parse", "HEAD").decode("ascii").strip()
        original = Path.open
        reads = []
        outer = self
        class BoundedStream:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *arguments):
                self.stream.close()
            def fileno(self):
                return self.stream.fileno()
            def read(self, size=-1):
                outer.assertGreater(size, 0)
                outer.assertLessEqual(size, 1024 * 1024)
                reads.append(size)
                return self.stream.read(size)
        def opened(path, *arguments, **kwargs):
            stream = original(path, *arguments, **kwargs)
            return BoundedStream(stream) if path == source / "main.cbl" else stream
        with mock.patch.object(Path, "open", opened):
            online_analysis._verify_checkout(source, commit, "", ["main.cbl"])
        self.assertGreaterEqual(len(reads), 3)


if __name__ == "__main__":
    unittest.main()
