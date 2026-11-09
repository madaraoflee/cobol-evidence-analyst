"""The synthetic pipeline runs unchanged through native and Windows shells."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import test_online_pipeline_template as fixture


class PipelinePortabilityTests(unittest.TestCase):
    def test_windows_uses_discovered_git_bash_without_a_posix_absolute_executable(self):
        with mock.patch.object(fixture.shutil, "which", side_effect=lambda name: {
            "git": None, "bash": "C:/Program Files/Git/bin/bash.exe"}.get(name)):
            self.assertEqual(fixture.template_shell(windows=True), "C:/Program Files/Git/bin/bash.exe")

    def test_missing_shell_is_a_failure_and_never_a_skip(self):
        with mock.patch.object(fixture.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "requires a POSIX shell"):
                fixture.template_shell(windows=True)

    def test_windows_paths_keep_spaces_and_use_shell_compatible_separators(self):
        self.assertEqual(fixture.shell_path(r"C:\Program Files\Python\python.exe", windows=True),
                         "C:/Program Files/Python/python.exe")
        self.assertEqual(fixture.shell_path("/tmp/source with spaces", windows=False), "/tmp/source with spaces")

    def test_shell_receives_unmodified_template_as_lf_bytes(self):
        script = fixture.script_block()
        response = subprocess.CompletedProcess(["shell"], 0, b"result\n", b"")
        with mock.patch.object(fixture, "template_shell", return_value="discovered-shell"), \
                mock.patch.object(fixture.subprocess, "run", return_value=response) as run:
            result = fixture.run_template_script(script, cwd="checkout", environment={})
        arguments, options = run.call_args
        self.assertEqual(arguments[0], ["discovered-shell"])
        self.assertIsInstance(options["input"], bytes)
        self.assertTrue(options["input"].endswith(script.encode("utf-8")))
        self.assertNotIn(b"\r", options["input"])
        self.assertNotIn("text", options)
        self.assertEqual(result.stdout, "result\n")

    def test_actual_shell_runs_python_wrapper_with_spaces_and_literal_arguments(self):
        with tempfile.TemporaryDirectory(prefix="pipeline portability ") as directory:
            root = Path(directory)
            wrapper = root / "wrapper with spaces.py"
            wrapper.write_text("import json,sys;print(json.dumps(sys.argv[1:]))\n", encoding="utf-8", newline="\n")
            environment = dict(os.environ, PIPELINE_TEST_PYTHON=fixture.shell_path(sys.executable),
                               PIPELINE_TEST_WRAPPER=fixture.shell_path(wrapper))
            result = fixture.run_template_script("python 'two words' 'literal $value'\n",
                                                  cwd=root, environment=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["two words", "literal $value"])


if __name__ == "__main__":
    unittest.main()
