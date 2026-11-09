from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import tracemalloc
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from online_material_format import MAX_SOURCE_BYTES, MaterialError, digest, reference_key
from online_material_query import MaterialQuery


class FakeReader:
    def __init__(self):
        self.manifest = {
            "revision": "d" * 64,
            "repository": {"identity": "workspace/source", "commit": "a" * 40,
                           "subdirectory": "batch"},
            "coverage": {"indexed_files": 3, "scope": "selected-source",
                         "boundaries": [{"status": "DYNAMIC_TARGET"}]},
        }
        self.files = {}
        self.file_facts = {}
        self.routes = {}
        self.reads = []

    def add(self, path, raw=b"DISPLAY READY.\n", encoding="utf-8", relations=None):
        self.files[path] = {
            "relative_path": path, "sha256": digest(raw), "size": len(raw),
            "encoding": encoding, "source_format": "free", "facts": [],
            "line_count": len(raw.decode("latin-1" if encoding == "latin-1-fallback"
                                         else encoding).lstrip("\ufeff").splitlines()),
        }
        self.file_facts[path] = {
            "kind": "facts", "relative_path": path, "source_sha256": digest(raw),
            "symbols": [], "rules": [], "relations": relations or [],
        }
        return raw

    def file(self, path):
        self.reads.append(("file", path))
        return deepcopy(self.files.get(path))

    def facts(self, path):
        self.reads.append(("facts", path))
        return deepcopy(self.file_facts.get(path))

    def postings(self, key, route="terms"):
        self.reads.append((route, key))
        return sorted(set(self.routes.get((route, key), [])))


def relation(target="WORKER", kind="CALLS", status="confirmed"):
    return {"relation_type": kind, "target_name": target, "status": status,
            "start_line": 1, "end_line": 1, "metadata": {"dynamic": False}}


class OnlineMaterialQueryTests(unittest.TestCase):
    def setUp(self):
        self.reader = FakeReader()
        self.query = MaterialQuery(self.reader)

    def assert_code(self, code, callback):
        with self.assertRaises(MaterialError) as caught:
            callback()
        self.assertEqual(str(caught.exception), code)

    def test_search_ranks_terms_and_reads_metadata_only_for_returned_hits(self):
        for path in ("a.cbl", "b.cbl", "c.cbl", "unrelated.cbl"):
            self.reader.add(path)
        self.reader.routes.update({
            ("terms", "balance"): ["a.cbl", "b.cbl"],
            ("terms", "account"): ["b.cbl", "c.cbl"],
        })
        result = self.query.search("ACCOUNT balance account", limit=2)
        self.assertEqual([item["relative_path"] for item in result["matches"]],
                         ["b.cbl", "a.cbl"])
        self.assertEqual(result["matches"][0]["score"], 2)
        self.assertEqual(result["matches"][0]["matched_terms"], ["account", "balance"])
        self.assertEqual(result["total_matches"], 3)
        self.assertTrue(result["truncated"])
        self.assertEqual(self.reader.reads, [("terms", "account"), ("terms", "balance"),
                                            ("file", "b.cbl"), ("file", "a.cbl")])
        self.assertEqual(result["coverage"]["scope"], "selected-source")
        self.assertEqual(result["query_kind"], "lexical")

    def test_natural_language_zero_match_does_not_establish_absence(self):
        result = self.query.search("如何检查账户欠款并决定业务状态")
        self.assertEqual(result["status"], "no_lexical_matches")
        self.assertFalse(result["absence_established"])
        self.assertEqual(result["coverage"]["boundaries"], [{"status": "DYNAMIC_TARGET"}])
        self.assertEqual(result["total_matches"], 0)
        self.assertFalse(result["truncated"])

    def test_query_without_tokens_is_explicit_and_does_not_read_material(self):
        result = self.query.search(" ... ")
        self.assertEqual(result["status"], "no_query_terms")
        self.assertFalse(result["absence_established"])
        self.assertEqual(self.reader.reads, [])

    def test_query_normalizes_full_width_and_hyphenated_terms(self):
        self.query.search("ＡＣＣＯＵＮＴ-BALANCE")
        self.assertEqual(self.reader.reads, [("terms", "account"),
                                           ("terms", "account-balance"),
                                           ("terms", "balance")])

    def test_query_and_result_count_limits_fail_before_reading(self):
        cases = [
            ("MATERIAL_INVALID_QUERY", lambda: self.query.search(None)),
            ("MATERIAL_INVALID_QUERY", lambda: self.query.search("x" * 16385)),
            ("MATERIAL_QUERY_TERM_LIMIT", lambda: self.query.search(" ".join(map(str, range(65))))),
        ]
        for limit in (True, 0, -1, 1001, "10", 2.5):
            cases.append(("MATERIAL_INVALID_LIMIT", lambda value=limit: self.query.search("a", value)))
        for code, call in cases:
            with self.subTest(code=code):
                self.assert_code(code, call)
        self.assertEqual(self.reader.reads, [])

    def test_search_resource_budgets_fail_explicitly_instead_of_silent_partial_hits(self):
        for path in ("a.cbl", "b.cbl"):
            self.reader.add(path)
        self.reader.routes[("terms", "account")] = ["a.cbl", "b.cbl"]
        self.reader.routes[("terms", "balance")] = ["a.cbl"]
        for constant in ("_MAX_CANDIDATE_PATHS", "_MAX_MATCHED_TERM_PAIRS", "_MAX_QUERY_PATH_BYTES"):
            self.reader.reads.clear()
            with mock.patch("online_material_query." + constant, 1):
                self.assert_code("MATERIAL_QUERY_BUDGET_EXCEEDED",
                                 lambda: self.query.search("account balance"))
            self.assertFalse(any(kind == "file" for kind, _ in self.reader.reads))

    def test_incoming_candidate_budget_is_checked_before_fetching_facts(self):
        self.reader.routes[("references", "CALLS:worker")] = ["a.cbl", "b.cbl"]
        with mock.patch("online_material_query._MAX_CANDIDATE_PATHS", 1):
            self.assert_code("MATERIAL_QUERY_BUDGET_EXCEEDED", lambda: self.query.incoming("worker"))
        self.assertEqual(self.reader.reads, [("references", "CALLS:worker")])

    def test_cumulative_transfer_budget_includes_separate_posting_reads(self):
        original = self.reader.postings
        self.reader.fetched_bytes = 0

        def read_postings(key, route="terms"):
            self.reader.fetched_bytes += 10
            return original(key, route)

        with mock.patch.object(self.reader, "postings", side_effect=read_postings), \
                mock.patch("online_material_query._MAX_QUERY_TRANSFER_BYTES", 15):
            self.assert_code("MATERIAL_QUERY_BUDGET_EXCEEDED",
                             lambda: self.query.search("account balance"))
        self.assertEqual(len(self.reader.reads), 2)

    def test_incoming_fact_budget_counts_nonmatching_and_nonrelation_facts(self):
        self.reader.add("main.cbl", relations=[relation("OTHER")])
        self.reader.file_facts["main.cbl"]["symbols"] = [{"name": "ONE"}, {"name": "TWO"}]
        self.reader.routes[("references", "CALLS:worker")] = ["main.cbl"]
        with mock.patch("online_material_query._MAX_SCANNED_FACTS", 2):
            self.assert_code("MATERIAL_QUERY_BUDGET_EXCEEDED", lambda: self.query.incoming("worker"))

    def test_manifest_identity_is_pinned_and_results_do_not_mutate_it(self):
        self.reader.manifest["repository"]["commit"] = "b" * 40
        result = self.query.search("account")
        result["coverage"]["boundaries"].clear()
        result["repository"]["identity"] = "other/source"
        another = self.query.search("account")
        self.assertEqual(another["repository"]["commit"], "a" * 40)
        self.assertEqual(another["repository"]["identity"], "workspace/source")
        self.assertTrue(another["coverage"]["boundaries"])

    def test_relation_filter_preserves_candidate_status_and_evidence(self):
        self.reader.add("main.cbl", relations=[relation(), relation("ROW", "INCLUDES_COPY", "candidate")])
        result = self.query.relations("main.cbl", "includes_copy")
        self.assertEqual(result["relations"], [relation("ROW", "INCLUDES_COPY", "candidate")])
        self.assertEqual(self.reader.reads, [("facts", "main.cbl")])
        result["relations"][0]["metadata"]["dynamic"] = True
        self.assertFalse(self.query.relations("main.cbl")["relations"][1]["metadata"]["dynamic"])

    def test_incoming_verifies_target_and_type_and_does_not_resolve_duplicate_names(self):
        self.reader.add("main.cbl", relations=[relation(), relation("OTHER"),
                                                relation("WORKER", "INCLUDES_COPY")])
        self.reader.add("other.cbl", relations=[relation("worker", status="candidate")])
        self.reader.add("stale.cbl", relations=[relation("OTHER")])
        self.reader.routes[("references", reference_key("CALLS", "worker"))] = [
            "stale.cbl", "other.cbl", "main.cbl"]
        result = self.query.incoming("worker", limit=1)
        self.assertEqual(result["total_matches"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["candidate_path_count"], 3)
        self.assertEqual(result["status_counts"], {"confirmed": 1, "candidate": 1})
        self.assertEqual(result["matches"], [{"relative_path": "main.cbl", "relation": relation()}])
        self.assertEqual(result["target_resolution"], "name_match_only")
        self.assertFalse(result["absence_established"])
        self.assertEqual(self.reader.reads, [("references", "CALLS:worker"),
                                            ("facts", "main.cbl"), ("facts", "other.cbl"),
                                            ("facts", "stale.cbl")])

    def test_incoming_copy_candidate_is_not_promoted_to_confirmed(self):
        self.reader.add("main.cbl", relations=[relation("shared.cpy", "INCLUDES_COPY", "candidate")])
        self.reader.routes[("references", "INCLUDES_COPY:shared.cpy")] = ["main.cbl"]
        result = self.query.incoming("SHARED.CPY", "includes_copy")
        self.assertEqual(result["status_counts"], {"candidate": 1})
        self.assertEqual(result["matches"][0]["relation"]["status"], "candidate")

    def test_unsupported_reverse_relation_and_bad_target_fail(self):
        self.assert_code("MATERIAL_UNSUPPORTED_REVERSE_RELATION",
                         lambda: self.query.incoming("WORKER", "READS"))
        self.assert_code("MATERIAL_INVALID_RELATION_TYPE",
                         lambda: self.query.relations("main.cbl", "calls/../"))
        for target in (None, "", "x" * 4097, "WORKER\n"):
            self.assert_code("MATERIAL_INVALID_TARGET", lambda value=target: self.query.incoming(value))
        self.assertEqual(self.reader.reads, [])

    def test_unindexed_paths_do_not_become_source_absence_claims(self):
        source = mock.Mock()
        self.assert_code("MATERIAL_FILE_NOT_INDEXED", lambda: self.query.relations("missing.cbl"))
        self.assert_code("MATERIAL_FILE_NOT_INDEXED",
                         lambda: self.query.evidence("missing.cbl", 1, 1, source))
        source.assert_not_called()

    def test_malformed_relation_fields_raise_bounded_material_errors(self):
        self.reader.add("main.cbl")
        self.reader.routes[("references", "CALLS:worker")] = ["main.cbl"]
        for malformed in ({}, {**relation(), "target_name": []},
                          {**relation(), "status": {"private": "value"}},
                          {**relation(), "relation_type": None}):
            self.reader.file_facts["main.cbl"]["relations"] = [malformed]
            self.assert_code("MATERIAL_INVALID_FACTS", lambda: self.query.incoming("worker"))
            self.assert_code("MATERIAL_INVALID_FACTS", lambda: self.query.relations("main.cbl"))

    def test_evidence_verifies_one_file_and_cites_fixed_commit_and_physical_lines(self):
        raw = self.reader.add("main.cbl", b"FIRST\r\nSECOND\rTHIRD\vFOURTH\n")
        source = mock.Mock(return_value=raw)
        result = self.query.evidence("main.cbl", 2, 3, source)
        source.assert_called_once_with("main.cbl", max_bytes=len(raw))
        self.assertEqual(self.reader.reads, [("file", "main.cbl")])
        self.assertEqual(result["text"], "SECOND\nTHIRD")
        self.assertEqual(result["citation"], {"revision": "d" * 64, "commit": "a" * 40,
                                             "path": "main.cbl", "hash": digest(raw),
                                             "start": 2, "end": 3})
        self.assertFalse(result["truncated"])
        self.assertEqual(result["complete_line_count"], 2)

    def test_source_uses_indexed_encoding_bom_and_all_physical_newlines(self):
        cases = [("utf-8", "\ufeff\ufeffone\u2028two\x85three"),
                 ("utf-16", "one\r\ntwo\fthree"),
                 ("utf-32", "one\x1ctwo\x1ethree"),
                 ("latin-1-fallback", "one\rdeux\ntrès"),
                 ("cp950", "第一\n第二\n第三")]
        for encoding, text in cases:
            with self.subTest(encoding=encoding):
                raw = text.encode("latin-1" if encoding == "latin-1-fallback" else encoding)
                self.reader.add("main.cbl", raw, encoding)
                result = self.query.evidence("main.cbl", 1, 3, lambda path, **kwargs: raw)
                self.assertEqual(result["text"], "\n".join(text.lstrip("\ufeff").splitlines()))

    def test_chunk_boundaries_preserve_multibyte_decoding_crlf_and_requested_lines(self):
        text = "\ufefffirst\r\n第二\x85third\r\nfourth\u2028last"
        for encoding in ("utf-8", "utf-16", "utf-32"):
            raw = self.reader.add("main.cbl", text.encode(encoding), encoding)
            for size in (1, 2, 3, 7, 17):
                with self.subTest(encoding=encoding, chunk_size=size), \
                        mock.patch("online_material_query._DECODE_CHUNK_BYTES", size):
                    result = self.query.evidence("main.cbl", 2, 4, lambda path, **kwargs: raw)
                    self.assertEqual(result["text"], "第二\nthird\nfourth")
                    self.assertFalse(result["truncated"])
                    result = self.query.evidence("main.cbl", 2, 5,
                                                 lambda path, **kwargs: raw, max_characters=4)
                    self.assertEqual(result["text"], "第二\nt")
                    self.assertEqual(result["truncated_at"], {"line": 3, "character": 1})

    def test_many_short_lines_do_not_allocate_a_whole_file_line_list(self):
        raw = self.reader.add("main.cbl", b"x\n" * (2 * 1024 * 1024))
        tracemalloc.start()
        try:
            result = self.query.evidence("main.cbl", 2, 3, lambda path, **kwargs: raw)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(result["text"], "x\nx")
        self.assertLess(peak, 8 * 1024 * 1024)

    def test_overlong_line_is_marked_partial_without_claiming_omitted_lines(self):
        raw = self.reader.add("main.cbl", b"ABCDEFGHIJKLM\nSECOND\n")
        result = self.query.evidence("main.cbl", 1, 2, lambda path, **kwargs: raw, max_characters=4)
        self.assertEqual(result["text"], "ABCD")
        self.assertTrue(result["truncated"])
        self.assertTrue(result["last_line_truncated"])
        self.assertEqual(result["truncated_at"], {"line": 1, "character": 4})
        self.assertEqual(result["citation"]["end"], 1)
        self.assertEqual(result["requested_range"], {"start": 1, "end": 2})
        self.assertEqual(result["complete_line_count"], 0)

    def test_excerpt_boundary_counts_newlines_and_reports_unrepresented_next_line(self):
        raw = self.reader.add("main.cbl", b"ABC\nDE\nFGH\n")
        for limit, expected_text, stop, partial, end in [
            (3, "ABC", {"line": 2, "character": 0}, False, 1),
            (4, "ABC", {"line": 2, "character": 0}, False, 1),
            (5, "ABC\nD", {"line": 2, "character": 1}, True, 2),
            (10, "ABC\nDE\nFGH", None, False, 3),
        ]:
            with self.subTest(limit=limit):
                result = self.query.evidence("main.cbl", 1, 3,
                                             lambda path, **kwargs: raw, max_characters=limit)
                self.assertEqual(result["text"], expected_text)
                self.assertLessEqual(len(result["text"]), limit)
                self.assertEqual(result["truncated_at"], stop)
                self.assertEqual(result["last_line_truncated"], partial)
                self.assertEqual(result["citation"]["end"], end)

    def test_empty_physical_lines_have_precise_count_and_no_synthetic_extra_line(self):
        raw = self.reader.add("main.cbl", b"\n\nX\n")
        result = self.query.evidence("main.cbl", 1, 3, lambda path, **kwargs: raw, max_characters=1)
        self.assertEqual(result["text"], "\n")
        self.assertEqual(result["complete_line_count"], 2)
        self.assertEqual(result["citation"]["end"], 2)
        self.assertEqual(result["truncated_at"], {"line": 3, "character": 0})

    def test_source_is_revalidated_on_every_call_without_implicit_cache(self):
        raw = self.reader.add("main.cbl")
        source = mock.Mock(side_effect=[raw, b"changed bytes!\n"])
        self.query.evidence("main.cbl", 1, 1, source)
        self.assert_code("MATERIAL_SOURCE_HASH_MISMATCH", lambda: self.query.evidence("main.cbl", 1, 1, source))
        self.assertEqual(source.call_count, 2)

    def test_source_size_hash_and_response_type_are_checked_before_text_exposure(self):
        raw = self.reader.add("main.cbl", b"TOP SECRET\n")
        for response, code in [(raw + b"x", "MATERIAL_SOURCE_SIZE_MISMATCH"),
                               (b"X" * len(raw), "MATERIAL_SOURCE_HASH_MISMATCH"),
                               ("TOP SECRET\n", "MATERIAL_SOURCE_RESPONSE_INVALID")]:
            with self.subTest(code=code):
                self.assert_code(code, lambda: self.query.evidence("main.cbl", 1, 1,
                                                                   lambda path, **kwargs: response))

    def test_bad_source_encoding_and_line_count_are_rejected(self):
        raw = self.reader.add("main.cbl", b"VALUE\n")
        for encoding in ("missing-encoding", None, "x" * 65):
            self.reader.files["main.cbl"]["encoding"] = encoding
            self.assert_code("MATERIAL_SOURCE_ENCODING_INVALID",
                             lambda: self.query.evidence("main.cbl", 1, 1, lambda path, **kwargs: raw))
        raw = self.reader.add("main.cbl", b"\xff\n", "latin-1")
        self.reader.files["main.cbl"]["encoding"] = "utf-8"
        self.assert_code("MATERIAL_SOURCE_ENCODING_INVALID",
                         lambda: self.query.evidence("main.cbl", 1, 1, lambda path, **kwargs: raw))
        self.reader.files["main.cbl"]["encoding"] = "latin-1"
        self.reader.files["main.cbl"]["line_count"] = 2
        self.assert_code("MATERIAL_SOURCE_LINE_COUNT_MISMATCH",
                         lambda: self.query.evidence("main.cbl", 1, 1, lambda path, **kwargs: raw))

    def test_invalid_ranges_and_oversized_file_fail_without_source_read(self):
        self.reader.add("main.cbl")
        source = mock.Mock()
        for start, end in ((0, 1), (1, False), (2, 1), (1.5, 2)):
            self.assert_code("MATERIAL_INVALID_RANGE",
                             lambda a=start, b=end: self.query.evidence("main.cbl", a, b, source))
        self.assert_code("MATERIAL_RANGE_OUT_OF_BOUNDS",
                         lambda: self.query.evidence("main.cbl", 1, 2, source))
        self.assert_code("MATERIAL_INVALID_CHARACTER_LIMIT",
                         lambda: self.query.evidence("main.cbl", 1, 1, source, 0))
        self.reader.files["main.cbl"]["size"] = MAX_SOURCE_BYTES + 1
        self.assert_code("MATERIAL_SOURCE_SIZE_LIMIT",
                         lambda: self.query.evidence("main.cbl", 1, 1, source))
        source.assert_not_called()

    def test_source_errors_never_expose_remote_response_or_credentials(self):
        self.reader.add("main.cbl")
        for exception in (RuntimeError("credential=private-value source=private-code"),
                          MaterialError("credential=private-value")):
            source = mock.Mock(side_effect=exception)
            self.assert_code("MATERIAL_SOURCE_UNAVAILABLE",
                             lambda: self.query.evidence("main.cbl", 1, 1, source))
        source = mock.Mock(side_effect=MaterialError("MATERIAL_RATE_LIMITED"))
        self.assert_code("MATERIAL_RATE_LIMITED", lambda: self.query.evidence("main.cbl", 1, 1, source))
        source = mock.Mock(side_effect=MaterialError("SOURCE_RATE_LIMIT"))
        self.assert_code("SOURCE_RATE_LIMIT", lambda: self.query.evidence("main.cbl", 1, 1, source))

    def test_source_path_traversal_is_rejected_before_any_read(self):
        for path in ("../main.cbl", "/main.cbl", "C:/main.cbl", "a\\main.cbl"):
            self.assert_code("MATERIAL_INVALID_PATH",
                             lambda value=path: self.query.evidence(value, 1, 1, mock.Mock()))
        self.assertEqual(self.reader.reads, [])


if __name__ == "__main__":
    unittest.main()
