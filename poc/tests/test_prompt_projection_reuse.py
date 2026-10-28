"""Frozen wire equivalence for projection-local reuse of immutable values."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import random
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "poc"))
sys.path.insert(0, str(ROOT))

from prompt_projection import project_prompt_payload
from tools.desktop_acceptance_runtime import decoded_navigation


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fixture(seed):
    rng = random.Random(seed)
    source = "IF INPUT-AMOUNT > 0\nMOVE INPUT-AMOUNT TO OUTPUT-AMOUNT\nEND-IF."
    digest = hashlib.sha256(source.encode()).hexdigest()
    page = {"evidence_id": "ev:source", "relative_path": "calculation.cbl", "source_text": source,
            "source_sha256": digest, "start_line": 12, "end_line": 14,
            "include_chain": [{"relative_path": "driver.cbl", "line": 8}],
            "replacement_trace": [{"from": "INPUT", "to": "OUTPUT"}], "span_truncated": False}
    provenance = {"relative_path": "record.cpy", "source_sha256": "b" * 64,
                  "include_chain": [{"relative_path": "driver.cbl", "line": 8}],
                  "replacement_trace": [{"from": "ORIGINAL-FIELD", "to": "RESULT-FIELD"}],
                  "execution_verified": False, "scope": "captured_source_occurrence"}
    options = (None, False, 0, 0.0, -0.0, True, 1, "0", "未知")
    rows = []
    for index in range(24):
        item = {"occurrence_id": f"occurrence:{index}", "kind": "source_effect",
                "start_line": index + 20, "end_line": index + 20, "evidence_ids": ["ev:source"],
                "argument": "INPUT-AMOUNT", "parameters": [{"name": "INPUT-AMOUNT", "mode": "REFERENCE"}],
                "provenance": deepcopy(provenance), "effect_known": False,
                "guards": [{"condition": "READY-FLAG = " + str(index % 3), "outcome": bool(index % 2)}]}
        if index % 7 != 0:
            item["optional"] = options[(index + seed) % len(options)]
        if index % 5 == 0:
            item["provenance"]["include_chain"][0]["line"] = rng.randrange(30, 40)
        if index % 8 == 0:
            item["protected"] = {"history": [{"role": "user", "content": f"source context {index}"}]}
        rows.append(item)
    # Distinct objects, aliases, and deeply nested repeated records all occur in
    # navigation; authority records must stay in place even when repeated.
    return {"question": "业务条件如何影响输出金额？",
            "source_context": [{"pages": [page], "working_set": {"physical_complete": True},
                                "outline": deepcopy(rows[:6])}],
            "history": [{"role": "user", "content": "保留前一个问题的上下文"}],
            "framework_references": [{"reference_id": "fw:record", "text": "The return value is unknown."}],
            "business_map": {"observations": rows, "nested": {"records": deepcopy(rows)}},
            "evidence_groups": [{"group_id": f"group:{index}", "observations": deepcopy(rows),
                                 "required_evidence_ids": ["ev:source"]} for index in range(3)],
            "question_investigation": {"observations": deepcopy(rows),
                "opaque": [{"_values": [None, False, 0, -0.0], "source_text": source} for _ in range(2)]},
            "business_analysis_brief": {"task": "Retain all custom instructions.",
                "nested": {"observations": rows}, "protected": {"pages": [deepcopy(page)]}}}


# Captured from b636fbdd96ac4483b342c2a9dbce2e00a3e17caf before the
# reuse optimization. Hash the complete canonical JSON, including reference
# numbering, row order, all extension fields and exact scalar types.
RELEASED_WIRE_SHA256 = (
    '3a946dee9b6e9a903929e36472992b212479133f2101da3ad32506e29aa64beb',
    '9d451c7a32f20cb3a18d642ce21d50129beed59297f2844672626b6d095ccdea',
    '6ede06e95878a90c114a7764d7ab6d8de034615d1d28d108da88818be9c27488',
    'baa79e4a3d872df9ab63dc7404533914494c2e0b1e20dad2f02be785ed7ad92e',
    '85056018c52525a0e16aabe933afc81fc96d18e9c25b9b6eecfa4223c6f6573c',
    'e1d3372779a96c816bd5f2cfe077b1574269c41a55dcc6c9af8f7440d4253e5d',
    '494dcf28cfca7161b4a4b9512907242c2eb849bddeb1853ff198f8270fb39bdf',
    '4f45ea8ac1768e865b2c91cd2a4f901eda97f0f1961ddea829e01e3c392c84d7',
    '331c03a1e668e8333ca25a03acad2d7a665e60ffa427b8f9ae23629ecb430eb0',
    '93350ba8d644a5080535c93622415763d7d47f4d83e08a01e880190d0211f8e8',
    '50d194e82891f0171d10dd876b45830357023399f4594b6fb837fc0a0ea2db78',
    '114c41a04a346274867b8e6aa623c8b598790fed94c7a56807878eb07803cf12',
)


class PromptProjectionReuseTests(unittest.TestCase):
    def test_exact_released_wire_for_randomized_nested_records(self):
        self.assertEqual(len(RELEASED_WIRE_SHA256), 12)
        for seed, digest in enumerate(RELEASED_WIRE_SHA256):
            with self.subTest(seed=seed):
                payload = fixture(seed)
                original = encoded(payload)
                projected = project_prompt_payload(payload)
                self.assertEqual(hashlib.sha256(encoded(projected).encode()).hexdigest(), digest)
                self.assertEqual(encoded(decoded_navigation(projected)), original)
                self.assertEqual(encoded(payload), original)

    def test_unknown_false_zero_and_negative_zero_keep_their_json_identity(self):
        payload = fixture(4)
        projected = project_prompt_payload(payload)
        restored = decoded_navigation(projected)
        self.assertEqual(encoded(restored), encoded(payload))
        variants = [row.get("optional", "missing") for row in restored["business_map"]["observations"]]
        self.assertTrue(any(value is None for value in variants))
        self.assertTrue(any(value is False for value in variants))
        self.assertIn("-0.0", [encoded(value) for value in variants])
        self.assertIn("0.0", [encoded(value) for value in variants])
        self.assertIn("0", [encoded(value) for value in variants])

    def test_each_projection_sees_edits_and_does_not_retain_prior_values(self):
        payload = fixture(2)
        first = project_prompt_payload(payload)
        frozen_first = encoded(first)
        payload["business_map"]["observations"][0]["provenance"]["source_sha256"] = "c" * 64
        payload["business_map"]["observations"][1]["optional"] = None
        payload["source_context"][0]["pages"][0]["replacement_trace"][0]["to"] = "NEW-OUTPUT"
        second = project_prompt_payload(payload)
        self.assertEqual(encoded(decoded_navigation(second)), encoded(payload))
        self.assertNotEqual(encoded(second), frozen_first)
        self.assertEqual(encoded(first), frozen_first)

    def test_reserved_names_still_leave_the_original_payload_untouched(self):
        for key in ("navigation_fact_ref", "shared_navigation_facts", "prompt_projection", "rows_shared_fields"):
            with self.subTest(key=key):
                payload = fixture(0)
                payload["question_investigation"]["nested"] = {key: {"known": False}}
                before = encoded(payload)
                self.assertIs(project_prompt_payload(payload), payload)
                self.assertEqual(encoded(payload), before)

    def test_protected_source_and_copy_provenance_are_independent_of_projection(self):
        payload = fixture(6)
        before = encoded(payload)
        projected = project_prompt_payload(payload)
        self.assertEqual(encoded(decoded_navigation(projected)), before)
        self.assertEqual(projected["source_context"][0]["pages"], payload["source_context"][0]["pages"])
        projected["source_context"][0]["pages"][0]["source_text"] = "different source"
        self.assertEqual(encoded(payload), before)


if __name__ == "__main__":
    unittest.main()
