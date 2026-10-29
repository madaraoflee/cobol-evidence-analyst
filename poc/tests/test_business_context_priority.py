"""Business source survives unrelated framework background under wire pressure."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_analysis import _framework_for_prompt
import business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig
from repository_discovery import ensure_repository_search, read_repository_context


class BusinessContextPriorityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "source.sqlite"
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model", api_key="offline-only")
        self.pages = []
        self.formulas = []
        for name, amount in (("credit", "CREDIT-AMOUNT"), ("refund", "RETURN-AMOUNT")):
            formula = f"SUBTRACT {amount} FROM DUE-AMOUNT"
            self.formulas.append(formula)
            text = (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name.upper()}-RULE.\nDATA DIVISION.\n"
                    f"WORKING-STORAGE SECTION.\n01 {amount} PIC 9(8).\n01 DUE-AMOUNT PIC 9(8).\n"
                    "PROCEDURE DIVISION.\nMAIN.\n"
                    "CALL 'RECORD-ACCESS' USING DUE-AMOUNT.\n"
                    f"IF {amount} > ZERO\n{formula}\nEND-IF.\n"
                    + "*> " + "x" * 7400 + "\nGOBACK.\n")
            (self.source / f"{name}.cbl").write_text(text, encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        for name in ("credit", "refund"):
            result = read_repository_context(self.database, self.source, relative_path=f"{name}.cbl",
                                             start_line=1, end_line=14, max_chars=12000)
            self.pages.extend(result["pages"])
        self.assertEqual(len(self.pages), 2)
        for page in self.pages:
            self.assertEqual(page["source_sha256"], hashlib.sha256(
                (self.source / page["relative_path"]).read_bytes()).hexdigest())

    def reference(self, number, *, reason="source_marker", small=False):
        return {"reference_id": f"fw:record-{number}", "heading": "Record access convention",
                "text": "Read the requested row and return status." if small else "记录访问约定与通用返回状态说明。" * 100,
                "selection_reason": reason, "text_truncated": False,
                "start_line": 1, "end_line": 1}

    def payload(self, references=None):
        return {"question": "Explain the credit and refund deduction business rules.",
                "repository": {}, "business_map": {},
                "source_context": [{"pages": deepcopy(self.pages), "call_chain": {"links": [], "omitted_links": 0},
                                    "outline": [], "notices": []}],
                "framework_references": references if references is not None else [self.reference(i) for i in range(4)]}

    def fit(self, payload):
        events = []
        messages, size = business_chat._fit_request(self.config, payload, [],
            AgentPolicy(max_request_bytes=32768), trim_events=events)
        self.assertLessEqual(size, 32768)
        return messages, json.loads(messages[-1]["content"]), events

    def assert_business_source_retained(self, wire):
        actual = {page["evidence_id"]: page for page in wire["source_context"][0]["pages"]}
        self.assertEqual(set(actual), {page["evidence_id"] for page in self.pages})
        for page in self.pages:
            self.assertEqual(actual[page["evidence_id"]]["source_text"], page["source_text"])
        text = "\n".join(page["source_text"] for page in actual.values())
        for formula in self.formulas:
            self.assertIn(formula, text)

    def test_automatic_unbound_framework_background_does_not_evict_business_rules(self):
        payload = self.payload()
        _, wire, events = self.fit(payload)
        self.assert_business_source_retained(wire)
        self.assertLess(len(wire["framework_references"]), 4)
        self.assertFalse(any(event["role"] == "source" for event in events))

    def test_current_framework_question_reference_survives_background_trimming(self):
        wanted = self.reference("wanted", reason="question_only", small=True)
        payload = self.payload([wanted, *[self.reference(i) for i in range(4)]])
        payload["question"] = "Explain the credit and refund deductions and the requested record access convention."
        _, wire, _ = self.fit(payload)
        self.assert_business_source_retained(wire)
        self.assertIn(wanted, wire["framework_references"])

    def test_source_bound_framework_contract_survives_background_trimming(self):
        wanted = self.reference("bound", small=True)
        payload = self.payload([wanted, *[self.reference(i) for i in range(4)]])
        page = self.pages[0]
        payload["framework_facts"] = [{"fact_id": "operation:record", "kind": "framework_operation",
            "relative_path": page["relative_path"], "source_sha256": page["source_sha256"],
            "source_ranges": [{"start_line": 9, "end_line": 9, "role": "callsite"}],
            "reference_ids": [wanted["reference_id"]], "dependency_covered": True}]
        _, wire, _ = self.fit(payload)
        self.assert_business_source_retained(wire)
        self.assertIn(wanted, wire["framework_references"])

    def test_framework_document_only_question_keeps_its_actual_text(self):
        wanted = self.reference("question", reason="question_only", small=True)
        payload = self.payload([wanted])
        payload["source_context"][0]["pages"] = []
        payload["question"] = "Explain the requested record access convention."
        _, wire, _ = self.fit(payload)
        self.assertEqual(wire["framework_references"], [wanted])

    def test_contract_bound_during_fitting_survives_real_request_pressure(self):
        wanted = self.reference("bound-during-fit")
        payload = self.payload([wanted])
        page = self.pages[0]
        payload["framework_facts"] = [{"fact_id": "operation:record", "kind": "framework_operation",
            "relative_path": page["relative_path"], "source_sha256": page["source_sha256"],
            "source_ranges": [{"start_line": 9, "end_line": 9, "role": "callsite"}],
            "reference_ids": [wanted["reference_id"]], "dependency_covered": True}]
        _, wire, events = self.fit(payload)
        self.assertTrue(events, "Exercise actual byte fitting, not an already-small request")
        self.assert_business_source_retained(wire)
        self.assertIn(wanted, wire["framework_references"])
        self.assertEqual(wire["framework_facts"][0]["source_evidence_ids"], [page["evidence_id"]])
        self.assertFalse(any(event.get("item_id") == wanted["reference_id"]
            and event["reason"] == "auxiliary_framework_request_bytes" for event in events))

    def test_stale_fact_source_identity_cannot_protect_unbound_background(self):
        wanted = self.reference("stale-binding")
        payload = self.payload([wanted])
        page = self.pages[0]
        payload["framework_facts"] = [{"fact_id": "operation:record", "kind": "framework_operation",
            "relative_path": page["relative_path"], "source_sha256": "0" * 64,
            "source_ranges": [{"start_line": 9, "end_line": 9, "role": "callsite"}],
            "source_evidence_ids": [page["evidence_id"]],
            "reference_ids": [wanted["reference_id"]], "dependency_covered": True}]
        _, wire, events = self.fit(payload)
        self.assert_business_source_retained(wire)
        self.assertFalse(wire.get("framework_facts"))
        self.assertNotIn(wanted, wire["framework_references"])
        self.assertTrue(any(event.get("item_id") == wanted["reference_id"]
            and event["reason"] == "auxiliary_framework_request_bytes" for event in events))

    def test_explicit_lookup_and_named_document_keep_text_and_custom_instructions(self):
        instruction = "Use one English paragraph about the business result, without headings."
        for kind in ("lookup", "named_document"):
            with self.subTest(kind=kind):
                wanted = {**self.reference("wanted"), "document_name": "business-reference.md"}
                framework = {"references": [wanted]}
                if kind == "named_document":
                    framework["coverage"] = {"document_lookup": [wanted["document_name"]]}
                references, _ = business_chat._framework_prompt_references(framework,
                    {wanted["reference_id"]: wanted} if kind == "lookup" else {},
                    [wanted["reference_id"]] if kind == "lookup" else [],
                    AgentPolicy(), _framework_for_prompt)
                self.assertTrue(references[0]["explicit_lookup"])
                payload = self.payload(references + [self.reference(index) for index in range(4)])
                payload["answer_instructions"] = instruction
                messages, wire, events = self.fit(payload)
                self.assert_business_source_retained(wire)
                self.assertIn(wanted["reference_id"], [ref["reference_id"] for ref in wire["framework_references"]])
                self.assertIn(instruction, messages[0]["content"])
                self.assertFalse(any(event.get("item_id") == wanted["reference_id"]
                    and event["reason"] == "auxiliary_framework_request_bytes" for event in events))


if __name__ == "__main__":
    unittest.main()
