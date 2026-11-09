"""Metadata-only deployment checks using fake transports and no external access."""

from contextlib import redirect_stdout
import base64
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from online_material_format import MaterialError
from online_preflight import (CloudMetadataClient, MAX_METADATA_BYTES, PENDING_GATES,
                              main, run_checks)
from online_source_client import _NoRedirect


COMMIT = "a" * 40


class Response(io.BytesIO):
    def __init__(self, data=None, *, raw=None, status=200, headers=None, url=None):
        super().__init__(raw if raw is not None else json.dumps(data or {}).encode())
        self.status, self.headers, self.url = status, headers or {}, url

    def geturl(self):
        return self.url


class Opener:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        response.url = response.url or request.full_url
        return response


def source_responses(*extra):
    return Opener(Response({"full_name": "example/source"}), Response({"hash": COMMIT}), *extra)


def material_responses(*extra):
    return Opener(Response({"full_name": "example/materials"}), Response({"values": []}), *extra)


class MetadataClientTests(unittest.TestCase):
    def assert_code(self, code, operation):
        with self.assertRaises(MaterialError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_fixed_metadata_routes_use_only_get_and_no_body(self):
        opener = Opener(Response({"full_name": "example/source"}), Response({"hash": COMMIT}),
                        Response({"values": []}), Response({"enabled": True}))
        client = CloudMetadataClient("example/source", opener=opener)
        client.repository_metadata()
        client.commit_metadata(COMMIT)
        client.downloads_probe()
        client.pipeline_config()
        expected = ["?fields=full_name", "/commit/" + COMMIT + "?fields=hash",
                    "/downloads?pagelen=1", "/pipelines_config"]
        self.assertEqual([request.full_url for request, _ in opener.requests],
            ["https://api.bitbucket.org/2.0/repositories/example/source" + suffix for suffix in expected])
        for request, timeout in opener.requests:
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            self.assertEqual(timeout, 30)
        self.assertEqual(client.requests, 4)
        self.assertGreater(client.received_bytes, 0)

    def test_unsafe_repository_and_nonimmutable_commit_are_refused_without_network(self):
        for value in ("https://bitbucket.org/example/source", "example/source/extra", "a/..", "../source",
                      "example/source?token=private", "example/source\n", "example", None):
            self.assert_code("PREFLIGHT_INVALID_REPOSITORY", lambda: CloudMetadataClient(value))
        opener = Opener()
        client = CloudMetadataClient("example/source", opener=opener)
        for commit in ("main", "a" * 7, "A" * 40, "a" * 40 + "/src"):
            self.assert_code("PREFLIGHT_INVALID_COMMIT", lambda: client.commit_metadata(commit))
        self.assertEqual(opener.requests, [])

    def test_credentials_support_basic_and_bearer_without_default_redirects(self):
        for username in (None, "reader@example.invalid"):
            opener = Opener(Response({"full_name": "example/source"}))
            with mock.patch("online_preflight.build_opener", return_value=opener) as build:
                CloudMetadataClient("example/source", username=username, token="private-token").repository_metadata()
            expected = ("Bearer private-token" if username is None else "Basic " +
                        base64.b64encode((username + ":private-token").encode()).decode())
            self.assertEqual(opener.requests[0][0].get_header("Authorization"), expected)
            self.assertIsInstance(build.call_args.args[0], _NoRedirect)

    def test_response_and_implicit_transport_redirects_are_rejected_and_closed(self):
        for response in (Response(status=302, headers={"Location": "https://outside.invalid/private"}),
                         Response({"full_name": "example/source"}, url="https://outside.invalid/private")):
            opener = Opener(response)
            self.assert_code("PREFLIGHT_REDIRECT_REJECTED", lambda:
                CloudMetadataClient("example/source", token="private-token", opener=opener).repository_metadata())
            self.assertEqual(len(opener.requests), 1)
            self.assertTrue(response.closed)

    def test_metadata_limits_apply_before_and_during_read(self):
        cases = [(Response(headers={"Content-Length": str(MAX_METADATA_BYTES + 1)}), "PREFLIGHT_SIZE_LIMIT"),
                 (Response(raw=b"x" * (MAX_METADATA_BYTES + 1)), "PREFLIGHT_SIZE_LIMIT"),
                 (Response(raw=b"{}", headers={"Content-Length": "3"}), "PREFLIGHT_LENGTH_MISMATCH"),
                 (Response(headers={"Content-Length": "-1"}), "PREFLIGHT_INVALID_RESPONSE"),
                 (Response(headers={"Content-Encoding": "gzip"}), "PREFLIGHT_ENCODING_UNSUPPORTED")]
        for response, code in cases:
            self.assert_code(code, lambda: CloudMetadataClient("example/source", opener=Opener(response)).repository_metadata())
            self.assertTrue(response.closed)

    def test_nonjson_duplicate_keys_and_arrays_are_rejected(self):
        for raw in (b"private source body", b'{"full_name":"first","full_name":"second"}', b'{"x":NaN}', b"[]"):
            with self.assertRaises(MaterialError) as caught:
                CloudMetadataClient("example/source", opener=Opener(Response(raw=raw))).repository_metadata()
            self.assertIn(caught.exception.code, {"PREFLIGHT_INVALID_JSON", "PREFLIGHT_INVALID_RESPONSE"})
            self.assertNotIn("private", str(caught.exception))

    def test_get_retries_are_bounded_and_error_bodies_are_never_read(self):
        waits = []
        responses = [Response(raw=b"private-error", status=429, headers={"Retry-After": "999"}) for _ in range(3)]
        opener = Opener(*responses)
        client = CloudMetadataClient("example/source", opener=opener, wait=waits.append)
        self.assert_code("PREFLIGHT_RATE_LIMIT", client.repository_metadata)
        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(waits, [5, 5])
        self.assertEqual(client.received_bytes, 0)
        self.assertTrue(all(response.closed for response in responses))
        self.assert_code("PREFLIGHT_INVALID_RETRY_LIMIT", lambda: CloudMetadataClient("example/source", attempts=4))

    def test_connection_errors_and_http_error_bodies_are_sanitized(self):
        opener = Opener(*(URLError("https://user:private@outside.invalid/") for _ in range(3)))
        self.assert_code("PREFLIGHT_CONNECTION_FAILED", lambda:
            CloudMetadataClient("example/source", opener=opener, wait=lambda _: None).repository_metadata())
        error = HTTPError("https://api.bitbucket.org/2.0/repositories/example/source?fields=full_name",
                          403, "private-error", {}, io.BytesIO(b"private-body"))
        self.assert_code("PREFLIGHT_FORBIDDEN", lambda:
            CloudMetadataClient("example/source", opener=Opener(error)).repository_metadata())
        self.assertTrue(error.fp.closed)

    def test_metadata_identity_and_boolean_pipeline_contract_are_strict(self):
        self.assert_code("PREFLIGHT_REPOSITORY_MISMATCH", lambda:
            CloudMetadataClient("example/source", opener=Opener(Response({"full_name": "other/source"}))).repository_metadata())
        self.assert_code("PREFLIGHT_COMMIT_MISMATCH", lambda:
            CloudMetadataClient("example/source", opener=Opener(Response({"hash": "b" * 40}))).commit_metadata(COMMIT))
        for enabled in (None, 1, "true"):
            self.assert_code("PREFLIGHT_INVALID_RESPONSE", lambda:
                CloudMetadataClient("example/source", opener=Opener(Response({"enabled": enabled}))).pipeline_config())

    def test_downloads_only_probes_first_page_and_retains_no_names_or_links(self):
        opener = Opener(Response({"values": [{"name": "private-name", "links": {"self": "https://outside.invalid/private"}}],
                                  "next": "https://outside.invalid/private"}))
        client = CloudMetadataClient("example/source", opener=opener)
        self.assertEqual(client.downloads_probe(), {"readable": True})
        self.assertEqual(len(opener.requests), 1)
        for values in ({}, "private-name", [{}, {}]):
            self.assert_code("PREFLIGHT_INVALID_RESPONSE", lambda:
                CloudMetadataClient("example/source", opener=Opener(Response({"values": values}))).downloads_probe())


class ReadinessTests(unittest.TestCase):
    def run_check(self, source=None, material=None, **kwargs):
        return run_checks("example/source", COMMIT, "example/materials", source_opener=source or source_responses(),
                          material_opener=material or material_responses(), wait=lambda _: None, **kwargs)

    def test_success_only_confirms_requested_reads_and_keeps_manual_gates_pending(self):
        result = self.run_check()
        self.assertEqual(result["status"], "read_checks_passed")
        self.assertEqual([item["status"] for item in result["checks"]], ["pass"] * 4)
        self.assertEqual(result["metrics"]["requests"], 4)
        self.assertEqual({item["check"] for item in result["pending_gates"]}, set(PENDING_GATES) | {"pipelines_enabled"})
        self.assertTrue(all(item["status"] == "not_assessed" for item in result["pending_gates"]))
        self.assertFalse(result["source_content_read"] or result["write_performed"] or result["model_called"])

    def test_source_and_material_credentials_never_cross_channels(self):
        source, material = source_responses(), material_responses()
        result = self.run_check(source, material, source_auth={"token": "source-secret"},
                                material_auth={"token": "material-secret", "username": "reader@example.invalid"})
        for request, _ in source.requests:
            self.assertEqual(request.get_header("Authorization"), "Bearer source-secret")
        expected = "Basic " + base64.b64encode(b"reader@example.invalid:material-secret").decode()
        for request, _ in material.requests:
            self.assertEqual(request.get_header("Authorization"), expected)
        raw = json.dumps(result)
        for secret in ("source-secret", "material-secret", "reader@example.invalid", "Authorization", "https://"):
            self.assertNotIn(secret, raw)

    def test_pipeline_false_is_blocked_but_admin_forbidden_is_unknown(self):
        for response, expected, row_status in ((Response({"enabled": False}), "blocked", "fail"),
                                              (Response(status=403), "incomplete", "unknown"),
                                              (Response({"enabled": True}), "read_checks_passed", "pass")):
            result = self.run_check(source=source_responses(response), check_pipelines=True)
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["checks"][-1]["status"], row_status)
            self.assertNotIn("pipelines_enabled", [item["check"] for item in result["pending_gates"]])

    def test_missing_hidden_and_unreachable_resources_stay_unknown(self):
        for response in (Response(status=404), Response(status=429), URLError("private-host")):
            source = Opener(response, Response({"hash": COMMIT}))
            result = self.run_check(source=source, attempts=1)
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["checks"][0]["status"], "unknown")
            self.assertNotIn("private-host", json.dumps(result))

    def test_source_or_material_unauthorized_and_mismatched_commit_block(self):
        for status in (401, 403):
            source = Opener(Response(status=status), Response({"hash": COMMIT}))
            result = self.run_check(source=source)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["checks"][0]["status"], "fail")
            material = Opener(Response({"full_name": "example/materials"}), Response(status=status))
            self.assertEqual(self.run_check(material=material)["status"], "blocked")
        source = Opener(Response({"full_name": "example/source"}), Response({"hash": "b" * 40}))
        result = self.run_check(source=source)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["checks"][1]["code"], "PREFLIGHT_COMMIT_MISMATCH")

    def test_all_input_and_credential_validation_precedes_network(self):
        source, material = Opener(), Opener()
        for overrides in ({"commit": "main"}, {"material_repository": "bad/path/extra"},
                          {"material_auth": {"token": "private\nvalue"}}, {"check_pipelines": "true"}):
            values = {"source_repository": "example/source", "commit": COMMIT, "material_repository": "example/materials",
                      "source_opener": source, "material_opener": material, **overrides}
            with self.assertRaises(MaterialError):
                run_checks(**values)
        self.assertEqual(source.requests + material.requests, [])


class CommandTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "report.json"

    def invoke(self, *extra, output=None, commit=COMMIT):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["check", "--source-repository", "example/source", "--commit", commit,
                         "--material-repository", "example/materials", "--output", str(output or self.output), *extra])
        return code, json.loads(stdout.getvalue())

    def test_explicit_check_reads_only_environment_credentials_and_writes_new_report(self):
        with mock.patch("online_preflight.run_checks", return_value={"status": "read_checks_passed"}) as check, \
             mock.patch.dict("os.environ", {"ONLINE_SOURCE_TOKEN": "source-secret", "ONLINE_MATERIAL_TOKEN": "material-secret"}):
            code, summary = self.invoke("--check-pipelines")
        self.assertEqual((code, summary["status"]), (0, "read_checks_passed"))
        self.assertTrue(check.call_args.kwargs["check_pipelines"])
        self.assertEqual(check.call_args.kwargs["source_auth"]["token"], "source-secret")
        self.assertEqual(check.call_args.kwargs["material_auth"]["token"], "material-secret")
        self.assertEqual(json.loads(self.output.read_text()), {"status": "read_checks_passed"})

    def test_existing_missing_parent_symlink_or_invalid_commit_do_not_issue_requests(self):
        self.output.write_text("keep")
        link = self.root / "link.json"
        link.symlink_to(self.root / "missing.json")
        with mock.patch("online_preflight.run_checks") as check:
            for output in (self.output, self.root / "missing" / "report.json", link):
                code, summary = self.invoke(output=output)
                self.assertEqual(code, 2)
                self.assertEqual(summary["status"], "blocked")
            code, summary = self.invoke(output=self.root / "new.json", commit="main")
            self.assertEqual((code, summary["error"]), (2, "PREFLIGHT_INVALID_COMMIT"))
            check.assert_not_called()
        self.assertEqual(self.output.read_text(), "keep")
        self.assertFalse((self.root / "new.json").exists())

    def test_incomplete_and_failed_checks_have_distinct_exit_codes(self):
        for index, (status, expected) in enumerate((("blocked", 2), ("incomplete", 3))):
            with mock.patch("online_preflight.run_checks", return_value={"status": status}):
                code, summary = self.invoke(output=self.root / (str(index) + ".json"))
            self.assertEqual((code, summary["status"]), (expected, status))

    def test_unexpected_exception_writes_only_sanitized_failure(self):
        with mock.patch("online_preflight.run_checks", side_effect=ValueError("private-secret https://private.invalid")):
            code, summary = self.invoke()
        self.assertEqual((code, summary["status"]), (3, "incomplete"))
        self.assertNotIn("private", self.output.read_text())
        self.assertEqual(json.loads(self.output.read_text())["error"], "PREFLIGHT_OPERATION_FAILED")


if __name__ == "__main__":
    unittest.main()
