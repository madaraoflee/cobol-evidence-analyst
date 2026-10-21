"""A new application folder must reuse a desktop user's external workspace.

These are source-entry integration tests, not compiled Windows acceptance.
They start separate loopback services from two staged application locations and
use Windows profile selection while keeping all model transports disabled.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from build_app import stage_sources


WORKER = r'''
from contextlib import ExitStack
from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

import app_paths
import desktop_app

phase, source, output = sys.argv[1:]
with patch("desktop_app.default_desktop_data_dir",
           side_effect=lambda: app_paths.default_desktop_data_dir(platform="win32")):
    profile = desktop_app.prepare_profile()

with ExitStack() as patches:
    patches.enter_context(patch("company_api.OpenAICompatibleChatClient.__init__",
                                side_effect=AssertionError("no model calls in upgrade tests")))
    if phase != "seed":
        patches.enter_context(patch("analyze_source.build_business_index",
                                    side_effect=AssertionError("opening an upgrade must not import source")))
    service = desktop_app.DesktopServer().start()
    try:
        app = service.server.app
        if phase == "seed":
            identifier = app.start({"source": source, "output": output,
                                    "source_format": "free", "allow_network": False})["job_id"]
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                job = app.state()["job"]
                if job and job["job_id"] == identifier and job["status"] in {
                        "COMPLETED", "FAILED", "CANCELLED"}:
                    break
                time.sleep(0.01)
            assert job["status"] == "COMPLETED", job
            conversation = app.new_conversation({})["conversation"]
            app.conversation_store.append(conversation["id"], "user", "How is the available amount calculated?")
            app.conversation_store.append(conversation["id"], "assistant",
                                          "The available amount is the limit less the amount already used.")
            app.get_conversation(conversation["id"])
        connection = HTTPConnection("127.0.0.1", service.server.server_port, timeout=5)
        try:
            connection.request("GET", "/api/state")
            response = connection.getresponse()
            state = json.loads(response.read())
            assert response.status == 200
        finally:
            connection.close()
        print(json.dumps({"profile": str(profile), "application": str(app_paths.RESOURCE_ROOT),
                          "state": state}, ensure_ascii=False))
    finally:
        service.close()
'''


class DesktopUpgradeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.old_application = stage_sources(self.root / "old-application")
        self.new_application = stage_sources(self.root / "new-application")
        self.local_data = self.root / "user-local-data"
        self.profile = self.local_data / "COBOLWorkbench"
        self.profile.mkdir(parents=True)
        self.source = self.root / "business-source"
        self.source.mkdir()
        (self.source / "amount.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. AMOUNT-RULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 AMOUNT-LIMIT PIC 9(5).\n01 AMOUNT-USED PIC 9(5).\n"
            "01 AMOUNT-AVAILABLE PIC 9(5).\nPROCEDURE DIVISION.\n"
            "COMPUTE AMOUNT-AVAILABLE = AMOUNT-LIMIT - AMOUNT-USED.\nGOBACK.\n",
            encoding="utf-8")
        self.output = self.root / "analysis-output"
        self.private_files = {
            ".env": "COMPANY_API_KEY=local-synthetic-credential\nFRAMEWORK_REFERENCE_PATH=framework/reference.md\n",
            "agent-settings.json": json.dumps({"version": 1, "agent": {"max_model_requests": 2}}),
            "framework/reference.md": "# Amount processing\nLocal business context.\n",
        }
        for name, content in self.private_files.items():
            path = self.profile / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def launch_source_entry(self, application, phase):
        environment = {name: value for name, value in os.environ.items()
                       if not name.startswith(("COMPANY_", "WORKBENCH_", "PYTHON"))
                       and name not in {"FRAMEWORK_REFERENCE_PATH", "AGENT_SETTINGS_PATH"}}
        environment.update(PYTHONPATH=str(application), PYTHONUTF8="1",
                           PYTHONIOENCODING="utf-8", LOCALAPPDATA=str(self.local_data))
        result = subprocess.run([sys.executable, "-c", WORKER, phase,
                                 str(self.source), str(self.output)],
                                cwd=application, env=environment, capture_output=True,
                                text=True, encoding="utf-8", check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_retained_profile_and_history(self, original, restored):
        self.assertEqual(original["profile"], str(self.profile))
        self.assertEqual(restored["profile"], str(self.profile))
        self.assertNotEqual(original["application"], restored["application"])
        self.assertEqual(restored["state"]["conversation"], original["state"]["conversation"])
        self.assertEqual(restored["state"]["conversations"], original["state"]["conversations"])
        for name, content in self.private_files.items():
            self.assertEqual((self.profile / name).read_text(encoding="utf-8"), content)
        self.assertEqual(restored["state"]["project"]["source"], str(self.source))
        self.assertEqual(restored["state"]["project"]["output"], str(self.output))

    def test_new_application_folder_preserves_profile_source_index_and_history(self):
        original = self.launch_source_entry(self.old_application, "seed")
        index = self.output / "structural-index.sqlite"
        index_digest = hashlib.sha256(index.read_bytes()).hexdigest()
        saved_workspace = (self.profile / "workbench-state.json").read_bytes()
        shutil.rmtree(self.old_application)
        restored = self.launch_source_entry(self.new_application, "restore")
        self.assert_retained_profile_and_history(original, restored)
        self.assertEqual(restored["state"]["project"]["snapshot_id"],
                         original["state"]["project"]["snapshot_id"])
        self.assertEqual(restored["state"]["project"]["programs"],
                         original["state"]["project"]["programs"])
        self.assertEqual(hashlib.sha256(index.read_bytes()).hexdigest(), index_digest)
        self.assertEqual((self.profile / "workbench-state.json").read_bytes(), saved_workspace)

    def test_upgrade_with_missing_index_retains_history_without_reimport(self):
        original = self.launch_source_entry(self.old_application, "seed")
        (self.output / "structural-index.sqlite").unlink()
        restored = self.launch_source_entry(self.new_application, "restore")
        self.assert_retained_profile_and_history(original, restored)
        self.assertIsNone(restored["state"]["project"].get("snapshot_id"))
        self.assertFalse((self.output / "structural-index.sqlite").exists())

    def test_upgrade_with_unavailable_source_retains_existing_history(self):
        original = self.launch_source_entry(self.old_application, "seed")
        source_archive = self.source.with_name("business-source-offline")
        self.source.rename(source_archive)
        restored = self.launch_source_entry(self.new_application, "restore")
        self.assert_retained_profile_and_history(original, restored)
        self.assertIsNone(restored["state"]["project"].get("snapshot_id"))
        self.assertFalse(self.source.exists())
        self.assertTrue((source_archive / "amount.cbl").is_file())


if __name__ == "__main__":
    unittest.main()
