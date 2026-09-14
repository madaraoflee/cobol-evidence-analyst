from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_loop import BoundedAgentLoop  # noqa: E402
from test_agent_loop import (  # noqa: E402
    FakeClient,
    FakeTools,
    json_action,
    native_action,
)


DEFAULT_SOURCE = (
    "       COMPUTE OUT-AMOUNT =\n"
    "           IN-AMOUNT\n"
    "       END-COMPUTE."
)


class CompleteComputeTools(FakeTools):
    def __init__(self, source: str = DEFAULT_SOURCE) -> None:
        super().__init__()
        self.source = source

    def search_code(self, query: str, *, limit: int = 10) -> dict[str, object]:
        result = super().search_code(query, limit=limit)
        for ref in result["evidence_refs"]:
            ref["end_line"] = 10 + len(self.source.splitlines()) - 1
        for hit in result["hits"]:
            hit["evidence_ref"]["end_line"] = (
                10 + len(self.source.splitlines()) - 1
            )
        return result

    def read_evidence(
        self, evidence_ids: list[str], *, max_chars: int = 16_000
    ) -> dict[str, object]:
        result = super().read_evidence(evidence_ids, max_chars=max_chars)
        for span in result["spans"]:
            span["source_text"] = self.source
            span["end_line"] = 10 + len(self.source.splitlines()) - 1
        result["returned_characters"] = len(self.source) * len(evidence_ids)
        return result


def structured_claim(**assertion_changes: object) -> dict[str, object]:
    assertion = {
        "predicate": "compute_statement",
        "target": "OUT-AMOUNT",
        "expression": ["IN-AMOUNT"],
        "rounded": False,
    }
    assertion.update(assertion_changes)
    return {
        "kind": "code_fact",
        "assertion": assertion,
        "evidence_ids": ["ev_OUT-AMOUNT"],
    }


def legacy_claim() -> dict[str, object]:
    return {
        "kind": "code_fact",
        "claim": "OUT-AMOUNT is computed from IN-AMOUNT.",
        "code_anchors": ["OUT-AMOUNT", "IN-AMOUNT"],
        "evidence_ids": ["ev_OUT-AMOUNT"],
        "support_status": "supported",
    }


class AgentClaimSupportTests(unittest.TestCase):
    def run_claims(
        self,
        claims: list[dict[str, object]],
        *,
        source: str = DEFAULT_SOURCE,
        final_changes: dict[str, object] | None = None,
        native_final: bool = False,
        extra_evidence: bool = False,
    ) -> dict[str, object]:
        evidence_ids = ["ev_OUT-AMOUNT"]
        responses = [json_action("search_code", {"query": "OUT-AMOUNT"})]
        if extra_evidence:
            responses.append(json_action("search_code", {"query": "OTHER-AMOUNT"}))
            evidence_ids.append("ev_OTHER-AMOUNT")
        responses.append(json_action("read_evidence", {"evidence_ids": evidence_ids}))
        final_arguments = {
            "claims": copy.deepcopy(claims),
            "evidence_ids": evidence_ids,
            "boundaries": [],
        }
        final_arguments.update(final_changes or {})
        responses.append(
            (native_action if native_final else json_action)(
                "final_answer", final_arguments
            )
        )
        return BoundedAgentLoop(
            FakeClient(responses), CompleteComputeTools(source)
        ).run("How is OUT-AMOUNT computed?")

    def test_supported_statement_is_rendered_locally_with_audit(self) -> None:
        result = self.run_claims(
            [structured_claim()],
            final_changes={"answer": "MODEL-ANSWER-CANARY", "status": "SUPPORTED"},
        )

        self.assertEqual(result["status"], "SUPPORTED_WITH_BOUNDARIES")
        self.assertEqual(result["stop_reason"], "completed")
        self.assertTrue(result["claims_semantically_verified"])
        claim = result["claims"][0]
        self.assertEqual(claim["support_status"], "supported")
        self.assertIn("OUT-AMOUNT", claim["claim"])
        self.assertIn("IN-AMOUNT", claim["claim"])
        self.assertRegex(claim["claim"], r"[\u4e00-\u9fff]")
        self.assertIn(claim["claim"], result["answer"])
        self.assertNotIn("MODEL-ANSWER-CANARY", json.dumps(result))
        verification = result["verification"]
        self.assertTrue(verification["semantic_claim_support_checked"])
        self.assertTrue(verification["checker_version"])
        self.assertEqual(len(verification["claim_checks"]), 1)
        check = verification["claim_checks"][0]
        self.assertEqual(check["claim_index"], 1)
        self.assertEqual(check["support_status"], "supported")
        self.assertEqual(check["checker_version"], verification["checker_version"])
        self.assertTrue(check["reason_code"])
        self.assertTrue(any(
            isinstance(boundary, dict)
            and boundary.get("reason") == "statement_scope_only"
            for boundary in result["boundaries"]
        ))

    def test_native_final_action_uses_same_independent_check(self) -> None:
        result = self.run_claims([structured_claim()], native_final=True)
        self.assertEqual(result["status"], "SUPPORTED_WITH_BOUNDARIES")
        self.assertTrue(result["claims_semantically_verified"])

    def test_legacy_supported_label_cannot_upgrade_prose(self) -> None:
        result = self.run_claims(
            [legacy_claim()], final_changes={"status": "SUPPORTED"}
        )
        self.assertEqual(result["status"], "CITATION_VERIFIED_ONLY")
        self.assertEqual(result["claims"][0]["support_status"], "citation_verified_only")
        self.assertFalse(result["claims_semantically_verified"])
        self.assertFalse(result["verification"]["semantic_claim_support_checked"])

    def test_mixed_claims_keep_per_claim_support_but_not_global_support(self) -> None:
        result = self.run_claims([structured_claim(), legacy_claim()])
        self.assertEqual(result["status"], "CITATION_VERIFIED_ONLY")
        self.assertEqual(
            [claim["support_status"] for claim in result["claims"]],
            ["supported", "citation_verified_only"],
        )
        self.assertFalse(result["claims_semantically_verified"])
        self.assertTrue(result["verification"]["semantic_claim_support_checked"])
        self.assertIn(result["claims"][0]["claim"], result["answer"])

    def test_model_cannot_supply_support_or_prose_with_assertion(self) -> None:
        additions = (
            {"support_status": "supported"},
            {"claim": "MODEL-PROSE-CANARY"},
            {"text": "MODEL-PROSE-CANARY"},
            {"code_anchors": ["OUT-AMOUNT", "IN-AMOUNT"]},
        )
        for addition in additions:
            with self.subTest(addition=addition):
                claim = structured_claim()
                claim.update(addition)
                result = self.run_claims([claim])
                self.assertEqual(result["status"], "ABSTAINED")
                self.assertEqual(result["stop_reason"], "invalid_final_answer")
                self.assertEqual(result["claims"], [])
                self.assertNotIn("MODEL-PROSE-CANARY", json.dumps(result))

    def test_structured_assertion_cannot_carry_extra_fields_or_free_text(self) -> None:
        changes = (
            {"claim": "ASSERTION-PROSE-CANARY"},
            {"expression": ["IN-AMOUNT because ASSERTION-PROSE-CANARY"]},
            {"rounded": "false"},
            {"target": "OUT-AMOUNT; ASSERTION-PROSE-CANARY"},
        )
        for change in changes:
            with self.subTest(change=change):
                result = self.run_claims([structured_claim(**change)])
                self.assertEqual(result["status"], "ABSTAINED")
                self.assertEqual(result["claims"], [])
                self.assertNotIn("ASSERTION-PROSE-CANARY", json.dumps(result))

    def test_wrong_target_operator_constant_or_rounding_abstains(self) -> None:
        source = "       COMPUTE OUT-AMOUNT = IN-AMOUNT * 1.05."
        changes = (
            {"target": "FORGED-AMOUNT", "expression": ["IN-AMOUNT", "*", "1.05"]},
            {"expression": ["IN-AMOUNT", "+", "1.05"]},
            {"expression": ["IN-AMOUNT", "*", "1.06"]},
            {"expression": ["IN-AMOUNT", "*", "1.05"], "rounded": True},
        )
        for change in changes:
            with self.subTest(change=change):
                result = self.run_claims([structured_claim(**change)], source=source)
                self.assertEqual(result["status"], "ABSTAINED")
                self.assertEqual(result["stop_reason"], "unsupported_claim")
                self.assertEqual(result["claims"], [])
                self.assertNotIn("FORGED-AMOUNT", json.dumps(result))
                self.assertTrue(result["verification"]["semantic_claim_support_checked"])
                self.assertNotEqual(
                    result["verification"]["claim_checks"][0]["support_status"],
                    "supported",
                )

    def test_unterminated_source_cannot_certify_an_expression_prefix(self) -> None:
        result = self.run_claims(
            [structured_claim()], source="       COMPUTE OUT-AMOUNT = IN-AMOUNT"
        )
        self.assertEqual(result["status"], "ABSTAINED")
        self.assertEqual(result["stop_reason"], "unsupported_claim")
        self.assertEqual(result["claims"], [])

    def test_assertion_requires_exactly_one_verified_evidence_span(self) -> None:
        claim = structured_claim()
        claim["evidence_ids"] = ["ev_OUT-AMOUNT", "ev_OTHER-AMOUNT"]
        result = self.run_claims([claim], extra_evidence=True)
        self.assertEqual(result["status"], "ABSTAINED")
        self.assertEqual(result["stop_reason"], "invalid_final_answer")
        self.assertEqual(result["claims"], [])

    def test_assertion_cannot_use_an_unread_evidence_id(self) -> None:
        claim = structured_claim()
        claim["evidence_ids"] = ["ev_UNREAD"]
        result = self.run_claims(
            [claim], final_changes={"evidence_ids": ["ev_UNREAD"]}
        )
        self.assertEqual(result["status"], "ABSTAINED")
        self.assertEqual(result["stop_reason"], "invalid_evidence_reference")
        self.assertFalse(result["verification"]["semantic_claim_support_checked"])

    def test_checker_exception_stops_safely_without_error_or_claim_leak(self) -> None:
        with mock.patch(
            "agent_loop.check_claim_support",
            side_effect=RuntimeError("CHECKER-ERROR-CANARY"),
        ):
            result = self.run_claims([structured_claim()])

        self.assertEqual(result["status"], "ABSTAINED")
        self.assertEqual(result["stop_reason"], "claim_checker_error")
        self.assertEqual(result["claims"], [])
        self.assertNotIn("CHECKER-ERROR-CANARY", json.dumps(result))

    def test_one_unsupported_claim_preserves_the_independent_valid_statement(self) -> None:
        result = self.run_claims(
            [structured_claim(), structured_claim(target="FORGED-AMOUNT")]
        )
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["stop_reason"], "completed")
        self.assertEqual(len(result["claims"]), 1)
        self.assertEqual(result["claims"][0]["support_status"], "supported")
        self.assertEqual(result["verification"]["rejected_claims"], [
            {"claim_index": 2, "reason_code": "UNSUPPORTED_CLAIM"}
        ])
        self.assertEqual(result["question_coverage"]["status"], "not_assessed")
        self.assertNotIn("FORGED-AMOUNT", json.dumps(result))

    def test_bad_free_text_claim_does_not_erase_valid_formula(self) -> None:
        bad = legacy_claim()
        bad.update(claim="FORGED-AMOUNT determines the result.", code_anchors=["FORGED-AMOUNT"])
        result = self.run_claims([structured_claim(), bad])
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(len(result["claims"]), 1)
        self.assertEqual(result["verification"]["rejected_claims"][0]["reason_code"],
                         "UNSUPPORTED_CLAIM_CONTENT")
        self.assertNotIn("FORGED-AMOUNT", json.dumps(result))

    def test_unavailable_internal_behavior_does_not_erase_visible_observation(self) -> None:
        unknown = {
            "kind": "business_inference", "claim": "The external object may persist the amount.",
            "code_anchors": [], "evidence_ids": ["ev_OUT-AMOUNT"], "support_status": "unsupported",
        }
        result = self.run_claims([structured_claim(), unknown])
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(len(result["claims"]), 1)
        self.assertNotIn("persist the amount", json.dumps(result))

    def test_explicit_abstention_never_claims_semantic_success(self) -> None:
        result = BoundedAgentLoop(
            FakeClient([
                json_action("abstain", {
                    "claims": [],
                    "evidence_ids": [],
                    "boundaries": ["Runtime values have not been provided."],
                    "status": "SUPPORTED",
                })
            ]),
            CompleteComputeTools(),
        ).run("What was the runtime amount?")
        self.assertEqual(result["status"], "ABSTAINED")
        self.assertEqual(result["stop_reason"], "model_abstained")
        self.assertFalse(result["claims_semantically_verified"])
        self.assertFalse(result["verification"]["semantic_claim_support_checked"])


if __name__ == "__main__":
    unittest.main()
