"""Online desktop resources remain complete without changing local contracts."""

from http.client import HTTPConnection
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "poc"))
import desktop_app
import web_app

SPEC = importlib.util.spec_from_file_location("online_desktop_builder", ROOT / "build_app.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)
ONLINE_RESOURCES = {"online.html", "online.js", "online.css"}
ONLINE_ROUTES = {"/online": "online.html", "/online.html": "online.html",
                 "/online.js": "online.js", "/online.css": "online.css"}


class OnlineDesktopPackagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="online-desktop-staging-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.staged = builder.stage_sources(self.root / "staged")

    def staged_server(self, port, *, app):
        return web_app.LocalServer(port, app, self.staged / "web")

    def test_staging_contains_each_online_resource_with_original_bytes(self):
        for name in ONLINE_RESOURCES:
            with self.subTest(name=name):
                target = self.staged / "web" / name
                self.assertTrue(target.is_file(), name)
                self.assertEqual(target.read_bytes(), (ROOT / "poc/web" / name).read_bytes())

    def test_online_routes_do_not_change_the_local_static_contract(self):
        self.assertEqual(set(web_app.STATIC_FILES.values()), {
            "index.html", "app.js", "i18n.js", "styles.css", "appearance.css",
            "theme.js", "layout.js", "markdown.js", "marked.umd.js"})
        self.assertEqual(web_app.ONLINE_STATIC_FILES, ONLINE_ROUTES)
        self.assertFalse(set(web_app.STATIC_FILES) & set(web_app.ONLINE_STATIC_FILES))

    def test_staged_online_runtime_and_cli_import_without_the_source_checkout(self):
        entries = ["online_web_bridge", "online_workspace", "online_investigation",
                   "online_analysis", "online_publish", "online_acceptance",
                   "online_deployment", "online_preflight"]
        script = ("import importlib,json,pathlib,socket,sys; "
                  "root=pathlib.Path(sys.argv.pop(1)).resolve();sys.path.insert(0,str(root)); "
                  "socket.create_connection=lambda *a,**k: (_ for _ in ()).throw(AssertionError('network forbidden')); "
                  "modules=[importlib.import_module(name) for name in sys.argv[1:]]; "
                  "assert all(pathlib.Path(module.__file__).resolve().parent==root for module in modules); "
                  "print(json.dumps([module.__name__ for module in modules]))")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", script,
                                 str(self.staged), *entries], cwd=self.root,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), entries)

    def test_self_test_rejects_missing_online_resource_before_starting_server(self):
        path = self.staged / "web/online.css"
        if path.exists():
            path.unlink()
        with mock.patch.object(desktop_app, "RESOURCE_ROOT", self.staged), \
                mock.patch.object(desktop_app, "DesktopServer") as server:
            with self.assertRaisesRegex(RuntimeError, "DESKTOP_ONLINE_WEB_RESOURCE_MISSING"):
                desktop_app.self_test()
        server.assert_not_called()

    def test_staged_online_routes_are_served_without_initializing_remote_state(self):
        app = web_app.WorkbenchState()
        with mock.patch.object(web_app, "create_server", side_effect=self.staged_server):
            service = desktop_app.DesktopServer(app=app).start()
        try:
            connection = HTTPConnection("127.0.0.1", service.server.server_port, timeout=5)
            try:
                for route, name in ONLINE_ROUTES.items():
                    with self.subTest(route=route):
                        connection.request("GET", route)
                        response = connection.getresponse()
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.read(), (ROOT / "poc/web" / name).read_bytes())
            finally:
                connection.close()
            self.assertIsNone(service.server.online_app)
        finally:
            service.close()

    def test_self_test_preserves_local_count_and_reports_online_checks_separately(self):
        from online_workspace import OnlineWorkbench
        profiles = []
        def isolated_online(path):
            profiles.append(Path(path))
            return OnlineWorkbench(path)
        with mock.patch.object(desktop_app, "RESOURCE_ROOT", self.staged), \
                mock.patch.object(web_app, "create_server", side_effect=self.staged_server), \
                mock.patch("online_workspace.OnlineWorkbench", side_effect=isolated_online), \
                mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("no remote requests")), \
                mock.patch("company_api.OpenAICompatibleChatClient.__init__", side_effect=AssertionError("no model")):
            result = desktop_app.self_test()
        self.assertEqual(result["static_files"], 9)
        self.assertEqual(result["online_static_files"], 3)
        self.assertEqual(result["model_calls"], 0)
        self.assertTrue(result["server_stopped"])
        self.assertTrue(result["online_state_http"])
        self.assertEqual(len(profiles), 1)
        self.assertTrue(profiles[0].parent.name.startswith("workbench-online-self-test-"))
        self.assertFalse(profiles[0].parent.exists())

    def test_online_state_probe_cleans_its_profile_after_failed_http(self):
        from online_workspace import OnlineWorkbench
        controllers = []
        def isolated_online(path):
            controller = OnlineWorkbench(path)
            controllers.append(controller)
            return controller
        service = SimpleNamespace(server=SimpleNamespace(online_app=None))
        connection = mock.Mock()
        connection.getresponse.return_value.status = 500
        connection.getresponse.return_value.read.return_value = b"{}"
        with mock.patch("online_workspace.OnlineWorkbench", side_effect=isolated_online):
            with self.assertRaisesRegex(RuntimeError, "DESKTOP_ONLINE_STATE_HTTP_FAILED"):
                desktop_app._self_test_online_state(service, connection, "synthetic-session")
        self.assertIsNone(service.server.online_app)
        self.assertEqual(len(controllers), 1)
        self.assertTrue(controllers[0]._closed)
        self.assertFalse(controllers[0].store.root.parent.exists())


if __name__ == "__main__":
    unittest.main()
