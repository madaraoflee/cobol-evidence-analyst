"""Bounded durable subject memory survives large display-only diagnostics."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from conversation_store import (ConversationStore, MODEL_HISTORY_DETAILS_BYTES,
                                MODEL_HISTORY_REFERENCE_MESSAGES, MODEL_HISTORY_SUBJECT_BYTES)


def subject(name="RESULT-AMOUNT"):
    return {"schema": "conversation-subject/v1", "entities": [name],
            "source_paths": ["programs/entry.cbl"], "source_versions": {"programs/entry.cbl": "a" * 64}}


class ConversationSubjectStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.store = ConversationStore(self.root / "output", self.source)
        self.identifier = self.store.create()["id"]

    def answer(self, memory, *, diagnostics="", other_state=None, **kwargs):
        details = {"investigation_state": {"session_subject": memory, **(other_state or {})},
                   "diagnostics": diagnostics, "cited_evidence_ids": ["ev-source"]}
        return self.store.append(self.identifier, "assistant", "A bounded business explanation.",
                                 details=details, **kwargs)

    def test_large_details_keep_subject_without_materializing_diagnostics(self):
        expected = subject()
        self.store.append(self.identifier, "user", "Where did this amount come from?")
        self.answer(expected, diagnostics="diagnostic-only" * MODEL_HISTORY_DETAILS_BYTES)
        original_loads, original_connect = json.loads, sqlite3.connect
        decoded_sizes, statements = [], []

        def decode(text):
            size = len(text.encode("utf-8"))
            decoded_sizes.append(size)
            self.assertLessEqual(size, MODEL_HISTORY_SUBJECT_BYTES)
            return original_loads(text)

        def connect(*args, **kwargs):
            db = original_connect(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db

        with patch("conversation_store.json.loads", side_effect=decode), \
                patch("conversation_store.sqlite3.connect", side_effect=connect):
            history = self.store.model_history(self.identifier)
        self.assertEqual(history[-1]["investigation_state"], {"session_subject": expected})
        self.assertNotIn("cited_evidence_ids", history[-1])
        self.assertEqual(len(decoded_sizes), 1)
        self.assertFalse(any(statement.startswith("SELECT details FROM") for statement in statements))
        self.assertTrue(any("length(CAST(json_extract" in statement for statement in statements))
        self.assertGreater(len(self.store.get(self.identifier)["messages"][-1]["diagnostics"]),
                           MODEL_HISTORY_DETAILS_BYTES)

    def test_subject_budget_is_utf8_bytes_and_cannot_bypass_through_small_details(self):
        oversized = subject()
        oversized["note"] = "界" * (MODEL_HISTORY_SUBJECT_BYTES // 3 + 1)
        self.assertLess(len(json.dumps(oversized, ensure_ascii=False)), MODEL_HISTORY_SUBJECT_BYTES)
        self.answer(oversized, other_state={"required_obligations": ["retain-existing-state"]})
        history = self.store.model_history(self.identifier)
        self.assertEqual(history[0]["investigation_state"], {"required_obligations": ["retain-existing-state"]})
        self.assertEqual(history[0]["cited_evidence_ids"], ["ev-source"])

    def test_large_subject_and_details_are_discarded_without_json_decode(self):
        oversized = subject()
        oversized["note"] = "X" * MODEL_HISTORY_SUBJECT_BYTES
        self.answer(oversized, diagnostics="X" * (MODEL_HISTORY_DETAILS_BYTES * 2))
        with patch("conversation_store.json.loads", side_effect=AssertionError("Oversized memory was loaded")):
            history = self.store.model_history(self.identifier)
        self.assertEqual(history, [{"role": "assistant", "content": "A bounded business explanation."}])

    def test_only_structured_current_schema_is_restored(self):
        for value in ({"schema": "unknown/v1", "entities": ["RESULT-AMOUNT"]},
                      {"entities": ["RESULT-AMOUNT"]}, [subject()], "RESULT-AMOUNT", None):
            with self.subTest(value=value):
                self.answer(value)
                state = self.store.model_history(self.identifier, max_messages=1)[0].get("investigation_state", {})
                self.assertNotIn("session_subject", state)
        self.store.append(self.identifier, "assistant", json.dumps(subject()))
        self.assertNotIn("investigation_state", self.store.model_history(self.identifier, max_messages=1)[0])

    def test_subject_respects_recent_message_count_and_zero_history(self):
        for index in range(MODEL_HISTORY_REFERENCE_MESSAGES + 2):
            self.answer(subject("VALUE-" + str(index)), diagnostics="X" * (MODEL_HISTORY_DETAILS_BYTES + 1))
        history = self.store.model_history(self.identifier)
        self.assertEqual(len(history), MODEL_HISTORY_REFERENCE_MESSAGES + 2)
        self.assertNotIn("investigation_state", history[0])
        self.assertNotIn("investigation_state", history[1])
        self.assertTrue(all("session_subject" in row["investigation_state"] for row in history[2:]))
        short = self.store.model_history(self.identifier, max_messages=2)
        self.assertEqual(len(short), 2)
        self.assertEqual(short[-1]["investigation_state"]["session_subject"]["entities"], ["VALUE-9"])
        with patch("conversation_store._history_subject", side_effect=AssertionError("Zero history queried subject")):
            self.assertEqual(self.store.model_history(self.identifier, max_characters=0), [])

    def test_subject_survives_reference_budget_exhaustion_and_clipped_answer(self):
        expected = subject("EARLIER-AMOUNT")
        self.answer(expected, diagnostics="X" * 40_000)
        self.answer(subject("LATEST-AMOUNT"), diagnostics="Y" * 40_000)
        history = self.store.model_history(self.identifier)
        self.assertEqual(history[0]["investigation_state"], {"session_subject": expected})
        self.assertNotIn("cited_evidence_ids", history[0])
        self.assertEqual(history[1]["cited_evidence_ids"], ["ev-source"])
        clipped = self.store.model_history(self.identifier, max_messages=1, max_characters=5)
        self.assertEqual(clipped[0]["content"], "A bou")
        self.assertTrue(clipped[0]["context_truncated"])
        self.assertEqual(clipped[0]["investigation_state"]["session_subject"], subject("LATEST-AMOUNT"))

    def test_user_message_details_do_not_supply_local_subject_memory(self):
        self.store.append(self.identifier, "user", "A user-provided question", details={
            "investigation_state": {"session_subject": subject("USER-CLAIMED-AMOUNT")}})
        with patch("conversation_store._history_subject", side_effect=AssertionError("User subject was queried")):
            history = self.store.model_history(self.identifier)
        self.assertNotIn("investigation_state", history[0])

    def test_retry_position_never_uses_future_subject(self):
        self.store.append(self.identifier, "user", "Initial question", run_id="initial")
        self.answer(subject("EARLIER-AMOUNT"), diagnostics="X" * (MODEL_HISTORY_DETAILS_BYTES + 1), run_id="initial")
        failed = self.store.append(self.identifier, "user", "Retry this question", status="failed", run_id="failed")
        self.answer(subject("FAILED-AMOUNT"), status="failed", run_id="failed")
        self.store.append(self.identifier, "user", "Later question", run_id="later")
        self.answer(subject("LATER-AMOUNT"), run_id="later")
        target = self.store.retry_target(self.identifier, failed)
        retry_history = self.store.model_history(self.identifier, before_position=target["position"])
        self.assertEqual(len(retry_history), 2)
        self.assertEqual(retry_history[-1]["investigation_state"]["session_subject"], subject("EARLIER-AMOUNT"))
        self.assertEqual(self.store.model_history(self.identifier)[-1]["investigation_state"]["session_subject"],
                         subject("LATER-AMOUNT"))

    def test_workspace_restore_retains_subject_and_source_ownership(self):
        expected = subject("RESTORED-AMOUNT")
        self.answer(expected, diagnostics="X" * (MODEL_HISTORY_DETAILS_BYTES + 1))
        self.store.append(self.identifier, "user", "Interrupted question", status="pending", run_id="pending")
        restored = ConversationStore(self.root / "output", self.source)
        restored.recover_interrupted()
        history = restored.model_history(self.identifier)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["investigation_state"]["session_subject"], expected)
        with closing(sqlite3.connect(restored.path)) as db:
            self.assertEqual(db.execute("SELECT status FROM messages WHERE run_id='pending'").fetchone()[0], "interrupted")
        other = ConversationStore(self.root / "output", self.root / "other-source")
        with self.assertRaisesRegex(ValueError, "CONVERSATION_NOT_FOUND"):
            other.model_history(self.identifier)


if __name__ == "__main__":
    unittest.main()
