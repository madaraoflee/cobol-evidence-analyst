"""English API diagnostics retain the existing allowlist and safe metadata."""

from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api_error_details import (SCHEMA_VERSION, TRANSPORT_EXPLANATIONS, _EXPLANATIONS,
                               build_diagnostic, format_diagnostic, sanitize_diagnostic)


LOCAL_ID = "local-" + "a" * 32
UPSTREAM_ID = "req_12345678"
PRIVATE_TEXT = "private-source-fragment-and-credential"


class DiagnosticLanguageTests(unittest.TestCase):
    def diagnostic(self, category="system_error", **values):
        return {"schema_version": SCHEMA_VERSION, "category": category,
                "request_id": LOCAL_ID, "evidence_source": "local",
                "reason": PRIVATE_TEXT, "next_step": PRIVATE_TEXT, **values}

    def assert_english_safe(self, text):
        self.assertTrue(text)
        self.assertIsNone(re.search(r"[\u3400-\u9fff]", text), text)
        self.assertNotIn(PRIVATE_TEXT, text)

    def test_every_allowed_category_has_english_reason_and_next_step(self):
        for category in _EXPLANATIONS:
            with self.subTest(category=category):
                value = self.diagnostic(category)
                before = value.copy()
                answer = format_diagnostic(value, language="en")
                self.assert_english_safe(answer)
                explanation = answer.split(" Local correlation ID:")[0]
                self.assertGreaterEqual(explanation.count("."), 2)
                self.assertIn("Local correlation ID: " + LOCAL_ID + ".", answer)
                self.assertEqual(value, before)

    def test_every_transport_reason_has_english_explanation(self):
        for reason in TRANSPORT_EXPLANATIONS:
            with self.subTest(reason=reason):
                category = "timeout" if reason == "timeout" else "connection"
                value = self.diagnostic(category, transport_reason=reason)
                answer = format_diagnostic(value, language="en")
                self.assert_english_safe(answer)
                self.assertNotIn(TRANSPORT_EXPLANATIONS[reason], answer)
                self.assertNotIn("The API could not be reached.", answer)
                self.assertEqual(sanitize_diagnostic(value)["transport_reason"], reason)

    def test_transport_labels_cannot_override_an_unrelated_category(self):
        for source, category, reason in (("provider_code", "connection", "connection_refused"),
                                         ("local", "timeout", "connection_refused"),
                                         ("local", "authentication", "timeout"),
                                         ("local", "connection", PRIVATE_TEXT)):
            with self.subTest(source=source, category=category, reason=reason):
                answer = format_diagnostic(self.diagnostic(category, evidence_source=source,
                                                          transport_reason=reason), language="en")
                expected = format_diagnostic(self.diagnostic(category), language="en")
                self.assertEqual(answer, expected)

    def test_sanitized_structure_and_default_chinese_are_unchanged(self):
        value = self.diagnostic("authentication", http_status=401,
                                upstream_request_id=UPSTREAM_ID, upstream_request_id_source="header")
        safe_before = sanitize_diagnostic(value)
        english = format_diagnostic(value, language="en")
        self.assert_english_safe(english)
        self.assertEqual(sanitize_diagnostic(value), safe_before)
        self.assertEqual(format_diagnostic(value),
            "接口认证失败。 检查 API 密钥及认证配置。 HTTP 401。 "
            f"本地关联编号：{LOCAL_ID}。 上游关联编号：{UPSTREAM_ID}。")
        self.assertEqual(format_diagnostic(value, language="zh-CN"), format_diagnostic(value))
        self.assertEqual(safe_before["reason"], "接口认证失败。")

    def test_english_formats_only_valid_http_status_and_correlation_ids(self):
        value = self.diagnostic("http_5xx_unknown", http_status=503,
                                upstream_request_id=UPSTREAM_ID, upstream_request_id_source="body")
        answer = format_diagnostic(value, language="en")
        self.assert_english_safe(answer)
        self.assertIn("HTTP 503.", answer)
        self.assertIn("Local correlation ID: " + LOCAL_ID + ".", answer)
        self.assertIn("Upstream correlation ID: " + UPSTREAM_ID + ".", answer)
        value.update(http_status=PRIVATE_TEXT, upstream_request_id=PRIVATE_TEXT)
        answer = format_diagnostic(value, language="en")
        self.assert_english_safe(answer)
        self.assertNotIn("Upstream correlation ID:", answer)
        self.assertNotIn("HTTP", answer)

    def test_raw_provider_error_and_request_material_are_not_reflected(self):
        value = build_diagnostic("REQUEST_FAILED", http_status=429,
            body='{"error":{"code":"insufficient_quota","message":"' + PRIVATE_TEXT + '"}}',
            protected_values=(PRIVATE_TEXT,), request_body=PRIVATE_TEXT)
        answer = format_diagnostic(value, language="en")
        self.assert_english_safe(answer)
        self.assertIn("insufficient quota or remaining usage allowance", answer)
        self.assertIn("HTTP 429.", answer)

    def test_invalid_diagnostic_never_exposes_input_or_creates_a_display_id(self):
        for value in (None, PRIVATE_TEXT, {"reason": PRIVATE_TEXT},
                      self.diagnostic(PRIVATE_TEXT), self.diagnostic(request_id=PRIVATE_TEXT),
                      self.diagnostic(schema_version=PRIVATE_TEXT)):
            with self.subTest(value=value):
                self.assertEqual(format_diagnostic(value, language="en"), "")

    def test_unconfirmed_http_429_remains_distinct_from_known_rate_limit(self):
        unknown = format_diagnostic(build_diagnostic("HTTP_ERROR", http_status=429), language="en")
        known = format_diagnostic(build_diagnostic("HTTP_ERROR", http_status=429,
            body='{"error":{"code":"rate_limit_exceeded"}}'), language="en")
        self.assertIn("without a confirmed cause", unknown)
        self.assertIn("explicitly reported a request rate limit", known)
        self.assertNotIn("explicitly reported a request rate limit", unknown)


if __name__ == "__main__":
    unittest.main()
