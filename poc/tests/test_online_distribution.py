"""Offline transport and publication tests; no real repository is contacted."""

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from online_distribution import (BUNDLE_SCHEMA, DownloadsClient, DownloadsObjectStore,
    MaterialError, PublicationUnknown, SNAPSHOT_RELEASE_SCHEMA, SNAPSHOT_SCHEMA, SYNC_SCHEMA,
    _bundles, load_release, publish_material, validate_release)
from online_material_format import (SCHEMA_VERSION, bucket, canonical_json, digest,
    object_reference, seal_manifest)
from online_material_reader import MaterialReader
from online_publish import main


class Response(io.BytesIO):
    def __init__(self, body=b"", status=200, headers=None, url=None):
        super().__init__(body)
        self.status, self.headers, self.url = status, headers or {}, url

    def geturl(self):
        return self.url


class Opener:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def open(self, request, timeout):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        response.url = response.url or request.full_url
        return response


class MemoryClient:
    def __init__(self):
        self.data, self.puts, self.reads = {}, [], []
        self.network_fetched_bytes, self.network_requests = 0, 0
        self.fail_after = None

    def put_immutable(self, name, data):
        if self.fail_after is not None and len(self.puts) >= self.fail_after:
            raise MaterialError("DISTRIBUTION_FORBIDDEN")
        self.puts.append(name)
        uploaded = name not in self.data
        if not uploaded and self.data[name] != data:
            raise MaterialError("DISTRIBUTION_CONTENT_CONFLICT")
        self.data[name] = data
        return {"name": name, "uploaded": uploaded, "verified": True}

    def read(self, name, max_bytes):
        self.reads.append(name)
        self.network_requests += 1
        if name not in self.data:
            raise MaterialError("DISTRIBUTION_NOT_FOUND")
        data = self.data[name]
        self.network_fetched_bytes += len(data)
        if len(data) > max_bytes:
            raise MaterialError("DISTRIBUTION_SIZE_LIMIT")
        return data

    def list_release_names(self):
        return sorted(name for name in self.data if name.startswith("analysis-release-"))


def material(root, *, commit="a" * 40, verification="git_checked"):
    root.mkdir()
    (root / "objects").mkdir()

    def put(value):
        raw = canonical_json(value)
        ref = object_reference(raw)
        (root / ref["path"]).write_bytes(raw)
        return ref

    routes = {"files": {}, "terms": {}, "references": {}}
    for path in ("entry.cbl", "worker.cbl"):
        sha = digest((path + "\n").encode())
        fact = put({"kind": "facts", "relative_path": path, "source_sha256": sha,
                    "symbols": [{"name": path}], "relations": [], "rules": []})
        item = {"relative_path": path, "sha256": sha, "size": len(path) + 1, "encoding": "utf-8",
                "source_format": "free", "line_count": 1, "facts": [fact]}
        routes["files"][bucket(path)] = put({"kind": "files", "entries": {path: item}})
    routes["terms"][bucket("entry")] = put({"kind": "terms", "entries": {"entry": {"count": 1, "paths": ["entry.cbl"]}}})
    manifest = seal_manifest({"schema_version": SCHEMA_VERSION,
        "repository": {"identity": "example/source", "commit": commit, "subdirectory": "src"},
        "analysis": {"commit_verification": verification}, "coverage": {"indexed_files": 2}, "routes": routes})
    (root / "material.json").write_bytes(canonical_json(manifest))
    receipt = {"schema_version": SYNC_SCHEMA, "repository": "example/source", "commit": commit,
               "subdirectory": "src", "complete": True, "completed_at": "2026-11-07T00:00:00Z", "sync_id": "sync-001"}
    return manifest, receipt


class TransportTests(unittest.TestCase):
    def assert_code(self, code, operation):
        with self.assertRaises(MaterialError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def name(self, data=b"{}"):
        return "analysis-object-" + digest(data) + ".json"

    def test_redirect_strips_authorization_and_both_responses_close(self):
        first = Response(status=302, headers={"Location": "https://bbuseruploads.s3.amazonaws.com/object?signature=secret"})
        second = Response(b"{}", headers={"Content-Length": "2"})
        opener = Opener(first, second)
        client = DownloadsClient("sample", "source", token="private-token", username="user@example.invalid", opener=opener)
        self.assertEqual(client.read(self.name(), 2), b"{}")
        self.assertIn("Authorization", opener.requests[0].headers)
        self.assertNotIn("Authorization", opener.requests[1].headers)
        self.assertTrue(first.closed and second.closed)
        self.assertEqual(client.network_fetched_bytes, 2)
        self.assertEqual(client.network_requests, 2)

    def test_redirect_rejects_unapproved_origin_credentials_http_and_second_hop(self):
        for location in ("http://bbuseruploads.s3.amazonaws.com/x", "https://evil.invalid/x",
                         "https://user@bbuseruploads.s3.amazonaws.com/x", "https://bbuseruploads.s3.amazonaws.com:8443/x",
                         "https://bbuseruploads.s3.amazonaws.com/x#fragment"):
            opener = Opener(Response(status=302, headers={"Location": location}))
            self.assert_code("DISTRIBUTION_UNSAFE_URL", lambda: DownloadsClient("sample", "source", opener=opener).read(self.name(), 2))
            self.assertEqual(len(opener.requests), 1)
        opener = Opener(Response(status=302, headers={"Location": "https://cdn.bitbucket.org/x"}),
                        Response(status=302, headers={"Location": "https://cdn.bitbucket.org/y"}))
        self.assert_code("DISTRIBUTION_REDIRECT_REJECTED", lambda: DownloadsClient("sample", "source", opener=opener).read(self.name(), 2))

    def test_followed_redirect_is_rejected_even_with_injected_transport(self):
        opener = Opener(Response(b"{}", url="https://elsewhere.invalid"))
        self.assert_code("DISTRIBUTION_UNEXPECTED_REDIRECT", lambda: DownloadsClient("sample", "source", opener=opener).read(self.name(), 2))

    def test_errors_are_bounded_and_get_retries_stop(self):
        for status, code in ((401, "UNAUTHORIZED"), (403, "FORBIDDEN"), (404, "NOT_FOUND")):
            url = "https://api.bitbucket.org/2.0/repositories/sample/source/downloads/" + self.name()
            error = HTTPError(url, status, "private body", {}, io.BytesIO(b"private body"))
            opener = Opener(error)
            self.assert_code("DISTRIBUTION_" + code, lambda: DownloadsClient("sample", "source", opener=opener).read(self.name(), 2))
            self.assertTrue(error.fp.closed)
        opener = Opener(*(Response(status=429, headers={"Retry-After": "999"}) for _ in range(3)))
        waits = []
        self.assert_code("DISTRIBUTION_RATE_LIMIT", lambda: DownloadsClient("sample", "source", opener=opener, wait=waits.append).read(self.name(), 2))
        self.assertEqual(len(opener.requests), 3)
        self.assertLessEqual(sum(waits), 10.001)
        opener = Opener(Response(status=503), Response(b"{}"))
        self.assertEqual(DownloadsClient("sample", "source", opener=opener, wait=lambda _: None).read(self.name(), 2), b"{}")

    def test_cancellation_prevents_wait_retry_and_read(self):
        def cancel():
            raise MaterialError("DISTRIBUTION_CANCELLED")
        opener = Opener(Response(b"{}"))
        self.assert_code("DISTRIBUTION_CANCELLED", lambda: DownloadsClient("sample", "source", opener=opener, check_cancel=cancel).read(self.name(), 2))
        self.assertEqual(opener.requests, [])

    def test_body_limits_lengths_and_compression_are_enforced(self):
        for response, code in ((Response(b"123", headers={"Content-Length": "3"}), "SIZE_LIMIT"),
                               (Response(b"123"), "SIZE_LIMIT"),
                               (Response(b"1", headers={"Content-Length": "2"}), "LENGTH_MISMATCH"),
                               (Response(b"1", headers={"Content-Encoding": "gzip"}), "ENCODING_UNSUPPORTED"),
                               (Response(b"1", headers={"Content-Length": "-1"}), "INVALID_RESPONSE")):
            self.assert_code("DISTRIBUTION_" + code, lambda: DownloadsClient("sample", "source", opener=Opener(response)).read(self.name(), 2))
            self.assertTrue(response.closed)

    def test_content_names_prevent_conflicting_writes(self):
        opener = Opener()
        client = DownloadsClient("sample", "source", opener=opener)
        self.assert_code("DISTRIBUTION_CONTENT_NAME_MISMATCH", lambda: client.put_immutable(self.name(), b"different"))
        self.assertEqual(opener.requests, [])
        opener = Opener(Response(b"XX"))
        self.assert_code("DISTRIBUTION_CONTENT_CONFLICT", lambda: DownloadsClient("sample", "source", opener=opener).put_immutable(self.name(), b"{}"))

    def test_upload_is_verified_and_existing_content_is_reused(self):
        opener = Opener(Response(status=404), Response(b"{}", status=201), Response(b"{}"))
        client = DownloadsClient("sample", "source", opener=opener, token="write-token")
        self.assertTrue(client.put_immutable(self.name(), b"{}")["uploaded"])
        self.assertEqual([request.method for request in opener.requests], ["GET", "POST", "GET"])
        self.assertIn(self.name().encode(), opener.requests[1].data)
        self.assertIn(b'name="files"', opener.requests[1].data)
        opener = Opener(Response(b"{}"))
        self.assertFalse(DownloadsClient("sample", "source", opener=opener).put_immutable(self.name(), b"{}")["uploaded"])

    def test_unknown_upload_outcome_is_never_blindly_retried(self):
        opener = Opener(Response(status=404), URLError("private-token"))
        self.assert_code("DISTRIBUTION_CONNECTION_FAILED", lambda: DownloadsClient("sample", "source", opener=opener).put_immutable(self.name(), b"{}"))
        self.assertEqual([request.method for request in opener.requests], ["GET", "POST"])

    def test_bounded_multipart_batch_verifies_every_uploaded_file(self):
        first, second = b"{}", b'{"value":1}'
        opener = Opener(Response(status=404), Response(status=404), Response(b"{}", status=201),
                        Response(first), Response(second))
        client = DownloadsClient("sample", "source", opener=opener)
        result = client.put_immutable_many([(self.name(first), first), (self.name(second), second)])
        self.assertTrue(all(item["verified"] and item["uploaded"] for item in result))
        self.assertEqual([request.method for request in opener.requests], ["GET", "GET", "POST", "GET", "GET"])
        self.assertEqual(opener.requests[2].data.count(b'name="files"'), 2)
        self.assert_code("DISTRIBUTION_DUPLICATE_UPLOAD", lambda: client.put_immutable_many([(self.name(), b"{}"), (self.name(), b"{}")]))
        self.assert_code("DISTRIBUTION_UPLOAD_BATCH_LIMIT", lambda: client.put_immutable_many([]))

    def test_listing_only_returns_immutable_releases_and_validates_pagination(self):
        release = "analysis-release-" + "a" * 64 + ".json"
        next_url = "https://api.bitbucket.org/2.0/repositories/sample/source/downloads?page=2"
        opener = Opener(Response(canonical_json({"values": [{"name": "unrelated.txt"}], "next": next_url})),
                        Response(canonical_json({"values": [{"name": release}]})))
        self.assertEqual(DownloadsClient("sample", "source", opener=opener).list_release_names(), [release])
        for next_url in ("https://evil.invalid/", "https://api.bitbucket.org/2.0/repositories/other/source/downloads?page=2"):
            opener = Opener(Response(canonical_json({"values": [], "next": next_url})))
            self.assert_code("DISTRIBUTION_UNSAFE_URL", lambda: DownloadsClient("sample", "source", opener=opener).list_release_names())
        opener = Opener(Response(canonical_json({"values": [{"name": "x"}, {"name": "y"}]})))
        self.assert_code("DISTRIBUTION_DISCOVERY_LIMIT", lambda: DownloadsClient("sample", "source", opener=opener).list_release_names(max_entries=1))

    def test_release_list_metadata_is_display_only_and_optional(self):
        name = "analysis-release-" + "b" * 64 + ".json"
        opener = Opener(Response(canonical_json({"values": [{"name": name, "created_on": "2026-11-07T01:00:00+00:00", "size": 100}]})))
        self.assertEqual(DownloadsClient("sample", "source", opener=opener).list_release_entries(),
                         [{"name": name, "created_on": "2026-11-07T01:00:00+00:00", "size": 100}])


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.material = self.root / "material"
        self.manifest, self.receipt = material(self.material)
        self.client = MemoryClient()

    def test_bundle_publication_is_readable_and_release_is_last(self):
        result = publish_material(self.material, self.client, self.receipt)
        self.assertEqual(self.client.puts[-1], result["release_name"])
        descriptor = load_release(self.client, result["release_name"])
        store = DownloadsObjectStore(self.client, descriptor["revision"], release=descriptor)
        reader = MaterialReader(store, expected_revision=self.manifest["revision"])
        self.assertEqual(reader.postings("entry"), ["entry.cbl"])
        self.assertEqual(reader.facts("entry.cbl")["symbols"], [{"name": "entry.cbl"}])
        before = self.client.network_requests
        self.assertEqual(reader.postings("entry"), ["entry.cbl"])
        self.assertEqual(self.client.network_requests, before)
        self.assertEqual(reader.network_fetched_bytes, self.client.network_fetched_bytes)
        self.assertFalse(any(name.startswith("analysis-object-") for name in self.client.puts))

    def test_repository_snapshot_is_readable_deterministic_and_does_not_claim_sync(self):
        result = publish_material(self.material, self.client, repository_snapshot=True)
        self.assertEqual(self.client.puts[-1], result["release_name"])
        descriptor = load_release(self.client, result["release_name"])
        self.assertEqual(descriptor["schema_version"], SNAPSHOT_RELEASE_SCHEMA)
        self.assertNotIn("sync_receipt", descriptor)
        self.assertEqual(descriptor["source_snapshot"], {"schema_version": SNAPSHOT_SCHEMA,
            "repository": "example/source", "commit": "a" * 40, "subdirectory": "src",
            "sync_completeness": "not_verified"})
        reader = MaterialReader(DownloadsObjectStore(self.client, descriptor["revision"], release=descriptor))
        self.assertEqual(reader.postings("entry"), ["entry.cbl"])
        repeated = publish_material(self.material, self.client, repository_snapshot=True)
        self.assertEqual(repeated["release_name"], result["release_name"])
        self.assertEqual(repeated["uploaded_bundles"], 0)
        legacy = publish_material(self.material, self.client, self.receipt)
        self.assertEqual(legacy["release"]["schema_version"], "online-release/v1")
        self.assertEqual(legacy["release"]["sync_receipt"], self.receipt)
        self.assertEqual(legacy["uploaded_bundles"], 0)

    def test_publication_requires_exactly_one_explicit_source_mode(self):
        for receipt, snapshot in ((None, False), (self.receipt, True), (None, "true"), (None, 1)):
            with self.assertRaisesRegex(MaterialError, "INVALID_PUBLICATION_SOURCE"):
                publish_material(self.material, self.client, receipt, repository_snapshot=snapshot)
        self.assertEqual(self.client.puts, [])

    def test_snapshot_release_rejects_claimed_completeness_and_wrong_binding(self):
        result = publish_material(self.material, self.client, repository_snapshot=True)
        for key, value in (("sync_completeness", "complete"), ("sync_completeness", True),
                           ("repository", "other/source"), ("commit", "b" * 40),
                           ("subdirectory", "other"), ("schema_version", SYNC_SCHEMA)):
            altered = copy.deepcopy(result["release"])
            altered["source_snapshot"][key] = value
            with self.assertRaisesRegex(MaterialError, "INVALID_SOURCE_SNAPSHOT"):
                validate_release(altered)
        for mutation in (lambda item: item.update(sync_receipt=self.receipt),
                         lambda item: item.update(schema_version="online-release/v1"),
                         lambda item: item.update(schema_version=[]),
                         lambda item: item.pop("source_snapshot")):
            altered = copy.deepcopy(result["release"])
            mutation(altered)
            with self.assertRaisesRegex(MaterialError, "INVALID_RELEASE"):
                validate_release(altered)

    def test_snapshot_publication_still_rejects_unverified_commit(self):
        other = self.root / "unverified-snapshot"
        material(other, verification="caller_declared")
        with self.assertRaisesRegex(MaterialError, "UNVERIFIED_COMMIT"):
            publish_material(other, self.client, repository_snapshot=True)
        self.assertEqual(self.client.puts, [])

    def test_retry_reuses_content_and_old_release_never_replaces_new(self):
        first = publish_material(self.material, self.client, self.receipt)
        repeated = publish_material(self.material, self.client, self.receipt)
        self.assertEqual(first["release_name"], repeated["release_name"])
        self.assertEqual(repeated["uploaded_bundles"], 0)
        next_material = self.root / "next"
        _, next_receipt = material(next_material, commit="b" * 40)
        second = publish_material(next_material, self.client, next_receipt)
        late = publish_material(self.material, self.client, self.receipt)
        self.assertEqual(late["release_name"], first["release_name"])
        self.assertEqual(set(self.client.list_release_names()), {first["release_name"], second["release_name"]})
        self.assertFalse(any("latest" in name for name in self.client.data))

    def test_bad_sync_and_unverified_commit_do_not_upload(self):
        for receipt in ({**self.receipt, "complete": False}, {**self.receipt, "commit": "b" * 40},
                        {**self.receipt, "repository": "other/source"}, {**self.receipt, "sync_id": "../secret"}):
            with self.assertRaises(MaterialError):
                publish_material(self.material, self.client, receipt)
        self.assertEqual(self.client.puts, [])
        other = self.root / "unverified"
        _, receipt = material(other, verification="caller_declared")
        with self.assertRaisesRegex(MaterialError, "UNVERIFIED_COMMIT"):
            publish_material(other, self.client, receipt)
        self.assertEqual(self.client.puts, [])

    def test_corrupt_input_fails_before_upload_and_partial_upload_has_no_release(self):
        path = next((self.material / "objects").iterdir())
        original = path.read_bytes()
        path.write_bytes(b"{}")
        with self.assertRaises(MaterialError):
            publish_material(self.material, self.client, self.receipt)
        self.assertEqual(self.client.puts, [])
        path.write_bytes(original)
        self.client.fail_after = 1
        with self.assertRaisesRegex(MaterialError, "FORBIDDEN"):
            publish_material(self.material, self.client, self.receipt)
        self.assertEqual(self.client.list_release_names(), [])

    def test_cancel_before_release_leaves_only_unreferenced_candidates(self):
        def cancel():
            if self.client.puts:
                raise MaterialError("DISTRIBUTION_CANCELLED")
        with self.assertRaisesRegex(MaterialError, "CANCELLED"):
            publish_material(self.material, self.client, self.receipt, check_cancel=cancel)
        self.assertEqual(self.client.list_release_names(), [])

    def test_final_upload_unknown_outcome_is_reconciled_by_immutable_name(self):
        class UnknownClient(MemoryClient):
            def put_immutable(inner, name, data):
                result = super().put_immutable(name, data)
                if name.startswith("analysis-release-"):
                    raise MaterialError("DISTRIBUTION_CONNECTION_FAILED")
                return result
        client = UnknownClient()
        with self.assertRaises(PublicationUnknown) as caught:
            publish_material(self.material, client, self.receipt)
        unknown = caught.exception
        self.assertEqual(unknown.code, "DISTRIBUTION_PUBLICATION_UNKNOWN")
        self.assertEqual(unknown.cause_code, "DISTRIBUTION_CONNECTION_FAILED")
        self.assertIn(unknown.release_name, client.list_release_names())
        release = load_release(client, unknown.release_name)
        self.assertEqual(release["revision"], self.manifest["revision"])
        reconciled = MemoryClient()
        reconciled.data.update(client.data)
        result = publish_material(self.material, reconciled, self.receipt)
        self.assertEqual(result["publication"], "published")
        self.assertEqual(result["release_name"], unknown.release_name)
        self.assertEqual(result["uploaded_bundles"], 0)

    def test_cli_reports_unknown_publication_without_losing_release_identity(self):
        receipt = self.root / "receipt.json"
        receipt.write_bytes(canonical_json(self.receipt))
        name = "analysis-release-" + "b" * 64 + ".json"
        output = io.StringIO()
        with mock.patch("online_publish._client", return_value=self.client), \
                mock.patch("online_publish.publish_material", side_effect=PublicationUnknown(name, "DISTRIBUTION_CONNECTION_FAILED")), \
                redirect_stderr(output):
            status = main(["publish", "--material", str(self.material), "--sync-receipt", str(receipt),
                           "--workspace", "example", "--repository", "materials"])
        self.assertEqual(status, 3)
        self.assertEqual(json.loads(output.getvalue()), {"status": "publication_unknown", "release_name": name,
            "error": "DISTRIBUTION_PUBLICATION_UNKNOWN", "cause": "DISTRIBUTION_CONNECTION_FAILED"})

    def test_release_and_bundle_hash_mismatches_are_rejected(self):
        result = publish_material(self.material, self.client, self.receipt)
        name = result["release_name"]
        raw = self.client.data[name]
        altered = copy.deepcopy(result["release"])
        altered["repository"]["commit"] = "b" * 40
        altered["sync_receipt"]["commit"] = "b" * 40
        self.client.data[name] = canonical_json(altered)
        with self.assertRaisesRegex(MaterialError, "RELEASE_HASH_MISMATCH"):
            load_release(self.client, name)
        self.client.data[name] = raw
        store = DownloadsObjectStore(self.client, result["release"]["revision"], release=result["release"])
        reader = MaterialReader(store)
        ref = self.manifest["routes"]["terms"][bucket("entry")]
        bundle_ref = result["release"]["bundle_routes"][ref["sha256"][:2]]
        self.client.data[bundle_ref["name"]] = b"{}"
        with self.assertRaisesRegex(MaterialError, "BUNDLE_MISMATCH"):
            reader.postings("entry")

    def test_network_budget_is_actual_bundle_bytes_and_can_reset_between_questions(self):
        result = publish_material(self.material, self.client, self.receipt)
        store = DownloadsObjectStore(self.client, result["release"]["revision"], release=result["release"],
                                     max_network_bytes=len(canonical_json(self.manifest)))
        reader = MaterialReader(store)
        with self.assertRaisesRegex(MaterialError, "NETWORK_BUDGET_EXCEEDED"):
            reader.postings("entry")
        reader.reset_network_budget()
        self.assertEqual(reader.postings("entry"), ["entry.cbl"])

    def test_overlapping_routes_and_wrong_repository_binding_are_rejected(self):
        result = publish_material(self.material, self.client, self.receipt)
        altered = copy.deepcopy(result["release"])
        key = next(iter(altered["bundle_routes"]))
        altered["bundle_routes"][key + "0"] = altered["bundle_routes"][key]
        with self.assertRaisesRegex(MaterialError, "OVERLAPPING_ROUTES"):
            validate_release(altered)
        with self.assertRaisesRegex(MaterialError, "REVISION_MISMATCH"):
            DownloadsObjectStore(self.client, "b" * 64, release=result["release"])

    def test_large_groups_split_hash_prefixes_without_losing_objects(self):
        class Store:
            def __init__(self):
                self.data = {}
            def read(self, path, maximum):
                return self.data[path]
        store, groups, references = Store(), {}, {}
        selected = None
        for number in range(1000):
            raw = canonical_json({"kind": "facts", "value": "x" * 5000, "number": number})
            ref = object_reference(raw)
            sha = ref["sha256"]
            store.data[ref["path"]] = raw
            references[sha] = ref
            group = groups.setdefault(sha[:2], [])
            group.append(sha)
            if len(group) == 2:
                selected = group
                break
        self.assertIsNotNone(selected)
        bundles = list(_bundles(store, {sha: references[sha] for sha in selected}, target_bytes=4096, check_cancel=None))
        self.assertEqual(len(bundles), 2)
        self.assertTrue(all(len(prefix) > 2 for prefix, _, _ in bundles))
        actual = set()
        for prefix, ref, raw in bundles:
            self.assertEqual(digest(raw), ref["sha256"])
            self.assertTrue(all(sha.startswith(prefix) for sha in json.loads(raw)["objects"]))
            actual.update(json.loads(raw)["objects"])
        self.assertEqual(actual, set(selected))

    def test_cli_reads_only_named_credentials_and_does_not_publish_without_them(self):
        receipt = self.root / "receipt.json"
        receipt.write_bytes(canonical_json(self.receipt))
        out = io.StringIO()
        with mock.patch.dict("os.environ", {}, clear=True), redirect_stderr(out):
            status = main(["publish", "--material", str(self.material), "--sync-receipt", str(receipt),
                           "--workspace", "example", "--repository", "materials"])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(out.getvalue()), {"error": "DISTRIBUTION_CREDENTIAL_REQUIRED"})
        out = io.StringIO()
        with mock.patch("online_publish.DownloadsClient", return_value=self.client), redirect_stdout(out):
            status = main(["list", "--workspace", "example", "--repository", "materials"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out.getvalue())["selection"], "explicit_revision_required")


if __name__ == "__main__":
    unittest.main()
