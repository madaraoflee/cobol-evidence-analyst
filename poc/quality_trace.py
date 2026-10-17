"""Bounded, local request-level evidence diagnostics."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import time
from run_store import RunStore

PIPELINE_VERSION = "question-evidence-v21"
PROMPT_VERSION = "business-chat-v24"

def _digest(value):
    return hashlib.sha256(value).hexdigest()


class QualityTrace:
    schema_version = "business-quality-trace/v1"

    def __init__(self, database_path, *, question, config, policy, capture_context=False,
                 question_id=None, conversation_id=None, resume_state=None, analysis_identity=None):
        run_id = resume_state["run_id"] if resume_state else secrets.token_hex(12)
        self.path = Path(database_path).resolve().parent / "quality-traces" / (run_id + ".json")
        self.capture_context = bool(capture_context)
        self.data = {"schema_version": self.schema_version, "run_id": self.path.stem,
                     "question_id": question_id or "sha256:" + _digest(question.encode()),
                     "conversation_id": conversation_id,
                     "created_at_utc": datetime.now(timezone.utc).isoformat(),
                     "pipeline_version": PIPELINE_VERSION, "prompt_version": PROMPT_VERSION,
                     "analysis_identity": analysis_identity,
                     "configuration": {"model_fingerprint": _digest(config.chat_model.encode()),
                         "configuration_fingerprint": _digest(json.dumps({
                             "base_url": config.base_url, "model": config.chat_model,
                             "timeout_seconds": config.timeout_seconds,
                             "max_output_tokens": config.max_output_tokens,
                             "api_style": config.api_style}, sort_keys=True).encode()),
                         "timeout_seconds": config.timeout_seconds,
                         "max_output_tokens": config.max_output_tokens,
                         "sampling": "provider_default", "policy": policy.to_dict()},
                     "rounds": [], "final": {}}
        self._pending = None
        self._pending_round_id = None
        self._pending_replayed = False
        if resume_state and resume_state.get("checkpoint", {}).get("quality"):
            self.data = resume_state["checkpoint"]["quality"]
        self.run_store = RunStore(Path(database_path).resolve().parent / "investigation-runs.sqlite")
        self.run_store.create(self.data["run_id"], {
            "question_id": self.data["question_id"], "configuration": self.data["configuration"],
            "pipeline_version": self.data["pipeline_version"], "prompt_version": self.data["prompt_version"],
            "analysis_identity": self.data.get("analysis_identity")})

    def prepare(self, *, stage, payload, messages, trim_events=(), replay_round_id=None):
        self._pending = (stage, payload, messages, list(trim_events), time.monotonic())
        self._pending_round_id = replay_round_id
        self._pending_replayed = replay_round_id is not None
        if replay_round_id is not None and self.response_round() is None:
            raise ValueError("REPLAY_ROUND_NOT_FOUND")

    def response_round(self):
        """The observed or replayed request associated with this attempt."""
        return next((row for row in reversed(self.data["rounds"])
                     if row["round_id"] == self._pending_round_id), None)

    def observe(self, request_body):
        if self._pending is None:
            return
        stage, payload, messages, trims, started = self._pending
        source = payload["source_context"][0].get("pages", [])
        framework = payload.get("framework_references", [])
        record = {"round_id": f"round-{len(self.data['rounds']) + 1}", "stage": stage,
                  "request_bytes": len(request_body), "request_body_sha256": _digest(request_body),
                  "system_prompt_sha256": _digest(messages[0]["content"].encode()),
                  "sources": [{"evidence_id": p["evidence_id"], "path": p["relative_path"],
                      "source_sha256": p["source_sha256"], "start_line": p["start_line"],
                      "end_line": p["end_line"], "excerpt_sha256": _digest(p["source_text"].encode()),
                      "characters": len(p["source_text"]), "group_id": p.get("group_id"),
                      "include_chain": p.get("include_chain", []),
                      "semantic_roles": p.get("semantic_roles", [])} for p in source],
                  "framework": [{"reference_id": r["reference_id"],
                      "document_sha256": r.get("document_sha256"),
                      "document_name": r.get("document_name"),
                      "range": {key: r.get(key) for key in ("start_line", "end_line")},
                      "text_offset_chars": r.get("text_offset_chars", 0),
                      "excerpt_sha256": _digest(r.get("text", "").encode()),
                      "characters": len(r.get("text", "")),
                      "text_truncated": bool(r.get("text_truncated"))} for r in framework],
                  "history_summary": {"count": len(messages) - 2,
                      "characters": sum(len(m["content"]) for m in messages[1:-1]),
                      "message_positions": list(range(1, len(messages) - 1))},
                  "trim_events": trims, "tool_results": [], "response": None,
                  "duration_ms": None}
        if self.capture_context:
            record["context"] = {"question": payload["question"],
                                 "sources": [{**s, "excerpt": p["source_text"]} for s, p in zip(record["sources"], source)],
                                 "framework": [{**f, "excerpt": r.get("text", "")} for f, r in zip(record["framework"], framework)],
                                 "history": messages[1:-1]}
        self.data["rounds"].append(record)
        self._pending_round_id = record["round_id"]
        self.run_store.start_request(self.data["run_id"], record["round_id"], record["request_body_sha256"])
        checkpoint = self.run_store.load(self.data["run_id"])["checkpoint"]
        self.run_store.checkpoint(self.data["run_id"], {**checkpoint, "quality": self.data,
            "request_payload": payload,
            "analysis_plan": payload.get("question_investigation", {}).get("analysis_plan", {}),
            "obligations": payload.get("question_investigation", {}).get("required_items", []),
            "frontier": payload.get("question_investigation", {}).get("planned_actions", []),
            "budget": payload.get("investigation_budget", {}),
            "source_pages": source, "framework_references": framework,
            "completed_actions": payload.get("completed_actions", [])})
        self._pending = (stage, payload, messages, trims, started)

    def finish_round(self, *, finish_reason=None, parsed_action=None, usage=None, error=None,
                     raw_content_characters=None, parsed_answer_characters=None, choice_index=None):
        if not self.data["rounds"] or self._pending is None or self._pending_round_id is None:
            return
        row = self.response_round()
        if row is None:
            return
        elapsed = round((time.monotonic() - self._pending[-1]) * 1000, 2)
        if self._pending_replayed:
            # Local replay is not another measurement of remote latency.
            row["replay_processing_ms"] = elapsed
        else:
            row["duration_ms"] = elapsed
        row["response"] = {"finish_reason": finish_reason, "parsed_action": parsed_action,
                           "usage": usage if isinstance(usage, dict) else None, "error": error,
                           "raw_content_characters": raw_content_characters,
                           "parsed_answer_characters": parsed_answer_characters,
                           "choice_index": choice_index}
        if error not in {"REQUEST_TIMEOUT", "TRANSPORT_ERROR", "TRANSPORT_RESPONSE_INVALID",
                         "QUESTION_TIME_BUDGET_EXHAUSTED"}:
            self.run_store.finish_request(self.data["run_id"], row["round_id"], row["response"])
        self._pending = None

    def record_remote_response(self, response):
        if self.data["rounds"]:
            self.run_store.finish_request(self.data["run_id"], self.data["rounds"][-1]["round_id"], {"remote_response": response})

    def add_tool_result(self, *, action, actual_result_ids=(), open_read_cursor=None,
                        status="completed"):
        tool = next(iter(action), "unknown") if isinstance(action, dict) else str(action)
        arguments = action.get(tool, {}) if isinstance(action, dict) else {}
        step = self.run_store.start_step(self.data["run_id"], tool, arguments)
        self.run_store.finish_step(self.data["run_id"], step, {"actual_result_ids": list(actual_result_ids),
            "open_read_cursor": open_read_cursor, "status": status})
        if self.data["rounds"]:
            self.data["rounds"][-1]["tool_results"].append({"action": action,
                "actual_result_ids": list(actual_result_ids),
                "open_read_cursor": open_read_cursor, "status": status})

    def save(self, *, final):
        self.data["final"] = final
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        saved = self.run_store.load(self.data["run_id"])
        current = saved["checkpoint"]
        # The process may stop before the caller writes its final result.
        # Never temporarily hide an uncertain request in a completed run.
        status = ("attention" if saved["uncertain_requests"] or current.get("remote_result_unknown")
                  else "running" if final.get("stop_reason") == "QUESTION_TIME_BUDGET_EXHAUSTED"
                  else "completed")
        self.run_store.checkpoint(self.data["run_id"], {**current, "quality": self.data,
            "final": final}, status=status)
        return str(self.path)
