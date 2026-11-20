"""The online model can resume its saved business interpretation at any length."""
from copy import deepcopy
import unittest

from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_online_investigation as fixture


class OnlineResumeDraftTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.OnlineInvestigationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def partial(self, text):
        return self.fixture.run_question([
            {"actions": [fixture.read()]},
            lambda context: fixture.answer(context, text, complete=False, remaining=["Check the worker"]),
        ])

    def test_resume_supplies_saved_interpretation_and_outstanding_question(self):
        first = self.partial("Positive amounts enter the worker; all other amounts require review.")
        resumed = self.fixture.run_question([fixture.answer], checkpoint=first["checkpoint"])
        self.assertEqual(resumed["status"], "answered")
        self.assertNotIn("prior_analysis", self.fixture.contexts[0])
        context = self.fixture.contexts[-1]
        self.assertEqual(context["prior_analysis"]["text"], first["answer"])
        self.assertEqual(context["prior_analysis"]["total_characters"], len(first["answer"]))
        self.assertEqual(context["remaining"], ["Check the worker"])
        self.assertTrue(context["source_context"])

    def test_long_saved_analysis_has_lossless_paging_and_separate_source_citations(self):
        first = self.partial('逐项核实每一条处理条件。' * 1500 + '最后一项适用条件。')
        pages = []
        def collect(context):
            page = (context["observations"][-1]["result"] if pages else context["prior_analysis"])
            self.assertEqual(page["offset"], sum(map(len, pages)))
            pages.append(page["text"])
            if page["next_offset"] is None:
                return fixture.answer(context)
            return {"actions": [{"tool": "read_draft", "offset": page["next_offset"]}]}
        resumed = self.fixture.run_question([collect] * 12, max_model_calls=12, checkpoint=first["checkpoint"])
        self.assertEqual(resumed["status"], "answered", resumed.get('error'))
        self.assertGreater(len(pages), 1)
        self.assertEqual(''.join(pages), first["answer"])
        self.assertEqual(first["answer"], first["checkpoint"]["answer"])
        self.assertEqual([x['path'] for x in resumed['citations']], ['main.cbl'])

    def test_resume_checks_the_draft_source_even_when_outside_the_active_window(self):
        first = self.partial("The caller sends positive amounts for processing.")
        checkpoint = deepcopy(first["checkpoint"])
        checkpoint["focus_evidence"] = []
        def forbidden(*args, **kwargs):
            from online_material_format import MaterialError
            raise MaterialError("SOURCE_FORBIDDEN")
        resumed = self.fixture.run_question([], checkpoint=checkpoint, source_reader=forbidden)
        self.assertEqual(resumed["error"], "SOURCE_FORBIDDEN")
        self.assertEqual(resumed["model_calls"], 0)
