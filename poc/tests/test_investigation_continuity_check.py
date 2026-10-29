"""The supplemental desktop checker binds actual HTTP source and boundaries."""

import hashlib
import http.client
import json
from pathlib import Path
import ssl
import sys
import tempfile
import unittest
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.check_investigation_continuity import (BUSINESS_CALLER, BUSINESS_SOURCE,
                                                CompletionProvider, SOURCE, continuation_turn, verify_pages)
from tools.desktop_acceptance_runtime import FIXTURES


class InvestigationContinuityCheckTests(unittest.TestCase):
    def test_cross_language_fixture_cannot_continue_a_completed_or_unsourced_turn(self):
        user = {"role": "user", "status": "partial"}
        assistant = {"role": "assistant", "status": "partial", "evidence_refs": [{"evidence_id": "ev:source"}],
                     "investigation_run_id": "a" * 24}
        self.assertEqual(continuation_turn({"messages": [user, assistant]}), (user, assistant))
        for changed in ({**assistant, "status": "completed"}, {**assistant, "evidence_refs": []},
                        {**assistant, "investigation_run_id": None}):
            with self.subTest(changed=changed), self.assertRaises(AssertionError):
                continuation_turn({"messages": [user, changed]})
        with self.assertRaises(AssertionError):
            continuation_turn({"messages": [{**user, "status": "completed"}, assistant]})

    def test_supply_check_rejects_wrong_version_and_invented_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            for newline in ("\n", "\r\n"):
                with self.subTest(newline=repr(newline)):
                    raw = SOURCE.replace("\n", newline).encode("utf-8")
                    (source / "root.cbl").write_bytes(raw)
                    page = {"relative_path": "root.cbl", "source_sha256": hashlib.sha256(raw).hexdigest(),
                        "start_line": 1, "end_line": len(SOURCE.splitlines()), "source_text": SOURCE}
                    payload = {"source_context": [{"pages": [page]}]}
                    self.assertEqual(verify_pages(source, payload), page)
                    for changed in ({"source_sha256": "0" * 64}, {"source_text": SOURCE.replace("* 2", "* 3")}):
                        with self.subTest(changed=list(changed)), self.assertRaises(AssertionError):
                            verify_pages(source, {"source_context": [{"pages": [{**page, **changed}]}]})

    def test_loopback_provider_emits_real_length_boundary_and_records_request(self):
        with tempfile.TemporaryDirectory() as directory:
            with CompletionProvider(directory, lambda envelope, payload, index: ("Retained partial.", "length")) as provider:
                address = urlsplit(provider.base_url)
                context = ssl.create_default_context(cafile=str(FIXTURES / "loopback-server.crt"))
                connection = http.client.HTTPSConnection(address.hostname, address.port, context=context, timeout=5)
                try:
                    body = json.dumps({"messages": [{"role": "user", "content": "{}"}]}).encode()
                    connection.request("POST", "/v1/chat/completions", body, {"Content-Type": "application/json"})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    data = json.loads(response.read())
                finally:
                    connection.close()
                self.assertEqual(data["choices"][0]["finish_reason"], "length")
                self.assertEqual(data["choices"][0]["message"]["content"], "Retained partial.")
                self.assertEqual(provider.requests[0]["request_sha256"], hashlib.sha256(body).hexdigest())
                self.assertEqual(provider.errors, [])

    def test_business_handoff_check_requires_both_original_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            pages = []
            for name, text in (("decision.cbl", BUSINESS_SOURCE), ("caller.cbl", BUSINESS_CALLER)):
                path = source / name
                path.write_text(text, encoding="utf-8")
                pages.append({"relative_path": name, "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "start_line": 1, "end_line": len(text.splitlines()), "source_text": text})
            payload = {"source_context": [{"pages": pages}]}
            self.assertEqual(verify_pages(source, payload, "MOVE 12 TO RESULT-CODE")["relative_path"], "decision.cbl")
            self.assertEqual(verify_pages(source, payload, "MOVE 'N' TO ALLOW-PROCESS")["relative_path"], "caller.cbl")
            with self.assertRaises(StopIteration):
                verify_pages(source, {"source_context": [{"pages": pages[:1]}]}, "MOVE 'N' TO ALLOW-PROCESS")
            invented = {**pages[1], "source_text": BUSINESS_CALLER.replace("RESULT-CODE = 12", "RESULT-CODE = 13")}
            with self.assertRaises(AssertionError):
                verify_pages(source, {"source_context": [{"pages": [pages[0], invented]}]}, "MOVE 'N' TO ALLOW-PROCESS")


if __name__ == "__main__":
    unittest.main()
