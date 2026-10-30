"""Historical and current citations must retain physical excerpt limits."""

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from company_api import CompanyAPIConfig
from conversation_store import ConversationStore
from source_reading import _identify_page
from source_session import archive_evidence
from web_app import WorkbenchState


class ArchivedEvidenceVisibilityTests(unittest.TestCase):
    def test_current_and_historical_archives_preserve_truncated_original(self):
        self.check_visibility(truncated=True)

    def test_complete_and_legacy_archives_remain_readable(self):
        for truncated in (False, None):
            with self.subTest(truncated=truncated):
                self.check_visibility(truncated=truncated)

    def check_visibility(self, *, truncated):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output, source = root / "output", root / "source"
            output.mkdir()
            source.mkdir()
            text = "COMPUTE RESULT-VALUE = INPUT-VALUE * 2 ON SIZE ERROR MOVE 0 TO RESULT-VALUE."
            page = {"relative_path": "rule.cbl", "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "start_line": 1, "end_line": 1, "source_text": text[:35] if truncated else text}
            if truncated is not None:
                page["span_truncated"] = truncated
            page["evidence_id"] = _identify_page(page)
            archive_evidence(output / "structural-index.sqlite", [page])
            config = CompanyAPIConfig("https://api.example.invalid/v1", "test-model", api_key="offline-only")
            app = WorkbenchState(state_path=root / "state.json", config_provider=lambda: config)
            app.project.update(source=str(source), output=str(output), snapshot_id="current-snapshot", run_id="current-run",
                agent={"agent_result": {"snapshot_id": "current-snapshot", "evidence_refs": [page]}})
            store = app.conversation_store = ConversationStore(output, source)
            conversation = store.create(snapshot_id="earlier-snapshot")
            message = store.append(conversation["id"], "assistant", "Earlier explanation.", run_id="earlier-run",
                details={"snapshot_id": "earlier-snapshot", "evidence_refs": [page]})
            for options in ({}, {"conversation_id": conversation["id"], "message_id": message}):
                with self.subTest(historical=bool(options)):
                    result = app.evidence(page["evidence_id"], **options)
                    self.assertEqual(result["spans"][0]["source_text"], page["source_text"])
                    self.assertEqual(result["spans"][0]["source_sha256"], page["source_sha256"])
                    self.assertIs(result["spans"][0]["span_truncated"], bool(truncated))
                    self.assertIs(result["truncated"], bool(truncated))


if __name__ == "__main__":
    unittest.main()
