"""Repository imports use pinned commits and never write outside their new snapshot."""

import io
import base64
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
from remote_source import (RemoteSourceError, fetch_remote_source, parse_remote_source,
                           resolve_remote_source, validate_remote_token, validate_remote_username)


COMMIT = "a1" * 20
URL = "https://code.example.test/projects/DEMO/repos/business-source/browse/sit?at=refs%2Fheads%2Fmain"


def archive_bytes(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, contents in entries:
            if isinstance(name, str):
                entry = zipfile.ZipInfo(name)
                # The writer normalizes Windows separators. Malformed names
                # must reach the reader unchanged to exercise its rejection.
                entry.filename = entry.orig_filename = name
                entry.compress_type = zipfile.ZIP_DEFLATED
                name = entry
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


class SequenceOpener:
    def __init__(self, *bodies):
        self.bodies = iter(bodies)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        body = next(self.bodies)
        return Response(json.dumps(body).encode() if isinstance(body, dict) else body)


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

    def test_browser_subdirectory_cannot_be_reinterpreted_as_another_repository(self):
        for subtree in ("projects/ARCHIVE/repos/history", "projects/ARCHIVE/repos/history/browse/sit"):
            with self.subTest(subtree=subtree):
                spec = parse_remote_source("https://code.example.test/review/projects/DEMO/repos/business-source/browse/"
                                           + subtree + "?at=main")
                self.assertEqual(spec.base_url, "https://code.example.test/review")
                self.assertEqual((spec.project, spec.repository), ("DEMO", "business-source"))
                self.assertEqual(spec.subdirectory, subtree)
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

    def test_cloud_browser_commit_and_encoded_branch_links_preserve_scope(self):
        for ref in [COMMIT, "main", "release/next"]:
            with self.subTest(ref=ref):
                url_ref = ref.replace("/", "%2F")
                spec = parse_remote_source("https://bitbucket.org/sample-workspace/business-source/src/" + url_ref + "/sit/programs/")
                self.assertEqual(spec.provider, "bitbucket_cloud")
                self.assertEqual(spec.api_url, "https://api.bitbucket.org/2.0/repositories/sample-workspace/business-source")
                self.assertEqual(spec.ref, ref)
                self.assertEqual(spec.subdirectory, "sit/programs")
                self.assertEqual(parse_remote_source(spec.public_metadata()["url"]), spec)

    def test_cloud_root_clone_and_explicit_scope(self):
        for ending in ["", "/", ".git"]:
            spec = parse_remote_source("https://bitbucket.org/sample-workspace/business-source" + ending,
                                       subdirectory="sit", ref="release/next")
            self.assertEqual(spec.repository, "business-source")
            self.assertEqual(spec.ref, "release/next")
            self.assertEqual(spec.subdirectory, "sit")
        spec = parse_remote_source("https://bitbucket.org/sample-workspace/business-source", subdirectory="sit")
        public = spec.public_metadata()
        self.assertIsNone(public["ref"])
        self.assertEqual(parse_remote_source(public["url"], subdirectory=public["subdirectory"]), spec)

    def test_cloud_unsafe_routes_do_not_change_the_download_host_or_path(self):
        for url in ["https://bitbucket.org:8443/sample-workspace/business-source",
                    "https://api.bitbucket.org/2.0/repositories/sample-workspace/business-source",
                    "https://bitbucket.org/sample-workspace/business-source/src/main/sit%2Fother",
                    "https://bitbucket.org/sample-workspace/business-source/src/main/../other",
                    "https://bitbucket.org/sample-workspace/business-source/pull-requests/1",
                    "https://bitbucket.org/sample-workspace/business-source/src/main?token=private-value"]:
            with self.subTest(url=url), self.assertRaises(RemoteSourceError):
                parse_remote_source(url)

    def test_revision_check_reads_metadata_without_downloading_or_writing_source(self):
        opener = self.opener()
        metadata = resolve_remote_source(self.spec, opener=opener)
        self.assertEqual(metadata["commit"], COMMIT)
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cloud_default_branch_is_resolved_before_commits_not_all_branches(self):
        spec = parse_remote_source("https://bitbucket.org/sample-workspace/business-source", subdirectory="sit")
        opener = SequenceOpener({"mainbranch": {"name": "release/next"}}, {"values": [{"hash": COMMIT}]})
        metadata = resolve_remote_source(spec, opener=opener, username="reader@example.test", token="api-token")
        self.assertEqual(metadata["default_branch"], "release/next")
        self.assertIsNone(metadata["ref"])
        self.assertEqual(metadata["commit"], COMMIT)
        self.assertEqual([r.full_url for r, _ in opener.requests], [spec.api_url,
            spec.api_url + "/commits/release%2Fnext?pagelen=1"])
        expected = "Basic " + base64.b64encode(b"reader@example.test:api-token").decode()
        self.assertTrue(all(r.get_header("Authorization") == expected for r, _ in opener.requests))
        self.assertNotIn("reader@example.test", json.dumps(metadata))
        self.assertNotIn("api-token", json.dumps(metadata))

    def test_cloud_archive_pins_commit_filters_tree_and_reuses_resolved_metadata(self):
        spec = parse_remote_source("https://bitbucket.org/sample-workspace/business-source/src/main/sit")
        generated = "sample-workspace-business-source-" + COMMIT[:12]
        raw = archive_bytes([(generated + "/README.txt", "outside"),
                             (generated + "/sit/entry.cbl", "PROGRAM-ID. ENTRY."),
                             (generated + "/sit/copy/fields.cpy", "01 FLAG PIC X."),
                             (generated + "/other/unrelated.cbl", "outside")])
        resolved = {**spec.public_metadata(), "commit": COMMIT}
        opener = SequenceOpener(raw)
        metadata = fetch_remote_source(spec, self.destination, opener=opener, resolved=resolved,
                                       username="reader@example.test", token="api-token")
        self.assertEqual(metadata["file_count"], 2)
        self.assertEqual({p.relative_to(self.destination).as_posix() for p in self.destination.rglob("*") if p.is_file()},
                         {"entry.cbl", "copy/fields.cpy"})
        self.assertEqual(len(opener.requests), 1)
        request = opener.requests[0][0]
        self.assertEqual(request.full_url, "https://bitbucket.org/sample-workspace/business-source/get/" + COMMIT + ".zip")
        self.assertTrue(request.get_header("Authorization").startswith("Basic "))
        self.assertNotIn("api-token", json.dumps(metadata))

    def test_cloud_access_token_and_pinned_reference_do_not_request_default_branch(self):
        spec = parse_remote_source("https://bitbucket.org/sample-workspace/business-source/src/" + COMMIT)
        opener = SequenceOpener({"values": [{"hash": COMMIT}]}, archive_bytes([("snapshot/entry.cbl", "entry")]))
        metadata = fetch_remote_source(spec, self.destination, opener=opener, token="access-token")
        self.assertEqual(metadata["ref"], COMMIT)
        self.assertNotIn("default_branch", metadata)
        self.assertEqual(len(opener.requests), 2)
        self.assertTrue(all(r.get_header("Authorization") == "Bearer access-token" for r, _ in opener.requests))

    def test_cloud_resolver_rejects_mismatched_pinned_commit_and_malformed_metadata(self):
        spec = parse_remote_source("https://bitbucket.org/sample-workspace/business-source/src/" + COMMIT)
        for metadata in [{"values": [{"hash": "b2" * 20}]}, {"values": [{"hash": "short"}]}, {"values": "wrong"}]:
            opener = SequenceOpener(metadata)
            with self.subTest(metadata=metadata), self.assertRaises(RemoteSourceError) as raised:
                fetch_remote_source(spec, self.destination, opener=opener)
            self.assertEqual(raised.exception.code, "REMOTE_SOURCE_INVALID_RESPONSE")
            self.assertFalse(self.destination.exists())
            self.assertEqual(len(opener.requests), 1)

    def test_resolved_commit_cannot_cross_sources_or_override_a_fixed_commit(self):
        for resolved in [{**self.spec.public_metadata(), "url": "https://other.example.test", "commit": COMMIT},
                         {**self.spec.public_metadata(), "commit": "../../outside"}]:
            with self.subTest(resolved=resolved), self.assertRaises(RemoteSourceError):
                fetch_remote_source(self.spec, self.destination, resolved=resolved, opener=SequenceOpener())
            self.assertFalse(self.destination.exists())

    def test_cloud_archive_rejects_unsafe_selected_entries_or_multiple_roots(self):
        spec = parse_remote_source("https://bitbucket.org/sample-workspace/business-source/src/main/sit")
        for entries in [[("snapshot/sit/../escape.cbl", "bad")],
                        [("snapshot/sit/a.cbl", "1"), ("snapshot/sit/A.cbl", "2")],
                        [("snapshot/sit/a.cbl", "1"), ("other/sit/b.cbl", "2")],
                        [("snapshot/sit/A/x.cbl", "1"), ("snapshot/sit/a/y.cbl", "2")]]:
            with self.subTest(entries=entries), self.assertRaises(RemoteSourceError) as raised:
                fetch_remote_source(spec, self.destination, opener=SequenceOpener(
                    {"values": [{"hash": COMMIT}]}, archive_bytes(entries)))
            self.assertEqual(raised.exception.code, "REMOTE_SOURCE_INVALID_ARCHIVE")
            self.assertFalse(self.destination.exists())

    def test_cloud_unselected_linux_names_do_not_block_portable_selected_tree(self):
        spec = parse_remote_source("https://bitbucket.org/sample-workspace/business-source/src/main/sit")
        raw = archive_bytes([("snapshot/sit/entry.cbl", "entry"), ("snapshot/tools/NUL.txt", "outside"),
                             ("snapshot/tools/aux", "outside"), ("snapshot/tools/task:debug", "outside"),
                             ("snapshot/tools/name.", "outside"), ("snapshot/tools/name ", "outside")])
        result = fetch_remote_source(spec, self.destination, opener=SequenceOpener(
            {"values": [{"hash": COMMIT}]}, raw))
        self.assertEqual(result["file_count"], 1)
        self.assertEqual([p.name for p in self.destination.iterdir()], ["entry.cbl"])

    def test_cloud_selected_windows_names_and_unselected_structural_escapes_remain_rejected(self):
        spec = parse_remote_source("https://bitbucket.org/sample-workspace/business-source/src/main/sit")
        for name in ["snapshot/sit/NUL.txt", "snapshot/sit/task:debug", "snapshot/sit/name.",
                     "snapshot/tools/../escape", "snapshot/tools/dir\\escape", "snapshot/tools//escape",
                     "/snapshot/tools/escape", "../tools/escape", "C:/tools/escape"]:
            with self.subTest(name=name), self.assertRaises(RemoteSourceError) as raised:
                fetch_remote_source(spec, self.destination, opener=SequenceOpener(
                    {"values": [{"hash": COMMIT}]}, archive_bytes([("snapshot/sit/entry.cbl", "entry"), (name, "bad")])))
            self.assertEqual(raised.exception.code, "REMOTE_SOURCE_INVALID_ARCHIVE")
            self.assertFalse(self.destination.exists())

    def test_cloud_selected_symlinks_and_symlink_ancestors_are_rejected(self):
        spec = parse_remote_source("https://bitbucket.org/sample-workspace/business-source/src/main/sit")
        for name in ["snapshot/", "snapshot/sit/", "snapshot/sit/linked.cbl"]:
            entry = zipfile.ZipInfo(name)
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            with self.subTest(name=name), self.assertRaises(RemoteSourceError) as raised:
                fetch_remote_source(spec, self.destination, opener=SequenceOpener(
                    {"values": [{"hash": COMMIT}]}, archive_bytes([(entry, "../../outside"),
                        ("snapshot/sit/entry.cbl", "entry")])))
            self.assertEqual(raised.exception.code, "REMOTE_SOURCE_INVALID_ARCHIVE")
            self.assertFalse(self.destination.exists())

    def test_account_names_reject_header_controls_without_echoing_credentials(self):
        for name in ["private-name:secret", "private-name\nInjected", " private-name", 3]:
            with self.subTest(name=name), self.assertRaises(RemoteSourceError) as raised:
                validate_remote_username(name)
            self.assertNotIn("private-name", str(raised.exception))

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

    def test_server_archive_records_every_selected_file_in_external_manifest(self):
        from remote_incremental import cached_snapshot_matches, manifest_path
        metadata = fetch_remote_source(self.spec, self.destination, opener=self.opener([
            ("source/sit/entry.cbl", "PROGRAM-ID. ENTRY."),
            ("source/sit/notes.md", "original notes")]))
        manifest = json.loads(manifest_path(self.destination).read_text())
        self.assertEqual(manifest["commit"], COMMIT)
        self.assertEqual(manifest["identity"][0], "bitbucket")
        self.assertEqual(set(manifest["files"]), {"entry.cbl", "notes.md"})
        self.assertTrue(cached_snapshot_matches(self.spec, self.destination, metadata))
        (self.destination / "notes.md").write_text("modified notes")
        self.assertFalse(cached_snapshot_matches(self.spec, self.destination, metadata))

    def test_server_existing_manifest_is_never_overwritten_or_removed(self):
        sidecar = self.destination.with_name(self.destination.name + ".remote-manifest.json")
        sidecar.write_text("prior manifest")
        opener = self.opener()
        with self.assertRaises(RemoteSourceError) as raised:
            fetch_remote_source(self.spec, self.destination, opener=opener)
        self.assertEqual(raised.exception.code, "REMOTE_SOURCE_DESTINATION_EXISTS")
        self.assertEqual(sidecar.read_text(), "prior manifest")
        self.assertFalse(self.destination.exists())
        self.assertFalse(opener.requests)

    def test_failed_server_manifest_publication_cleans_only_new_snapshot(self):
        sidecar = self.destination.with_name(self.destination.name + ".remote-manifest.json")

        def fail_manifest(*args):
            sidecar.write_text("partial manifest")
            raise AnalysisCancelled("cancelled")

        with patch("remote_incremental.write_manifest", side_effect=fail_manifest), self.assertRaises(AnalysisCancelled):
            fetch_remote_source(self.spec, self.destination, opener=self.opener())
        self.assertFalse(self.destination.exists())
        self.assertFalse(sidecar.exists())

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
            with self.subTest(name=name):
                opener = self.opener([("source/sit/good.cbl", "good"), (name, "bad")])
                self.assertIn(name.encode("utf-8"), opener.archive)
                with zipfile.ZipFile(io.BytesIO(opener.archive)) as archive:
                    self.assertEqual(archive.infolist()[-1].orig_filename, name)
                with self.assertRaises(RemoteSourceError) as raised:
                    fetch_remote_source(self.spec, self.destination, opener=opener)
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
