"""A supplied distant CL guard is a source fact, with execution still unknown."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.desktop_acceptance_cases import (DEFAULT_MANIFEST, _cl_context_checks,
    _cl_guard_sources_supplied, prepare_source)


class DesktopCLGuardAcceptanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        source = Path(temporary.name) / "source"
        manifest = json.loads(DEFAULT_MANIFEST.read_bytes())
        self.case = next(row for row in manifest["cases"] if row["id"] == "cl_late_context")
        prepare_source(self.case, manifest, source, None)
        raw = (source / "late.clle").read_bytes()
        lines = raw.decode().splitlines()
        version = hashlib.sha256(raw).hexdigest()
        self.pages = [{"relative_path": "late.clle", "source_sha256": version,
            "evidence_id": identity, "start_line": start, "end_line": end,
            "source_text": "\n".join(lines[start - 1:end]), "span_truncated": False}
            for identity, start, end in (("ev_guard", 3, 3), ("ev_call", 294, 306))]
        guard = {"command": "IF", "condition": "&READY *EQ 'Y'", "source_line": 3,
            "outcome": True, "outcome_evaluated": False, "source_supplied": True,
            "evidence_ids": ["ev_guard"], "source_ref": {"relative_path": "late.clle",
                "source_sha256": version, "start_line": 3, "end_line": 3, "statement": lines[2]}}
        self.call = {"occurrence_id": "late_call", "command": "CALL", "target_name": "BATCHPAY",
            "relative_path": "late.clle", "source_sha256": version, "start_line": 304, "end_line": 304,
            "evidence_ids": ["ev_call"], "guards": [guard],
            "binding_basis": "indexed_complete_source_parse_and_supplied_span",
            "control_scope_supplied": True, "scope_known": True, "scope_unknown": False,
            "missing_guard_source_lines": [], "runtime_execution_verified": False,
            "runtime_object_verified": False}
        self.caller = {"occurrence_id": "late_call", "scope_known": True,
            "scope_unknown_reason": None, "execution_verified": False, "binding_confirmed": False}
        self.agent = {"answer": "源码在 &READY *EQ 'Y' 成立的分支调用 BATCHPAY，实际执行未知。",
            "metrics": {"model_requests": 1, "tool_calls": {"read": 3}}}

    def checks(self, *, pages=None, call=None, caller=None, agent=None):
        trace = {"final": {"question_investigation": {
            "cl_context": {"observations": [self.call if call is None else call],
                "unread_candidate_count": 0, "scope": {"scheduler_verified": False}},
            "activation_evidence": {"callers": [self.caller if caller is None else caller]},
            "planned_actions": []}}}
        return _cl_context_checks(self.case, self.agent if agent is None else agent,
            self.pages if pages is None else pages, trace)

    def test_distant_guard_requires_source_and_preserves_runtime_unknown(self):
        self.assertTrue(_cl_guard_sources_supplied(self.call, self.pages))
        self.assertTrue(all(self.checks().values()))

    def test_missing_or_false_guard_cannot_pass_with_scope_flags_alone(self):
        variants = [[self.pages[1]]]
        for change in ({"source_sha256": "0" * 64}, {"source_text": "IF COND(1 *EQ 1) THEN(DO)"},
                       {"span_truncated": True}, {"evidence_id": "ev_other"}, {"start_line": 4}):
            variants.append([{**self.pages[0], **change}, self.pages[1]])
        for pages in variants:
            with self.subTest(pages=pages):
                self.assertFalse(self.checks(pages=pages)["quality_cl_late_control_source_supplied"])

    def test_unknown_or_stale_activation_scope_cannot_pass(self):
        for caller in ({**self.caller, "scope_unknown_reason": "not_supplied"},
                       {**self.caller, "scope_known": False},
                       {**self.caller, "occurrence_id": "different_call"}):
            with self.subTest(caller=caller):
                self.assertFalse(self.checks(caller=caller)["quality_cl_late_control_source_supplied"])
        call = {**self.call, "control_scope_supplied": False}
        self.assertFalse(self.checks(call=call)["quality_cl_late_control_source_supplied"])
        call = copy.deepcopy(self.call)
        call["guards"][0]["outcome"] = False
        self.assertFalse(self.checks(call=call)["quality_cl_late_control_source_supplied"])

    def test_source_guard_never_proves_runtime_or_object_binding(self):
        for name in ("runtime_execution_verified", "runtime_object_verified"):
            self.assertFalse(self.checks(call={**self.call, name: True})["quality_cl_late_runtime_unknown"])
        call = copy.deepcopy(self.call)
        call["guards"][0]["outcome_evaluated"] = True
        self.assertFalse(self.checks(call=call)["quality_cl_late_runtime_unknown"])
        for name in ("execution_verified", "binding_confirmed"):
            self.assertFalse(self.checks(caller={**self.caller, name: True})["quality_cl_late_runtime_unknown"])

    def test_old_missing_source_answer_is_rejected(self):
        old = {**self.agent, "answer": "前序控制范围未完整供应，实际执行未知。"}
        self.assertFalse(self.checks(agent=old)["quality_cl_late_control_source_supplied"])


if __name__ == "__main__":
    unittest.main()
