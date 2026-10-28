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
from tools.check_investigation_continuity import CompletionProvider, SOURCE, verify_pages
from tools.desktop_acceptance_runtime import FIXTURES


class InvestigationContinuityCheckTests(unittest.TestCase):
    def test_supply_check_rejects_wrong_version_and_invented_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "root.cbl").write_text(SOURCE, encoding="utf-8")
            page = {"relative_path": "root.cbl", "source_sha256": hashlib.sha256(SOURCE.encode()).hexdigest(),
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


if __name__ == "__main__":
    unittest.main()
