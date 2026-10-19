"""Reuse physical COPY tokens without reusing an inclusion's meaning or origin."""

from dataclasses import replace
import hashlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_index import build_business_index
from compilation_context import CompilationContext
import procedure_expansion as expansion
from repository_discovery import ensure_repository_search
from semantic_scope import prepare_semantic_scope
import source_session
from source_session import QuestionSourceSession


def program(name, body):
    return (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 INPUT-VALUE PIC 9(8).\n01 RESULT-VALUE PIC 9(8).\n"
            f"PROCEDURE DIVISION.\nMAIN.\n{body}\nGOBACK.\n")


class ExpansionSourceCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"

    def write(self, name, text):
        (self.source / name).write_text(text, encoding="utf-8")

    def build(self):
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)

    def expand(self, session, host="FIRST", **options):
        return expansion.expand_program(session.mirror_root, host,
            source_provider=options.pop("provider", session),
            source_catalog=sorted(path.name for path in self.source.iterdir()),
            entry_relative_path=host.lower() + ".cbl",
            compilation_context=options.pop("compilation_context", CompilationContext(source_format="free")),
            **options)

    def fixture(self, body="COPY SHARED.", copy="MOVE INPUT-VALUE TO RESULT-VALUE.\n"):
        self.write("first.cbl", program("FIRST", body))
        self.write("shared.cpy", copy)
        self.build()

    def test_shared_copy_reuses_lexing_but_keeps_host_replacements_and_inclusion_sites(self):
        self.write("first.cbl", program("FIRST", "COPY SHARED REPLACING ==INPUT-VALUE== BY ==FIRST-VALUE==."))
        self.write("second.cbl", program("SECOND", "COPY SHARED REPLACING ==INPUT-VALUE== BY ==SECOND-VALUE==."))
        self.write("shared.cpy", "MOVE INPUT-VALUE TO RESULT-VALUE.\n" * 80)
        self.build()
        with QuestionSourceSession(self.database, self.source) as session:
            first = self.expand(session)
            with mock.patch("procedure_expansion._lex_line", wraps=expansion._lex_line) as lex:
                second = self.expand(session, "SECOND")
            self.assertEqual(lex.call_count, len((self.source / "second.cbl").read_text().splitlines()))
            with mock.patch.object(session, "cached_expansion_source", return_value=None):
                uncached = self.expand(session, "SECOND")
            self.assertEqual(second, uncached)
            first_lines = [row for row in first["lines"] if row["include_chain"]]
            second_lines = [row for row in second["lines"] if row["include_chain"]]
            self.assertIn("FIRST-VALUE", first_lines[0]["code"])
            self.assertIn("SECOND-VALUE", second_lines[0]["code"])
            self.assertEqual(first_lines[0]["origin"], second_lines[0]["origin"])
            self.assertNotEqual(first_lines[0]["include_chain"], second_lines[0]["include_chain"])
            self.assertNotEqual(first_lines[0]["replacement_trace"], second_lines[0]["replacement_trace"])
            self.assertTrue(all(isinstance(value[0], expansion._Source) for value in session._expansion_sources.values()))

    def test_compile_conditions_and_full_context_have_separate_cache_entries(self):
        self.fixture(copy=">>IF FEATURE\nMOVE 1 TO RESULT-VALUE.\n>>ELSE\nMOVE 2 TO RESULT-VALUE.\n>>END-IF\n")
        enabled = CompilationContext(source_format="free", compile_conditions=(("FEATURE", True),))
        disabled = replace(enabled, compile_conditions=(("FEATURE", False),))
        with QuestionSourceSession(self.database, self.source) as session:
            first = self.expand(session, compilation_context=enabled)
            with mock.patch("procedure_expansion._lex_line", wraps=expansion._lex_line) as lex:
                second = self.expand(session, compilation_context=disabled)
            self.assertGreater(lex.call_count, 0)
            self.assertNotEqual(first["lines"], second["lines"])
            with mock.patch.object(session, "cached_expansion_source", return_value=None):
                self.assertEqual(second, self.expand(session, compilation_context=disabled))
            for changed in (replace(enabled, dialect="test-dialect"), replace(enabled, encoding="latin-1"),
                            replace(enabled, debug_lines=True), replace(enabled, source_format="auto"),
                            replace(enabled, copy_libraries=("library",)), replace(enabled, target_profile="alternate")):
                with mock.patch("procedure_expansion._lex_line", wraps=expansion._lex_line) as lex:
                    self.expand(session, compilation_context=changed)
                self.assertGreater(lex.call_count, 0)

    def test_changed_capture_hash_encoding_and_lexical_version_do_not_reuse_tokens(self):
        self.fixture()
        with QuestionSourceSession(self.database, self.source) as session:
            before = self.expand(session)
            capture = session.capture("shared.cpy")
            data = b"MOVE 7 TO RESULT-VALUE.\n"
            capture.path.write_bytes(data)
            session._captured["shared.cpy"] = replace(capture, sha256=hashlib.sha256(data).hexdigest(), size=len(data))
            with mock.patch("procedure_expansion._lex_line", wraps=expansion._lex_line) as lex:
                after = self.expand(session)
            self.assertEqual(lex.call_count, 1)
            self.assertNotEqual(before["lines"], after["lines"])
            session._captured["shared.cpy"] = replace(session.capture("shared.cpy"), encoding="latin-1")
            with mock.patch("procedure_expansion._lex_line", wraps=expansion._lex_line) as lex:
                self.expand(session)
            self.assertEqual(lex.call_count, 1)
            with mock.patch("procedure_expansion.LEXICAL_SOURCE_VERSION", "test-new-lexical-version"), \
                 mock.patch("procedure_expansion._lex_line", wraps=expansion._lex_line) as lex:
                self.expand(session)
            self.assertGreater(lex.call_count, 1)

    def test_cached_source_still_observes_cancellation(self):
        self.fixture()
        with QuestionSourceSession(self.database, self.source) as session:
            self.expand(session)
            failure = RuntimeError("cancelled cached source")
            session.check_cancel = mock.Mock(side_effect=failure)
            with mock.patch("procedure_expansion._lex_line", side_effect=AssertionError("repeated lexing")):
                with self.assertRaises(RuntimeError) as caught:
                    self.expand(session)
            self.assertIs(caught.exception, failure)

    def test_failed_lexing_is_not_published_as_a_cached_source(self):
        self.fixture()
        with QuestionSourceSession(self.database, self.source) as session:
            with mock.patch("procedure_expansion._lex_line", side_effect=RuntimeError("lexing interrupted")):
                with self.assertRaises(RuntimeError):
                    self.expand(session)
            self.assertEqual(len(session._expansion_sources), 0)
            self.assertTrue(self.expand(session)["source_expansion_complete"])

    def test_token_cache_limits_evict_old_entries_and_skip_oversized_values(self):
        self.fixture()
        with QuestionSourceSession(self.database, self.source) as session:
            self.expand(session)
            key, (tokens, size) = next(iter(session._expansion_sources.items()))
            session._expansion_sources.clear()
            session._expansion_source_bytes = 0
            with mock.patch.object(source_session, "EXPANSION_CACHE_MAX_ENTRIES", 2):
                session.cache_expansion_source(("first",), tokens)
                session.cache_expansion_source(("second",), tokens)
                session.cached_expansion_source(("first",))
                session.cache_expansion_source(("third",), tokens)
            self.assertEqual(list(session._expansion_sources), [("first",), ("third",)])
            self.assertLessEqual(session._expansion_source_bytes, source_session.EXPANSION_CACHE_MAX_BYTES)
            with mock.patch.object(source_session, "EXPANSION_CACHE_MAX_BYTES", 1):
                session.cache_expansion_source(key, tokens)
            self.assertNotIn(key, session._expansion_sources)
            session.cache_expansion_source(("mutable",), {"lines": []})
            self.assertNotIn(("mutable",), session._expansion_sources)
            session._expansion_sources.clear()
            session._expansion_source_bytes = 0
            session.cache_expansion_source(("alpha",), tokens)
            byte_limit = session._expansion_source_bytes + 1
            with mock.patch.object(source_session, "EXPANSION_CACHE_MAX_BYTES", byte_limit):
                session.cache_expansion_source(("bravo",), tokens)
            self.assertEqual(list(session._expansion_sources), [("bravo",)])
            self.assertLessEqual(session._expansion_source_bytes, byte_limit)
        self.assertEqual(session._expansion_source_bytes, 0)
        self.assertFalse(session._expansion_sources)

    def test_source_with_lexical_problems_is_not_cached(self):
        self.fixture(copy="DISPLAY 'unterminated\n")
        with QuestionSourceSession(self.database, self.source) as session:
            result = self.expand(session)
            self.assertFalse(result["source_expansion_complete"])
            self.assertFalse(any(key[0] == "shared.cpy" for key in session._expansion_sources))
            with mock.patch("procedure_expansion._lex_line", wraps=expansion._lex_line) as lex:
                self.assertEqual(self.expand(session), result)
            self.assertEqual(lex.call_count, 1)

    def test_close_releases_cached_tokens_even_when_a_scope_cannot_close(self):
        self.fixture()
        session = QuestionSourceSession(self.database, self.source)
        self.expand(session)
        self.assertTrue(session._expansion_sources)
        session.register_scope(SimpleNamespace(close=mock.Mock(side_effect=RuntimeError("scope close failed"))))
        with self.assertRaisesRegex(RuntimeError, "scope close failed"):
            session.close()
        self.assertFalse(session._expansion_sources)
        self.assertEqual(session._expansion_source_bytes, 0)
        self.assertFalse(session.mirror_root.exists())

    def test_capture_only_provider_remains_compatible_without_cache_or_hash_metadata(self):
        self.fixture()
        with QuestionSourceSession(self.database, self.source) as session:
            expected = self.expand(session)
            def capture(relative):
                value = session.capture(relative)
                return SimpleNamespace(path=value.path, encoding=value.encoding)
            provider = SimpleNamespace(capture=capture)
            with mock.patch("procedure_expansion._lex_line", wraps=expansion._lex_line) as lex:
                actual = self.expand(session, provider=provider)
            self.assertGreater(lex.call_count, 0)
            self.assertEqual(actual, expected)

    def test_warm_semantic_scope_preserves_derived_hashes_without_relexing(self):
        self.fixture(copy="COMPUTE RESULT-VALUE = INPUT-VALUE * 2.\n" * 120)
        with QuestionSourceSession(self.database, self.source) as session:
            options = {"anchors": [{"relative_path": "first.cbl", "line": 9}], "policy": AgentPolicy()}
            with prepare_semantic_scope(self.database, session, **options) as cold:
                manifest, mapping, key = cold.derived_manifest, cold.source_map, cold.scope_key
                self.assertFalse(cold.cache_hit)
            with mock.patch("procedure_expansion._lex_line", side_effect=AssertionError("warm tokenization")):
                with prepare_semantic_scope(self.database, session, **options) as warm:
                    self.assertTrue(warm.cache_hit)
                    self.assertEqual(warm.derived_manifest, manifest)
                    self.assertEqual(warm.source_map, mapping)
                    self.assertEqual(warm.scope_key, key)


if __name__ == "__main__":
    unittest.main()
