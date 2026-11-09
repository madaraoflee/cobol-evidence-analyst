"""Portable material reads retain revision identity and verify every fetched object."""

import copy
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from online_material_format import (MAX_OBJECT_BYTES, MaterialError, SCHEMA_VERSION,
    bucket, canonical_json, digest, object_reference, seal_manifest)
from online_material_reader import DirectoryObjectStore, HTTPSObjectStore, MaterialReader


COMMIT = "a" * 40


class MemoryStore:
    def __init__(self):
        self.data, self.reads = {}, []

    def read(self, path, max_bytes):
        self.reads.append(path)
        if path not in self.data:
            raise MaterialError("MATERIAL_NOT_FOUND")
        return self.data[path]

    def put(self, value):
        data = canonical_json(value)
        ref = object_reference(data)
        self.data[ref["path"]] = data
        return ref


class Fixture:
    def __init__(self, *, fact_hash=None, paths=None, count=None):
        self.store = MemoryStore()
        self.path, self.sha = "programs/entry.cbl", digest(b"source")
        self.fact = {"kind": "facts", "relative_path": self.path,
                     "source_sha256": fact_hash or self.sha,
                     "symbols": [{"name": "AMOUNT"}], "relations": [], "rules": []}
        self.fact_ref = self.store.put(self.fact)
        self.metadata = {"relative_path": self.path, "sha256": self.sha, "size": 6,
                         "encoding": "utf-8", "source_format": "free", "line_count": 1,
                         "facts": [self.fact_ref]}
        self.files_ref = self.store.put({"kind": "files", "entries": {self.path: self.metadata}})
        paths = [self.path] if paths is None else paths
        self.page_ref = self.store.put({"kind": "postings", "key": "amount", "paths": paths})
        self.terms_ref = self.store.put({"kind": "terms", "entries": {
            "amount": {"count": len(paths) if count is None else count, "pages": [self.page_ref]}}})
        self.body = {"schema_version": SCHEMA_VERSION,
                     "repository": {"identity": "sample/business-source", "commit": COMMIT, "subdirectory": ""},
                     "analysis": {}, "coverage": {"complete": False, "reason": "synthetic sample"},
                     "routes": {"files": {bucket(self.path): self.files_ref},
                                "terms": {bucket("amount"): self.terms_ref}, "references": {}}}
        self.seal()

    def seal(self):
        self.manifest = seal_manifest(self.body)
        self.store.data["material.json"] = canonical_json(self.manifest)


class ReaderTests(unittest.TestCase):
    def assert_code(self, code, call):
        with self.assertRaises(MaterialError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_constructor_reads_only_manifest_and_pins_copy(self):
        fixture = Fixture()
        reader = MaterialReader(fixture.store, expected_repository="sample/business-source", expected_commit=COMMIT,
                                expected_revision=fixture.manifest["revision"])
        self.assertEqual(fixture.store.reads, ["material.json"])
        manifest = reader.manifest
        manifest["repository"]["commit"] = "b" * 40
        fixture.store.data["material.json"] = b"changed"
        self.assertEqual(reader.manifest["repository"]["commit"], COMMIT)
        self.assertEqual(reader.fetched_objects, 0)

    def test_previous_version_paged_postings_remain_readable(self):
        fixture = Fixture()
        fixture.body["schema_version"] = "online-analysis/v1"
        fixture.seal()
        self.assertEqual(MaterialReader(fixture.store).postings("amount"), [fixture.path])

    def test_inline_short_postings_avoid_extra_fetch_and_validate_limits(self):
        fixture = Fixture()
        for paths, count, expected in (([fixture.path], 1, None),
                                       ([fixture.path, fixture.path], 2, "MATERIAL_INVALID_POSTINGS"),
                                       ([fixture.path], 2, "MATERIAL_POSTINGS_COUNT_MISMATCH"),
                                       ([str(index) + ".cbl" for index in range(17)], 17, "MATERIAL_INVALID_POSTINGS")):
            reference = fixture.store.put({"kind": "terms", "entries": {"amount": {"count": count, "paths": paths}}})
            fixture.body["routes"]["terms"][bucket("amount")] = reference
            fixture.seal()
            reader = MaterialReader(fixture.store)
            if expected:
                self.assert_code(expected, lambda: reader.postings("amount"))
            else:
                self.assertEqual(reader.postings("amount"), [fixture.path])
                self.assertEqual(reader.fetched_objects, 1)

    def test_identity_mismatches_are_rejected(self):
        for kw, code in (({"expected_repository": "other/source"}, "MATERIAL_REPOSITORY_MISMATCH"),
                         ({"expected_commit": "b" * 40}, "MATERIAL_COMMIT_MISMATCH"),
                         ({"expected_revision": "b" * 64}, "MATERIAL_REVISION_MISMATCH")):
            self.assert_code(code, lambda: MaterialReader(Fixture().store, **kw))

    def test_file_facts_and_postings_share_verified_cache_without_mutation(self):
        fixture = Fixture()
        reader = MaterialReader(fixture.store)
        metadata = reader.file(fixture.path)
        metadata["facts"].clear()
        facts = reader.facts(fixture.path)
        self.assertEqual(facts["symbols"], [{"name": "AMOUNT"}])
        facts["symbols"][0]["name"] = "CHANGED"
        self.assertEqual(reader.facts(fixture.path)["symbols"][0]["name"], "AMOUNT")
        self.assertEqual(reader.postings("amount"), [fixture.path])
        self.assertEqual(reader.fetched_objects, 4)
        self.assertEqual(reader.fetched_bytes, sum(len(fixture.store.data[p]) for p in fixture.store.reads))

    def test_missing_keys_are_empty_and_coverage_remains_visible(self):
        fixture = Fixture()
        reader = MaterialReader(fixture.store)
        self.assertIsNone(reader.file("missing.cbl"))
        self.assertIsNone(reader.facts("missing.cbl"))
        self.assertEqual(reader.postings("missing"), [])
        self.assertEqual(reader.postings("CALL:missing", route="references"), [])
        self.assertFalse(reader.manifest["coverage"]["complete"])

    def test_missing_object_is_not_an_empty_result(self):
        fixture = Fixture()
        del fixture.store.data[fixture.page_ref["path"]]
        reader = MaterialReader(fixture.store)
        self.assert_code("MATERIAL_NOT_FOUND", lambda: reader.postings("amount"))

    def test_wrong_source_version_cannot_supply_facts(self):
        fixture = Fixture(fact_hash="b" * 64)
        self.assert_code("MATERIAL_SOURCE_MISMATCH", lambda: MaterialReader(fixture.store).facts(fixture.path))

    def test_hash_and_length_mismatch_are_rejected_even_if_store_ignores_limit(self):
        fixture = Fixture()
        raw = fixture.store.data[fixture.fact_ref["path"]]
        for corrupt, code in ((b"x" * len(raw), "MATERIAL_HASH_MISMATCH"), (raw + b"x", "MATERIAL_SIZE_MISMATCH")):
            fixture.store.data[fixture.fact_ref["path"]] = corrupt
            self.assert_code(code, lambda: MaterialReader(fixture.store).read_object(fixture.fact_ref))

    def test_reference_kind_and_size_constraints_are_checked(self):
        fixture = Fixture()
        reader = MaterialReader(fixture.store)
        self.assert_code("MATERIAL_KIND_MISMATCH", lambda: reader.read_object(fixture.fact_ref, kind="files"))
        oversized = {**fixture.fact_ref, "size": MAX_OBJECT_BYTES + 1}
        self.assert_code("MATERIAL_SIZE_LIMIT", lambda: reader.read_object(oversized))
        escaped = {**fixture.fact_ref, "path": "../" + fixture.fact_ref["path"]}
        self.assert_code("MATERIAL_INVALID_REFERENCE", lambda: reader.read_object(escaped))

    def test_posting_count_sort_unique_and_path_checks(self):
        variants = [(["a.cbl"], 2, "MATERIAL_POSTINGS_COUNT_MISMATCH"),
                    (["b.cbl", "a.cbl"], 2, "MATERIAL_INVALID_POSTINGS"),
                    (["a.cbl", "a.cbl"], 2, "MATERIAL_INVALID_POSTINGS"),
                    (["../a.cbl"], 1, "MATERIAL_INVALID_PATH")]
        for paths, count, code in variants:
            fixture = Fixture(paths=paths, count=count)
            self.assert_code(code, lambda: MaterialReader(fixture.store).postings("amount"))

    def test_cache_is_byte_bounded_lru_and_zero_disables_it(self):
        fixture = Fixture()
        limit = max(fixture.fact_ref["size"], fixture.page_ref["size"])
        reader = MaterialReader(fixture.store, cache_bytes=limit)
        reader.read_object(fixture.fact_ref)
        reader.read_object(fixture.page_ref)
        reader.read_object(fixture.fact_ref)
        self.assertEqual(reader.fetched_objects, 3)
        self.assertLessEqual(reader.cached_bytes, limit)
        reader = MaterialReader(fixture.store, cache_bytes=0)
        reader.read_object(fixture.fact_ref)
        reader.read_object(fixture.fact_ref)
        self.assertEqual(reader.fetched_objects, 2)
        self.assertEqual(reader.cached_bytes, 0)

    def test_cancellation_prevents_further_reads_including_cache_hits(self):
        fixture = Fixture()
        cancelled = False
        class Cancelled(Exception):
            pass
        def check():
            if cancelled:
                raise Cancelled()
        reader = MaterialReader(fixture.store, check_cancel=check)
        reader.read_object(fixture.fact_ref)
        before = len(fixture.store.reads)
        cancelled = True
        with self.assertRaises(Cancelled):
            reader.read_object(fixture.fact_ref)
        self.assertEqual(len(fixture.store.reads), before)

    def test_manifest_revision_and_invalid_json_are_rejected(self):
        fixture = Fixture()
        broken = copy.deepcopy(fixture.manifest)
        broken["coverage"]["complete"] = True
        fixture.store.data["material.json"] = canonical_json(broken)
        self.assert_code("MATERIAL_REVISION_MISMATCH", lambda: MaterialReader(fixture.store))
        fixture.store.data["material.json"] = b'{"duplicate":1,"duplicate":2}'
        self.assert_code("MATERIAL_INVALID_JSON", lambda: MaterialReader(fixture.store))

    def test_aggregate_object_budget_is_shared_by_nested_file_and_facts_reads(self):
        fixture = Fixture()
        reader = MaterialReader(fixture.store, max_operation_objects=1)
        self.assert_code("MATERIAL_OPERATION_LIMIT", lambda: reader.facts(fixture.path))
        self.assertEqual(fixture.store.reads, ["material.json", fixture.files_ref["path"]])
        # An aborted aggregate operation does not poison a subsequent operation.
        self.assertEqual(reader.file(fixture.path), fixture.metadata)

    def test_aggregate_bytes_limit_applies_even_to_warm_cache(self):
        fixture = Fixture()
        limit = max(fixture.files_ref["size"], fixture.fact_ref["size"])
        reader = MaterialReader(fixture.store, max_operation_bytes=limit)
        reader.read_object(fixture.files_ref)
        reader.read_object(fixture.fact_ref)
        count_before = reader.fetched_objects
        self.assert_code("MATERIAL_OPERATION_LIMIT", lambda: reader.facts(fixture.path))
        self.assertEqual(reader.fetched_objects, count_before)

    def test_aggregate_posting_page_budget_rejects_before_fetching_excess_page(self):
        fixture = Fixture()
        first = fixture.store.put({"kind": "postings", "key": "amount", "paths": ["a.cbl"]})
        second = fixture.store.put({"kind": "postings", "key": "amount", "paths": ["b.cbl"]})
        routing = fixture.store.put({"kind": "terms", "entries": {"amount": {"count": 2, "pages": [first, second]}}})
        fixture.body["routes"]["terms"] = {bucket("amount"): routing}
        fixture.seal()
        reader = MaterialReader(fixture.store, max_operation_objects=2)
        self.assert_code("MATERIAL_OPERATION_LIMIT", lambda: reader.postings("amount"))
        self.assertNotIn(second["path"], fixture.store.reads)

    def test_aggregate_entry_limit_rejects_facts_and_postings_without_partial_result(self):
        fixture = Fixture()
        for method in ("facts", "postings"):
            reader = MaterialReader(fixture.store, max_operation_entries=1)
            call = (lambda: reader.facts(fixture.path)) if method == "facts" else (lambda: reader.postings("amount"))
            self.assert_code("MATERIAL_OPERATION_LIMIT", call)


class DirectoryStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "objects"
        self.root.mkdir()
        (self.root / "entry.json").write_bytes(b"1234")
        self.store = DirectoryObjectStore(self.root)

    def test_bounded_file_and_missing_file(self):
        self.assertEqual(self.store.read("entry.json", 4), b"1234")
        for path, limit, code in (("entry.json", 3, "MATERIAL_SIZE_LIMIT"),
                                  ("missing.json", 4, "MATERIAL_NOT_FOUND"),
                                  ("../entry.json", 4, "MATERIAL_INVALID_PATH")):
            with self.assertRaises(MaterialError) as caught:
                self.store.read(path, limit)
            self.assertEqual(caught.exception.code, code)

    def test_final_and_parent_symlinks_are_rejected(self):
        (self.parent / "outside.json").write_bytes(b"private")
        (self.root / "link.json").symlink_to(self.parent / "outside.json")
        (self.root / "parent").symlink_to(self.parent, target_is_directory=True)
        for path in ("link.json", "parent/outside.json"):
            with self.assertRaises(MaterialError):
                self.store.read(path, 100)

    def test_directories_and_fifo_are_not_read_as_files(self):
        (self.root / "directory").mkdir()
        with self.assertRaises(MaterialError):
            self.store.read("directory", 100)
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.root / "pipe")
            with self.assertRaises(MaterialError):
                self.store.read("pipe", 100)


class Response(io.BytesIO):
    def __init__(self, data=b"{}", status=200):
        super().__init__(data)
        self.status, self.headers = status, {}


class Opener:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.requests = response or Response(), error, []

    def open(self, request, timeout):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.response


class HTTPSStoreTests(unittest.TestCase):
    def test_origin_and_base_path_are_fixed(self):
        opener = Opener()
        store = HTTPSObjectStore("https://objects.example.test/materials", token="read-only", opener=opener)
        self.assertEqual(store.read("objects/a ?#.json", 10), b"{}")
        request = opener.requests[0]
        self.assertEqual(request.full_url, "https://objects.example.test/materials/objects/a%20%3F%23.json")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer read-only")

    def test_invalid_base_urls_and_escape_paths_fail(self):
        for url in ("http://objects.example.test", "https://name:token@objects.example.test", "https://objects.example.test?token=x",
                    "https://objects.example.test/#x", "https://objects.example.test/../other", "https://objects.example.test/%2e%2e/other"):
            with self.assertRaises(MaterialError):
                HTTPSObjectStore(url)
        opener = Opener()
        store = HTTPSObjectStore("https://objects.example.test", opener=opener)
        for path in ("https://other.example.test/object", "//other.example.test/object", "../object"):
            with self.assertRaises(MaterialError):
                store.read(path, 10)
        self.assertEqual(opener.requests, [])

    def test_redirect_auth_and_rate_limit_errors_are_sanitized(self):
        for status, code in ((302, "MATERIAL_REDIRECT"), (403, "MATERIAL_FORBIDDEN"), (429, "MATERIAL_RATE_LIMIT")):
            opener = Opener(error=HTTPError("https://private", status, "secret body", {}, io.BytesIO(b"secret")))
            with self.assertRaises(MaterialError) as caught:
                HTTPSObjectStore("https://objects.example.test", opener=opener).read("material.json", 100)
            self.assertEqual(str(caught.exception), code)
            self.assertEqual(len(opener.requests), 1)


if __name__ == "__main__":
    unittest.main()
