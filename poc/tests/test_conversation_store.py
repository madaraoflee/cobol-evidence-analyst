from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conversation_store import ConversationStore, MODEL_HISTORY_DETAILS_BYTES


class ConversationStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = ConversationStore(self.root / "output", self.root / "source")
        self.identifier = self.store.create()["id"]

    def test_append_checks_ownership_without_reading_history(self):
        with patch.object(self.store, "get", side_effect=AssertionError("transcript loaded")):
            self.store.append(self.identifier, "user", "Explain the adjustment.")
        self.assertEqual(len(self.store.get(self.identifier)["messages"]), 1)
        other = ConversationStore(self.root / "output", self.root / "other-source")
        with self.assertRaisesRegex(ValueError, "CONVERSATION_NOT_FOUND"):
            other.append(self.identifier, "user", "Different source.")
        self.assertEqual(len(self.store.get(self.identifier)["messages"]), 1)

    def test_metadata_does_not_decode_messages(self):
        self.store.append(self.identifier, "assistant", "A retained answer.")
        with patch("conversation_store.json.loads", side_effect=AssertionError("details decoded")):
            metadata = self.store.get_metadata(self.identifier)
        self.assertEqual(metadata["id"], self.identifier)
        self.assertNotIn("messages", metadata)

    def test_history_keeps_recent_turns_in_order_with_source_references(self):
        for i in range(20):
            self.store.append(self.identifier, "user", f"Question {i}")
            self.store.append(self.identifier, "assistant", f"Answer {i}", details={
                "evidence_refs": [{"evidence_id": f"ev_{i}", "relative_path": "entry.cbl"}],
                "cited_evidence_ids": [f"ev_{i}"], "diagnostics": ["display only"]})
        history = self.store.model_history(self.identifier, max_messages=6)
        self.assertEqual([item["content"] for item in history],
                         [text for i in range(17, 20) for text in (f"Question {i}", f"Answer {i}")])
        self.assertEqual(history[-1]["cited_evidence_ids"], ["ev_19"])
        self.assertEqual(history[-1]["evidence_refs"][0]["relative_path"], "entry.cbl")
        self.assertNotIn("diagnostics", history[-1])
        self.assertEqual(len(self.store.get(self.identifier)["messages"]), 40)

    def test_large_display_details_are_not_loaded_into_provider_memory(self):
        large_details = {"diagnostics": ["X" * (MODEL_HISTORY_DETAILS_BYTES * 2)]}
        self.store.append(self.identifier, "assistant", "First answer", details=large_details)
        self.store.append(self.identifier, "user", "Follow-up")
        self.store.append(self.identifier, "assistant", "Latest answer", details={
            "cited_evidence_ids": ["ev_latest"], "evidence_refs": [{"evidence_id": "ev_latest"}]})
        original_loads = json.loads
        lengths = []

        def decode(value):
            lengths.append(len(value.encode("utf-8")))
            self.assertLessEqual(lengths[-1], MODEL_HISTORY_DETAILS_BYTES)
            return original_loads(value)

        with patch("conversation_store.json.loads", side_effect=decode):
            history = self.store.model_history(self.identifier)
        self.assertLessEqual(sum(lengths), MODEL_HISTORY_DETAILS_BYTES)
        self.assertEqual(history[-1]["cited_evidence_ids"], ["ev_latest"])
        self.assertNotIn("diagnostics", history[0])
        self.assertEqual(self.store.get(self.identifier)["messages"][0]["diagnostics"],
                         large_details["diagnostics"])

    def test_history_clips_oversized_message_in_sql_without_changing_saved_text(self):
        content = "业务说明" * 10000
        self.store.append(self.identifier, "assistant", content)
        original_connect = sqlite3.connect
        statements = []

        def connection(*args, **kwargs):
            db = original_connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db

        with patch("conversation_store.sqlite3.connect", side_effect=connection):
            history = self.store.model_history(self.identifier, max_characters=100)
        self.assertEqual(history[0]["content"], content[:100])
        self.assertTrue(history[0]["context_truncated"])
        self.assertTrue(any("substr(content,1,100)" in statement for statement in statements))
        self.assertEqual(self.store.get(self.identifier)["messages"][0]["content"], content)

    def test_history_respects_source_ownership(self):
        other = ConversationStore(self.root / "output", self.root / "other-source")
        with self.assertRaisesRegex(ValueError, "CONVERSATION_NOT_FOUND"):
            other.model_history(self.identifier)

    def test_large_latest_answer_keeps_the_question_and_honors_larger_policy_budget(self):
        question = "Explain the exceptions for this adjustment."
        answer = "Business rule explanation. " * 1600
        self.store.append(self.identifier, "user", question)
        self.store.append(self.identifier, "assistant", answer)
        history = self.store.model_history(self.identifier, max_characters=18000)
        self.assertEqual(history[0]["content"], question)
        self.assertEqual(history[1]["role"], "assistant")
        self.assertTrue(history[1]["content"].startswith("Business rule explanation."))
        self.assertEqual(sum(len(item["content"]) for item in history), 18000)
        for budget in (60000, 128000):
            retained = self.store.model_history(self.identifier, max_characters=budget)
            self.assertEqual(retained[1]["content"], answer)
        self.assertEqual(self.store.model_history(self.identifier, max_characters=0), [])
        self.assertEqual(self.store.get(self.identifier)["messages"][1]["content"], answer)


if __name__ == "__main__":
    unittest.main()
