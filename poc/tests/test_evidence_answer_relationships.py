"""Bound relationship follow-ups retain source syntax without inventing a graph."""

from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evidence_answer import build_evidence_answer


SOURCE = ('IDENTIFICATION DIVISION.\nPROGRAM-ID. REQUEST-ENTRY.\n'
          'PROCEDURE DIVISION.\nMAIN.\nIF RUN-FLAG = 1\n'
          'CALL "VALUE-WORKER"\nUSING INPUT-REC\nEND-IF.\n'
          'PERFORM LOCAL-STEP.\nMOVE 9 TO RESULT-AMOUNT.\nGOBACK.\n'
          'LOCAL-STEP.\nCONTINUE.')


def page(text=SOURCE, *, path="entry.cbl", identifier="ev_entry", first=1, version=None):
    return {"evidence_id": identifier, "relative_path": path, "start_line": first,
        "end_line": first + len(text.splitlines()) - 1, "source_text": text,
        "source_sha256": version or hashlib.sha256(text.encode()).hexdigest(), "span_truncated": False}


def investigation(source, direction="outgoing"):
    return {"state": "located", "analysis_plan": {"intents": ["call_relationships"],
        "paths": [source["relative_path"]], "entities": [], "relation_direction": direction,
        "relation_root": {"relative_path": source["relative_path"], "source_sha256": source["source_sha256"],
                          "program_name": "REQUEST-ENTRY"}}}


class RelationshipEvidenceAnswerTests(unittest.TestCase):
    def build(self, pages, plan, question="它相关程序有哪些？"):
        return build_evidence_answer(question, pages, plan, source_format="free")

    def test_pronoun_related_root_uses_actual_call_not_perform_or_nearby_assignment(self):
        source = page()
        for direction in ("outgoing", "related"):
            with self.subTest(direction=direction):
                result = self.build([source], investigation(source, direction))
                self.assertEqual([(fact["target"], fact["statement_kind"], fact["relationship_direction"])
                                  for fact in result["facts"]], [("VALUE-WORKER", "CALL", "outgoing")])
                self.assertIn('CALL "VALUE-WORKER" USING INPUT-REC', result["answer"])
                self.assertIn("IF RUN-FLAG = 1", result["answer"])
                self.assertIn("[ev_entry]", result["answer"])
                self.assertNotIn("LOCAL-STEP", result["answer"])
                self.assertNotIn("RESULT-AMOUNT", result["answer"])
                self.assertEqual(result["status"], "PARTIAL")
                self.assertEqual(result["answer_origin"], "local_source_facts")
                self.assertFalse(result["model_answer_recorded"])
                self.assertFalse(result["execution_verified"])
                self.assertFalse(result["claims_semantically_verified"])
                self.assertIn("不代表完整的上下游程序清单", result["answer"])

    def test_outgoing_calls_do_not_answer_an_incoming_question(self):
        source = page()
        self.assertIsNone(self.build([source], investigation(source, "incoming"), "谁调用 REQUEST-ENTRY？"))

    def test_incoming_needs_bound_actual_caller_in_the_right_direction(self):
        source = page()
        caller = page('PROCEDURE DIVISION.\nCALL "REQUEST-ENTRY".\nCALL "OTHER-WORKER".',
                      path="launcher.cbl", identifier="ev_caller")
        plan = investigation(source, "incoming")
        binding = {"relative_path": caller["relative_path"], "source_sha256": caller["source_sha256"],
            "line": 2, "occurrence_id": "bound-call-two", "target_name": "REQUEST-ENTRY",
            "binding_confirmed": True, "evidence_ids": ["ev_caller"], "compilation_status": "active",
            "scope_known": True, "conditions": [], "condition_candidate_count": 0}
        plan["activation_evidence"] = {"callers": [binding]}
        result = self.build([source, caller], plan, "哪些程序调用它？")
        self.assertEqual([fact["target"] for fact in result["facts"]], ["REQUEST-ENTRY"])
        self.assertEqual(result["facts"][0]["relationship_direction"], "incoming")
        self.assertEqual(result["source_ids"], ["ev_caller"])
        self.assertNotIn("VALUE-WORKER", result["answer"])
        self.assertNotIn("OTHER-WORKER", result["answer"])
        for override in ({"source_sha256": "0" * 64}, {"line": 3}, {"binding_confirmed": False},
                         {"evidence_ids": ["ev_missing"]}, {"target_name": "OTHER-WORKER"}):
            with self.subTest(override=override):
                broken = deepcopy(plan)
                broken["activation_evidence"]["callers"][0].update(override)
                self.assertIsNone(self.build([source, caller], broken))

    def test_missing_ambiguous_unknown_or_changed_root_cannot_expand_pronoun(self):
        source = page()
        base = investigation(source)
        for change in ({"relation_root": None}, {"paths": ["entry.cbl", "other.cbl"]},
                       {"unresolved": ["root_not_found"]}, {"relation_direction": None},
                       {"relation_root": {**base["analysis_plan"]["relation_root"], "source_sha256": "0" * 64}},
                       {"relation_root": {**base["analysis_plan"]["relation_root"], "program_name": "OTHER-ENTRY"}}):
            with self.subTest(change=change):
                plan = deepcopy(base)
                plan["analysis_plan"].update(change)
                self.assertIsNone(self.build([source], plan))
        for change in ({"state": "unresolved"}, {"state": "ambiguous"},
                       {"source_identity": {"status": "not_found"}},
                       {"source_identity": {"status": "ambiguous"}}):
            with self.subTest(change=change):
                self.assertIsNone(self.build([source], {**base, **change}))
        changed = {**source, "source_sha256": "0" * 64, "evidence_id": "ev_changed"}
        self.assertIsNone(self.build([source, changed], base))

    def test_incoming_required_item_must_match_actual_call_span_and_resolved_target(self):
        source = page()
        caller = page('PROCEDURE DIVISION.\nCALL "REQUEST-ENTRY"\nUSING INPUT-REC.\nCALL "OTHER-WORKER".',
                      path="launcher.cbl", identifier="ev_caller", first=12)
        plan = investigation(source, "incoming")
        item = {"kind": "calls", "status": "SATISFIED", "relation_direction": "incoming",
            "relative_path": caller["relative_path"], "source_sha256": caller["source_sha256"],
            "start_line": 13, "end_line": 14, "target_name": "REQUEST-ENTRY", "target_path": "entry.cbl",
            "evidence_ids": [caller["evidence_id"]]}
        plan["required_items"] = [item]
        result = self.build([source, caller], plan, "哪些程序调用它？")
        self.assertEqual([(fact["target"], fact["relationship_direction"]) for fact in result["facts"]],
                         [("REQUEST-ENTRY", "incoming")])
        self.assertEqual(result["source_ids"], ["ev_caller"])
        self.assertIn("控制条件尚未完整核对", result["answer"])
        for changes in ({"status": "OPEN"}, {"relation_direction": "outgoing"},
                        {"target_path": None}, {"target_path": "other.cbl"}, {"source_sha256": "0" * 64},
                        {"start_line": 12}, {"end_line": 13}, {"target_name": "OTHER-WORKER"},
                        {"evidence_ids": ["ev_missing"]}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.build([source, caller], {**plan, "required_items": [{**item, **changes}]}))
        changed_raw = {**caller, "source_text": caller["source_text"].replace("REQUEST-ENTRY", "WRONG-ENTRY")}
        self.assertIsNone(self.build([source, changed_raw], plan))
        scope = {"relative_path": "launcher.cbl", "source_sha256": caller["source_sha256"],
                 "start_line": 13, "end_line": 14, "exclusive": True}
        self.assertIsNotNone(self.build([source, caller], {**plan, "source_scope": {"ranges": [scope]}}))
        for changed in ({"end_line": 13}, {"source_sha256": "0" * 64}, {"start_line": 0}):
            with self.subTest(scope=changed):
                self.assertIsNone(self.build([source, caller],
                    {**plan, "source_scope": {"ranges": [{**scope, **changed}]}}))

    def test_entire_call_must_fit_a_valid_same_version_explicit_scope(self):
        source = page()
        scope = {"relative_path": "entry.cbl", "source_sha256": source["source_sha256"],
                 "start_line": 6, "end_line": 7, "exclusive": True}
        plan = {**investigation(source), "source_scope": {"ranges": [scope]}}
        self.assertIsNotNone(self.build([source], plan))
        for changed in ({"end_line": 6}, {"start_line": 8, "end_line": 9},
                        {"start_line": 0}, {"source_sha256": "0" * 64}):
            with self.subTest(changed=changed):
                broken = {**plan, "source_scope": {"ranges": [{**scope, **changed}]}}
                self.assertIsNone(self.build([source], broken))

    def test_fragment_retains_only_visible_call_and_does_not_claim_complete_guards(self):
        source = page()
        fragment = page('CALL "VALUE-WORKER"\nUSING INPUT-REC', first=6, version=source["source_sha256"])
        result = self.build([fragment], investigation(source))
        self.assertIn("该调用的控制条件尚未完整核对", result["answer"])
        self.assertEqual(result["evidence_refs"][0]["start_line"], 6)
        self.assertEqual(result["evidence_refs"][0]["end_line"], 7)
        self.assertNotIn("IF RUN-FLAG", result["answer"])
        for extra in ({"span_truncated": True}, {"source_text": ""}, {"end_line": 8},
                      {"include_chain": [{"relative_path": "other.cbl"}]},
                      {"copy_instance": "copy-elsewhere"}):
            with self.subTest(extra=extra):
                self.assertIsNone(self.build([{**fragment, **extra}], investigation(source)))

    def test_relationship_and_activation_intents_preserve_bound_pronoun_fact(self):
        source = page()
        plan = investigation(source)
        plan["analysis_plan"]["intents"].append("activation")
        self.assertIsNotNone(self.build([source], plan))


if __name__ == "__main__":
    unittest.main()
