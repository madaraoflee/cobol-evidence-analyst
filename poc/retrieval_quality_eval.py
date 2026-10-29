"""Offline, span-based retrieval and final-wire evidence diagnostics.

No model client is called. Oracle arms prepare raw source only, never answers.
Scores describe annotated synthetic evidence, not production answer quality.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import itertools
import json
import math
from pathlib import Path

from run_store import fingerprint


def _identity(page):
    # A content hash does not erase a distinct file or COPY instantiation.
    return (page.get("original_relative_path", page.get("relative_path")),
            page["source_sha256"], fingerprint(page.get("include_chain") or []),
            fingerprint(page.get("replacement_trace") or []), fingerprint(page.get("copy_instance")))


def deduplicate(pages):
    """Deduplicate original spans/versions rather than retrieval chunk IDs."""
    result, seen = [], set()
    for page in pages:
        key = (*_identity(page), page["start_line"], page["end_line"])
        if key not in seen:
            result.append(page)
            seen.add(key)
    return result


def _covered(first, last, ranges):
    cursor = first
    for begin, end in sorted(ranges):
        if begin > cursor:
            return False
        if end >= cursor:
            cursor = end + 1
        if cursor > last:
            return True
    return False


def _matching_ranges(unit, pages, *, require_text=False):
    ranges = []
    for page in pages:
        if _identity(page) != _identity(unit) or page.get("span_truncated"):
            continue
        first, last = page["start_line"], page["end_line"]
        actual, expected = page.get("source_text"), unit.get("source_text")
        if require_text and (not isinstance(actual, str) or not actual
                             or len(actual.splitlines()) != last - first + 1):
            continue
        if actual is None or expected is None:
            ranges.append((first, last))
            continue
        actual, expected = actual.splitlines(), expected.splitlines()
        last = min(last, first + len(actual) - 1, unit["end_line"])
        for line in range(max(first, unit["start_line"]), last + 1):
            if actual[line - first] == expected[line - unit["start_line"]]:
                ranges.append((line, line))
    return ranges


def covered_units(units, pages, *, require_text=False):
    """A different chunk split can cover the same complete original range."""
    return {unit["id"] for unit in units if _covered(unit["start_line"], unit["end_line"],
                                                   _matching_ranges(unit, pages, require_text=require_text))}


def _overlaps(unit, page):
    return (_identity(unit) == _identity(page)
            and unit["start_line"] <= page["end_line"]
            and page["start_line"] <= unit["end_line"])


def _sufficiency(required, covered):
    items = [{"id": item["id"], "critical": bool(item.get("critical")),
              "sufficient": any(bool(choice) and set(choice) <= covered
                                for choice in item["sufficient_sets"])} for item in required]
    return {"rate": sum(item["sufficient"] for item in items) / len(items) if items else None,
            "items": items,
            "critical_missing": [item["id"] for item in items
                                 if item["critical"] and not item["sufficient"]]}


def _normalize_gold(gold):
    units, aliases, spans = [], {}, {}
    for unit in gold["units"]:
        if unit["id"] in aliases:
            raise ValueError("EVALUATION_DUPLICATE_GOLD_UNIT")
        key = (*_identity(unit), unit["start_line"], unit["end_line"])
        canonical = spans.get(key)
        if canonical is not None and canonical["relevance"] != unit["relevance"]:
            raise ValueError("EVALUATION_CONFLICTING_GOLD_LABEL")
        if canonical is None:
            spans[key] = canonical = unit
            units.append(unit)
        aliases[unit["id"]] = canonical["id"]
    required = []
    for item in gold.get("required_items", []):
        choices = item["sufficient_sets"]
        if not choices or any(not choice or not set(choice) <= aliases.keys() for choice in choices):
            raise ValueError("EVALUATION_SUFFICIENT_SET_INVALID")
        required.append({**item, "sufficient_sets": [sorted({aliases[name] for name in choice})
                                                     for choice in choices]})
    return {"units": units, "required_items": required}


def evaluate_evidence(gold, candidates, loaded, *, k=5):
    """Score retrieved material and actual final-wire source independently."""
    if type(k) is not int or k < 1:
        raise ValueError("EVALUATION_K_INVALID")
    gold = _normalize_gold(gold)
    units, required = gold["units"], gold.get("required_items", [])
    unit_ids = {unit["id"] for unit in units}
    if len(unit_ids) != len(units):
        raise ValueError("EVALUATION_DUPLICATE_GOLD_UNIT")
    for unit in units:
        if type(unit["relevance"]) is not int or not 0 <= unit["relevance"] <= 3:
            raise ValueError("EVALUATION_RELEVANCE_INVALID")
    for item in required:
        if not item["sufficient_sets"] or any(not choice or not set(choice) <= unit_ids
                                              for choice in item["sufficient_sets"]):
            raise ValueError("EVALUATION_SUFFICIENT_SET_INVALID")
    raw_count = len(candidates)
    candidates = deduplicate(candidates)
    ranked = candidates[:k]
    positive = {unit["id"] for unit in units if unit["relevance"] > 0}
    retrieved = covered_units(units, candidates) & positive
    retrieved_k = covered_units(units, ranked) & positive
    supplied = covered_units(units, deduplicate(loaded), require_text=True) & positive
    needed = {identifier for item in required for choice in item["sufficient_sets"]
              for identifier in choice}
    judged, gains, credited = [], [], set()
    for page in ranked:
        matches = [unit for unit in units if _overlaps(unit, page)]
        related = [unit for unit in matches if unit["relevance"] > 0]
        if related:
            judged.append(True)
        elif _covered(page["start_line"], page["end_line"],
                      [(unit["start_line"], unit["end_line"]) for unit in matches]):
            judged.append(False)
        else:
            judged.append(None)
        # Overlapping chunks cannot earn the same gold relevance repeatedly.
        gains.append(max((2 ** unit["relevance"] - 1 for unit in related
                          if unit["id"] not in credited), default=0))
        credited.update(unit["id"] for unit in related)
    relevant_count, unknown = judged.count(True), judged.count(None)
    first = next((rank for rank, related in enumerate(judged, 1) if related is True), None)
    unknown_first = next((rank for rank, related in enumerate(judged, 1) if related is None), None)
    reciprocal = 1 / first if first else 0.0
    possible_first = min(value for value in (first, unknown_first, k + 1) if value is not None)
    upper_rr = 1 / possible_first if possible_first <= len(ranked) else 0.0
    ideal = sorted((2 ** unit["relevance"] - 1 for unit in units if unit["relevance"] > 0),
                   reverse=True)[:k]
    ideal_dcg = sum(gain / math.log2(rank + 1) for rank, gain in enumerate(ideal, 1))
    dcg = sum(gain / math.log2(rank + 1) for rank, gain in enumerate(gains, 1))
    count = len(ranked)
    return {"k": k, "rank_unit": "unique_original_span", "raw_returned": raw_count,
            "duplicates_removed": raw_count - len(candidates),
            "returned_unique": count, "unjudged_results": unknown,
            "judgment_status": "needs_judgment" if unknown else "judged",
            "recall_at_k": len(retrieved_k) / len(positive) if positive else None,
            "recall_scope": "annotated_relevant_original_spans",
            "precision_at_k": relevant_count / k if not unknown else None,
            "precision_at_k_bounds": [relevant_count / k, (relevant_count + unknown) / k],
            "precision_returned": relevant_count / count if count and not unknown else None,
            "precision_returned_bounds": ([relevant_count / count, (relevant_count + unknown) / count]
                                          if count else None),
            "mrr_at_k": reciprocal if positive and (not unknown_first or first and first < unknown_first) else None,
            "mrr_at_k_bounds": [reciprocal, upper_rr] if positive else None,
            "ndcg_at_k": dcg / ideal_dcg if ideal_dcg and not unknown else None,
            "sufficiency_scope": "all_returned_unique_candidates",
            "retrieved_sufficiency": _sufficiency(required, retrieved),
            "loaded_sufficiency": _sufficiency(required, supplied),
            "context_support_rate": len(supplied & retrieved & needed) / len(retrieved & needed)
                                    if retrieved & needed else None,
            "retrieved_required_unit_ids": sorted(retrieved & needed),
            "loaded_required_unit_ids": sorted(supplied & needed),
            "loading_lost_unit_ids": sorted((retrieved - supplied) & needed),
            "unrecalled_required_unit_ids": sorted({name for item in required
                for name in min((set(choice) - retrieved for choice in item["sufficient_sets"]),
                                key=lambda missing: (len(missing), sorted(missing)))}),
            "empty_relevant_gold": not positive}


def production_loader(question, *, max_request_bytes=32768, max_input_tokens=None,
                      token_counter=None, tokenizer_id=None):
    """Fit final wire material, optionally under a supplied model tokenizer.

    The callback receives all final request messages, including instructions.
    No token estimator is silently substituted for a model-specific tokenizer.
    """
    from agent_policy import AgentPolicy
    from business_chat import _fit_request
    from company_api import CompanyAPIConfig

    config = CompanyAPIConfig("https://example.invalid/v1", "offline-evaluation", api_key="unused")
    policy = AgentPolicy(max_request_bytes=max_request_bytes)
    if max_input_tokens is not None and (type(max_input_tokens) is not int or max_input_tokens < 1
                                         or not callable(token_counter) or not tokenizer_id):
        raise ValueError("EVALUATION_TOKEN_COUNTER_REQUIRED")

    def load(pages):
        pages = copy.deepcopy(pages)
        protected = [page["evidence_id"] for page in pages if page.pop("_oracle_required", False)]
        trim = []
        while True:
            payload = {"question": question,
                       "source_context": [{"pages": copy.deepcopy(pages),
                           "call_chain": {"links": [], "omitted_links": 0}, "outline": [], "notices": []}],
                       "business_map": {}, "repository": {"program_samples": []},
                       "framework_references": [], "completed_actions": [], "completed_searches": []}
            if protected:
                payload["evidence_groups"] = [{"group_id": "offline-prioritized-evidence",
                                                "core_evidence_ids": protected}]
            messages, size = _fit_request(config, payload, [], policy, trim_events=trim)
            wire = json.loads(messages[-1]["content"])
            supplied = [page for bundle in wire["source_context"] for page in bundle["pages"]]
            tokens = token_counter(messages) if max_input_tokens is not None else None
            if tokens is not None and (type(tokens) is not int or tokens < 0):
                raise ValueError("EVALUATION_TOKEN_COUNT_INVALID")
            if max_input_tokens is None or tokens <= max_input_tokens:
                return {"pages": supplied, "request_bytes": size, "trim_events": trim,
                        "wire_sha256": fingerprint(wire), "input_tokens": tokens,
                        "max_input_tokens": max_input_tokens, "tokenizer_id": tokenizer_id}
            if not supplied:
                raise ValueError("EVALUATION_TOKEN_BUDGET_BELOW_INSTRUCTIONS")
            # This is the controlled offline token arm, not a claim that the
            # production byte fitter already knows a provider's tokenization.
            removable = [page for page in supplied if page["evidence_id"] not in protected]
            removed = (removable or supplied)[-1]
            pages = [page for page in supplied if page["evidence_id"] != removed["evidence_id"]]
            protected = [identifier for identifier in protected if identifier != removed["evidence_id"]]
            trim.append({"item_id": removed["evidence_id"], "reason": "offline_input_token_budget"})
    return load


def oracle_arms(gold, candidates, load, *, k=5):
    """Construct A/B/C/D raw-material controls; no inference or gold prose."""
    gold = _normalize_gold(gold)
    units = {unit["id"]: unit for unit in gold["units"]}
    required = gold.get("required_items", [])
    choices = [item["sufficient_sets"] for item in required]
    if not choices or math.prod(map(len, choices)) > 256:
        raise ValueError("EVALUATION_ORACLE_SETS_UNAVAILABLE")
    combinations = {frozenset(itertools.chain.from_iterable(items))
                    for items in itertools.product(*choices)}
    minimal = min(combinations, key=lambda ids: (
        sum(len(units[item].get("source_text", "").encode()) for item in ids), len(ids), sorted(ids)))
    if not minimal or any(not units[item].get("source_text") for item in minimal):
        raise ValueError("EVALUATION_ORACLE_RAW_SOURCE_REQUIRED")
    source_fields = {"evidence_id", "relative_path", "original_relative_path", "source_sha256",
                     "start_line", "end_line", "source_text", "include_chain", "replacement_trace", "copy_instance",
                     "semantic_roles", "selection_reasons"}
    raw = [{key: value for key, value in units[item].items() if key in source_fields}
           for item in sorted(minimal)]
    candidates = deduplicate(candidates)
    # C can only move already-recalled material. It cannot inject missing code.
    preferred = [{**page, "_oracle_required": any(_overlaps(units[item], page) for item in minimal)}
                 for page in candidates]
    preferred.sort(key=lambda page: not page["_oracle_required"])
    inputs = {"A": candidates, "B": deduplicate([*raw, *candidates]),
              "C": preferred, "D": raw}
    arms = {}
    for name, pages in inputs.items():
        loaded = load(copy.deepcopy(pages))
        arms[name] = {"candidate_pages": pages, "loaded_pages": loaded["pages"],
                      "request_bytes": loaded.get("request_bytes"),
                      "input_tokens": loaded.get("input_tokens"),
                      "max_input_tokens": loaded.get("max_input_tokens"),
                      "tokenizer_id": loaded.get("tokenizer_id"),
                      "trim_events": loaded.get("trim_events", []),
                      "metrics": evaluate_evidence(gold, pages, loaded["pages"], k=k)}
    d_metrics = arms["D"]["metrics"]
    return {"arms": arms, "gold_sufficient_unit_ids": sorted(minimal),
            "sufficient_evidence_exceeds_budget": d_metrics["loaded_sufficiency"]["rate"] != 1,
            "budget_conflict_scope": "selected_smallest_raw_byte_gold_set_not_all_alternatives",
            "generation_evaluated": False,
            "diagnosis": {"candidate_gap": arms["A"]["metrics"]["unrecalled_required_unit_ids"],
                          "loading_loss": arms["A"]["metrics"]["loading_lost_unit_ids"],
                          "generation": "not_evaluated_no_model_request"}}


def load_fixture(path):
    """Expand original neutral fixture source and bind every label to its hash."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    for case in document["cases"]:
        sources = {}
        for name, definition in case["sources"].items():
            lines = definition["lines"] * definition.get("repeat", 1)
            text = "\n".join(lines)
            sources[name] = (lines, hashlib.sha256(text.encode()).hexdigest())
        pages = {}
        for specification in case["evidence"]:
            name = specification["relative_path"]
            lines, digest = sources[name]
            first, last = specification["start_line"], specification["end_line"]
            if not 1 <= first <= last <= len(lines):
                raise ValueError("EVALUATION_FIXTURE_RANGE_INVALID")
            pages[specification["id"]] = {**specification, "evidence_id": specification["id"],
                "source_sha256": digest, "source_text": "\n".join(lines[first - 1:last])}
        case["gold"] = {"units": [{**pages[item["evidence_id"]], "id": item["id"],
                                   "relevance": item["relevance"]} for item in case["labels"]],
                        "required_items": case["required_items"]}
        case["candidates"] = [pages[identifier] for identifier in case["candidate_ids"]]
        case["snapshot_id"] = fingerprint({name: digest for name, (_, digest) in sources.items()})
    return document


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path(__file__).parent /
                        "fixtures/retrieval-quality-v1/cases.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args(argv)
    fixture = load_fixture(args.cases)
    rows = []
    for case in fixture["cases"]:
        loader = production_loader(case["question"], max_request_bytes=case.get("max_request_bytes", 32768))
        result = oracle_arms(case["gold"], case["candidates"], loader, k=args.k)
        # Report metrics and source identities, not a second source archive.
        for arm in result["arms"].values():
            for key in ("candidate_pages", "loaded_pages"):
                arm[key] = [{**{name: page[name] for name in
                             ("relative_path", "source_sha256", "start_line", "end_line")},
                             "source_instance_sha256": fingerprint(_identity(page))}
                            for page in arm[key]]
        rows.append({"query_id": case["id"], "snapshot_id": case["snapshot_id"], **result})
    report = {"schema": "retrieval-quality/v1", "scope": "synthetic_offline_material_diagnostic",
              "retrieval_execution": "authored_fixture_candidate_order",
              "production_retrieval_executed": False,
              "model_requests": 0, "token_budget_verified": False,
              "budget_unit": "serialized_request_bytes", "gold_review": fixture["review"], "cases": rows}
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
