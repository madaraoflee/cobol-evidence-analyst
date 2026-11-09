"""Execute the regression reporter through an encoded cross-platform file URL."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class BugReporterPortabilityTests(unittest.TestCase):
    def test_reporter_file_url_handles_spaces_and_fragment_characters(self):
        with tempfile.TemporaryDirectory(prefix="reporter path ") as directory:
            root = Path(directory)
            reporter = root / "reporter # isolated.cjs"
            shutil.copyfile(ROOT / "tools/bug_browser_reporter.cjs", reporter)
            case = root / "sample.test.cjs"
            case.write_bytes(b"const test=require('node:test');test('executed',()=>{});test.skip('deferred',()=>{});\n")
            result = subprocess.run(["node", "--test", "--test-reporter=" + reporter.as_uri(), str(case)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            events = [json.loads(line) for line in result.stdout.splitlines()]
            passed = [event["data"] for event in events if event["type"] == "test:pass"]
            self.assertEqual({row["name"] for row in passed}, {"executed", "deferred"})
            self.assertTrue(next(row for row in passed if row["name"] == "deferred")["skip"])
            summary = [event["data"] for event in events if event["type"] == "test:summary"][-1]
            self.assertEqual(summary["counts"]["passed"], 1)
            self.assertEqual(summary["counts"]["skipped"], 1)


if __name__ == "__main__":
    unittest.main()
