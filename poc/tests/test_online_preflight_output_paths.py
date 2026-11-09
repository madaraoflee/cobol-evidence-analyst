"""Report leaf links are rejected before any platform file open or metadata read."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import online_preflight


class PreflightOutputPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="online-preflight-output-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "report.json"
        self.target = self.root / "target.json"

    def invoke(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = online_preflight.main([
                "check", "--source-repository", "example/source", "--commit", "a" * 40,
                "--material-repository", "example/materials", "--output", str(self.output),
            ])
        return code, json.loads(stdout.getvalue())

    def test_leaf_link_is_rejected_even_if_platform_exclusive_open_would_follow_it(self):
        original_open = Path.open
        opened = []
        def following_open(path, *args, **kwargs):
            opened.append(path)
            # Simulate a platform CRT that follows a dangling leaf link before
            # exclusively creating its nonexistent target.
            return original_open(self.target if path == self.output else path, *args, **kwargs)
        original_is_symlink = Path.is_symlink
        def leaf_is_symlink(path):
            return path == self.output or original_is_symlink(path)
        with mock.patch.object(Path, "is_symlink", leaf_is_symlink), \
                mock.patch.object(Path, "open", following_open), \
                mock.patch.object(online_preflight, "run_checks", return_value={"status": "read_checks_passed"}) as check:
            code, summary = self.invoke()
        self.assertEqual((code, summary), (2, {"status": "blocked", "error": "PREFLIGHT_REPORT_SYMLINK"}))
        check.assert_not_called()
        self.assertEqual(opened, [])
        self.assertFalse(self.target.exists())
        self.assertFalse(self.output.exists())

    def test_real_dangling_leaf_link_preserves_link_and_does_not_create_target(self):
        self.output.symlink_to(self.target)
        with mock.patch.object(online_preflight, "run_checks") as check:
            code, summary = self.invoke()
        self.assertEqual((code, summary), (2, {"status": "blocked", "error": "PREFLIGHT_REPORT_SYMLINK"}))
        check.assert_not_called()
        self.assertTrue(self.output.is_symlink())
        self.assertFalse(self.target.exists())

    def test_report_open_failure_is_not_swallowed_and_never_starts_checks(self):
        with mock.patch.object(Path, "open", side_effect=PermissionError("private output detail")), \
                mock.patch.object(online_preflight, "run_checks") as check:
            code, summary = self.invoke()
        self.assertEqual((code, summary), (2, {"status": "blocked", "error": "PREFLIGHT_REPORT_UNAVAILABLE"}))
        check.assert_not_called()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
