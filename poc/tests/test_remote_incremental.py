"""Pinned HTTP sync downloads changed files and preserves independently hashed old snapshots."""

import hashlib
from contextlib import closing
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlsplit
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyze_source import AnalysisCancelled
from remote_source import fetch_remote_source, parse_remote_source, RemoteSourceError
from remote_incremental import manifest_path


OLD, NEW = "a1" * 20, "b2" * 20
BASE = "/2.0/repositories/sample-workspace/business-source"


def change(status, old=None, new=None):
    return {"type": "diffstat", "status": status,
            "old": {"type": "commit_file", "path": old} if old else None,
            "new": {"type": "commit_file", "path": new} if new else None}


class LocalTransport:
    """Real loopback HTTP; map the test's canonical repository URLs locally."""
    def __init__(self, server):
        self.server = server

    def open(self, request, timeout):
        url = urlsplit(request.full_url)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=timeout)
        connection.request(request.get_method(), url.path + ("?" + url.query if url.query else ""),
                           headers=dict(request.header_items()))
        response = connection.getresponse()
        if response.status >= 400:
            raise HTTPError(request.full_url, response.status, "test response", response.headers, response)
        return response


class RemoteIncrementalTests(unittest.TestCase):
    scale_observation = None

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="incremental-source-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.old = self.root / "old"
        self.new = self.root / "new"
        self.spec = parse_remote_source("https://bitbucket.org/sample-workspace/business-source/src/main/stage")
        self.files = {
            OLD: {"stage/steady.cbl": b"UNCHANGED\r\n", "stage/change.cbl": b"BEFORE\n",
                  "stage/delete.cbl": b"REMOVE\n", "stage/move.cbl": b"MOVE\n", "other/remote.cbl": b"OUTSIDE\n"},
            NEW: {"stage/steady.cbl": b"UNCHANGED\r\n", "stage/change.cbl": b"AFTER\n",
                  "stage/new.cbl": b"ADDED\n", "stage/renamed.cbl": b"MOVE\n", "other/remote.cbl": b"NEW OUTSIDE\n"},
        }
        self.changes = [change("modified", "stage/change.cbl", "stage/change.cbl"),
                        change("removed", "stage/delete.cbl"), change("added", new="stage/new.cbl"),
                        change("renamed", "stage/move.cbl", "stage/renamed.cbl"),
                        change("modified", "other/remote.cbl", "other/remote.cbl")]
        self.requests, self.raw_paths = [], []
        self.override = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                owner.requests.append(self.path)
                try:
                    answer = owner.override(self.path) if owner.override else None
                    status, body = answer if answer is not None else (200, owner.route(self.path))
                    if isinstance(body, dict):
                        body = json.dumps(body).encode()
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as exc:
                    self.send_error(500, type(exc).__name__)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.transport = LocalTransport(self.server)

    def entry(self, commit, path):
        return {"type": "commit_file", "path": path, "commit": {"hash": commit},
                "size": len(self.files[commit][path]), "attributes": []}

    def route(self, path):
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        if parsed.path.startswith("/sample-workspace/business-source/get/"):
            commit = parsed.path.rsplit("/", 1)[-1].removesuffix(".zip")
            result = io.BytesIO()
            with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, body in self.files[commit].items():
                    archive.writestr("snapshot/" + name, body)
            return result.getvalue()
        if parsed.path == BASE + "/commits/main":
            return {"values": [{"hash": OLD}]}
        if parsed.path == BASE + "/diffstat/" + NEW + ".." + OLD:
            self.assertEqual(query.get("topic"), ["false"])
            self.assertEqual(query.get("renames"), ["true"])
            page = int(query.get("page", ["1"])[0])
            result = {"values": self.changes[(page - 1) * 2:page * 2], "size": len(self.changes)}
            if page * 2 < len(self.changes):
                result["next"] = "https://api.bitbucket.org" + parsed.path + "?topic=false&renames=true&pagelen=100&page=" + str(page + 1)
            return result
        if parsed.path.startswith(BASE + "/src/"):
            commit, name = unquote(parsed.path[len(BASE + "/src/"):]).split("/", 1)
            if name.endswith("/") or not name:
                prefix = name.rstrip("/")
                contents = [self.entry(commit, file) for file in self.files[commit]
                            if str(Path(file).parent).replace("\\", "/") == prefix]
                return {"values": contents, "size": len(contents)}
            if query.get("format") == ["meta"]:
                return self.entry(commit, name)
            self.raw_paths.append((commit, name))
            return self.files[commit][name]
        raise AssertionError(path)

    def initial(self):
        metadata = fetch_remote_source(self.spec, self.old, opener=self.transport)
        self.assertEqual(metadata["sync_mode"], "archive")
        self.requests.clear(); self.raw_paths.clear()
        return metadata

    def sync(self, previous, **kwargs):
        return fetch_remote_source(self.spec, self.new, opener=self.transport,
            resolved={**self.spec.public_metadata(), "commit": NEW}, previous_source=self.old,
            previous_metadata=previous, **kwargs)

    def old_bytes(self):
        return {p.relative_to(self.old).as_posix(): p.read_bytes() for p in self.old.rglob("*") if p.is_file()}

    def test_real_http_paged_changes_only_download_changed_files_and_keep_old_bytes(self):
        previous = self.initial(); before = self.old_bytes(); events = []
        summary = self.sync(previous, progress=events.append)
        expected = {path.removeprefix("stage/"): body for path, body in self.files[NEW].items() if path.startswith("stage/")}
        self.assertEqual({p.relative_to(self.new).as_posix(): p.read_bytes() for p in self.new.rglob("*") if p.is_file()}, expected)
        self.assertEqual(self.old_bytes(), before)
        self.assertEqual(summary["sync_mode"], "incremental")
        self.assertEqual(summary["reused_files"], 1)
        self.assertEqual(summary["downloaded_files"], 3)
        self.assertEqual(summary["downloaded_bytes"], len(b"AFTER\nADDED\nMOVE\n"))
        self.assertEqual(summary["removed_files"], 2)
        self.assertEqual({path for _, path in self.raw_paths}, {"stage/change.cbl", "stage/new.cbl", "stage/renamed.cbl"})
        self.assertFalse(any("/get/" in path for path in self.requests))
        self.assertTrue(manifest_path(self.new).is_file())
        self.assertTrue(any(event["phase"] == "remote_syncing" for event in events))
        (self.new / "steady.cbl").write_bytes(b"NEW LOCAL EDIT")
        self.assertEqual((self.old / "steady.cbl").read_bytes(), before["steady.cbl"])

    def test_twelve_thousand_file_snapshot_fetches_one_changed_file_not_the_archive(self):
        self.files[OLD] = {f"stage/programs/item-{number:05}.cbl": b"NEUTRAL SOURCE\r\n" * 64
                           for number in range(12202)}
        self.files[NEW] = dict(self.files[OLD])
        selected = "stage/programs/item-06100.cbl"
        self.files[NEW][selected] = b"UPDATED SOURCE\r\n"
        self.changes = [change("modified", selected, selected)]
        previous = self.initial()
        started = time.monotonic()
        summary = self.sync(previous)
        self.assertEqual(summary["file_count"], 12202)
        self.assertEqual(summary["reused_files"], 12201)
        self.assertEqual(summary["downloaded_files"], 1)
        self.assertEqual(summary["downloaded_bytes"], len(self.files[NEW][selected]))
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.raw_paths, [(NEW, selected)])
        type(self).scale_observation = {**summary, "source_files": 12202,
            "http_requests": len(self.requests), "archive_requests": sum("/get/" in value for value in self.requests),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "source_bytes": sum(len(value) for key, value in self.files[OLD].items() if key.startswith("stage/"))}

    def test_legacy_hashes_from_another_source_are_never_used(self):
        previous = self.initial(); manifest_path(self.old).unlink()
        output = self.root / "analysis"; output.mkdir()
        with closing(sqlite3.connect(output / "source-versions.sqlite")) as db:
            db.execute("CREATE TABLE source_versions(revision INTEGER PRIMARY KEY,source_key TEXT,manifest_json TEXT)")
            db.execute("INSERT INTO source_versions VALUES(1,?,?)", ("different-source", json.dumps({
                name: hashlib.sha256(body).hexdigest() for name, body in self.old_bytes().items()})))
            db.commit()
        result = self.sync(previous, previous_output=output)
        self.assertEqual(result["reused_files"], 0)
        self.assertEqual(result["downloaded_files"], 4)

    def test_local_symlink_cache_does_not_read_or_copy_outside_source(self):
        previous = self.initial()
        outside = self.root / "outside.cbl"; outside.write_bytes(b"PRIVATE OUTSIDE")
        path = self.old / "steady.cbl"; path.unlink()
        path.symlink_to(outside)
        summary = self.sync(previous)
        self.assertEqual(summary["downloaded_files"], 4)
        self.assertEqual((self.new / "steady.cbl").read_bytes(), b"UNCHANGED\r\n")
        self.assertEqual(outside.read_bytes(), b"PRIVATE OUTSIDE")

    def test_source_limits_and_duplicate_changes_are_not_silently_truncated(self):
        previous = self.initial()
        with patch("remote_incremental.MAX_FILES", 2), self.assertRaises(RemoteSourceError):
            self.sync(previous)
        self.assertFalse(self.new.exists())
        self.changes = [change("modified", "stage/change.cbl", "stage/change.cbl")] * 2
        with self.assertRaises(RemoteSourceError): self.sync(previous)
        self.assertFalse(self.new.exists())

    def test_incremental_credentials_never_enter_manifest_metadata_or_progress(self):
        previous = self.initial(); events = []
        result = self.sync(previous, token="private-test-token", username="reader@example.test", progress=events.append)
        retained = json.dumps([result, events]) + manifest_path(self.new).read_text()
        self.assertNotIn("private-test-token", retained)
        self.assertNotIn("reader@example.test", retained)

    def test_legacy_snapshot_migrates_using_original_index_hashes_without_unchanged_download(self):
        previous = self.initial(); manifest_path(self.old).unlink()
        output = self.root / "analysis"; output.mkdir()
        hashes = {name: hashlib.sha256(body).hexdigest() for name, body in self.old_bytes().items()}
        with closing(sqlite3.connect(output / "source-versions.sqlite")) as db:
            db.execute("CREATE TABLE source_versions(revision INTEGER PRIMARY KEY,source_key TEXT,manifest_json TEXT)")
            db.execute("INSERT INTO source_versions VALUES(1,?,?)", (hashlib.sha256(str(self.old.resolve()).encode()).hexdigest(), json.dumps(hashes)))
            db.commit()
        summary = self.sync(previous, previous_output=output)
        self.assertEqual(summary["downloaded_files"], 3)
        self.assertEqual(summary["reused_files"], 1)
        self.assertTrue(any("/src/" + OLD + "/stage/" in url for url in self.requests))
        self.assertNotIn((NEW, "stage/steady.cbl"), self.raw_paths)

    def test_local_modified_cached_file_is_repaired_in_new_snapshot_only(self):
        previous = self.initial()
        (self.old / "steady.cbl").write_bytes(b"CORRUPTED\n")
        summary = self.sync(previous)
        self.assertEqual(summary["downloaded_files"], 4)
        self.assertEqual((self.new / "steady.cbl").read_bytes(), b"UNCHANGED\r\n")
        self.assertEqual((self.old / "steady.cbl").read_bytes(), b"CORRUPTED\n")

    def test_cross_scope_renames_fetch_only_new_in_scope_file(self):
        previous = self.initial()
        self.files[NEW] = {**self.files[OLD]}
        self.files[NEW]["other/left.cbl"] = self.files[NEW].pop("stage/move.cbl")
        self.files[NEW]["stage/entered.cbl"] = self.files[NEW].pop("other/remote.cbl")
        self.changes = [change("renamed", "stage/move.cbl", "other/left.cbl"),
                        change("renamed", "other/remote.cbl", "stage/entered.cbl")]
        summary = self.sync(previous)
        self.assertEqual(summary["downloaded_files"], 1)
        self.assertEqual(self.raw_paths, [(NEW, "stage/entered.cbl")])
        self.assertFalse((self.new / "move.cbl").exists())

    def test_commit_changed_outside_scope_copies_cache_with_zero_source_download(self):
        previous = self.initial(); self.changes = [change("modified", "other/remote.cbl", "other/remote.cbl")]
        summary = self.sync(previous)
        self.assertEqual(summary["downloaded_bytes"], 0)
        self.assertEqual(summary["downloaded_files"], 0)
        self.assertEqual(summary["reused_files"], 4)
        self.assertEqual(self.raw_paths, [])

    def test_same_commit_uses_verified_local_files_without_any_request(self):
        previous = self.initial()
        result = fetch_remote_source(self.spec, self.new, opener=self.transport, resolved=previous,
            previous_source=self.old, previous_metadata=previous)
        self.assertEqual(self.requests, [])
        self.assertEqual(result["reused_files"], 4)

    def test_failures_and_rate_limit_never_fall_back_to_zip_or_change_old_snapshot(self):
        for status, code in ((404, "REMOTE_SOURCE_NOT_FOUND"), (429, "REMOTE_SOURCE_RATE_LIMITED"),
                             (401, "REMOTE_SOURCE_AUTH_FAILED")):
            with self.subTest(status=status):
                if not self.old.exists(): previous = self.initial()
                before = self.old_bytes(); self.requests.clear()
                self.override = lambda path: (status, b"not included in public errors") if "/diffstat/" in path else None
                with self.assertRaises(RemoteSourceError) as raised: self.sync(previous)
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(self.old_bytes(), before)
                self.assertFalse(self.new.exists())
                self.assertFalse(manifest_path(self.new).exists())
                self.assertFalse(any("/get/" in path for path in self.requests))

    def test_bad_pagination_or_missing_entries_cannot_publish_partial_snapshot(self):
        previous = self.initial()
        for following in ("https://evil.example.test/next", "https://api.bitbucket.org" + BASE + "/commits/main",
                          "https://api.bitbucket.org" + BASE + "/diffstat/" + NEW + ".." + OLD + "?topic=true&renames=true&pagelen=100&page=2", None):
            with self.subTest(following=following):
                self.requests.clear()
                self.override = lambda path: (200, {"values": self.changes[:1], "size": 5,
                    **({"next": following} if following else {})}) if "/diffstat/" in path else None
                with self.assertRaises(RemoteSourceError): self.sync(previous)
                self.assertFalse(self.new.exists())
                self.assertEqual(len(self.requests), 1)

    def test_next_page_is_opaque_and_may_omit_defaults_or_use_a_cursor(self):
        previous = self.initial()
        expected_next = "https://api.bitbucket.org" + BASE + "/diffstat/" + NEW + ".." + OLD + "?cursor=next-slice&fields=-links&pagelen=50"
        def override(path):
            if "/diffstat/" not in path: return None
            if "cursor=next-slice" in path:
                return 200, {"values": self.changes[2:], "size": len(self.changes)}
            return 200, {"values": self.changes[:2], "size": len(self.changes), "next": expected_next}
        self.override = override
        result = self.sync(previous)
        self.assertEqual(result["downloaded_files"], 3)
        self.assertIn(urlsplit(expected_next).path + "?" + urlsplit(expected_next).query, self.requests)

    def test_explicit_empty_next_without_size_does_not_publish_a_partial_change_set(self):
        previous = self.initial()
        self.override = lambda path: (200, {"values": self.changes[:1], "next": ""}) if "/diffstat/" in path else None
        with self.assertRaises(RemoteSourceError): self.sync(previous)
        self.assertFalse(self.new.exists())
        self.assertFalse(manifest_path(self.new).exists())

    def test_portable_path_and_case_conflicts_are_rejected_without_old_changes(self):
        previous = self.initial(); before = self.old_bytes()
        for name in ("stage/../escape.cbl", "stage/NUL.cbl", "stage/STEADY.cbl", "stage/steady.cbl/nested.cbl"):
            with self.subTest(name=name):
                self.changes = [change("added", new=name)]
                with self.assertRaises(RemoteSourceError): self.sync(previous)
                self.assertFalse(self.new.exists())
                self.assertEqual(self.old_bytes(), before)

    def test_bad_raw_length_symlinks_and_metadata_revision_are_rejected(self):
        previous = self.initial(); baseline = self.old_bytes()
        for mutation in ("size", "link", "commit"):
            with self.subTest(mutation=mutation):
                def override(path):
                    if "format=meta" not in path: return None
                    item = self.entry(NEW, "stage/change.cbl")
                    if mutation == "size": item["size"] += 1
                    elif mutation == "link": item["attributes"] = ["link"]
                    else: item["commit"] = {"hash": OLD}
                    return 200, item
                self.override = override
                with self.assertRaises(RemoteSourceError): self.sync(previous)
                self.assertFalse(self.new.exists())
                self.assertEqual(self.old_bytes(), baseline)

    def test_cancellation_removes_new_snapshot_and_preserves_old(self):
        previous = self.initial(); before = self.old_bytes()
        def progress(event):
            if event["phase"] == "remote_syncing" and event["completed"] == 2:
                raise AnalysisCancelled()
        with self.assertRaises(AnalysisCancelled): self.sync(previous, progress=progress)
        self.assertEqual(self.old_bytes(), before)
        self.assertFalse(self.new.exists())
        self.assertFalse(manifest_path(self.new).exists())

    def test_different_repository_or_manifest_version_never_reuses_cached_bytes(self):
        previous = self.initial()
        for corrupt in ("repository", "manifest"):
            with self.subTest(corrupt=corrupt):
                old = dict(previous)
                if corrupt == "repository": old["url"] = old["url"].replace("sample-workspace", "different-workspace")
                else:
                    payload = json.loads(manifest_path(self.old).read_text()); payload["commit"] = NEW
                    manifest_path(self.old).write_text(json.dumps(payload))
                self.requests.clear()
                with self.assertRaises(RemoteSourceError): self.sync(old)
                self.assertEqual(self.requests, [])
                self.assertFalse(self.new.exists())


if __name__ == "__main__":
    unittest.main()
