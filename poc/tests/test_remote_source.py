"""Repository imports use pinned commits and never write outside their new snapshot."""

import io
import json
from pathlib import Path
import socket
import ssl
import stat
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyze_source import AnalysisCancelled
import remote_source
from remote_source import RemoteSourceError, fetch_remote_source, parse_remote_source, validate_remote_token


COMMIT = "a1" * 20
URL = "https://code.example.test/projects/DEMO/repos/business-source/browse/sit?at=refs%2Fheads%2Fmain"


def archive_bytes(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, contents in entries:
            archive.writestr(name, contents)
    return output.getvalue()


class Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}
        self.status = 200


class Opener:
    def __init__(self, archive, commits=None, headers=None):
        self.archive = archive
        self.commits = commits if commits is not None else {"values": [{"id": COMMIT}]}
        self.headers = headers
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if len(self.requests) == 1:
            return Response(json.dumps(self.commits).encode())
        return Response(self.archive, self.headers)


class RemoteSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="remote-source-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = self.root / "snapshot"
        self.spec = parse_remote_source(URL)

    def opener(self, entries=None, **options):
        entries = entries if entries is not None else [
            ("source/", ""), ("source/sit/", ""),
            ("source/sit/programs/entry.cbl", "IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRY.\n"),
            ("source/sit/copybooks/fields.cpy", "01 STATUS-CODE PIC X.\n")]
        return Opener(archive_bytes(entries), **options)

    def test_browser_url_preserves_subtree_ref_and_context(self):
        spec = parse_remote_source("https://CODE.example.test:8443/review/projects/DEMO/repos/business-source/browse/test%20area?at=refs%2Fheads%2Frelease%2F1")
        self.assertEqual(spec.base_url, "https://code.example.test:8443/review")
        self.assertEqual(spec.project, "DEMO")
        self.assertEqual(spec.repository, "business-source")
        self.assertEqual(spec.subdirectory, "test area")
        self.assertEqual(spec.ref, "refs/heads/release/1")
        self.assertEqual(parse_remote_source(spec.public_metadata()["url"]), spec)

    def test_repository_and_clone_links_allow_explicit_scope(self):
        for url in ["https://code.example.test/projects/DEMO/repos/business-source",
                    "https://code.example.test/scm/DEMO/business-source.git"]:
            with self.subTest(url=url):
                spec = parse_remote_source(url, ref="release/2", subdirectory="sit/programs/")
                self.assertEqual(spec.ref, "release/2")
                self.assertEqual(spec.subdirectory, "sit/programs")
                self.assertEqual(spec.api_url, "https://code.example.test/rest/api/1.0/projects/DEMO/repos/business-source")

    def test_explicit_values_override_browse_link(self):
        spec = parse_remote_source(URL, ref="refs/tags/stable", subdirectory="uat")
        self.assertEqual(spec.ref, "refs/tags/stable")
        self.assertEqual(spec.subdirectory, "uat")

    def test_unsafe_or_ambiguous_urls_are_rejected_before_download(self):
        for url in [URL.replace("https:", "http:"), URL.replace("https:", "file:"),
                    URL.replace("//", "//user:secret@", 1), URL + "&token=secret", URL + "&at=another",
                    URL + "#line-1", URL.replace("/browse/sit", "/commits/abcd"),
                    URL.replace("/browse/sit", "/browse/../sit"), URL.replace("/browse/sit", "/browse/%2e%2e/sit"),
                    URL.replace("/browse/sit", "/browse/sit%2Fother"), URL.replace("/browse/sit", "/browse/sit\\other"),
                    URL + "\n", " " + URL, URL.replace(".test", ".test:99999"),
                    "https://code.example.test/repository", URL.replace("/DEMO/", "/%00/")]:
            with self.subTest(url=url), self.assertRaises(RemoteSourceError) as raised:
                parse_remote_source(url)
            self.assertEqual(raised.exception.code, "REMOTE_SOURCE_INVALID_URL")
            self.assertNotIn("secret", str(raised.exception))

    def test_cloud_requires_a_separate_supported_adapter(self):
        with self.assertRaises(RemoteSourceError) as raised:
            parse_remote_source("https://bitbucket.org/workspace/business-source/src/main")
        self.assertEqual(raised.exception.code, "REMOTE_SOURCE_UNSUPPORTED")

    def test_invalid_refs_paths_and_tokens_have_safe_errors(self):
        for value in ["main\n", "../main", "main~1", "main..other", "-main", "refs//heads/main", 4]:
            with self.subTest(ref=value), self.assertRaises(RemoteSourceError) as raised:
                parse_remote_source(URL, ref=value)
            self.assertEqual(raised.exception.code, "REMOTE_SOURCE_INVALID_REF")
        for value in ["../sit", "/sit", "C:/sit", "sit\\other", "sit/NUL.cbl", "sit./other", "sit//other", 4]:
            with self.subTest(path=value), self.assertRaises(RemoteSourceError) as raised:
                parse_remote_source(URL, subdirectory=value)
            self.assertEqual(raised.exception.code, "REMOTE_SOURCE_INVALID_PATH")
        for value in [" secret", "secret\r\nInjected: yes", "x" * 8193, 3]:
            with self.subTest(token=value), self.assertRaises(RemoteSourceError) as raised:
                validate_remote_token(value)
            self.assertEqual(raised.exception.code, "REMOTE_SOURCE_INVALID_TOKEN")
            self.assertNotIn("secret", str(raised.exception))

    def test_download_pins_commit_and_only_extracts_selected_directory(self):
        opener = self.opener()
        events = []
        metadata = fetch_remote_source(self.spec, self.destination, token="read-only-token", opener=opener, progress=events.append)
        self.assertEqual(metadata["commit"], COMMIT)
        self.assertEqual(metadata["file_count"], 2)
        self.assertEqual(metadata["subdirectory"], "sit")
        self.assertEqual((self.destination / "copybooks/fields.cpy").read_text(), "01 STATUS-CODE PIC X.\n")
        self.assertFalse((self.destination / "source").exists())
        self.assertEqual(len(opener.requests), 2)
        requests = [request for request, _ in opener.requests]
        self.assertEqual(parse_qs(urlsplit(requests[0].full_url).query), {"limit": ["1"], "until": ["refs/heads/main"]})
        self.assertEqual(parse_qs(urlsplit(requests[1].full_url).query), {"at": [COMMIT], "format": ["zip"], "prefix": ["source/"], "path": ["sit"]})
        for request, timeout in opener.requests:
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.get_header("Authorization"), "Bearer read-only-token")
            self.assertEqual(timeout, remote_source.REQUEST_TIMEOUT_SECONDS)
        self.assertNotIn("read-only-token", json.dumps([metadata, events]))
        self.assertEqual({event["phase"] for event in events}, {"remote_resolving", "remote_downloading", "remote_extracting"})

    def test_root_snapshot_uses_default_branch_without_an_authorization_header(self):
        spec = parse_remote_source("https://code.example.test/projects/DEMO/repos/business-source")
        opener = self.opener([("source/entry.cbl", "PROGRAM-ID. ENTRY.")])
        fetch_remote_source(spec, self.destination, opener=opener)
        self.assertTrue((self.destination / "entry.cbl").is_file())
        first, second = [request for request, _ in opener.requests]
        self.assertNotIn("until", parse_qs(urlsplit(first.full_url).query))
        self.assertNotIn("path", parse_qs(urlsplit(second.full_url).query))
        self.assertIsNone(first.get_header("Authorization"))

    def test_existing_destination_and_symlink_are_never_modified(self):
        self.destination.mkdir()
        (self.destination / "keep.cbl").write_text("old source")
        for path in [self.destination, self.root / "linked-source"]:
            if path != self.destination:
                path.symlink_to(self.destination, target_is_directory=True)
            with self.subTest(path=path), self.assertRaises(RemoteSourceError) as raised:
                fetch_remote_source(self.spec, path, opener=self.opener())
            self.assertEqual(raised.exception.code, "REMOTE_SOURCE_DESTINATION_EXISTS")
        self.assertEqual((self.destination / "keep.cbl").read_text(), "old source")

    def test_malicious_archive_paths_are_rejected_and_partial_files_removed(self):
        for name in ["../escape.cbl", "/escape.cbl", "source/sit/../../escape.cbl", "source/sit/C:/escape.cbl",
                     "source/sit/dir\\escape.cbl", "source/sit/NUL.txt", "source/sit/CON", "source/sit/dir./a.cbl",
                     "source/sit/file.cbl:stream", "source/other/private.cbl", "source/sit//entry.cbl"]:
            with self.subTest(name=name), self.assertRaises(RemoteSourceError) as raised:
                fetch_remote_source(self.spec, self.destination, opener=self.opener([("source/sit/good.cbl", "good"), (name, "bad")]))
            self.assertEqual(raised.exception.code, "REMOTE_SOURCE_INVALID_ARCHIVE")
            self.assertFalse(self.destination.exists())
            self.assertEqual(list(self.root.iterdir()), [])

    def test_symlink_archive_is_rejected(self):
        entry = zipfile.ZipInfo("source/sit/linked.cbl")
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(RemoteSourceError) as raised:
            fetch_remote_source(self.spec, self.destination, opener=self.opener([(entry, "../../outside")]))
        self.assertEqual(raised.exception.code, "REMOTE_SOURCE_INVALID_ARCHIVE")
        self.assertFalse(self.destination.exists())

    def test_case_collisions_and_file_directory_collisions_are_rejected(self):
        for entries in [[("source/sit/one.cbl", "1"), ("source/sit/ONE.cbl", "2")],
                        [("source/sit/folder", "1"), ("source/sit/folder/entry.cbl", "2")],
                        [("source/sit/one.cbl/", ""), ("source/sit/one.cbl", "2")],
                        [("source/sit/A/entry.cbl", "1"), ("source/sit/a/other.cbl", "2")],
                        [("source/sit/é/entry.cbl", "1"), ("source/sit/e\u0301/other.cbl", "2")],
                        [("source/sit/FOLDER", "1"), ("source/sit/folder/entry.cbl", "2")]]:
            with self.subTest(entries=entries), self.assertRaises(RemoteSourceError) as raised:
                fetch_remote_source(self.spec, self.destination, opener=self.opener(entries))
            self.assertEqual(raised.exception.code, "REMOTE_SOURCE_INVALID_ARCHIVE")
            self.assertFalse(self.destination.exists())

    def test_transfer_and_extract_limits_stop_before_publication(self):
        for setting, limit in [("MAX_ARCHIVE_BYTES", 10), ("MAX_EXTRACTED_BYTES", 1), ("MAX_FILES", 1),
                               ("MAX_ZIP_DIRECTORY_BYTES", 10)]:
            with self.subTest(setting=setting), patch.object(remote_source, setting, limit), self.assertRaises(RemoteSourceError) as raised:
                fetch_remote_source(self.spec, self.destination, opener=self.opener())
            self.assertEqual(raised.exception.code, "REMOTE_SOURCE_TOO_LARGE")
            self.assertFalse(self.destination.exists())

    def test_zip_directory_count_is_checked_before_zipfile_allocates_entries(self):
        raw = bytearray(self.opener().archive)
        offset = raw.rfind(b"PK\x05\x06")
        # A forged footer must not bypass the actual directory entry count.
        struct.pack_into("<HH", raw, offset + 8, 1, 1)
        with patch.object(remote_source.zipfile, "ZipFile", side_effect=AssertionError("must reject before parsing")):
            with self.assertRaises(RemoteSourceError) as raised:
                fetch_remote_source(self.spec, self.destination, opener=Opener(bytes(raw)))
        self.assertEqual(raised.exception.code, "REMOTE_SOURCE_INVALID_ARCHIVE")

    def test_zip64_directory_can_be_read_without_loading_unbounded_metadata(self):
        with patch.object(zipfile, "ZIP_FILECOUNT_LIMIT", 1):
            opener = self.opener()
        metadata = fetch_remote_source(self.spec, self.destination, opener=opener)
        self.assertEqual(metadata["file_count"], 2)

    def test_declared_transfer_limit_is_checked_before_reading_archive(self):
        opener = self.opener(headers={"Content-Length": str(remote_source.MAX_ARCHIVE_BYTES + 1)})
        with self.assertRaises(RemoteSourceError) as raised:
            fetch_remote_source(self.spec, self.destination, opener=opener)
        self.assertEqual(raised.exception.code, "REMOTE_SOURCE_TOO_LARGE")

    def test_truncated_download_and_html_response_do_not_publish_source(self):
        for opener in [self.opener(headers={"Content-Length": "99999"}), Opener(b"<html>Login required</html>")]:
            with self.subTest(opener=opener), self.assertRaises(RemoteSourceError) as raised:
                fetch_remote_source(self.spec, self.destination, opener=opener)
            self.assertEqual(raised.exception.code, "REMOTE_SOURCE_INVALID_ARCHIVE")
            self.assertFalse(self.destination.exists())

    def test_missing_and_malformed_commit_response(self):
        for commits, code in [({"values": []}, "REMOTE_SOURCE_NOT_FOUND"), ({"values": [{"id": "main"}]}, "REMOTE_SOURCE_INVALID_RESPONSE"),
                              ({"values": "not a list"}, "REMOTE_SOURCE_INVALID_RESPONSE"), ({}, "REMOTE_SOURCE_INVALID_RESPONSE")]:
            with self.subTest(commits=commits), self.assertRaises(RemoteSourceError) as raised:
                fetch_remote_source(self.spec, self.destination, opener=self.opener(commits=commits))
            self.assertEqual(raised.exception.code, code)
            self.assertFalse(self.destination.exists())

    def test_empty_selected_directory_is_not_a_successful_import(self):
        with self.assertRaises(RemoteSourceError) as raised:
            fetch_remote_source(self.spec, self.destination, opener=self.opener([("source/", ""), ("source/sit/", "")]))
        self.assertEqual(raised.exception.code, "REMOTE_SOURCE_EMPTY")
        self.assertFalse(self.destination.exists())

    def test_cancellation_during_every_phase_is_preserved_and_cleaned_up(self):
        for phase in ["remote_resolving", "remote_downloading", "remote_extracting"]:
            state = {"cancelled": False}

            def progress(event):
                if event["phase"] == phase:
                    state["cancelled"] = True

            def cancel():
                if state["cancelled"]:
                    raise AnalysisCancelled("cancelled")

            with self.subTest(phase=phase), self.assertRaises(AnalysisCancelled):
                fetch_remote_source(self.spec, self.destination, opener=self.opener(), progress=progress, check_cancel=cancel)
            self.assertFalse(self.destination.exists())

    def test_errors_do_not_echo_remote_body_url_or_token(self):
        for status, code in [(401, "REMOTE_SOURCE_AUTH_FAILED"), (403, "REMOTE_SOURCE_AUTH_FAILED"),
                             (404, "REMOTE_SOURCE_NOT_FOUND"), (302, "REMOTE_SOURCE_REDIRECT"), (500, "REMOTE_SOURCE_NETWORK_FAILED")]:
            opener = self.opener()
            opener.open = lambda *args, **kwargs: (_ for _ in ()).throw(HTTPError("secret-url", status, "secret-body", {}, io.BytesIO(b"secret-token")))
            with self.subTest(status=status), self.assertRaises(RemoteSourceError) as raised:
                fetch_remote_source(self.spec, self.destination, token="secret-token", opener=opener)
            self.assertEqual(raised.exception.code, code)
            self.assertNotIn("secret", str(raised.exception))
            self.assertFalse(self.destination.exists())

    def test_network_tls_and_timeout_errors_are_classified(self):
        for reason, code in [(ssl.SSLCertVerificationError("secret-host"), "REMOTE_SOURCE_TLS_FAILED"),
                             (socket.timeout("secret-host"), "REMOTE_SOURCE_TIMEOUT"), (OSError("secret-host"), "REMOTE_SOURCE_NETWORK_FAILED")]:
            opener = self.opener()
            opener.open = lambda *args, **kwargs: (_ for _ in ()).throw(URLError(reason))
            with self.subTest(code=code), self.assertRaises(RemoteSourceError) as raised:
                fetch_remote_source(self.spec, self.destination, opener=opener)
            self.assertEqual(raised.exception.code, code)
            self.assertNotIn("secret-host", str(raised.exception))

    def test_redirect_handler_never_creates_request_to_another_host(self):
        handler = remote_source._NoRedirect()
        with self.assertRaises(RemoteSourceError) as raised:
            handler.redirect_request(None, None, 302, "redirect", {}, "https://other.example.test/login")
        self.assertEqual(raised.exception.code, "REMOTE_SOURCE_REDIRECT")

    def test_total_time_budget_applies_even_when_connections_keep_returning_data(self):
        with patch.object(remote_source.time, "monotonic", side_effect=[0, remote_source.TRANSFER_TIMEOUT_SECONDS + 1]):
            with self.assertRaises(RemoteSourceError) as raised:
                fetch_remote_source(self.spec, self.destination, opener=self.opener())
        self.assertEqual(raised.exception.code, "REMOTE_SOURCE_TIMEOUT")
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
