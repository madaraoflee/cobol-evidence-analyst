"""Desktop packaging preserves source mode, isolated settings, and clean shutdown."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

import app_paths
import desktop_app


class DesktopPathsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def module_paths(self, directory=None):
        environment = dict(os.environ)
        environment.pop("WORKBENCH_DATA_DIR", None)
        environment["PYTHONPATH"] = str(POC_ROOT)
        if directory is not None:
            environment["WORKBENCH_DATA_DIR"] = str(directory)
        script = ("import json, company_api, runtime_settings, framework_knowledge, web_app; "
                  "print(json.dumps([str(company_api.PROJECT_ENV_FILE), "
                  "str(runtime_settings.DEFAULT_SETTINGS_PATH), str(framework_knowledge.PROJECT_ROOT), "
                  "str(framework_knowledge.DEFAULT_REFERENCE_PATH), str(web_app.FRAMEWORK_DEMO_OUTPUT), "
                  "str(web_app.WEB_ROOT)]))")
        result = subprocess.run([sys.executable, "-c", script], cwd=self.root, env=environment,
                                capture_output=True, text=True, check=True, timeout=20)
        return [Path(value) for value in json.loads(result.stdout)]

    def test_source_entry_points_preserve_existing_locations_from_any_cwd(self):
        project = POC_ROOT.parent
        self.assertEqual(self.module_paths(), [
            project / ".env", project / ".poc-data" / "agent-settings.json", project,
            project / ".poc-data" / "framework" / "reference.md",
            project / ".poc-data" / "framework-runs", POC_ROOT / "web",
        ])

    def test_desktop_override_moves_only_writable_and_private_files(self):
        profile = self.root / "用户 profile"
        self.assertEqual(self.module_paths(profile), [
            profile / ".env", profile / "agent-settings.json", profile,
            profile / "framework" / "reference.md", profile / "framework-runs", POC_ROOT / "web",
        ])

    def test_profile_starts_with_empty_credentials_and_preserves_user_edits(self):
        profile = self.root / "desktop"
        with mock.patch.dict(os.environ, {"COMPANY_API_KEY": "ambient-private-value"}, clear=True):
            self.assertEqual(desktop_app.prepare_profile(profile), profile)
            content = (profile / ".env").read_text(encoding="utf-8")
            self.assertIn("COMPANY_API_KEY=\n", content)
            self.assertNotIn("ambient-private-value", content)
            (profile / ".env").write_text("COMPANY_API_KEY=local-value\n", encoding="utf-8")
            desktop_app.prepare_profile(profile)
            self.assertEqual((profile / ".env").read_text(), "COMPANY_API_KEY=local-value\n")
        if os.name != "nt":
            self.assertEqual((profile / ".env").stat().st_mode & 0o777, 0o600)

    def test_profile_refuses_configuration_symlink(self):
        target = self.root / "private.env"
        target.write_text("unchanged", encoding="utf-8")
        (self.root / ".env").symlink_to(target)
        with self.assertRaisesRegex(ValueError, "DESKTOP_CONFIG_SYMLINK"):
            desktop_app.prepare_profile(self.root)
        self.assertEqual(target.read_text(), "unchanged")

    def test_default_profile_is_in_each_operating_system_user_data_location(self):
        home = self.root / "home"
        self.assertEqual(app_paths.default_desktop_data_dir(platform="win32", environ={"LOCALAPPDATA": str(self.root)}, home=home), self.root / "COBOLWorkbench")
        self.assertEqual(app_paths.default_desktop_data_dir(platform="darwin", environ={}, home=home), home / "Library" / "Application Support" / "COBOLWorkbench")
        self.assertEqual(app_paths.default_desktop_data_dir(platform="linux", environ={}, home=home), home / ".local" / "share" / "COBOLWorkbench")


class DesktopRuntimeTests(unittest.TestCase):
    def test_native_picker_uses_window_dialog_without_a_python_child(self):
        window = mock.Mock()
        window.create_file_dialog.return_value = (str(POC_ROOT),)
        webview = types.SimpleNamespace(FileDialog=types.SimpleNamespace(FOLDER="folder"))
        picker = desktop_app.native_folder_picker(window, webview)
        with mock.patch("subprocess.run", side_effect=AssertionError("must not spawn an interpreter")):
            self.assertEqual(picker(POC_ROOT), str(POC_ROOT))
            window.create_file_dialog.assert_called_with("folder", directory=str(POC_ROOT), allow_multiple=False)
            window.create_file_dialog.return_value = None
            self.assertIsNone(picker(POC_ROOT))

    def test_server_binds_only_loopback_and_stops_idempotently(self):
        from web_app import WorkbenchState

        service = desktop_app.DesktopServer(app=WorkbenchState()).start()
        self.addCleanup(service.close)
        address = service.server.server_address
        self.assertEqual(address[0], "127.0.0.1")
        with socket.create_connection(address, timeout=2):
            pass
        service.close()
        service.close()
        self.assertTrue(service.server.app.cancel_event.is_set())
        self.assertFalse(service.thread.is_alive())
        with self.assertRaises(OSError):
            socket.create_connection(address, timeout=2)

    def test_headless_self_test_serves_assets_without_model_or_analysis_calls(self):
        with mock.patch("web_app.WorkbenchState.start", side_effect=AssertionError("no analysis")), \
             mock.patch("company_api.OpenAICompatibleChatClient.__init__", side_effect=AssertionError("no model")):
            result = desktop_app.self_test()
        self.assertEqual(result["model_calls"], 0)
        self.assertTrue(result["server_stopped"])
        self.assertEqual(result["static_files"], 9)

    def test_gui_failure_closes_started_server(self):
        service = mock.Mock()
        service.start.return_value = service
        webview = types.ModuleType("webview")
        webview.settings = {}
        webview.create_window = mock.Mock(side_effect=RuntimeError("window unavailable"))
        menu = types.ModuleType("webview.menu")
        menu.Menu = mock.Mock()
        menu.MenuAction = mock.Mock()
        with mock.patch.dict(sys.modules, {"webview": webview, "webview.menu": menu}), \
             mock.patch("desktop_app.DesktopServer", return_value=service):
            with self.assertRaises(desktop_app.NativeWindowUnavailable):
                desktop_app.run_desktop(Path.cwd())
        service.close.assert_called_once_with()

    def test_tls_uses_bundle_and_preserves_explicit_certificate_override(self):
        certificates = types.SimpleNamespace(where=lambda: "/bundle/cacert.pem")
        with mock.patch.dict(sys.modules, {"certifi": certificates}), mock.patch.dict(os.environ, {}, clear=True):
            desktop_app.configure_tls()
            self.assertEqual(os.environ["SSL_CERT_FILE"], "/bundle/cacert.pem")
            os.environ["SSL_CERT_FILE"] = "/private/custom.pem"
            desktop_app.configure_tls()
            self.assertEqual(os.environ["SSL_CERT_FILE"], "/private/custom.pem")


class BrowserCompatibilityTests(unittest.TestCase):
    def test_browser_headless_check_does_not_import_embedded_renderer(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "profile"
            report = Path(directory) / "result.json"
            with mock.patch.dict(os.environ, {}, clear=True), \
                 mock.patch.dict(sys.modules, {"webview": None}), \
                 mock.patch("desktop_app.configure_tls"), \
                 mock.patch("desktop_app.logging.basicConfig"), \
                 mock.patch("desktop_app.self_test", return_value={"ok": True}), \
                 mock.patch("desktop_app.sys.stdout", None):
                result = desktop_app.main(["--browser", "--self-test", "--data-dir", str(profile),
                                           "--self-test-result", str(report)])
            self.assertEqual(result, 0)
            self.assertTrue(json.loads(report.read_text())["browser_mode"])
            self.assertFalse(json.loads(report.read_text())["desktop_dependencies"])

    def test_explicit_browser_mode_does_not_attempt_embedded_renderer(self):
        with mock.patch("desktop_app.run_desktop", side_effect=AssertionError("no renderer")), \
             mock.patch("desktop_app.run_browser", return_value=0) as run:
            self.assertEqual(desktop_app.launch(POC_ROOT, browser=True), 0)
        run.assert_called_once_with(POC_ROOT)

    def test_windows_falls_back_only_for_native_startup_failure(self):
        with mock.patch("desktop_app.sys.platform", "win32"), \
             mock.patch("desktop_app.run_desktop", side_effect=desktop_app.NativeWindowUnavailable("renderer")), \
             mock.patch("desktop_app.run_browser", return_value=0) as run:
            self.assertEqual(desktop_app.launch(POC_ROOT), 0)
        run.assert_called_once_with(POC_ROOT)
        for outcome in (0, RuntimeError("failure after startup")):
            with self.subTest(outcome=outcome), mock.patch("desktop_app.sys.platform", "win32"), \
                 mock.patch("desktop_app.run_desktop", side_effect=outcome if isinstance(outcome, Exception) else None,
                            return_value=outcome), mock.patch("desktop_app.run_browser") as run:
                if isinstance(outcome, Exception):
                    with self.assertRaises(RuntimeError):
                        desktop_app.launch(POC_ROOT)
                else:
                    self.assertEqual(desktop_app.launch(POC_ROOT), 0)
                run.assert_not_called()

    def test_server_failure_is_not_reclassified_as_renderer_failure(self):
        webview = types.ModuleType("webview")
        menu = types.ModuleType("webview.menu")
        menu.Menu, menu.MenuAction = mock.Mock(), mock.Mock()
        with mock.patch.dict(sys.modules, {"webview": webview, "webview.menu": menu}), \
             mock.patch("desktop_app.DesktopServer", side_effect=OSError("local service blocked")):
            with self.assertRaisesRegex(OSError, "local service blocked"):
                desktop_app.run_desktop(POC_ROOT)

    def test_windows_controller_reopens_edits_and_exits_without_console(self):
        origin = "http://127.0.0.1:12345"
        with mock.patch("desktop_app.sys.platform", "win32"), \
             mock.patch("desktop_app.windows_message_box", side_effect=[6, 7, 2]) as dialog, \
             mock.patch("desktop_app.webbrowser.open") as opened, \
             mock.patch("desktop_app.open_profile") as configured:
            desktop_app.browser_controller(origin, POC_ROOT)
        opened.assert_called_once_with(origin)
        configured.assert_called_once_with(POC_ROOT, edit_config=True)
        self.assertIn(origin, dialog.call_args.args[0])
        self.assertIn("取消", dialog.call_args.args[0])

    def test_browser_exit_stops_service_and_installs_native_folder_picker(self):
        from web_app import WorkbenchState

        service = desktop_app.DesktopServer(app=WorkbenchState())
        with mock.patch("desktop_app.DesktopServer", return_value=service), \
             mock.patch("desktop_app.webbrowser.open") as opened, \
             mock.patch("desktop_app.browser_controller"):
            self.assertEqual(desktop_app.run_browser(POC_ROOT), 0)
        opened.assert_called_once_with(service.server.origin)
        self.assertIs(service.server.app.folder_picker, desktop_app.browser_folder_picker)
        self.assertFalse(service.thread.is_alive())
        self.assertTrue(service.server.app.cancel_event.is_set())

    def test_browser_controller_failure_still_stops_service(self):
        service = mock.Mock()
        service.start.return_value = service
        with mock.patch("desktop_app.DesktopServer", return_value=service), \
             mock.patch("desktop_app.webbrowser.open"), \
             mock.patch("desktop_app.browser_controller", side_effect=OSError("controller unavailable")):
            with self.assertRaises(OSError):
                desktop_app.run_browser(POC_ROOT)
        service.close.assert_called_once_with()

    def test_browser_folder_picker_never_launches_python(self):
        with mock.patch("desktop_app.sys.platform", "win32"), \
             mock.patch("desktop_app.windows_folder_picker", return_value=str(POC_ROOT)) as picker, \
             mock.patch("subprocess.run", side_effect=AssertionError("no interpreter child")):
            self.assertEqual(desktop_app.browser_folder_picker(POC_ROOT), str(POC_ROOT))
        picker.assert_called_once_with(POC_ROOT)


class WindowsFolderDialogTests(unittest.TestCase):
    def libraries(self, *, selected=100, initialized=0, converted=True):
        import ctypes

        shell, ole, user = mock.Mock(), mock.Mock(), mock.Mock()
        ole.CoInitializeEx.return_value = initialized
        shell.SHBrowseForFolderW.return_value = selected
        def path(_selected, buffer, _size, _options):
            buffer.value = "C:\\分析目录\\source"
            return converted
        shell.SHGetPathFromIDListEx.side_effect = path
        return ctypes, shell, ole, user

    def pick(self, libraries):
        ctypes = libraries[0]
        with mock.patch("desktop_app._windows_libraries", return_value=libraries), \
             mock.patch.object(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, create=True):
            return desktop_app.windows_folder_picker(POC_ROOT)

    def test_selected_unicode_path_frees_shell_memory_and_com(self):
        libraries = self.libraries()
        self.assertEqual(self.pick(libraries), "C:\\分析目录\\source")
        libraries[2].CoTaskMemFree.assert_called_once_with(100)
        libraries[2].CoUninitialize.assert_called_once_with()

    def test_cancel_releases_com_without_freeing_a_null_selection(self):
        libraries = self.libraries(selected=None)
        self.assertIsNone(self.pick(libraries))
        libraries[2].CoTaskMemFree.assert_not_called()
        libraries[2].CoUninitialize.assert_called_once_with()

    def test_invalid_shell_item_releases_all_resources(self):
        libraries = self.libraries(converted=False)
        with self.assertRaisesRegex(OSError, "FOLDER_PICKER_UNAVAILABLE"):
            self.pick(libraries)
        libraries[2].CoTaskMemFree.assert_called_once_with(100)
        libraries[2].CoUninitialize.assert_called_once_with()

    def test_failed_com_initialization_does_not_uninitialize_someone_elses_apartment(self):
        libraries = self.libraries(initialized=-2147417850)
        with self.assertRaisesRegex(OSError, "FOLDER_PICKER_UNAVAILABLE"):
            self.pick(libraries)
        libraries[1].SHBrowseForFolderW.assert_not_called()
        libraries[2].CoUninitialize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
