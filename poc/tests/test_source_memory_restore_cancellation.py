"""Cancellation during real archived-source recovery preserves resumable work."""

from contextlib import contextmanager
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from analyze_source import AnalysisCancelled
import prior_source_memory
from run_store import RunStore
from source_session import read_archived_evidence
import test_analysis_compaction_chat as compaction_fixtures
import test_conversation_analysis_handoff as handoff_fixtures


@contextmanager
def cancel_after_first_archive_read():
    read = prior_source_memory.read_archived_evidence
    identifiers = []

    def observed_read(database_path, identifier):
        page = read(database_path, identifier)
        identifiers.append(identifier)
        return page

    def check_cancel():
        if identifiers:
            raise AnalysisCancelled("Cancelled during archived-source recovery")

    with patch.object(prior_source_memory, "read_archived_evidence", observed_read):
        yield check_cancel, identifiers


class SourceMemoryRestoreCancellationTests(unittest.TestCase):
    def test_ordinary_handoff_stops_between_archives_without_replacing_old_checkpoint(self):
        case = handoff_fixtures.ConversationAnalysisHandoffTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        prior = case.prior_analysis()
        before = case.old
        memory = before["checkpoint"]["prior_source_memory"]
        self.assertGreater(len(memory["pages"]), 2)
        archives = {page["evidence_id"]: read_archived_evidence(case.fixture.database, page["evidence_id"])
                    for page in memory["pages"]}
        question = "root.cbl 前面抵扣条件如何共同影响最终金额？"
        case.fixture.question = question
        history = case.store.model_history(case.conversation["id"], question=question,
                                           include_analysis_handoff=True)
        request_count = len(case.fixture.requests)
        with cancel_after_first_archive_read() as (check_cancel, identifiers):
            with self.assertRaises(AnalysisCancelled):
                case.fixture.ask(lambda *_: self.fail("Cancelled handoff sent a model request"),
                    policy=case.case.policy, request_guard=request_count,
                    history=history, conversation_id=case.conversation["id"], check_cancel=check_cancel)
        self.assertEqual(len(identifiers), 1, "Stop before reading the next archived page")
        self.assertEqual(len(case.fixture.requests), request_count)
        self.assertEqual(case.case.saved(prior), before)
        self.assertEqual({identifier: read_archived_evidence(case.fixture.database, identifier)
                          for identifier in archives}, archives)

    def test_same_question_resumes_after_cancellation_inside_memory_restore(self):
        case = compaction_fixtures.AnalysisCompactionChatTests()
        case.setUp()
        self.addCleanup(case.doCleanups)

        class Interrupted(RuntimeError):
            pass

        # A received compression remains safely journaled before its local
        # application. Recovery must replay it rather than send it twice.
        with patch.object(prior_source_memory.PriorSourceMemory, "accept_analysis_compaction", side_effect=Interrupted):
            with self.assertRaises(Interrupted):
                case.fixture.ask(case.respond, policy=case.policy, request_guard=3)
        store = RunStore(case.fixture.root / "investigation-runs.sqlite")
        with store.connect() as connection:
            run_id = connection.execute("SELECT run_id FROM investigation_runs").fetchone()[0]
        before = store.load(run_id)
        pages = before["checkpoint"]["prior_source_memory"]["pages"]
        self.assertGreater(len(pages), 1)
        archives = {page["evidence_id"]: read_archived_evidence(case.fixture.database, page["evidence_id"])
                    for page in pages}
        pending = before["checkpoint"]["prior_source_memory"]["analysis_ledger"]["pending_task"]
        self.assertIsNotNone(pending)
        request_count = len(case.fixture.requests)
        with cancel_after_first_archive_read() as (check_cancel, identifiers):
            with self.assertRaises(AnalysisCancelled):
                case.fixture.ask(case.respond, policy=case.policy, request_guard=request_count,
                    resume_run_id=run_id, check_cancel=check_cancel)
        self.assertEqual(len(identifiers), 1, "Main workflow restore must also check cancellation between pages")
        self.assertEqual(len(case.fixture.requests), request_count)
        self.assertEqual(store.load(run_id), before)
        self.assertEqual({identifier: read_archived_evidence(case.fixture.database, identifier)
                          for identifier in archives}, archives)
        result = case.fixture.ask(case.respond, policy=case.policy, request_guard=7, resume_run_id=run_id)
        self.assertTrue(result["answer_available"], result.get("errors"))
        self.assertEqual(result["run_id"], run_id)
        self.assertEqual(len(case.compactions), 2, "The received first compression must not be sent twice")
        saved = store.load(run_id)
        self.assertEqual(len(saved["requests"]), 7)
        self.assertEqual(len(saved["checkpoint"]["prior_source_memory"]["analysis_ledger"]["generations"]), 2)
        self.assertIn(case.early_id, {row["evidence_id"] for row in result["narrative"]["citations"]})


if __name__ == "__main__":
    unittest.main()
