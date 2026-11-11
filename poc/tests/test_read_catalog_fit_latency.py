"""Read-catalog fitting preserves source authority and the sequential trim contract."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import business_chat
from agent_policy import AgentPolicy
from company_api import CompanyAPIConfig
from evidence_context import EvidenceContext
import test_prior_source_memory as memory_fixtures


class ReadCatalogFitLatencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = memory_fixtures.PriorSourceMemoryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory = self.fixture.memory
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model",
                                       api_key="offline-only")
        self.policy = AgentPolicy(max_request_bytes=32768)

    def payload(self, *, count=32, prior_omissions=0):
        archive = [self.fixture.make_page(relative_path=f"rules/规则-{number:02}.cbl")
                   for number in range(count)]
        self.assertEqual(self.memory.remember_supplied_sources(
            payload={"source_context": [{"pages": archive}]},
            round_record=self.fixture.round_record(archive)), count)
        prompt = self.memory.prompt()
        catalog = prompt["read_catalog"]
        self.assertEqual(len(catalog["entries"]), count)
        catalog["omitted_count"] = prior_omissions
        catalog["total_count"] += prior_omissions
        for number, item in enumerate(catalog["entries"]):
            # Exercise the catalog's nested JSON escaping and UTF-8 byte count.
            item["display_label"] = f'记录 "{number}"\\段落\n细节\t' * (number % 5 + 1)
        return {"question": self.fixture.question, "repository": {}, "business_map": {},
            "framework_references": [], "prior_source_memory": prompt, "task": "保持原文。" * 2300,
            "business_analysis_brief_omitted": True,
            "source_context": [{"pages": [self.fixture.page],
                "call_chain": {"links": [], "omitted_links": 0}, "outline": [], "notices": []}]}

    @staticmethod
    def investigation(pages):
        ids = [page["evidence_id"] for page in pages]
        return {"can_answer": bool(ids), "required_items": [{"kind": "formula",
            "status": "SATISFIED" if ids else "OPEN", "evidence_ids": ids}]}

    def encode(self, payload):
        messages = [{"role": "system", "content": business_chat._system_for_payload(payload)},
            {"role": "user", "content": json.dumps(business_chat._prompt_payload(payload),
                ensure_ascii=False, separators=(",", ":"))}]
        wire = json.dumps({"model": self.config.chat_model, "messages": messages,
            "max_tokens": self.config.max_output_tokens, **self.config.generation_parameters()},
            ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return messages, len(wire)

    def sequential_states(self, payload):
        trial = deepcopy(payload)
        EvidenceContext.reconcile_payload(trial)
        trial["question_investigation"] = self.investigation(self.memory.effective_pages(trial))
        trial["context_reduced"] = True
        catalog = trial["prior_source_memory"]["read_catalog"]
        removed = []
        for _ in range(len(catalog["entries"])):
            removed.append(catalog["entries"].pop(len(catalog["entries"]) // 2)["evidence_id"])
            catalog["omitted_count"] += 1
            messages, size = self.encode(trial)
            yield messages, size, list(removed), deepcopy(catalog)

    def fit(self, payload, policy, *, cancel=None):
        trims = []
        builder = mock.Mock(side_effect=self.investigation)
        with mock.patch.object(business_chat, "_prompt_payload", wraps=business_chat._prompt_payload) as projection:
            messages, size = business_chat._fit_request(self.config, payload, [], policy,
                investigation_builder=builder, source_memory=self.memory,
                trim_events=trims, check_cancel=cancel)
        return messages, size, trims, builder, projection

    def test_partial_catalog_fit_preserves_wire_order_and_exact_byte_boundary(self):
        original = self.payload(prior_omissions=98)
        states = list(self.sequential_states(original))
        for removed_count in (1, 2, 17, 31):
            with self.subTest(removed_count=removed_count):
                messages, limit, removed, catalog = states[removed_count - 1]
                self.assertGreaterEqual(limit, self.policy.max_request_bytes)
                if removed_count > 1:
                    self.assertGreater(states[removed_count - 2][1], limit)
                payload = deepcopy(original)
                saved = self.memory.checkpoint()
                actual, size, trims, builder, projection = self.fit(
                    payload, replace(self.policy, max_request_bytes=limit))
                self.assertEqual((actual, size), (messages, limit))
                self.assertEqual(payload["prior_source_memory"]["read_catalog"], catalog)
                self.assertEqual([row["item_id"] for row in trims], removed)
                self.assertEqual([row["reason"] for row in trims], ["prior_read_catalog_request_bytes"] * removed_count)
                self.assertEqual(builder.call_count, 1)
                self.assertLessEqual(projection.call_count, 3)
                self.assertEqual(self.memory.checkpoint(), saved)
                self.assertEqual(self.memory.visible_ids(payload), set())

    def test_single_entry_needs_no_extra_full_projection(self):
        payload = self.payload(count=1)
        messages, limit, removed, catalog = next(self.sequential_states(payload))
        actual, size, trims, builder, projection = self.fit(payload, replace(self.policy, max_request_bytes=limit))
        self.assertEqual((actual, size), (messages, limit))
        self.assertEqual(payload["prior_source_memory"]["read_catalog"], catalog)
        self.assertEqual([row["item_id"] for row in trims], removed)
        self.assertEqual(builder.call_count, 1)
        self.assertEqual(projection.call_count, 2)

    def test_exhausting_catalog_rechecks_after_removing_actual_source(self):
        payload = self.payload(count=24)
        payload["task"] = "保持原文。"
        filler = "DISPLAY 'source content'.\n" * 1500
        payload["source_context"][0]["pages"].append(self.fixture.make_page(relative_path="detail.cbl",
            source_text=filler, end_line=len(filler.splitlines())))
        before = self.memory.checkpoint()
        all_removed = list(self.sequential_states(payload))[-1][2]
        _, size, trims, builder, _ = self.fit(payload, self.policy)
        self.assertLessEqual(size, self.policy.max_request_bytes)
        self.assertEqual([row["item_id"] for row in trims if row["reason"] == "prior_read_catalog_request_bytes"],
                         all_removed)
        self.assertEqual([len(call.args[0]) for call in builder.call_args_list], [2, 1])
        self.assertEqual(payload["source_context"][0]["pages"], [self.fixture.page])
        self.assertEqual(self.memory.checkpoint(), before)

    def test_cancellation_keeps_committed_trim_receipts_and_original_archive(self):
        payload = self.payload()
        catalog = payload["prior_source_memory"]["read_catalog"]
        initial, snapshot = len(catalog["entries"]), self.memory.checkpoint()
        _, limit, expected, retained = list(self.sequential_states(payload))[3]
        trims = []

        class Cancelled(Exception):
            pass

        def cancel_after_four_removals():
            if initial - len(catalog["entries"]) == 4:
                raise Cancelled

        with self.assertRaises(Cancelled):
            business_chat._fit_request(self.config, payload, [], replace(self.policy, max_request_bytes=limit - 1),
                investigation_builder=self.investigation, source_memory=self.memory,
                trim_events=trims, check_cancel=cancel_after_four_removals)
        self.assertEqual(catalog, retained)
        self.assertEqual([row["item_id"] for row in trims], expected)
        self.assertEqual(payload["source_context"][0]["pages"], [self.fixture.page])
        self.assertEqual(self.memory.checkpoint(), snapshot)


if __name__ == "__main__":
    unittest.main()
