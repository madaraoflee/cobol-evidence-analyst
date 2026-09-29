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

    def test_delete_removes_only_the_owned_conversation_and_its_messages(self):
        self.store.append(self.identifier, "user", "Explain the request.")
        other = ConversationStore(self.root / "output", self.root / "other-source")
        other_id = other.create()["id"]
        other.append(other_id, "user", "Explain the other request.")
        with self.assertRaisesRegex(ValueError, "CONVERSATION_NOT_FOUND"):
            other.delete(self.identifier)
        self.assertEqual(len(self.store.get(self.identifier)["messages"]), 1)

        self.store.delete(self.identifier)
        self.assertEqual(self.store.list(), [])
        with self.assertRaisesRegex(ValueError, "CONVERSATION_NOT_FOUND"):
            self.store.get(self.identifier)
        with self.assertRaisesRegex(ValueError, "CONVERSATION_NOT_FOUND"):
            self.store.delete(self.identifier)
        with sqlite3.connect(self.store.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE conversation_id=?",
                                        (self.identifier,)).fetchone()[0], 0)
        self.assertEqual(other.get(other_id)["messages"][0]["content"],
                         "Explain the other request.")

    def test_retry_reuses_failed_turn_positions_and_only_completed_prior_history(self):
        self.store.append(self.identifier, "user", "First question", run_id="first-run")
        self.store.append(self.identifier, "assistant", "First answer", run_id="first-run")
        user_id = self.store.append(self.identifier, "user", "Failed question",
                                    status="failed", run_id="failed-run")
        assistant_id = self.store.append(self.identifier, "assistant", "No answer",
                                         status="failed", run_id="failed-run")
        self.store.append(self.identifier, "user", "Later question", run_id="later-run")
        self.store.append(self.identifier, "assistant", "Later answer", run_id="later-run")
        target = self.store.retry_target(self.identifier, user_id)
        self.assertEqual(target["question"], "Failed question")
        self.assertEqual(target["assistant_message_id"], assistant_id)
        self.assertEqual([item["content"] for item in
                          self.store.model_history(self.identifier, before_position=target["position"])],
                         ["First question", "First answer"])
        self.assertEqual([item["content"] for item in self.store.model_history(self.identifier)],
                         ["First question", "First answer", "Later question", "Later answer"])

        self.store.claim_retry(self.identifier, user_id, "retry-run")
        with self.assertRaisesRegex(ValueError, "RETRY_NOT_AVAILABLE"):
            self.store.claim_retry(self.identifier, user_id, "duplicate-run")
        self.assertEqual(self.store.get(self.identifier)["messages"][2]["status"], "pending")
        self.store.complete_user("retry-run", "failed")
        repeated = self.store.retry_target(self.identifier, user_id)
        self.assertEqual(repeated["assistant_message_id"], assistant_id)
        self.store.claim_retry(self.identifier, user_id, "second-retry-run")
        self.store.complete_user("second-retry-run", "completed")
        replaced_id = self.store.replace_assistant(
            self.identifier, assistant_id, "Recovered answer", run_id="second-retry-run",
            details={"evidence_refs": [{"evidence_id": "ev-recovered"}]})
        self.assertEqual(replaced_id, assistant_id)
        messages = self.store.get(self.identifier)["messages"]
        self.assertEqual(len(messages), 6)
        self.assertEqual([item["id"] for item in messages][2:4], [user_id, assistant_id])
        self.assertEqual([item["run_id"] for item in messages][2:4],
                         ["second-retry-run", "second-retry-run"])
        self.assertEqual(messages[3]["content"], "Recovered answer")
        self.assertEqual(messages[3]["evidence_refs"], [{"evidence_id": "ev-recovered"}])
        self.assertEqual([item["content"] for item in self.store.model_history(self.identifier)],
                         ["First question", "First answer", "Failed question", "Recovered answer",
                          "Later question", "Later answer"])

    def test_retry_without_failed_assistant_appends_one_answer(self):
        for status in ("failed", "cancelled", "interrupted"):
            identifier = self.store.create()["id"]
            user_id = self.store.append(identifier, "user", status, status=status,
                                        run_id=status + "-run")
            target = self.store.retry_target(identifier, user_id)
            self.assertIsNone(target["assistant_message_id"])
            self.store.claim_retry(identifier, user_id, status + "-retry")
            self.store.complete_user(status + "-retry", "completed")
            answer_id = self.store.replace_assistant(identifier, None, "Recovered",
                                                     run_id=status + "-retry")
            messages = self.store.get(identifier)["messages"]
            self.assertEqual([item["id"] for item in messages], [user_id, answer_id])
            self.assertEqual([item["status"] for item in messages],
                             ["completed", "completed"])

    def test_retry_rejects_other_sources_and_nonfailed_messages(self):
        user_id = self.store.append(self.identifier, "user", "A completed question")
        assistant_id = self.store.append(self.identifier, "assistant", "A completed answer")
        other = ConversationStore(self.root / "output", self.root / "other-source")
        with self.assertRaisesRegex(ValueError, "CONVERSATION_NOT_FOUND"):
            other.retry_target(self.identifier, user_id)
        with self.assertRaisesRegex(ValueError, "CONVERSATION_NOT_FOUND"):
            other.claim_retry(self.identifier, user_id, "new-run")
        with self.assertRaisesRegex(ValueError, "CONVERSATION_NOT_FOUND"):
            other.replace_assistant(self.identifier, assistant_id, "Changed")
        with self.assertRaisesRegex(ValueError, "RETRY_NOT_AVAILABLE"):
            self.store.retry_target(self.identifier, user_id)
        with self.assertRaisesRegex(ValueError, "RETRY_NOT_AVAILABLE"):
            self.store.claim_retry(self.identifier, user_id, "new-run")
        with self.assertRaisesRegex(ValueError, "RETRY_NOT_AVAILABLE"):
            self.store.replace_assistant(self.identifier, assistant_id, "Changed")
        for position in (0, -1, True, "2"):
            with self.assertRaisesRegex(ValueError, "CONVERSATION_HISTORY_POSITION_INVALID"):
                self.store.model_history(self.identifier, before_position=position)

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
