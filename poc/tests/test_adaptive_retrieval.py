"""Corpus-independent search leads must survive bounded evidence retrieval."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
import repository_discovery as retrieval


LONG_QUESTION = (
    "希望根据当前提供的程序和数据定义详细解释这段业务逻辑在运行过程当中如何对不同输入情况进行处理"
    "并说明相关计算的依据判断条件字段来源以及结果输出请结合上下文给出完整解释最后需要重点检查星辉额度"
)


def program(name, body="GOBACK."):
    return f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nPROCEDURE DIVISION.\n{body}\n"


class AdaptiveRetrievalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.database = self.base / "index.sqlite"

    def write(self, relative, body, name="ENTRY"):
        path = self.source / relative
        path.write_text(program(name, body), encoding="utf-8")
        return path

    def build(self):
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        retrieval.ensure_repository_search(self.database, self.source)

    def test_long_chinese_question_retains_new_english_clue_and_consistent_match_status(self):
        self.write("first.cbl", "GOBACK.", "FIRST")
        self.write("target.cbl", "*> NebulaRewardCap\nGOBACK.", "TARGET")
        self.build()
        terms, omitted = retrieval._query_terms(LONG_QUESTION, ["NebulaRewardCap"])
        self.assertIn("nebularewardcap", terms)
        self.assertGreater(omitted, 0)
        self.assertLessEqual(len(terms), 128)
        discovery = retrieval.discover_repository(self.database, LONG_QUESTION,
            search_terms=["NebulaRewardCap"], fallback_to_repository=False)
        context = retrieval.retrieve_repository_context(self.database, self.source, LONG_QUESTION,
            search_terms=["NebulaRewardCap"], max_pages=1)
        self.assertEqual(discovery["selected_paths"], ["target.cbl"])
        self.assertEqual(discovery["matched_file_count"], context["matched_file_count"])
        self.assertEqual(context["matched_file_count"], 1)
        self.assertEqual(context["matched_page_count"], 1)
        self.assertFalse(context["orientation_only"])
        self.assertEqual(context["selected_paths"], ["target.cbl"])

    def test_late_search_clue_survives_an_earlier_verbose_suggestion(self):
        self.write("target.cbl", "*> LanternCapValue\nGOBACK.")
        self.build()
        suggestions = [LONG_QUESTION, "LanternCapValue"]
        terms, omitted = retrieval._query_terms(LONG_QUESTION, suggestions)
        self.assertIn("lanterncapvalue", terms)
        self.assertGreater(omitted, 0)
        context = retrieval.retrieve_repository_context(self.database, self.source, LONG_QUESTION,
            search_terms=suggestions, max_pages=1)
        self.assertEqual(context["selected_paths"], ["target.cbl"])
        self.assertFalse(context["orientation_only"])

    def test_exact_identifier_precedes_more_than_a_candidate_limit_of_split_phrase_hits(self):
        for index in range(24):
            self.write(f"a-noise-{index:02}.cbl", "*> settlement cutoff code\nGOBACK.", f"NOISE-{index}")
        self.write("z-target.cbl", "*> SETTLEMENT-CUTOFF-CODE.\n" +
            "\n".join(f"*> filler{index}" for index in range(650)) + "\nGOBACK.", "TARGET")
        self.build()
        context = retrieval.retrieve_repository_context(self.database, self.source, LONG_QUESTION,
            search_terms=["SETTLEMENT-CUTOFF-CODE"], max_pages=1, max_chars=1000)
        self.assertEqual(context["selected_paths"], ["z-target.cbl"])
        self.assertIn("SETTLEMENT-CUTOFF-CODE.", context["pages"][0]["source_text"])
        self.assertEqual(context["matched_file_count"], 25)

    def test_identifier_priority_preserves_case_underscores_and_rejects_longer_names(self):
        self.write("a-noise.cbl", "*> prefix_NebulaRewardCap_suffix\nGOBACK.", "NOISE")
        self.write("z-target.cbl", "*> nebulaRewardCap.\nGOBACK.", "TARGET")
        self.build()
        context = retrieval.retrieve_repository_context(self.database, self.source,
            "NebulaRewardCap.", max_pages=1)
        self.assertEqual(context["selected_paths"], ["z-target.cbl"])
        self.assertIn("nebulaRewardCap.", context["pages"][0]["source_text"])

    def test_independent_search_leads_each_receive_a_candidate_slot(self):
        self.write("a-first.cbl", "*> amber\nGOBACK.", "FIRST")
        self.write("b-second.cbl", "*> amber\nGOBACK.", "SECOND")
        self.write("z-target.cbl", "*> cobalt\n" +
            "\n".join(f"*> filler{index}" for index in range(650)) + "\nGOBACK.", "TARGET")
        self.build()
        context = retrieval.retrieve_repository_context(self.database, self.source, "explain",
            search_terms=["amber", "cobalt"], max_pages=2, max_chars=16000)
        self.assertEqual(set(context["selected_paths"]), {"a-first.cbl", "z-target.cbl"})

    def test_independent_leads_on_one_index_page_receive_separate_bounded_excerpts(self):
        self.write("rule.cbl", "*> AMBER-TARGET\n" +
            "\n".join("*> neutral filler text without clues" for _ in range(80)) +
            "\n*> COBALT-TARGET\nGOBACK.", "RULE")
        self.build()
        context = retrieval.retrieve_repository_context(self.database, self.source, "explain",
            search_terms=["AMBER-TARGET", "COBALT-TARGET"], max_pages=2, max_chars=1024)
        self.assertEqual(context["matched_page_count"], 1)
        self.assertEqual(len(context["pages"]), 2)
        self.assertEqual(len({page["evidence_id"] for page in context["pages"]}), 2)
        for term in ("AMBER-TARGET", "COBALT-TARGET"):
            self.assertTrue(any(term in page["source_text"] for page in context["pages"]), term)
        self.assertLessEqual(sum(len(page["source_text"]) for page in context["pages"]), 1024)
        self.assertTrue(all(page["relative_path"] == "rule.cbl" for page in context["pages"]))
        self.assertLess(context["pages"][0]["end_line"], context["pages"][1]["start_line"])

        single = retrieval.retrieve_repository_context(self.database, self.source, "explain",
            search_terms=["AMBER-TARGET"], max_pages=2, max_chars=1024)
        self.assertEqual(len(single["pages"]), 1)
        self.assertGreater(single["pages"][0]["source_characters"], 512)
        self.assertIn("AMBER-TARGET", single["pages"][0]["source_text"])

    def test_same_page_lead_crops_respect_exclusive_source_scope(self):
        self.write("selected.cbl", "*> AMBER-TARGET\n" +
            "\n".join("*> neutral filler text without clues" for _ in range(80)) +
            "\n*> COBALT-TARGET\nGOBACK.", "SELECTED")
        self.write("other.cbl", "*> AMBER-TARGET COBALT-TARGET\nGOBACK.", "OTHER")
        self.build()
        context = retrieval.retrieve_repository_context(self.database, self.source,
            "Only analyze selected.cbl", search_terms=["AMBER-TARGET", "COBALT-TARGET"],
            max_pages=2, max_chars=1024)
        self.assertTrue(context["source_identity"]["exclusive_source_scope"])
        self.assertEqual(context["selected_paths"], ["selected.cbl"])
        self.assertEqual(len(context["pages"]), 2)
        self.assertLessEqual(context["coverage"]["selected_characters"], 1024)
        for term in ("AMBER-TARGET", "COBALT-TARGET"):
            self.assertTrue(any(term in page["source_text"] for page in context["pages"]), term)

    def test_common_compound_lead_cannot_displace_an_independent_unsplit_clue(self):
        for index in range(24):
            self.write(f"a-common-{index:02}.cbl", "MOVE 1 TO WORK-VALUE.\nGOBACK.", f"COMMON-{index}")
        self.write("z-target.cbl", "*> LANTERNCAPVALUE\n" +
            "\n".join(f"*> filler{index}" for index in range(650)) + "\nGOBACK.", "TARGET")
        self.build()
        context = retrieval.retrieve_repository_context(self.database, self.source, LONG_QUESTION,
            search_terms=["WORK-VALUE", "LANTERNCAPVALUE"], max_pages=2, max_chars=16000)
        self.assertIn("z-target.cbl", context["selected_paths"])
        self.assertEqual(len(context["selected_paths"]), 2)

    def test_all_routes_respect_exclusive_file_scope_and_escape_query_syntax(self):
        self.write("selected.cbl", "*> ordinary selection\nGOBACK.", "SELECTED")
        self.write("other.cbl", "*> Nebula-Reward-Cap\nGOBACK.", "OTHER")
        self.build()
        question = "Only analyze selected.cbl " + LONG_QUESTION
        malicious = 'Nebula-Reward-Cap\" OR 1=1; DROP TABLE source_files; --'
        context = retrieval.retrieve_repository_context(self.database, self.source, question,
            search_terms=[malicious], max_pages=1)
        self.assertEqual(context["selected_paths"], ["selected.cbl"])
        self.assertTrue(context["source_identity"]["exclusive_source_scope"])
        self.assertNotIn("Nebula-Reward-Cap", context["pages"][0]["source_text"])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_files").fetchone()[0], 2)

    def test_routes_and_output_remain_bounded_with_many_search_suggestions(self):
        self.write("target.cbl", "*> LATE-ORBITAL-TERM\nGOBACK.")
        self.build()
        suggestions = [f"candidate{index}" for index in range(80)] + ["LATE-ORBITAL-TERM"]
        terms, _ = retrieval._query_terms(LONG_QUESTION, suggestions)
        groups = retrieval._retrieval_query_groups(LONG_QUESTION, suggestions, terms)
        self.assertLessEqual(sum(len(channel) for group in groups for channel in group), 10)
        self.assertLessEqual(len(terms), 128)
        self.assertIn("late-orbital-term", terms)
        context = retrieval.retrieve_repository_context(self.database, self.source, LONG_QUESTION,
            search_terms=suggestions, max_pages=1, max_chars=512)
        self.assertEqual(context["selected_paths"], ["target.cbl"])
        self.assertLessEqual(sum(len(page["source_text"]) for page in context["pages"]), 512)

    def test_exact_source_check_preserves_cancellation_exception(self):
        self.write("target.cbl", "*> ORBITAL-LIMIT.\nGOBACK.")
        self.build()
        terms, _ = retrieval._query_terms("ORBITAL-LIMIT", None)
        groups = retrieval._retrieval_query_groups("ORBITAL-LIMIT", None, terms)
        calls = 0
        cancellation = RuntimeError("cancelled during exact source check")

        def cancel():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise cancellation

        with closing(retrieval._connect(self.database)) as connection:
            with self.assertRaises(RuntimeError) as raised:
                retrieval._ranked_search_rows(connection, groups, "", [], 1, cancel)
        self.assertIs(raised.exception, cancellation)

    def test_no_match_stays_orientation_and_does_not_invent_evidence(self):
        self.write("selected.cbl", "GOBACK.")
        self.build()
        context = retrieval.retrieve_repository_context(self.database, self.source, LONG_QUESTION,
            search_terms=["UnrecordedHypothesis"], max_pages=1)
        self.assertEqual(context["matched_file_count"], 0)
        self.assertTrue(context["orientation_only"])
        self.assertTrue(all("repository_orientation" in page["selection_reasons"] for page in context["pages"]))

    def test_chat_accepts_translated_evidence_after_long_chinese_question(self):
        (self.source / "decision.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. REQUEST-GATE.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 REQUEST-AMOUNT PIC 9(6).\n"
            "01 REQUEST-STATE PIC X(8).\nPROCEDURE DIVISION.\nMAIN.\n"
            "*> NebulaRewardCap business marker\n"
            "IF REQUEST-AMOUNT > 5000\nMOVE 'REVIEW' TO REQUEST-STATE\nEND-IF.\nGOBACK.\n",
            encoding="utf-8")
        self.build()
        requests = []

        def transport(request):
            wire = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(wire)
            if len(requests) == 1:
                self.assertEqual(wire["response_mode"], "source_discovery")
                self.assertFalse(wire["source_selection"]["source_supplied"])
                answer = '{"search":["NebulaRewardCap"]}'
            else:
                self.assertTrue(wire["source_selection"]["source_supplied"])
                self.assertEqual(wire["response_mode"], "business_analysis")
                page = next(page for bundle in wire["source_context"] for page in bundle["pages"]
                            if "MOVE 'REVIEW'" in page["source_text"])
                answer = f"请求金额大于5000时进入REVIEW。[{page['evidence_id']}]"
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": answer}, "finish_reason": "stop"}]}, ensure_ascii=False))

        config = CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model", api_key="offline-only")
        output = run_business_chat(LONG_QUESTION + "。什么情况需要人工复核？", self.database, self.source,
            config, framework_reference_path="", transport=transport)
        self.assertGreaterEqual(len(requests), 2)
        self.assertTrue(requests[1]["source_selection"]["source_supplied"])
        self.assertEqual(requests[1]["response_mode"], "business_analysis")
        self.assertIn("大于5000", output["agent_result"]["answer"])
        self.assertGreater(output["agent_result"]["metrics"]["tool_calls"]["search"], 0)


if __name__ == "__main__":
    unittest.main()
