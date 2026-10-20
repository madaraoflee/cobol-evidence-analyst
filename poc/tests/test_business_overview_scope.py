"""Business overview nomination respects an explicitly selected source file."""

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
from business_map import build_business_map
from question_investigation import build_question_investigation
from repository_discovery import ensure_repository_search
from tools.business_onboarding_fixture import sources


class BusinessOverviewScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.source = root / "source"
        self.source.mkdir()
        contents = sources()
        for name, text in contents.items():
            # Match the exact bytes hashed by the captured source page below.
            (self.source / name).write_bytes(text.encode("utf-8"))
        self.database = root / "index.sqlite"
        build_business_index(self.source, self.database, source_format="free", quiet=True,
                             framework_reference_path="")
        ensure_repository_search(self.database, self.source)
        text = contents["settle.cbl"]
        self.page = {"relative_path": "settle.cbl", "source_text": text,
                     "start_line": 1, "end_line": len(text.splitlines()), "format_hint": "free",
                     "source_sha256": hashlib.sha256(text.encode()).hexdigest(), "evidence_id": "ev-settle"}

    def test_exclusive_overview_does_not_nominate_external_cl_or_leave_its_read_pending(self):
        question = "只分析 settle.cbl 的业务目的和整体流程，不要读取其他文件。"
        mapping = build_business_map(self.database, self.source, question)
        self.assertTrue(mapping["source_identity"]["exclusive_source_scope"])
        self.assertEqual(mapping["source_identity"]["direct_paths"], ["settle.cbl"])
        with patch("batch_context_evidence.nominate_cl_evidence",
                   side_effect=AssertionError("CL nomination is outside this explicit scope")):
            result = build_question_investigation(question, mapping, database_path=self.database,
                                                  source_pages=[self.page])
        self.assertNotIn("cl_context", result)
        self.assertFalse(result["planned_actions"])
        self.assertTrue(result["can_answer"])

    def test_ordinary_overview_still_nominates_the_known_cl_caller_with_one_read(self):
        question = "说明 SETTLE 的业务目的和整体流程。"
        mapping = build_business_map(self.database, self.source, question)
        self.assertFalse(mapping["source_identity"]["exclusive_source_scope"])
        result = build_question_investigation(question, mapping, database_path=self.database,
                                              source_pages=[self.page], max_actions=1)
        self.assertGreater(result["cl_context"]["candidate_count"], 0)
        action, = result["planned_actions"]
        self.assertEqual(action["tool"], "read")
        self.assertEqual(action["arguments"], {"relative_path": "nightjob.clle", "start_line": 1, "end_line": 6})
        self.assertEqual(action["reason"], "indexed_batch_context_not_supplied")


if __name__ == "__main__":
    unittest.main()
