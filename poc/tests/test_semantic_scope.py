from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_index import build_business_index
from repository_discovery import ensure_repository_search
from semantic_scope import _prune_cache, build_business_evidence, prepare_semantic_scope
from source_session import QuestionSourceSession, read_archived_evidence


class SemanticScopeTests(unittest.TestCase):
    def test_repeated_process_copy_has_distinct_context_and_warm_sidecar(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            (source / "entry.cbl").write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRY.\n"
                "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 INPUT-VALUE PIC 9(4).\n"
                "01 RESULT-VALUE PIC 9(4).\nPROCEDURE DIVISION.\nMAIN.\n"
                "COPY STEPCTL.\nMOVE RESULT-VALUE TO INPUT-VALUE.\nCOPY STEPCTL.\nGOBACK.\n", encoding="utf-8")
            (source / "stepctl.cpy").write_text("COMPUTE RESULT-VALUE = INPUT-VALUE * 2.\n", encoding="utf-8")
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", verify_content=True)
            ensure_repository_search(database, source)
            policy = AgentPolicy()
            with QuestionSourceSession(database, source) as session:
                with mock.patch("procedure_expansion.iter_source_files", side_effect=AssertionError("directory scan")):
                    scope = prepare_semantic_scope(database, session,
                        anchors=[{"relative_path": "stepctl.cpy", "line": 1}],
                        requested_calls=["entry.cbl"], policy=policy)
                with scope:
                    self.assertFalse(scope.cache_hit)
                    self.assertIn("entry.cbl", scope.source_map)
                    group = build_business_evidence(scope, session,
                        anchor={"relative_path": "stepctl.cpy", "line": 1}, policy=policy)
                    copies = [ref for observation in group.observations for ref in observation.source_refs
                              if ref.original_relative_path == "stepctl.cpy" and ref.include_chain]
                    self.assertGreaterEqual(len({ref.evidence_id for ref in copies}), 2)
                    self.assertEqual({item["relative_path"] for item in scope.input_manifest},
                                     {"entry.cbl", "stepctl.cpy"})
                    self.assertNotEqual(next(item["sha256"] for item in scope.derived_manifest
                        if item["relative_path"] == "entry.cbl"),
                        next(item["sha256"] for item in scope.input_manifest
                        if item["relative_path"] == "entry.cbl"))
                with mock.patch("semantic_scope.build_structural_index", side_effect=AssertionError("warm rebuild")):
                    warm = prepare_semantic_scope(database, session,
                        anchors=[{"relative_path": "stepctl.cpy", "line": 1}],
                        requested_calls=["entry.cbl"], policy=policy)
                self.assertTrue(warm.cache_hit)
                warm.close()
                _prune_cache(scope.database_path.parent, 0)
                self.assertFalse(scope.database_path.exists())
                self.assertIsNotNone(read_archived_evidence(database, copies[0].evidence_id))


if __name__ == "__main__":
    unittest.main()
