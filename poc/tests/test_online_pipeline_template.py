"""Execute the inactive producer template with synthetic source and local publication."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


PROJECT = Path(__file__).resolve().parents[2]
TEMPLATE = PROJECT / "docs/templates/online-material-pipeline.yml.example"


def script_block():
    text = TEMPLATE.read_text(encoding="utf-8")
    marker = "            - |\n"
    if text.count(marker) != 1:
        raise AssertionError("Expected one executable shell block in the inactive template")
    lines = text.partition(marker)[2].splitlines()
    if any(line and not line.startswith(" " * 14) for line in lines):
        raise AssertionError("Unexpected content outside the template shell block")
    return "\n".join(line[14:] for line in lines) + "\n"


def template_shell(*, windows=None):
    windows = os.name == "nt" if windows is None else windows
    if windows:
        git = shutil.which("git")
        if git:
            for base in (Path(git).parent.parent, Path(git).parent.parent.parent):
                candidate = base / "bin/bash.exe"
                if candidate.is_file():
                    return str(candidate)
        shell = shutil.which("bash")
    else:
        shell = shutil.which("sh")
    if not shell:
        raise RuntimeError("The pipeline fixture requires a POSIX shell; use Git Bash on Windows")
    return shell


def shell_path(value, *, windows=None):
    windows = os.name == "nt" if windows is None else windows
    return str(value).replace("\\", "/") if windows else str(value)


def run_template_script(script, *, cwd, environment):
    # A shell function preserves the actual template and invokes the current
    # Python explicitly. Native Windows cannot execute a Python shebang wrapper.
    prefix = 'python() { "$PIPELINE_TEST_PYTHON" "$PIPELINE_TEST_WRAPPER" "$@"; }\n'
    completed = subprocess.run([template_shell()], input=(prefix + script).encode("utf-8"),
                               cwd=cwd, env=environment, capture_output=True, timeout=60)
    # Byte input avoids Windows text-mode CRLF conversion of the shell script.
    return subprocess.CompletedProcess(completed.args, completed.returncode,
                                       completed.stdout.decode("utf-8"), completed.stderr.decode("utf-8"))


class PipelineTemplateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="online-pipeline-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.checkout = self.root / "checkout with spaces"
        self.scope = "source members"
        self.source = self.checkout / self.scope
        self.source.mkdir(parents=True)
        program = 'IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nPROCEDURE DIVISION.\nGOBACK.\n'
        (self.source / "entry.cbl").write_text(program.format(name="ENTRY"), encoding="utf-8")
        (self.source / "MEMBER").write_text(program.format(name="MEMBER"), encoding="utf-8")
        (self.source / "notes.md").write_text("Synthetic non-source fixture.\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.checkout)], check=True)
        subprocess.run(["git", "-C", str(self.checkout), "config", "core.autocrlf", "false"], check=True)
        subprocess.run(["git", "-C", str(self.checkout), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.checkout), "-c", "user.name=Test User",
                        "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
                        "commit", "-qm", "synthetic source"], check=True)
        self.commit = subprocess.check_output(
            ["git", "-C", str(self.checkout), "rev-parse", "HEAD"], text=True).strip()
        self.tool_dir = self.checkout / "tooling/analysis/poc"
        self.tool_dir.parent.mkdir(parents=True)
        result = subprocess.run([sys.executable, str(PROJECT / "poc/online_deployment.py"),
                                 "stage", "--output", str(self.tool_dir)],
                                capture_output=True, text=True, cwd=self.root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.output = self.root / "local publication"
        self.output.mkdir()
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.wrapper = self.bin_dir / "python-wrapper.py"
        self.wrapper.write_text(textwrap.dedent('''\
            import json
            import os
            from pathlib import Path
            import runpy
            import socket
            import sys

            destination = Path(os.environ["PIPELINE_TEST_OUTPUT"])
            with (destination / "calls.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(sys.argv[1:]) + "\\n")
            target, arguments = Path(sys.argv[1]), sys.argv[2:]
            sys.path.insert(0, str(target.parent))
            sys.argv = [str(target), *arguments]

            def reject_network(*args, **kwargs):
                raise AssertionError("Synthetic pipeline test must not access the network")

            socket.socket.connect = reject_network
            socket.socket.connect_ex = reject_network
            socket.create_connection = reject_network
            if target.name == "online_publish.py" and arguments[0] == "publish":
                import online_publish

                class LocalClient:
                    def put_immutable(self, name, data):
                        path = destination / name
                        uploaded = not path.exists()
                        if uploaded:
                            path.write_bytes(data)
                        if path.read_bytes() != data:
                            raise AssertionError("Immutable publication conflict")
                        return {"name": name, "uploaded": uploaded, "verified": True}

                online_publish._client = lambda *args, **kwargs: LocalClient()
                material = Path(arguments[arguments.index("--material") + 1])
                (destination / "prepared-material.json").write_bytes((material / "material.json").read_bytes())
                raise SystemExit(online_publish.main(arguments))
            runpy.run_path(str(target), run_name="__main__")
            '''), encoding="utf-8", newline="\n")

    def run_template(self, **changes):
        environment = dict(os.environ)
        environment.update({
            "BITBUCKET_CLONE_DIR": shell_path(self.checkout),
            "BITBUCKET_COMMIT": self.commit,
            "BITBUCKET_REPO_FULL_NAME": "example/source",
            "MATERIAL_WORKSPACE": "example",
            "MATERIAL_REPOSITORY": "materials",
            "ONLINE_PUBLISH_TOKEN": "synthetic-publication-token",
            "SOURCE_SUBDIRECTORY": self.scope,
            "SOURCE_ENCODING": "utf-8",
            "SOURCE_FORMAT": "free",
            "INCLUDE_EXTENSIONLESS": "0",
            "MINIMUM_FREE_BYTES": "0",
            "PIPELINE_TEST_OUTPUT": str(self.output),
            "PIPELINE_TEST_PYTHON": shell_path(sys.executable),
            "PIPELINE_TEST_WRAPPER": shell_path(self.wrapper),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        for key, value in changes.items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
        return run_template_script(script_block(), cwd=self.checkout, environment=environment)

    def assert_no_publisher(self):
        path = self.output / "calls.jsonl"
        calls = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        self.assertFalse(any(Path(call[0]).name == "online_publish.py" and call[1] == "publish"
                             for call in calls))
        self.assertFalse(list(self.output.glob("analysis-release-*.json")))

    def test_script_prepares_and_publishes_selected_extensionless_members(self):
        for include, count in (("0", 1), ("1", 2)):
            with self.subTest(include_extensionless=include):
                result = self.run_template(INCLUDE_EXTENSIONLESS=include)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                responses = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
                inventory = next(item for item in responses if item.get("status") == "inventory_checked")
                self.assertEqual(inventory["selected_files"], count)
                self.assertEqual(inventory["excluded_extensionless_file_count"], int(include == "0"))
                self.assertEqual(inventory["tracked_not_selected"], 3 - count)
                self.assertEqual(inventory["excluded_extensions"], {".md": 1})
                self.assertEqual(inventory["sync_completeness"], "not_verified")
                prepared = json.loads((self.output / "prepared-material.json").read_text())
                self.assertEqual(prepared["coverage"]["indexed_files"], count)
                self.assertEqual(prepared["repository"]["commit"], self.commit)
                published = responses[-1]
                self.assertEqual(published["publication"], "published")
                self.assertEqual(published["release"]["schema_version"], "online-release/v2")
                self.assertEqual(published["release"]["source_snapshot"]["sync_completeness"], "not_verified")
                calls = [json.loads(line) for line in (self.output / "calls.jsonl").read_text().splitlines()]
                runtime = next(call for call in reversed(calls) if call[1] == "runtime")
                temporary = Path(runtime[runtime.index("--temporary-directory") + 1])
                output = Path(runtime[runtime.index("--output-directory") + 1])
                self.assertNotIn(self.checkout, temporary.parents)
                self.assertNotIn(self.checkout, output.parents)
                self.assertFalse(temporary.parent.exists(), "Owned temporary data should be cleaned")

    def test_invalid_configuration_stops_before_publication(self):
        invalid = [
            {"INCLUDE_EXTENSIONLESS": None}, {"INCLUDE_EXTENSIONLESS": "yes"},
            {"SOURCE_ENCODING": None}, {"SOURCE_ENCODING": "not-a-real-codec"},
            {"SOURCE_FORMAT": "unknown"},
            {"MINIMUM_FREE_BYTES": "-1"}, {"MINIMUM_FREE_BYTES": None},
            {"SOURCE_SUBDIRECTORY": "../"}, {"SOURCE_SUBDIRECTORY": "tooling"},
            {"SOURCE_SUBDIRECTORY": "tooling/analysis/poc"},
            {"BITBUCKET_COMMIT": "b" * 40},
            {"MINIMUM_FREE_BYTES": "999999999999999999999999999999"},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                result = self.run_template(**changes)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assert_no_publisher()

    def test_modified_staged_source_fails_before_inventory_or_publication(self):
        target = self.tool_dir / "online_publish.py"
        target.write_text(target.read_text() + "\n# Synthetic mutation after staging.\n", encoding="utf-8")
        result = self.run_template()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_no_publisher()
        calls = [json.loads(line) for line in (self.output / "calls.jsonl").read_text().splitlines()]
        self.assertEqual([call[1] for call in calls], ["verify"])

    def test_template_remains_manual_source_only_example(self):
        text = TEMPLATE.read_text()
        self.assertEqual(TEMPLATE.suffix, ".example")
        self.assertIn("  custom:\n", text)
        self.assertIn("          max-time: 60\n", text)
        self.assertNotIn("  default:", text)
        self.assertNotIn("  branches:", text)
        self.assertNotIn("  pull-requests:", text)
        self.assertNotIn("SYNC_RECEIPT_JSON", text)
        self.assertNotIn("eval ", script_block())


if __name__ == "__main__":
    unittest.main()
