from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("desktop_build_under_test", PROJECT_ROOT / "build_app.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class DesktopBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def write(self, relative, content="fixture", *, root=None):
        path = (root or self.directory) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def source_tree(self):
        root = self.directory / "checkout"
        self.write("poc/desktop_app.py", "pass\n", root=root)
        self.write("poc/analysis.py", "pass\n", root=root)
        for name in builder.WEB_FILES:
            self.write("poc/web/" + name, root=root)
        self.write("poc/fixtures/framework-workbench/manifest.json", "{}", root=root)
        self.write("poc/fixtures/framework-workbench/source/programs/entry.cbl", root=root)
        self.write("poc/fixtures/framework-workbench/source/copybooks/record.cpy", root=root)
        return root

    def test_stage_excludes_developer_settings_state_tests_and_unselected_resources(self):
        root = self.source_tree()
        excluded = (
            ".env", "poc/.env", "poc/.env.production", "poc/.poc-data/history.sqlite",
            "poc/tests/test_private.py", "poc/__pycache__/analysis.pyc",
            "poc/web/private.json", "poc/web/secrets.key",
            "poc/fixtures/private/source.cbl",
            "poc/fixtures/framework-workbench/source/settings.json",
        )
        for name in excluded:
            self.write(name, "do-not-distribute", root=root)
        destination = builder.stage_sources(self.directory / "staged", root=root)
        actual = {p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()}
        expected = {"desktop_app.py", "analysis.py", *["web/" + name for name in builder.WEB_FILES],
                    "fixtures/framework-workbench/manifest.json",
                    "fixtures/framework-workbench/source/programs/entry.cbl",
                    "fixtures/framework-workbench/source/copybooks/record.cpy"}
        self.assertEqual(actual, expected)
        self.assertTrue(all("do-not-distribute" not in p.read_text() for p in destination.rglob("*") if p.is_file()))

    def test_resource_allowlist_contains_every_served_asset_and_its_license(self):
        module = ast.parse((PROJECT_ROOT / "poc/web_app.py").read_text(encoding="utf-8"))
        assignment = next(node for node in module.body if isinstance(node, ast.Assign)
                          and any(isinstance(name, ast.Name) and name.id == "STATIC_FILES" for name in node.targets))
        routes = ast.literal_eval(assignment.value)
        self.assertTrue(set(routes.values()).issubset(builder.WEB_FILES))
        self.assertIn("marked.LICENSE.md", builder.WEB_FILES)
        for name in builder.WEB_FILES:
            self.assertTrue((PROJECT_ROOT / "poc/web" / name).is_file(), name)

    def test_missing_required_web_resource_fails_before_compilation(self):
        root = self.source_tree()
        (root / "poc/web/index.html").unlink()
        with self.assertRaises(ValueError):
            builder.stage_sources(self.directory / "staged", root=root)

    def test_symlinked_source_file_is_rejected(self):
        root = self.source_tree()
        secret = self.write("outside.py", "do-not-distribute")
        (root / "poc/linked.py").symlink_to(secret)
        with self.assertRaises(ValueError):
            builder.stage_sources(self.directory / "staged", root=root)

    def test_symlinked_resource_parent_is_rejected(self):
        root = self.source_tree()
        web = root / "poc/web"
        external = self.directory / "external-web"
        web.rename(external)
        web.symlink_to(external, target_is_directory=True)
        with self.assertRaises(ValueError):
            builder.stage_sources(self.directory / "staged", root=root)

    def test_symlinked_fixture_parent_is_rejected(self):
        root = self.source_tree()
        fixture = root / "poc/fixtures/framework-workbench"
        external = self.directory / "external-fixture"
        fixture.rename(external)
        fixture.symlink_to(external, target_is_directory=True)
        with self.assertRaises(ValueError):
            builder.stage_sources(self.directory / "staged", root=root)

    def test_delivery_audit_rejects_source_configuration_keys_and_state(self):
        for index, name in enumerate(("module.py", "module.PYC", "settings/.env", "settings/.env.production",
                                      "tls/client.key", "tls/client.pem", "data/history.sqlite",
                                      ".poc-data/snapshot.json", "tests/fixture.txt")):
            with self.subTest(name=name):
                package = self.directory / str(index)
                self.write(name, root=package)
                with self.assertRaises(ValueError):
                    builder.audit_delivery(package)

    def test_delivery_audit_inspects_archive_contents_for_source_and_secrets(self):
        for index, name in enumerate(("module.py", "cache/module.pyc", "settings/.env", "tls/client.key",
                                      "data/history.sqlite", ".poc-data/state.json")):
            with self.subTest(name=name):
                package = self.directory / str(index)
                package.mkdir()
                with zipfile.ZipFile(package / "payload.zip", "w") as archive:
                    archive.writestr(name, "do-not-distribute")
                with self.assertRaises(ValueError):
                    builder.audit_delivery(package)

    def test_delivery_audit_rejects_links_outside_package(self):
        package = self.directory / "package"
        package.mkdir()
        outside = self.write("outside.txt")
        (package / "linked-resource").symlink_to(outside)
        with self.assertRaises(ValueError):
            builder.audit_delivery(package)

    def test_delivery_audit_inspects_nested_archives(self):
        package = self.directory / "package"
        package.mkdir()
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, "w") as archive:
            archive.writestr("private/.env", "do-not-distribute")
        with zipfile.ZipFile(package / "outer.zip", "w") as archive:
            archive.writestr("nested.zip", nested.getvalue())
        with self.assertRaises(ValueError):
            builder.audit_delivery(package)

    def test_public_certificate_exception_requires_exact_digest_and_location(self):
        package = self.directory / "package"
        certificate = self.write("certifi/cacert.pem", "public-ca-fixture", root=package)
        digest = hashlib.sha256(certificate.read_bytes()).hexdigest()
        builder.audit_delivery(package, public_ca_sha256=digest)
        certificate.write_text("private-key-fixture", encoding="utf-8")
        with self.assertRaises(ValueError):
            builder.audit_delivery(package, public_ca_sha256=digest)
        certificate.unlink()
        self.write("elsewhere/cacert.pem", "public-ca-fixture", root=package)
        with self.assertRaises(ValueError):
            builder.audit_delivery(package, public_ca_sha256=digest)

    def test_delivery_audit_allows_native_binaries_public_data_and_internal_links(self):
        package = self.directory / "package"
        resource = self.write("resources/license.txt", root=package)
        self.write("Contents/MacOS/COBOLWorkbench", root=package)
        self.write("runtime/library.dylib", root=package)
        self.write("web/app.js", root=package)
        self.write("source/example.cbl", root=package)
        (package / "license.txt").symlink_to(resource)
        with zipfile.ZipFile(package / "resources.zip", "w") as archive:
            archive.writestr("public/index.html", "public-resource")
        builder.audit_delivery(package)

    def test_targets_match_the_build_system_and_interpreter_architecture(self):
        for system, architecture, expected in (("win32", "AMD64", "windows-x64"),
                                                ("win32", "ARM64", "windows-arm64"),
                                                ("darwin", "x86_64", "macos-x64"),
                                                ("darwin", "aarch64", "macos-arm64")):
            with self.subTest(system=system, architecture=architecture):
                self.assertEqual(builder.target_label(system, architecture), expected)
        for system, architecture in (("linux", "x86_64"), ("win32", "i386")):
            with self.subTest(system=system, architecture=architecture), self.assertRaises(ValueError):
                builder.target_label(system, architecture)

    def test_platform_compiler_options_preserve_paths_with_spaces(self):
        source = self.directory / "source with spaces"
        output = self.directory / "output with spaces"
        for system in ("win32", "darwin"):
            with self.subTest(system=system):
                command = builder.compiler_command(source, output, system=system, jobs=2)
                self.assertIn("--mode=app-dist", command)
                self.assertIn(f"--output-dir={output}", command)
                self.assertIn(f"--include-data-dir={source / 'web'}=web", command)
                self.assertIn(f"--include-data-dir={source / 'fixtures'}=fixtures", command)
                self.assertIn(f"--include-data-dir={source / 'licenses'}=licenses", command)
                self.assertEqual(command[-1], str(source / "desktop_app.py"))
                if system == "win32":
                    self.assertIn("--windows-console-mode=disable", command)
                    self.assertIn("--msvc=latest", command)
                    self.assertNotIn("--macos-create-app-bundle", command)
                else:
                    self.assertIn("--macos-app-name=" + builder.APP_NAME, command)
                    self.assertNotIn("--windows-console-mode=disable", command)

    def test_prepared_environment_uses_platform_interpreter_without_installing_in_offline_mode(self):
        for system, suffix in (("win32", "Scripts/python.exe"), ("darwin", "bin/python")):
            with self.subTest(system=system):
                build_root = self.directory / system
                interpreter = self.write("venv/" + suffix, root=build_root)
                with patch.object(builder, "BUILD_ROOT", build_root), patch.object(builder.sys, "platform", system), \
                     patch.object(builder, "run_checked") as run:
                    self.assertEqual(builder.prepare_environment(argparse.Namespace(skip_install=True)), interpreter)
                    run.assert_not_called()

    def test_macos_minimum_comes_from_the_actual_binary(self):
        with patch.object(builder.sys, "platform", "darwin"), \
             patch.object(builder.subprocess, "check_output", return_value="minos 12.0\nminos 26.0\n"):
            self.assertEqual(builder.minimum_macos_version(Path("app")), "26.0")
        with patch.object(builder.sys, "platform", "darwin"), \
             patch.object(builder.subprocess, "check_output", return_value="unrecognized binary"):
            with self.assertRaises(ValueError):
                builder.minimum_macos_version(Path("app"))

    def test_simulated_build_delivers_verified_archives_for_both_platforms(self):
        root = self.source_tree()
        original_stage = builder.stage_sources
        certificate = self.write("public-ca-fixture.txt", "public-ca-fixture")
        for system in ("win32", "darwin"):
            with self.subTest(system=system):
                build_root = self.directory / (system + "-build")
                calls = []

                def fake_run(command, **kwargs):
                    command = [str(item) for item in command]
                    calls.append((command, kwargs))
                    if "nuitka" in command:
                        output = Path(next(item.split("=", 1)[1] for item in command if item.startswith("--output-dir=")))
                        relative = ("desktop_app.app/Contents/MacOS/" + builder.APP_NAME if system == "darwin"
                                    else "desktop_app.dist/" + builder.APP_NAME + ".exe")
                        self.write(relative, "compiled-binary", root=output)
                    elif "--self-test" in command:
                        report = Path(command[command.index("--self-test-result") + 1])
                        report.write_text(json.dumps({"ok": True}), encoding="utf-8")
                    elif command[0] == "/usr/bin/ditto":
                        package, archive = map(Path, command[-2:])
                        if "-c" in command:
                            with zipfile.ZipFile(archive, "w") as zipped:
                                for path in package.rglob("*"):
                                    if path.is_file():
                                        zipped.write(path, path.relative_to(package.parent))
                        else:
                            shutil.copytree(package, archive, symlinks=True)
                    elif command[0] == "/usr/bin/codesign":
                        self.assertIn("--verify", command)
                    else:
                        self.fail("Unexpected external build command")

                def fake_licenses(destination):
                    self.write("NOTICE.txt", root=destination)

                with patch.object(builder.sys, "platform", system), patch.object(builder.sys, "version_info", (3, 12, 0)), \
                     patch.object(builder, "BUILD_ROOT", build_root), \
                     patch.object(builder, "target_label", return_value=("windows" if system == "win32" else "macos") + "-x64"), \
                     patch.object(builder, "stage_sources", side_effect=lambda destination: original_stage(destination, root=root)), \
                     patch.object(builder, "collect_licenses", side_effect=fake_licenses), \
                     patch.object(builder, "minimum_macos_version", return_value="26.0" if system == "darwin" else None), \
                     patch.object(builder.importlib.metadata, "distributions", return_value=[]), \
                     patch.dict(builder.sys.modules, {"certifi": SimpleNamespace(where=lambda: str(certificate))}), \
                     patch.object(builder, "run_checked", side_effect=fake_run), patch("sys.stdout", new_callable=io.StringIO):
                    archive = builder.build(argparse.Namespace(output_dir=self.directory / "release", jobs=2))
                self.assertTrue(archive.is_file())
                checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
                self.assertEqual(archive.with_suffix(".zip.sha256").read_text().split()[0], checksum)
                with zipfile.ZipFile(archive) as zipped:
                    names = zipped.namelist()
                    executable_suffix = (".app/Contents/MacOS/" + builder.APP_NAME if system == "darwin"
                                         else "/" + builder.APP_NAME + ".exe")
                    self.assertTrue(any(name.endswith(executable_suffix) for name in names), names)
                    metadata = json.loads(zipped.read(builder.APP_NAME + "/BUILD-INFO.json"))
                    self.assertTrue(metadata["self_test"]["ok"])
                    self.assertFalse(any(name.endswith((".py", ".pyc")) for name in names))
                self_test_call = next((command, options) for command, options in calls if "--self-test" in command)
                self.assertIn("--data-dir", self_test_call[0])
                self.assertFalse(any(key.startswith(("PYTHON", "WORKBENCH_", "COMPANY_")) for key in self_test_call[1]["env"]))


if __name__ == "__main__":
    unittest.main()
