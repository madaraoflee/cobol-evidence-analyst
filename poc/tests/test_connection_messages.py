"""Verify connection diagnostics without real sockets or model calls."""

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from company_api import APIClientError, CompanyAPIConfig, TransportResponse
from web_app import WorkbenchState


class ConnectionMessageTests(unittest.TestCase):
    def setUp(self):
        self.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model",
                                       api_key="synthetic-credential", max_output_tokens=4096)

    def check(self, transport):
        return WorkbenchState(config_provider=lambda: self.config,
            model_check_transport=transport, state_path=None).check_model({})

    def test_connection_check_keeps_safe_transport_reason(self):
        for code, reason in (("TRANSPORT_ERROR", "dns_resolution_failed"),
                             ("TRANSPORT_ERROR", "tls_certificate_invalid"),
                             ("TRANSPORT_ERROR", "connection_refused"),
                             ("REQUEST_TIMEOUT", "timeout")):
            with self.subTest(reason=reason):
                requests = []

                def transport(request):
                    requests.append(json.loads(request.body))
                    raise APIClientError(code, transport_reason=reason)

                result = self.check(transport)
                self.assertEqual(result, {"usable": False, "code": code, "http_status": None,
                                          "model_returned": False, "transport_reason": reason})
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]["max_tokens"], 32)
                for secret in (self.config.api_key, self.config.base_url, self.config.chat_model):
                    self.assertNotIn(secret, json.dumps(result))

    def test_server_failure_is_an_http_response_not_a_transport_failure(self):
        result = self.check(lambda request: TransportResponse(500, "synthetic failure"))
        self.assertEqual(result, {"usable": False, "code": "HTTP_ERROR", "http_status": 500,
                                  "model_returned": False})


if __name__ == "__main__":
    unittest.main()
