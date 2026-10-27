"""Request-local lexical reuse preserves every source-flow witness."""
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import question_investigation
import statement_facts


def fixture(repeats=12):
    statement = "COMPUTE RESULT-AMOUNT = AMOUNT OF INPUT-REC * 2."
    lines = ["MOVE 4 TO AMOUNT OF INPUT-REC.", *([statement] * repeats)]
    text = "\n".join(lines)
    version = hashlib.sha256(text.encode()).hexdigest()

    def reference(number):
        return {"relative_path": "ledger.cbl", "source_sha256": version,
                "start_line": number, "end_line": number, "start_column": 1,
                "excerpt_sha256": hashlib.sha256(lines[number - 1].encode()).hexdigest()}

    candidates = [{"kind": "formula", "relative_path": "ledger.cbl", "source_sha256": version,
        "statement": statement, "reads": ["AMOUNT OF INPUT-REC"], "occurrence_count": repeats,
        "occurrences": [{"start_line": number, "end_line": number, "start_column": 1}
                        for number in range(2, repeats + 2)]}]
    proofs = [{"reader_field": "amount in input-rec", "reader_statement": statement[:-1],
        "reader_occurrence_id": "read-" + str(number), "reader_source_refs": [reference(number)],
        "source_refs": [reference(1), reference(number)], "status": "resolved_source_origin",
        "initial_definitions": [{"statement": lines[0], "activation_semantics": "local_assignment"}],
        "unknown_reasons": []} for number in range(2, repeats + 2)]
    pages = [{"relative_path": "ledger.cbl", "source_sha256": version,
        "start_line": 1, "end_line": len(lines), "source_text": text,
        "evidence_id": "ledger-source", "span_truncated": False}]
    groups = [{"semantic_analysis": {"assignments": [{"read_origins": proofs}]}}]
    return candidates, pages, groups


class FlowOriginCacheTests(unittest.TestCase):
    def test_exact_witnesses_match_uncached_lexical_path_with_fewer_parses(self):
        inputs = fixture()
        original = deepcopy(inputs)
        with patch.object(statement_facts, "data_references", wraps=statement_facts.data_references) as fields, \
             patch.object(question_investigation, "lex_cobol", wraps=question_investigation.lex_cobol) as tokens:
            actual = question_investigation._flow_input_origins(*inputs)
        self.assertEqual(fields.call_count, 2)
        self.assertEqual(tokens.call_count, 2)
        with patch("functools.cache", side_effect=lambda function: function), \
             patch.object(statement_facts, "data_references", wraps=statement_facts.data_references) as old_fields, \
             patch.object(question_investigation, "lex_cobol", wraps=question_investigation.lex_cobol) as old_tokens:
            expected = question_investigation._flow_input_origins(*inputs)
        self.assertEqual(actual, expected)
        self.assertGreater(old_fields.call_count, fields.call_count * 50)
        self.assertGreater(old_tokens.call_count, tokens.call_count * 50)
        self.assertEqual(actual[0], {("ledger.cbl", "AMOUNT OF INPUT-REC")})
        self.assertEqual(actual[1], set())
        self.assertEqual(len(actual[2][0]["reader_occurrences"]), 12)
        self.assertEqual(inputs, original)

    def test_each_call_rechecks_source_text_version_and_missing_witnesses(self):
        inputs = fixture()
        self.assertTrue(question_investigation._flow_input_origins(*inputs)[0])
        for mutation in ("text", "version", "missing", "conflicting", "unknown"):
            candidates, pages, groups = deepcopy(inputs)
            if mutation == "text":
                pages[0]["source_text"] = pages[0]["source_text"].replace("MOVE 4", "MOVE 9")
            elif mutation == "version":
                pages[0]["source_sha256"] = "0" * 64
            elif mutation == "missing":
                pages[0]["start_line"] = 2
                pages[0]["source_text"] = "\n".join(pages[0]["source_text"].splitlines()[1:])
            elif mutation == "conflicting":
                pages.append({**pages[0], "evidence_id": "conflicting-source",
                    "source_text": pages[0]["source_text"].replace("MOVE 4", "MOVE 9")})
            else:
                groups[0]["semantic_analysis"]["assignments"][0]["read_origins"][0].update(
                    status="unresolved_source_origin", unknown_reasons=["call_effect_unknown"])
            with self.subTest(mutation=mutation):
                with patch.object(statement_facts, "data_references", wraps=statement_facts.data_references) as parsed:
                    result = question_investigation._flow_input_origins(candidates, pages, groups)
                self.assertEqual(parsed.call_count, 2)
                self.assertEqual(result[0], set())
                self.assertEqual(result[1], {("ledger.cbl", "AMOUNT OF INPUT-REC")})
                self.assertEqual(result[2][0]["status"], "source_origin_unresolved")
                with patch("functools.cache", side_effect=lambda function: function):
                    self.assertEqual(result, question_investigation._flow_input_origins(candidates, pages, groups))
        self.assertTrue(question_investigation._flow_input_origins(*inputs)[0])

    def test_missing_reader_and_invalid_syntax_remain_unresolved(self):
        for mutation in ("different_statement", "missing_occurrence", "invalid_field", "invalid_statement"):
            candidates, pages, groups = fixture()
            proof = groups[0]["semantic_analysis"]["assignments"][0]["read_origins"][0]
            if mutation == "different_statement":
                proof["reader_statement"] = "COMPUTE RESULT-AMOUNT = AMOUNT OF INPUT-REC * 3"
            elif mutation == "missing_occurrence":
                proof["reader_source_refs"][0]["start_line"] += 1
            elif mutation == "invalid_field":
                proof["reader_field"] = "`INVALID"
            else:
                proof["reader_statement"] = "COMPUTE `INVALID"
            with self.subTest(mutation=mutation):
                result = question_investigation._flow_input_origins(candidates, pages, groups)
                self.assertEqual(result[0], set())
                self.assertEqual(result[2][0]["missing_reader_occurrences"][0]["start_line"], 2)
                with patch("functools.cache", side_effect=lambda function: function):
                    self.assertEqual(result, question_investigation._flow_input_origins(candidates, pages, groups))


if __name__ == "__main__":
    unittest.main()
