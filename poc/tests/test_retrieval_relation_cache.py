"""Question-local relation reuse must preserve source and citation semantics."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
import repository_discovery as retrieval
from source_session import QuestionSourceSession


class RetrievalRelationCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.main = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. MAIN.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 AMOUNT PIC 9(9).\n"
            "PROCEDURE DIVISION.\nMAIN-WORK.\nCALL 'WORKER'.\nPERFORM CALCULATE-AMOUNT.\n"
            "GOBACK.\nCALCULATE-AMOUNT.\n" + "ADD 1 TO AMOUNT.\n" * 800 + "EXIT.\n")
        (self.source / "main.cbl").write_text(self.main)
        (self.source / "worker.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. WORKER.\nPROCEDURE DIVISION.\nGOBACK.\n")
        self.build()

    def build(self):
        build_business_index(self.source, self.database, source_format="free", quiet=True,
                             framework_reference_path="")
        retrieval.ensure_repository_search(self.database, self.source)

    def read(self, session, path="main.cbl", start=1, end=20, **kwargs):
        return retrieval.read_repository_context(self.database, self.source,
            relative_path=path, start_line=start, end_line=end, source_session=session, **kwargs)

    def test_all_context_fields_match_uncached_reads_and_search(self):
        def sequence(session):
            result = [retrieval.retrieve_repository_context(self.database, self.source,
                "MAIN AMOUNT", source_session=session)]
            result.extend(self.read(session, start=line, end=line + 35)
                          for line in (1, 100, 200, 350, 600))
            result.append(self.read(session, "worker.cbl"))
            return result

        with QuestionSourceSession(self.database, self.source) as session, \
             patch.object(retrieval, "_session_relation_cache", return_value=None), \
             patch.object(retrieval, "_context_relations", wraps=retrieval._context_relations) as query:
            uncached = sequence(session)
            uncached_queries = query.call_count
        with QuestionSourceSession(self.database, self.source) as session, \
             patch.object(retrieval, "_context_relations", wraps=retrieval._context_relations) as query:
            cached = sequence(session)
            cached_queries = query.call_count
        self.assertEqual(cached, uncached)
        self.assertLess(cached_queries, uncached_queries)

    def test_citation_ranges_and_caller_mutation_do_not_modify_cached_facts(self):
        with QuestionSourceSession(self.database, self.source) as session, \
             patch.object(retrieval, "_context_relations", wraps=retrieval._context_relations) as query:
            first = self.read(session)
            call = next(link for link in first["call_chain"]["links"] if link["relation_type"] == "CALLS")
            self.assertTrue(call["caller_evidence_ids"])
            call["resolution"] = "caller-mutated"
            call["caller_evidence_ids"].append("invalid-citation")
            second = self.read(session, start=100, end=120)
        self.assertEqual(query.call_count, 1)
        call = next(link for link in second["call_chain"]["links"] if link["relation_type"] == "CALLS")
        self.assertEqual(call["resolution"], "confirmed")
        self.assertEqual(call["caller_evidence_ids"], [])

    def test_cached_relations_still_verify_callers_against_captured_content(self):
        with QuestionSourceSession(self.database, self.source) as session, \
             patch.object(retrieval, "_context_relations", wraps=retrieval._context_relations) as query:
            first = self.read(session, "worker.cbl")
            self.assertTrue(first["call_chain"]["links"])
            capture = session.capture

            def mismatched_caller(path):
                item = capture(path)
                return replace(item, sha256="0" * 64) if path == "main.cbl" else item

            with patch.object(session, "capture", side_effect=mismatched_caller):
                second = self.read(session, "worker.cbl")
        self.assertEqual(query.call_count, 1)
        self.assertTrue(second["pages"])
        self.assertEqual(second["call_chain"]["links"], [])
        self.assertTrue(second["needs_refresh"])
        self.assertEqual(second["boundaries"][0]["relative_path"], "main.cbl")

    def test_refresh_with_new_snapshot_reloads_relations(self):
        with QuestionSourceSession(self.database, self.source) as session, \
             patch.object(retrieval, "_context_relations", wraps=retrieval._context_relations) as query:
            first = self.read(session)
            (self.source / "worker.cbl").write_text(
                "IDENTIFICATION DIVISION.\nPROGRAM-ID. OTHER-WORKER.\nPROCEDURE DIVISION.\nGOBACK.\n")
            self.build()
            second = self.read(session)
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(query.call_count, 2)
        call = next(link for link in second["call_chain"]["links"] if link["relation_type"] == "CALLS")
        self.assertEqual(call["resolution"], "unresolved")
        self.assertIsNone(call["target_path"])

    def test_same_source_hash_does_not_reuse_another_parser_generation(self):
        with QuestionSourceSession(self.database, self.source) as session, \
             patch.object(retrieval, "_context_relations", wraps=retrieval._context_relations) as query:
            first = self.read(session)
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute("UPDATE metadata SET value='alternate-parser/v2' WHERE key='parser_version'")
                db.execute("UPDATE relations SET status='unresolved',target_entity_id=NULL WHERE relation_type='CALLS'")
            second = self.read(session)
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(query.call_count, 2)
        call = next(link for link in second["call_chain"]["links"] if link["relation_type"] == "CALLS")
        self.assertEqual(call["resolution"], "unresolved")

    def test_cache_is_question_local_and_cancel_still_propagates(self):
        failure = RuntimeError("QUESTION_CANCELLED")

        def cancelled():
            raise failure

        with patch.object(retrieval, "_context_relations", wraps=retrieval._context_relations) as query:
            with QuestionSourceSession(self.database, self.source) as session:
                self.read(session)
                with self.assertRaises(RuntimeError) as caught:
                    self.read(session, check_cancel=cancelled)
                self.assertIs(caught.exception, failure)
            with QuestionSourceSession(self.database, self.source) as another:
                self.read(another)
        self.assertEqual(query.call_count, 2)

    def test_paths_limits_and_eviction_keep_queries_equivalent(self):
        with QuestionSourceSession(self.database, self.source) as session, \
             closing(retrieval._connect(self.database)) as db:
            db.execute("BEGIN")
            cache = retrieval._session_relation_cache(db, session)
            for path, limit in [("main.cbl", count) for count in range(1, 35)] + [("worker.cbl", 1), ("main.cbl", 1)]:
                expected = retrieval._context_relations(db, [path], limit)
                actual = retrieval._cached_context_relations(db, [path], limit, relation_cache=cache)
                self.assertEqual(actual, expected)
                self.assertLessEqual(len(cache), 32)


if __name__ == "__main__":
    unittest.main()
