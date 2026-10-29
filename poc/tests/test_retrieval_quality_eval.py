"""Neutral gold separates ranking, sufficient evidence and actual wire loss."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from retrieval_quality_eval import (covered_units, deduplicate, evaluate_evidence, load_fixture,
                                    main, oracle_arms, production_loader)


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/retrieval-quality-v1/cases.json"


class RetrievalQualityEvalTests(unittest.TestCase):
    def setUp(self):
        self.cases = load_fixture(FIXTURE)["cases"]

    def test_first_relevant_result_and_ndcg_can_be_perfect_without_sufficient_evidence(self):
        case = self.cases[0]
        result = evaluate_evidence(case["gold"], case["candidates"], case["candidates"], k=1)
        self.assertEqual(result["mrr_at_k"], 1)
        self.assertEqual(result["ndcg_at_k"], 1)
        self.assertEqual(result["recall_at_k"], 0.5)
        self.assertEqual(result["loaded_sufficiency"]["rate"], 0)
        self.assertEqual(result["loaded_sufficiency"]["critical_missing"], ["conditional-result"])
        self.assertEqual(result["unrecalled_required_unit_ids"], ["condition"])

    def test_fixed_k_and_actual_returned_precision_use_different_denominators(self):
        case = self.cases[0]
        result = evaluate_evidence(case["gold"], case["candidates"], case["candidates"], k=5)
        self.assertEqual(result["precision_at_k"], 0.2)
        self.assertEqual(result["precision_returned"], 1)

    def test_duplicate_chunk_ids_cannot_inflate_ranking_or_recall(self):
        case = self.cases[0]
        original = case["candidates"][0]
        copies = [{**original, "evidence_id": "duplicate-" + str(index)} for index in range(5)]
        result = evaluate_evidence(case["gold"], copies, copies, k=5)
        baseline = evaluate_evidence(case["gold"], [original], [original], k=5)
        for name in ("recall_at_k", "precision_at_k", "precision_returned", "mrr_at_k", "ndcg_at_k"):
            self.assertEqual(result[name], baseline[name])
        self.assertEqual(result["returned_unique"], 1)
        self.assertEqual(result["duplicates_removed"], 4)

    def test_gold_duplicate_spans_do_not_inflate_denominator(self):
        case = self.cases[0]
        gold = copy.deepcopy(case["gold"])
        gold["units"].append({**gold["units"][1], "id": "formula-alias"})
        gold["required_items"][0]["sufficient_sets"] = [["condition", "formula-alias"]]
        result = evaluate_evidence(gold, case["candidates"], case["candidates"], k=1)
        self.assertEqual(result["recall_at_k"], 0.5)
        self.assertEqual(result["unrecalled_required_unit_ids"], ["condition"])

    def test_new_chunk_split_covers_same_original_span(self):
        case = self.cases[0]
        condition, formula = case["gold"]["units"]
        combined = {**condition, "id": "whole", "end_line": 2,
                    "source_text": condition["source_text"] + "\n" + formula["source_text"]}
        self.assertEqual(covered_units([combined], [condition, formula]), {"whole"})
        self.assertEqual(covered_units([combined], [condition]), set())

    def test_same_hash_does_not_merge_distinct_scope_or_copy_instance(self):
        page = self.cases[0]["candidates"][0]
        other_path = {**page, "relative_path": "other.cbl"}
        other_instance = {**page, "include_chain": ["second-callsite"]}
        self.assertEqual(len(deduplicate([page, other_path, other_instance])), 3)
        self.assertEqual(covered_units([page], [other_path, other_instance]), set())

    def test_copy_instance_identity_controls_scores_and_gold_deduplication(self):
        original = {**self.cases[0]["candidates"][0], "copy_instance": "first-copy"}
        other = {**original, "copy_instance": "second-copy", "id": "other-instance"}
        self.assertEqual(len(deduplicate([original, other])), 2)
        self.assertEqual(covered_units([original], [other]), set())
        gold = {"units": [{**original, "id": "first", "relevance": 3},
                          {**other, "id": "second", "relevance": 3}],
                "required_items": [{"id": "both", "critical": True,
                                    "sufficient_sets": [["first", "second"]]}]}
        result = evaluate_evidence(gold, [original], [original], k=1)
        self.assertEqual(result["recall_at_k"], 0.5)
        self.assertEqual(result["loaded_sufficiency"]["rate"], 0)

    def test_loaded_metadata_without_actual_text_cannot_claim_sufficient_source(self):
        case = self.cases[0]
        sources = case["gold"]["units"]
        for value in (None, ""):
            with self.subTest(value=value):
                loaded = [{**page, "source_text": value} for page in sources]
                result = evaluate_evidence(case["gold"], sources, loaded)
                self.assertEqual(result["retrieved_sufficiency"]["rate"], 1)
                self.assertEqual(result["loaded_sufficiency"]["rate"], 0)
                self.assertEqual(result["context_support_rate"], 0)

    def test_oracle_material_preserves_copy_instance(self):
        case = copy.deepcopy(self.cases[0])
        for unit in case["gold"]["units"]:
            unit["copy_instance"] = "required-copy"
        result = oracle_arms(case["gold"], [], lambda pages: {"pages": pages})
        for arm in ("B", "D"):
            self.assertTrue(all(page.get("copy_instance") == "required-copy"
                                for page in result["arms"][arm]["loaded_pages"]))
            self.assertEqual(result["arms"][arm]["metrics"]["loaded_sufficiency"]["rate"], 1)

    def test_changed_version_or_clipped_text_cannot_claim_original_coverage(self):
        unit = self.cases[0]["gold"]["units"][0]
        for changed in ({**unit, "source_sha256": "changed"},
                        {**unit, "source_text": "IF DIFFERENT-FLAG = 'Y'"},
                        {**unit, "span_truncated": True}):
            with self.subTest(changed=changed):
                self.assertEqual(covered_units([unit], [changed]), set())

    def test_unjudged_result_is_pending_not_automatically_irrelevant(self):
        case = self.cases[2]
        result = evaluate_evidence(case["gold"], case["candidates"], case["candidates"], k=3)
        self.assertEqual(result["unjudged_results"], 1)
        self.assertEqual(result["judgment_status"], "needs_judgment")
        self.assertIsNone(result["precision_at_k"])
        self.assertEqual(result["precision_at_k_bounds"], [2 / 3, 1])
        self.assertIsNone(result["mrr_at_k"])
        self.assertEqual(result["mrr_at_k_bounds"], [0.5, 1])
        self.assertIsNone(result["ndcg_at_k"])
        self.assertEqual(result["recall_at_k"], 1)

    def test_empty_relevant_gold_has_no_arbitrary_perfect_scores(self):
        case = self.cases[0]
        irrelevant = {**case["candidates"][0], "id": "irrelevant", "relevance": 0}
        result = evaluate_evidence({"units": [irrelevant], "required_items": []}, [irrelevant], [], k=2)
        for key in ("recall_at_k", "mrr_at_k", "ndcg_at_k", "context_support_rate"):
            self.assertIsNone(result[key])
        self.assertIsNone(result["loaded_sufficiency"]["rate"])
        self.assertTrue(result["empty_relevant_gold"])
        self.assertEqual(result["precision_returned"], 0)

    def test_alternative_sufficient_sets_do_not_require_every_alternative(self):
        case = self.cases[0]
        gold = copy.deepcopy(case["gold"])
        gold["required_items"][0]["sufficient_sets"] = [["condition"], ["formula"]]
        result = evaluate_evidence(gold, case["candidates"], case["candidates"], k=1)
        self.assertEqual(result["loaded_sufficiency"]["rate"], 1)
        self.assertEqual(result["unrecalled_required_unit_ids"], [])

    def test_empty_sufficient_sets_cannot_vacuously_pass(self):
        gold = copy.deepcopy(self.cases[0]["gold"])
        for choices in ([], [[]], [["unknown-unit"]]):
            gold["required_items"][0]["sufficient_sets"] = choices
            with self.subTest(choices=choices), self.assertRaises(ValueError):
                evaluate_evidence(gold, [], [])

    def test_real_request_fitter_reveals_loss_after_full_recall(self):
        case = self.cases[1]
        loader = production_loader(case["question"], max_request_bytes=32768)
        loaded = loader(case["candidates"])
        result = evaluate_evidence(case["gold"], case["candidates"], loaded["pages"], k=3)
        self.assertEqual(result["recall_at_k"], 1)
        self.assertEqual(result["retrieved_sufficiency"]["rate"], 1)
        self.assertEqual(result["loaded_sufficiency"]["rate"], 0)
        self.assertEqual(result["context_support_rate"], 0)
        self.assertEqual(result["loading_lost_unit_ids"], ["condition", "formula"])
        self.assertTrue(loaded["trim_events"])
        self.assertLessEqual(loaded["request_bytes"], 32768)

    def test_oracle_b_adds_missing_source_but_c_never_does(self):
        case = self.cases[0]
        result = oracle_arms(case["gold"], case["candidates"], production_loader(case["question"]))
        arms = result["arms"]
        self.assertEqual(arms["A"]["metrics"]["loaded_sufficiency"]["rate"], 0)
        self.assertEqual(arms["B"]["metrics"]["loaded_sufficiency"]["rate"], 1)
        self.assertEqual(arms["C"]["metrics"]["loaded_sufficiency"]["rate"], 0)
        self.assertEqual({page["evidence_id"] for page in arms["C"]["candidate_pages"]},
                         {page["evidence_id"] for page in case["candidates"]})
        self.assertFalse(result["generation_evaluated"])

    def test_oracle_c_can_isolate_loading_loss_without_changing_recall(self):
        case = self.cases[1]
        result = oracle_arms(case["gold"], case["candidates"], production_loader(case["question"]))
        arms = result["arms"]
        self.assertEqual(arms["A"]["metrics"]["loaded_sufficiency"]["rate"], 0)
        self.assertEqual(arms["B"]["metrics"]["loaded_sufficiency"]["rate"], 0)
        self.assertEqual(arms["C"]["metrics"]["loaded_sufficiency"]["rate"], 1)
        self.assertEqual(arms["D"]["metrics"]["loaded_sufficiency"]["rate"], 1)
        self.assertEqual(arms["A"]["metrics"]["recall_at_k"], arms["C"]["metrics"]["recall_at_k"])
        self.assertFalse(result["sufficient_evidence_exceeds_budget"])

    def test_oracle_never_sends_gold_answer_or_label_prose(self):
        case = copy.deepcopy(self.cases[0])
        marker = "FORBIDDEN-GOLD-ANSWER"
        for unit in case["gold"]["units"]:
            unit["expected_answer"] = marker
            unit["label_basis"] = marker
        seen = []

        def load(pages):
            seen.append(pages)
            return {"pages": pages}

        result = oracle_arms(case["gold"], case["candidates"], load)
        self.assertNotIn(marker, json.dumps(seen))
        self.assertFalse(result["generation_evaluated"])

    def test_sufficient_raw_evidence_exceeding_budget_is_explicit(self):
        case = self.cases[0]
        result = oracle_arms(case["gold"], case["candidates"], lambda pages: {"pages": []})
        self.assertTrue(result["sufficient_evidence_exceeds_budget"])
        self.assertEqual(result["budget_conflict_scope"],
                         "selected_smallest_raw_byte_gold_set_not_all_alternatives")

    def test_fixed_token_control_requires_counter_and_applies_to_every_arm(self):
        case = self.cases[0]
        with self.assertRaisesRegex(ValueError, "TOKEN_COUNTER_REQUIRED"):
            production_loader(case["question"], max_input_tokens=1000)
        observed = []

        def fixture_counter(messages):
            # Deliberately identified fixture tokenizer, not a model estimate.
            observed.append(messages)
            return sum(len(message["content"].split()) for message in messages)

        loader = production_loader(case["question"], max_input_tokens=10000,
                                   token_counter=fixture_counter, tokenizer_id="fixture-whitespace-v1")
        result = oracle_arms(case["gold"], case["candidates"], loader)
        for arm in result["arms"].values():
            self.assertEqual(arm["max_input_tokens"], 10000)
            self.assertLessEqual(arm["input_tokens"], 10000)
            self.assertEqual(arm["tokenizer_id"], "fixture-whitespace-v1")
        self.assertEqual(len(observed), 4)
        self.assertTrue(all(messages[0]["role"] == "system" for messages in observed))

    def test_token_control_reports_evidence_conflict_and_does_not_hide_overhead(self):
        case = self.cases[0]

        def fixture_counter(messages):
            wire = json.loads(messages[-1]["content"])
            return 10 + 10 * sum(len(bundle["pages"]) for bundle in wire["source_context"])

        loader = production_loader(case["question"], max_input_tokens=20,
                                   token_counter=fixture_counter, tokenizer_id="fixture-page-tokens-v1")
        result = oracle_arms(case["gold"], case["candidates"], loader)
        self.assertTrue(result["sufficient_evidence_exceeds_budget"])
        self.assertLessEqual(result["arms"]["D"]["input_tokens"], 20)
        self.assertTrue(any(event["reason"] == "offline_input_token_budget"
                            for event in result["arms"]["D"]["trim_events"]))
        impossible = production_loader(case["question"], max_input_tokens=5,
                                        token_counter=fixture_counter, tokenizer_id="fixture-page-tokens-v1")
        with self.assertRaisesRegex(ValueError, "TOKEN_BUDGET_BELOW_INSTRUCTIONS"):
            impossible(case["candidates"])

    def test_cli_runs_without_network_or_model_and_labels_measurement_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "report.json"
            with mock.patch("socket.create_connection", side_effect=AssertionError("No network")), \
                 mock.patch("company_api.OpenAICompatibleChatClient.complete", side_effect=AssertionError("No model")):
                self.assertEqual(main(["--output", str(output)]), 0)
            report = json.loads(output.read_text())
        self.assertEqual(len(report["cases"]), 3)
        self.assertEqual(report["model_requests"], 0)
        self.assertEqual(report["budget_unit"], "serialized_request_bytes")
        self.assertFalse(report["token_budget_verified"])
        self.assertFalse(report["production_retrieval_executed"])
        self.assertFalse(report["gold_review"]["independent_human_review"])


if __name__ == "__main__":
    unittest.main()
