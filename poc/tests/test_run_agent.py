from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from company_api import (  # noqa: E402
    CompanyAPIConfig,
    TransportRequest,
    TransportResponse,
)
from run_agent import main, run_investigation  # noqa: E402
from structural_index import build_structural_index  # noqa: E402


API_KEY = "runner-test-key-must-not-be-recorded"
BASE_URL = "https://company.example/v1"
CHAT_MODEL = "company-chat-model"
FIXTURE_ROOT = POC_ROOT / "fixtures" / "synthetic-insurance-v1"


def response(status: int, value: object) -> TransportResponse:
    return TransportResponse(
        status_code=status,
        body=json.dumps(value, separators=(",", ":")).encode("utf-8"),
    )


class AgentReadyTransport:
    def __init__(self, *, reject_tool_result: bool = False) -> None:
        self.reject_tool_result = reject_tool_result
        self.requests: list[TransportRequest] = []

    def __call__(self, request: TransportRequest) -> TransportResponse:
        self.requests.append(request)
        if request.endpoint == "models":
            return response(200, {"data": [{"id": CHAT_MODEL}]})

        payload = json.loads(request.body or b"{}")
        messages = payload.get("messages", [])
        if any(message.get("role") == "system" for message in messages):
            return response(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(
                                    {
                                        "action": "abstain",
                                        "arguments": {
                                            "claims": [],
                                            "evidence_ids": [],
                                            "boundaries": [
                                                "The test planner intentionally abstained."
                                            ],
                                        },
                                    }
                                ),
                            }
                        }
                    ]
                },
            )

        tools = payload.get("tools", [])
        tool_names = [
            tool.get("function", {}).get("name")
            for tool in tools
            if isinstance(tool, dict)
        ]
        if "capability_probe" in tool_names:
            if any(message.get("role") == "tool" for message in messages):
                if self.reject_tool_result:
                    return response(400, {"error": {"message": "rejected"}})
                return response(
                    200,
                    {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "DONE",
                                }
                            }
                        ]
                    },
                )
            return response(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-probe",
                                        "type": "function",
                                        "function": {
                                            "name": "capability_probe",
                                            "arguments": '{"ok":true}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        if "response_format" in payload:
            return response(
                200,
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": '{"ok":true}',
                            }
                        }
                    ]
                },
            )
        return response(
            200,
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "OK"}}
                ]
            },
        )


class RunAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temporary.name) / "runner.sqlite"
        build_structural_index(FIXTURE_ROOT, cls.database, quiet=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    @staticmethod
    def config() -> CompanyAPIConfig:
        return CompanyAPIConfig(
            base_url=BASE_URL,
            chat_model=CHAT_MODEL,
            api_key=API_KEY,
        )

    def test_probe_selects_native_mode_before_running_agent(self) -> None:
        transport = AgentReadyTransport()

        output = run_investigation(
            "What is the current production value?",
            self.database,
            self.config(),
            transport=transport,
        )

        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertEqual(output["selected_mode"], "NATIVE_TOOL_CALLING")
        self.assertEqual(output["agent_result"]["status"], "ABSTAINED")
        self.assertEqual(len(transport.requests), 6)
        serialized = json.dumps(output)
        self.assertNotIn(API_KEY, serialized)
        self.assertNotIn(BASE_URL, serialized)
        self.assertNotIn(CHAT_MODEL, serialized)

    def test_probe_falls_back_to_validated_json_mode(self) -> None:
        transport = AgentReadyTransport(reject_tool_result=True)

        output = run_investigation(
            "What is the current production value?",
            self.database,
            self.config(),
            transport=transport,
        )

        self.assertEqual(output["runner_status"], "COMPLETED")
        self.assertEqual(output["selected_mode"], "VALIDATED_JSON_FALLBACK")
        agent_request = json.loads(transport.requests[-1].body or b"{}")
        self.assertNotIn("tools", agent_request)
        self.assertEqual(agent_request["response_format"]["type"], "json_schema")

    def test_cli_remains_offline_without_explicit_network_flag(self) -> None:
        environment = {
            "COMPANY_API_BASE_URL": BASE_URL,
            "COMPANY_API_KEY": API_KEY,
            "COMPANY_CHAT_MODEL": CHAT_MODEL,
        }
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, environment, clear=True):
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--database",
                        str(self.database),
                        "--question",
                        "Explain the calculation",
                    ]
                )

        output = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(output["runner_status"], "NOT_READY")
        self.assertEqual(output["reason_code"], "COMPANY_API_NOT_READY")
        self.assertNotIn(API_KEY, stdout.getvalue())
        self.assertNotIn(BASE_URL, stdout.getvalue())
        self.assertNotIn(CHAT_MODEL, stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
