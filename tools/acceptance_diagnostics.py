"""Keep actual executable diagnostics distinct from a passed mechanism check."""
from __future__ import annotations

import copy
import hashlib
import json


SCHEMA = "executable-answer-diagnostics/v1"
PARTIAL_CLASSIFICATIONS = ("scope_mismatch", "answer_omission", "evidence_gap",
                           "explicit_unknown", "unsupported_semantics")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def capture_diagnostics(trace, agent, *, trace_sha256):
    """Copy observations; never derive semantic success from agent status alone."""
    final = copy.deepcopy(trace.get("final") or {})
    return {"schema_version": SCHEMA, "run_id": trace.get("run_id"),
            "trace_sha256": trace_sha256,
            "answer_sha256": hashlib.sha256(agent.get("answer", "").encode()).hexdigest(),
            "agent_status": agent.get("status"), "stop_reason": agent.get("stop_reason"),
            "reason_code": agent.get("reason_code"),
            "final": final, "final_sha256": fingerprint(final),
            "question_investigation_rounds": copy.deepcopy(trace.get("question_investigation_rounds", [])),
            "boundaries": copy.deepcopy(agent.get("boundaries", [])),
            "claim_ledger": copy.deepcopy(agent.get("claim_ledger") or {}),
            "status_scope": "actual_executable_diagnostics_not_a_quality_score"}
