"""Compare source and desktop deployment inputs through an offline question."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from build_app import stage_sources


WORKER = r'''
import json
from pathlib import Path
import socket
import sys
from unittest.mock import patch

profile, source, database = sys.argv[1:]
if profile != "source":
    from desktop_app import prepare_profile
    prepare_profile(Path(profile))

from business_index import build_business_index
from business_chat import run_business_chat
from company_api import TransportResponse
from framework_knowledge import framework_status
from model_profiles import model_config
from repository_discovery import ensure_repository_search
from runtime_settings import load_agent_policy

config = model_config("workbench")
policy = load_agent_policy()
requests = []
def transport(request):
    envelope = json.loads(request.body)
    requests.append(envelope)
    payload = json.loads(envelope["messages"][-1]["content"])
    pages = [page for context in payload["source_context"] for page in context.get("pages", [])]
    page = next(page for page in pages if "COMPUTE NET-AMOUNT" in page["source_text"])
    answer = f"净金额等于基础金额减费用。[{page['evidence_id']}]"
    return TransportResponse(200, json.dumps({"choices": [{"message": {
        "role": "assistant", "content": answer}, "finish_reason": "stop"}]}))

with patch("socket.socket.connect", side_effect=AssertionError("offline only")), \
     patch("answer_diagnostics._commit", return_value="unknown"):
    build_business_index(Path(source), Path(database), source_format="free", verify_content=True)
    ensure_repository_search(Path(database), Path(source))
    output = run_business_chat("AMOUNT-RULE 的 NET-AMOUNT 如何计算？", Path(database),
        Path(source), config, policy=policy, transport=transport)
result = output["agent_result"]
print(json.dumps({"policy": policy.to_dict(), "framework": framework_status(),
    "requests": requests, "answer": result["answer"], "status": result["status"],
    "model_requests": result["metrics"]["model_requests"]}, ensure_ascii=False))
'''


class DesktopAnswerParityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.legacy = self.root / "legacy"
        self.staged = stage_sources(self.legacy / "poc")
        self.profile = self.root / "desktop"
        self.profile.mkdir()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "amount.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. AMOUNT-RULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 BASE-AMOUNT PIC 9(5).\n01 FEE PIC 9(5).\n01 NET-AMOUNT PIC 9(5).\n"
            "PROCEDURE DIVISION.\nMAIN.\n"
            "COMPUTE NET-AMOUNT = BASE-AMOUNT - FEE.\nGOBACK.\n", encoding="utf-8")
        self.settings = {"version": 1, "agent": {"max_model_requests": 2,
                         "max_answer_revisions": 0, "max_history_characters": 5000}}
        self.reference = "# Amount rules\n\n## NET-AMOUNT\nNET-AMOUNT is the amount remaining after fees.\n"
        self.configure(self.legacy, ".poc-data")
        self.configure(self.profile, "")

    def configure(self, root, data_subdirectory):
        data = root / data_subdirectory
        reference = data / "framework" / "reference.md"
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_text(self.reference, encoding="utf-8")
        (data / "agent-settings.json").write_text(json.dumps(self.settings), encoding="utf-8")
        (root / ".env").write_text(
            "COMPANY_API_BASE_URL=https://gateway.example.invalid/v1\n"
            "COMPANY_API_KEY=synthetic-credential\nCOMPANY_CHAT_MODEL=release-model\n"
            "COMPANY_MAX_OUTPUT_TOKENS=3072\n"
            f"FRAMEWORK_REFERENCE_PATH={reference.relative_to(root).as_posix()}\n", encoding="utf-8")

    def run_mode(self, desktop):
        environment = {name: value for name, value in os.environ.items()
                       if not name.startswith(("COMPANY_", "WORKBENCH_", "PYTHON"))
                       and name not in {"FRAMEWORK_REFERENCE_PATH", "AGENT_SETTINGS_PATH"}}
        environment["PYTHONPATH"] = str(self.staged)
        result = subprocess.run([sys.executable, "-c", WORKER,
                                 str(self.profile) if desktop else "source", str(self.source),
                                 str(self.root / ("desktop.sqlite" if desktop else "source.sqlite"))],
                                cwd=self.root, env=environment, capture_output=True, text=True,
                                check=True, timeout=30)
        return json.loads(result.stdout)

    def test_same_configuration_preserves_model_requests_evidence_and_answer(self):
        source = self.run_mode(False)
        desktop = self.run_mode(True)
        self.assertEqual(source["framework"]["status"], "LOADED")
        self.assertEqual(desktop["framework"]["status"], "LOADED")
        self.assertEqual(source["policy"], desktop["policy"])
        self.assertEqual(desktop["policy"]["max_model_requests"], 2)
        self.assertEqual(source["requests"], desktop["requests"])
        self.assertEqual(desktop["requests"][0]["model"], "release-model")
        self.assertEqual(desktop["requests"][0]["max_tokens"], 3072)
        payload = json.loads(desktop["requests"][0]["messages"][-1]["content"])
        self.assertTrue(payload["framework_references"])
        self.assertTrue(any("COMPUTE NET-AMOUNT" in page["source_text"]
                            for context in payload["source_context"] for page in context.get("pages", [])))
        self.assertEqual(source["answer"], desktop["answer"])
        self.assertEqual(source["status"], desktop["status"])
        self.assertEqual(desktop["model_requests"], 1)

    def test_old_relative_reference_path_is_reported_unavailable_after_incomplete_migration(self):
        (self.profile / ".env").write_text((self.legacy / ".env").read_text(), encoding="utf-8")
        desktop = self.run_mode(True)
        self.assertEqual(desktop["framework"]["status"], "UNAVAILABLE")
        self.assertEqual(desktop["framework"]["reason_code"], "FRAMEWORK_REFERENCE_UNREADABLE")


if __name__ == "__main__":
    unittest.main()
