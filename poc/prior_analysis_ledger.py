"""Persistent, source-bound candidate analysis across bounded model contexts.

The ledger preserves interpretations and unfinished work, not verified facts.
Compression replaces only its active presentation; the original generations,
source coverage, and caller-owned investigation snapshots remain local.
"""

from copy import deepcopy
import hashlib
import json
import re


SCHEMA = "prior-analysis-ledger/v1"
TASK_SCHEMA = "analysis-ledger-task/v1"
RESULT_SCHEMA = "analysis-ledger-result/v1"
KINDS = ("rules", "conditions", "handoffs", "conflicts", "open_questions")
MAX_ENTRIES = 128
MAX_ENTRY_CHARACTERS = 4000
MAX_SOURCE_REFERENCES = 256
_REFERENCE = re.compile(r"\[((?:ev[_:-]|fw:)[^\]\r\n]{1,160})\]")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def wire_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _unique_strings(value, *, maximum=MAX_SOURCE_REFERENCES):
    return (isinstance(value, list) and len(value) <= maximum
            and all(isinstance(item, str) and 0 < len(item) <= 180 for item in value)
            and len(set(value)) == len(value))


class PriorAnalysisLedger:
    def __init__(self, scope):
        self.scope = scope
        self.generations = []
        self.attempts = []
        self.investigations = {}
        self.source_coverage = {}
        self.pending_task = None

    @property
    def generation(self):
        return len(self.generations)

    @property
    def entries(self):
        return deepcopy(self.generations[-1]["entries"]) if self.generations else []

    def checkpoint(self):
        return deepcopy({"schema": SCHEMA, "scope": self.scope,
            "generations": self.generations, "attempts": self.attempts,
            "investigations": self.investigations, "source_coverage": self.source_coverage,
            "pending_task": self.pending_task})

    @staticmethod
    def _entry_body(entry):
        return {key: entry[key] for key in
                ("kind", "text", "evidence_ids", "covers_entry_ids", "source_proof_sha256")}

    @staticmethod
    def _proof(identifiers, origins):
        return digest({identifier: origins[identifier] for identifier in sorted(identifiers)})

    @staticmethod
    def _historical_references(entries, origins):
        identifiers = sorted({identifier for entry in entries for identifier in entry["evidence_ids"]})
        # The full binding/origin manifest is verified locally and bound by one
        # aggregate digest. Repeating a long hash for each original source would
        # consume the context that compression is intended to preserve.
        return identifiers

    def restore(self, saved, *, origins):
        if not saved:
            return
        try:
            if saved["schema"] != SCHEMA or saved["scope"] != self.scope:
                raise ValueError("ANALYSIS_LEDGER_INVALID")
            generations = deepcopy(saved["generations"])
            previous, derived_coverage = {}, {}
            for number, generation in enumerate(generations, 1):
                source_ids = generation["source_ids"]
                deferred = generation["deferred_source_ids"]
                origin = generation["response_origin"]
                if (generation["generation"] != number or generation["base_generation"] != number - 1
                        or not isinstance(generation["entries"], list) or len(generation["entries"]) > MAX_ENTRIES
                        or not _unique_strings(source_ids) or not source_ids or set(source_ids) - origins.keys()
                        or not _unique_strings(deferred) or set(deferred) - set(source_ids)
                        or generation.get("semantic_verification") is not False
                        or generation.get("structural_preservation_verified") is not True
                        or re.fullmatch(r"round-[1-9][0-9]*", str(origin.get("round_id", ""))) is None
                        or re.fullmatch(r"[0-9a-f]{64}", str(origin.get("request_body_sha256", ""))) is None):
                    raise ValueError("ANALYSIS_LEDGER_INVALID")
                covered, referenced = set(), set()
                permitted = set(source_ids) | {identifier for entry in previous.values() for identifier in entry["evidence_ids"]}
                for entry in generation["entries"]:
                    body = self._entry_body(entry)
                    if (entry["entry_id"] != "analysis_" + digest(body)
                            or entry.get("content_kind") != "model_candidate"
                            or entry.get("semantic_verification") is not False
                            or not self._valid_entry(body, previous, origins)
                            or set(body["evidence_ids"]) - permitted
                            or body["source_proof_sha256"] != self._proof(body["evidence_ids"], origins)):
                        raise ValueError("ANALYSIS_LEDGER_INVALID")
                    covered.update(body["covers_entry_ids"])
                    referenced.update(body["evidence_ids"])
                if (covered != set(previous) or set(source_ids) - referenced - set(deferred)
                        or len({entry["entry_id"] for entry in generation["entries"]}) != len(generation["entries"])):
                    raise ValueError("ANALYSIS_LEDGER_INVALID")
                body = {key: value for key, value in generation.items() if key != "generation_sha256"}
                if generation["generation_sha256"] != digest(body):
                    raise ValueError("ANALYSIS_LEDGER_INVALID")
                for identifier in source_ids:
                    row = derived_coverage.setdefault(identifier, {
                        "source_proof_sha256": self._proof([identifier], origins), "generations": [],
                        "semantic_verification": False})
                    row["generations"].append(number)
                    row["status"] = "deferred" if identifier in deferred else "represented_in_candidate"
                references = self._historical_references(generation["entries"], origins)
                manifest_hash = self._proof(references, origins)
                if (generation.get("historical_source_references") != references
                        or generation.get("source_manifest_sha256") != manifest_hash):
                    raise ValueError("ANALYSIS_LEDGER_INVALID")
                expected_bytes = wire_bytes(self._presentation(generation["entries"], number, derived_coverage,
                                                               references, manifest_hash))
                if (generation.get("after_bytes") != expected_bytes
                        or expected_bytes > generation["max_output_bytes"]):
                    raise ValueError("ANALYSIS_LEDGER_INVALID")
                previous = {entry["entry_id"]: entry for entry in generation["entries"]}
            investigations = deepcopy(saved["investigations"])
            if any(key != digest(value) for key, value in investigations.items()):
                raise ValueError("ANALYSIS_LEDGER_INVALID")
            if any(generation.get("investigation_id") not in investigations for generation in generations):
                raise ValueError("ANALYSIS_LEDGER_INVALID")
            attempts = deepcopy(saved["attempts"])
            if any(row.get("status") not in {"prepared", "accepted", "failed"}
                   or row.get("investigation_id") not in investigations
                   or not isinstance(row.get("material_fingerprint"), str)
                   or not isinstance(row.get("task_id"), str)
                   or (row.get("retry_token") is not None
                       and (not isinstance(row["retry_token"], str) or not 0 < len(row["retry_token"]) <= 180))
                   for row in attempts):
                raise ValueError("ANALYSIS_LEDGER_INVALID")
            coverage = deepcopy(saved["source_coverage"])
            if coverage != derived_coverage:
                raise ValueError("ANALYSIS_LEDGER_INVALID")
            for identifier, row in coverage.items():
                if (identifier not in origins or row.get("source_proof_sha256") != self._proof([identifier], origins)
                        or not isinstance(row.get("generations"), list)
                        or any(type(number) is not int or not 1 <= number <= len(generations)
                               for number in row["generations"])
                        or row.get("semantic_verification") is not False):
                    raise ValueError("ANALYSIS_LEDGER_INVALID")
            pending = deepcopy(saved.get("pending_task"))
            if pending is not None:
                self._validate_task(pending, origins=origins, generation=len(generations))
                if pending.get("previous_entries") != (generations[-1]["entries"] if generations else []):
                    raise ValueError("ANALYSIS_LEDGER_INVALID")
                if not any(row["task_id"] == pending["task_id"] and row["status"] == "prepared"
                           for row in attempts):
                    raise ValueError("ANALYSIS_LEDGER_INVALID")
            self.generations, self.attempts = generations, attempts
            self.investigations, self.source_coverage = investigations, coverage
            self.pending_task = pending
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("ANALYSIS_LEDGER_INVALID") from exc

    @staticmethod
    def _valid_entry(entry, previous, origins):
        identifiers, covers = entry.get("evidence_ids"), entry.get("covers_entry_ids")
        if (entry.get("kind") not in KINDS or not isinstance(entry.get("text"), str)
                or not 0 < len(entry["text"].strip()) <= MAX_ENTRY_CHARACTERS
                or not _unique_strings(identifiers) or not identifiers
                or not _unique_strings(covers, maximum=MAX_ENTRIES)
                or set(identifiers) - origins.keys() or set(covers) - previous.keys()
                or set(_REFERENCE.findall(entry["text"])) - set(identifiers)):
            return False
        # Contradictions and unfinished questions cannot disappear into a rule
        # summary merely because the model describes them as settled.
        return all(previous[key]["kind"] == entry["kind"]
                   and set(previous[key]["evidence_ids"]) <= set(identifiers) for key in covers)

    def _validate_task(self, task, *, origins, generation=None):
        body = {key: value for key, value in task.items() if key != "task_id"}
        if (task.get("schema") != TASK_SCHEMA or task.get("scope") != self.scope
                or task.get("task_id") != "compression_" + digest(body)
                or generation is not None and task.get("base_generation") != generation
                or not _unique_strings(task.get("source_ids"))
                or set(task["source_ids"]) - origins.keys()
                or task.get("source_proof_sha256") != self._proof(task["source_ids"], origins)):
            raise ValueError("ANALYSIS_LEDGER_INVALID")

    def prepare(self, *, source_ids, origins, frontier=(), coverage=None, max_output_bytes=12000,
                source_bytes=0, retry_token=None):
        """Declare one bounded compression attempt; no model call happens here.

        Output generations never count as new input. The same external material
        cannot recursively request another compression, including after restart
        or failure. An explicit continuation may retry failed or still-deferred
        material once per persisted request token; fully represented material
        cannot be retried this way.
        """
        identifiers = list(dict.fromkeys(source_ids))
        if (not _unique_strings(identifiers) or not identifiers or set(identifiers) - origins.keys()
                or type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= 65536
                or (retry_token is not None
                    and (not isinstance(retry_token, str) or not 0 < len(retry_token) <= 180))):
            raise ValueError("ANALYSIS_LEDGER_INPUT_INVALID")
        if self.pending_task is not None:
            return None
        investigation = {"frontier": deepcopy(list(frontier)), "coverage": deepcopy(coverage or {})}
        investigation_id = digest(investigation)
        proof = self._proof(identifiers, origins)
        material = digest({"source_ids": sorted(identifiers), "source_proof_sha256": proof,
                           "investigation_id": investigation_id})
        prior_attempts = [row for row in self.attempts if row["material_fingerprint"] == material]
        still_deferred = any(self.source_coverage.get(identifier, {}).get("status") == "deferred"
                             for identifier in identifiers)
        if prior_attempts and (retry_token is None
                or any(row["status"] == "prepared" for row in prior_attempts)
                or (any(row["status"] == "accepted" for row in prior_attempts) and not still_deferred)
                or any(row.get("retry_token") == retry_token for row in prior_attempts)):
            return None
        self.investigations[investigation_id] = investigation
        entries = self.entries
        task = {"schema": TASK_SCHEMA, "scope": self.scope, "base_generation": self.generation,
            "material_fingerprint": material, "source_ids": identifiers, "source_proof_sha256": proof,
            "previous_entries": entries, "required_previous_entry_ids": [row["entry_id"] for row in entries],
            "investigation_id": investigation_id,
            "frontier": investigation["frontier"], "coverage": investigation["coverage"],
            "retry_token": retry_token,
            "max_output_bytes": max_output_bytes,
            "before_bytes": source_bytes + (wire_bytes(self.declared_prompt(self.generation)) if self.generation else 0),
            "output_contract": {"schema": RESULT_SCHEMA, "kinds": list(KINDS),
                "maximum_entries": MAX_ENTRIES, "maximum_text_characters": MAX_ENTRY_CHARACTERS,
                "semantic_verification": False, "summary_is_original_source": False},
            "instruction": "Compress the global analysis, preserving business rules, applicability and skip conditions, "
                "call parameters and returned-value handoffs, conflicting interpretations and unfinished questions. "
                "Every entry remains an unverified model candidate bound to supplied original source IDs. "
                "Cover every previous entry with covers_entry_ids in the same kind; retain all its evidence_ids. "
                "Do not silently resolve conflicts or unfinished questions. New source IDs must be cited by entries "
                "or listed in deferred_source_ids. Do not turn navigation, initialization or names into execution proof. "
                "Return only {schema,task_id,entries:[{kind,text,evidence_ids,covers_entry_ids}],deferred_source_ids}."}
        task["task_id"] = "compression_" + digest(task)
        self.pending_task = deepcopy(task)
        self.attempts.append({"task_id": task["task_id"], "material_fingerprint": material,
            "investigation_id": investigation_id, "status": "prepared", "retry_token": retry_token,
            "before_bytes": task["before_bytes"]})
        return deepcopy(task)

    def fail(self, task_id, reason="compression_unavailable"):
        if self.pending_task is None or self.pending_task["task_id"] != task_id:
            return False
        for attempt in reversed(self.attempts):
            if attempt["task_id"] == task_id:
                attempt.update(status="failed", reason=str(reason)[:160])
                break
        self.pending_task = None
        return True

    def accept(self, value, *, task_id, origins, response_origin):
        """Atomically replace the active candidate only after full validation."""
        task = self.pending_task
        if task is None or task["task_id"] != task_id:
            return False
        try:
            self._validate_task(task, origins=origins, generation=self.generation)
            if wire_bytes(value) > task["max_output_bytes"]:
                self.fail(task_id, "compressed_analysis_exceeds_budget")
                return False
            if (not isinstance(value, dict) or set(value) != {"schema", "task_id", "entries", "deferred_source_ids"}
                    or value["schema"] != RESULT_SCHEMA or value["task_id"] != task_id
                    or not isinstance(value["entries"], list) or len(value["entries"]) > MAX_ENTRIES
                    or not _unique_strings(value["deferred_source_ids"])
                    or set(value["deferred_source_ids"]) - set(task["source_ids"])):
                raise ValueError("ANALYSIS_LEDGER_RESULT_INVALID")
            previous = {entry["entry_id"]: entry for entry in task["previous_entries"]}
            permitted = set(task["source_ids"]) | {identifier for entry in previous.values() for identifier in entry["evidence_ids"]}
            entries, covered, referenced = [], set(), set()
            for row in value["entries"]:
                if (not isinstance(row, dict) or set(row) != {"kind", "text", "evidence_ids", "covers_entry_ids"}
                        or not self._valid_entry(row, previous, origins)
                        or set(row["evidence_ids"]) - permitted):
                    raise ValueError("ANALYSIS_LEDGER_RESULT_INVALID")
                body = {**deepcopy(row), "source_proof_sha256": self._proof(row["evidence_ids"], origins)}
                entries.append({"entry_id": "analysis_" + digest(body), **body,
                                "content_kind": "model_candidate", "semantic_verification": False})
                covered.update(row["covers_entry_ids"])
                referenced.update(row["evidence_ids"])
            if (covered != set(previous)
                    or set(task["source_ids"]) - referenced - set(value["deferred_source_ids"])
                    or len({entry["entry_id"] for entry in entries}) != len(entries)):
                raise ValueError("ANALYSIS_LEDGER_RESULT_INVALID")
            generation = {"generation": self.generation + 1, "base_generation": self.generation,
                "task_id": task_id, "material_fingerprint": task["material_fingerprint"],
                "investigation_id": task["investigation_id"], "entries": entries,
                "source_ids": list(task["source_ids"]), "deferred_source_ids": list(value["deferred_source_ids"]),
                "response_origin": deepcopy(response_origin), "before_bytes": task["before_bytes"],
                "max_output_bytes": task["max_output_bytes"],
                "historical_source_references": self._historical_references(entries, origins),
                "source_manifest_sha256": self._proof(
                    {identifier for entry in entries for identifier in entry["evidence_ids"]}, origins),
                "structural_preservation_verified": True, "semantic_verification": False}
            coverage = deepcopy(self.source_coverage)
            for identifier in task["source_ids"]:
                row = coverage.setdefault(identifier, {
                    "source_proof_sha256": self._proof([identifier], origins), "generations": [],
                    "semantic_verification": False})
                row["generations"].append(generation["generation"])
                row["status"] = ("deferred" if identifier in value["deferred_source_ids"]
                                 else "represented_in_candidate")
            presentation = self._presentation(entries, generation["generation"], coverage,
                                               generation["historical_source_references"],
                                               generation["source_manifest_sha256"])
            generation["after_bytes"] = wire_bytes(presentation)
            if generation["after_bytes"] > task["max_output_bytes"]:
                self.fail(task_id, "compressed_analysis_exceeds_budget")
                return False
            generation["generation_sha256"] = digest(generation)
            # No mutation above this point: partial or malformed model output
            # cannot replace the last usable global candidate.
            self.generations.append(generation)
            self.source_coverage = coverage
            self.attempts[-1]["status"] = "accepted"
            self.pending_task = None
            return True
        except (KeyError, TypeError, ValueError):
            self.fail(task_id, "invalid_compression_result")
            return False

    def _presentation(self, entries, generation, coverage, historical_references=(), manifest_hash=None):
        result = {"schema": SCHEMA, "generation": generation,
            "content_kind": "model_candidate", "semantic_verification": False,
            "structural_preservation_verified": bool(generation),
            "summary_is_original_source": False, "citation_permission": False,
            "historical_sources_citation_permission": bool(generation),
            "historical_source_references": deepcopy(list(historical_references)),
            "source_manifest_sha256": manifest_hash or digest({}),
            "entries": deepcopy(entries), "entry_count": len(entries), "omitted_entry_count": 0,
            "source_coverage_count": len(coverage),
            "deferred_source_ids": [key for key, row in coverage.items() if row.get("status") == "deferred"],
            "global_analysis_supplied": True,
            "instruction": "These are source-bound candidate interpretations, not original text or verified facts. "
                "Preserve their conditions, handoffs, conflicts and open questions while answering the current task. "
                "Historical source references authorize attribution only, never the truth of a candidate. "
                "Re-read decisive or disputed original source. Omitted entries remain in the local ledger; "
                "omission does not mean the rule or unfinished work is absent."}
        result["summary_sha256"] = digest(result)
        return result

    def declared_prompt(self, generation):
        """Reconstruct an immutable declaration, including an earlier request."""
        if type(generation) is not int or not 1 <= generation <= self.generation:
            return None
        coverage = {}
        for item in self.generations[:generation]:
            for identifier in item["source_ids"]:
                coverage[identifier] = {"status": "deferred" if identifier in item["deferred_source_ids"]
                                       else "represented_in_candidate"}
        saved = self.generations[generation - 1]
        return self._presentation(saved["entries"], generation, coverage, saved["historical_source_references"],
                                  saved["source_manifest_sha256"])

    def prompt(self, *, max_bytes=16000):
        """Supply an entire active generation or explicitly report it cannot fit.

        Never silently pop individual rules, conditions or unfinished work from
        an accepted generation. The caller must reserve its accepted byte budget
        or schedule a further bounded compaction with genuinely new input.
        """
        if type(max_bytes) is not int or max_bytes < 256:
            raise ValueError("ANALYSIS_LEDGER_BUDGET_INVALID")
        result = self.declared_prompt(self.generation) or self._presentation([], 0, {})
        required = wire_bytes(result)
        if required <= max_bytes:
            return result
        return {"schema": SCHEMA, "generation": self.generation,
            "status": "analysis_summary_exceeds_budget", "requires_batched_compaction": True,
            "global_analysis_supplied": False, "entry_count": len(self.entries),
            "omitted_entry_count": len(self.entries), "required_bytes": required,
            "semantic_verification": False, "citation_permission": False}

    def analysis_state(self):
        latest = self.generations[-1] if self.generations else {}
        return {"schema": SCHEMA, "generation": self.generation,
            "attempt_count": len(self.attempts),
            "attempt_status": self.attempts[-1]["status"] if self.attempts else "not_started",
            "last_failure_reason": (self.attempts[-1].get("reason") if self.attempts
                                    and self.attempts[-1]["status"] == "failed" else None),
            "entry_count": len(self.entries),
            "entries_by_kind": {kind: sum(row["kind"] == kind for row in self.entries) for kind in KINDS},
            "source_count": len(self.source_coverage),
            "deferred_source_count": sum(row.get("status") == "deferred" for row in self.source_coverage.values()),
            "before_bytes": latest.get("before_bytes", 0), "after_bytes": latest.get("after_bytes", 0),
            "structural_preservation_verified": bool(self.generation),
            "semantic_verification": False, "content_kind": "model_candidate",
            "local_generation_count": self.generation,
            "local_investigation_snapshot_count": len(self.investigations)}
