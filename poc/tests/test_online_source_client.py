"""Pinned source transport is read-only, bounded, and never follows redirects."""

import base64
import io
from pathlib import Path
import sys
import unittest
from urllib.error import HTTPError, URLError
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from online_material_format import MAX_SOURCE_BYTES, MaterialError
from online_source_client import PinnedSourceClient, _NoRedirect


COMMIT = "a" * 40


class Response(io.BytesIO):
    def __init__(self, data=b"source", *, status=200, headers=None, url=None):
        super().__init__(data)
        self.status, self.headers, self.url = status, headers or {}, url

    def geturl(self):
        return self.url


class Opener:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.requests = response or Response(), error, []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        if self.response.url is None:
            self.response.url = request.full_url
        return self.response


class PinnedSourceClientTests(unittest.TestCase):
    def client(self, opener, **kwargs):
        return PinnedSourceClient("sample-workspace", "business-source", COMMIT, opener=opener, **kwargs)

    def assert_code(self, code, callable_):
        with self.assertRaises(MaterialError) as caught:
            callable_()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_pinned_read_quotes_each_path_and_uses_only_get(self):
        opener = Opener(Response(b"body"))
        client = self.client(opener)
        self.assertEqual(client.read_file(relative_path="src/a ?#% name.cbl"), b"body")
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, "https://api.bitbucket.org/2.0/repositories/sample-workspace/business-source/src/" + COMMIT + "/src/a%20%3F%23%25%20name.cbl")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(client.commit, COMMIT)
        self.assertGreater(timeout, 0)
        self.assertIsNone(request.get_header("Authorization"))

    def test_basic_and_bearer_credentials(self):
        for username in (None, "reader@example.test"):
            with self.subTest(username=username):
                opener = Opener()
                self.client(opener, token="read-only-value", username=username).read_file("entry.cbl")
                expected = ("Bearer read-only-value" if username is None else "Basic " +
                            base64.b64encode((username + ":read-only-value").encode()).decode())
                self.assertEqual(opener.requests[0][0].get_header("Authorization"), expected)

    def test_invalid_paths_never_issue_requests(self):
        opener = Opener()
        client = self.client(opener)
        for path in ("", "../entry.cbl", "a/../entry.cbl", "/entry.cbl", "a\\entry.cbl", "a//b", "a\n"):
            with self.subTest(path=path):
                self.assert_code("MATERIAL_INVALID_PATH", lambda: client.read_file(path))
        self.assertEqual(opener.requests, [])

    def test_branch_and_unsafe_repository_identifiers_are_rejected(self):
        self.assert_code("MATERIAL_INVALID_COMMIT", lambda: PinnedSourceClient("sample", "source", "main"))
        for slug in ("../source", "source/other", "https://host", "name@host", ""):
            self.assert_code("SOURCE_INVALID_REPOSITORY", lambda: PinnedSourceClient(slug, "source", COMMIT))

    def test_invalid_credentials_are_rejected_without_output(self):
        for options in ({"token": "secret\nvalue"}, {"username": "a:b", "token": "secret"},
                        {"username": "reader@example.test"}, {"token": "secret value"}, {"token": ""}):
            self.assert_code("SOURCE_INVALID_CREDENTIAL", lambda: self.client(Opener(), **options))

    def test_status_failures_are_distinct_sanitized_and_not_retried(self):
        expected = {401: "SOURCE_UNAUTHORIZED", 403: "SOURCE_FORBIDDEN", 404: "SOURCE_NOT_FOUND",
                    429: "SOURCE_RATE_LIMIT", 500: "SOURCE_HTTP_ERROR", 302: "SOURCE_REDIRECT"}
        for status, code in expected.items():
            with self.subTest(status=status):
                opener = Opener(error=HTTPError("https://host/private", status, "sensitive response", {}, io.BytesIO(b"secret")))
                self.assert_code(code, lambda: self.client(opener, token="secret").read_file("entry.cbl"))
                self.assertEqual(len(opener.requests), 1)

    def test_response_redirect_and_followed_redirect_are_rejected(self):
        for response in (Response(status=302), Response(url="https://other.example.test/content")):
            self.assert_code("SOURCE_REDIRECT", lambda: self.client(Opener(response)).read_file("entry.cbl"))
            self.assertTrue(response.closed)

    def test_response_is_closed_after_success_and_size_failure(self):
        response = Response(b"1234")
        self.assertEqual(self.client(Opener(response)).read_file("entry.cbl"), b"1234")
        self.assertTrue(response.closed)
        response = Response(b"1234")
        self.assert_code("SOURCE_SIZE_LIMIT", lambda: self.client(Opener(response)).read_file("entry.cbl", max_bytes=3))
        self.assertTrue(response.closed)

    def test_default_transport_disables_redirects(self):
        opener = Opener()
        with patch("online_source_client.build_opener", return_value=opener) as build:
            PinnedSourceClient("sample", "source", COMMIT).read_file("entry.cbl")
        handler = build.call_args.args[0]
        self.assertIsInstance(handler, _NoRedirect)
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://other.example.test"))

    def test_size_limits_apply_with_and_without_content_length(self):
        for response in (Response(b"12345"), Response(b"12345", headers={"Content-Length": "5"})):
            self.assert_code("SOURCE_SIZE_LIMIT", lambda: self.client(Opener(response)).read_file("entry.cbl", max_bytes=4))
        self.assert_code("SOURCE_SIZE_LIMIT", lambda: self.client(Opener()).read_file("entry.cbl", max_bytes=MAX_SOURCE_BYTES + 1))

    def test_empty_source_and_exact_limit_are_supported(self):
        self.assertEqual(self.client(Opener(Response(b""))).read_file("empty.cbl", max_bytes=0), b"")
        self.assertEqual(self.client(Opener(Response(b"1234"))).read_file("entry.cbl", max_bytes=4), b"1234")

    def test_content_length_is_validated(self):
        for length, code in (("4", "SOURCE_LENGTH_MISMATCH"), ("-1", "SOURCE_INVALID_RESPONSE"),
                             ("9" * 100, "SOURCE_INVALID_RESPONSE")):
            self.assert_code(code, lambda: self.client(Opener(Response(b"12", headers={"Content-Length": length}))).read_file("entry.cbl"))

    def test_lfs_pointer_and_media_type_are_not_treated_as_source(self):
        for response in (Response(b"version https://git-lfs.github.com/spec/v1\noid sha256:123\nsize 5\n"),
                         Response(b"{}", headers={"Content-Type": "application/vnd.git-lfs+json"})):
            self.assert_code("SOURCE_LFS_UNSUPPORTED", lambda: self.client(Opener(response)).read_file("entry.cbl"))

    def test_network_errors_are_sanitized(self):
        for error, code in ((URLError("secret-host-token"), "SOURCE_CONNECTION_FAILED"),
                            (TimeoutError("secret response"), "SOURCE_TIMEOUT")):
            self.assert_code(code, lambda: self.client(Opener(error=error)).read_file("entry.cbl"))


if __name__ == "__main__":
    unittest.main()
