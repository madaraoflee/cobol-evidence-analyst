"""Independent business-location acceptance through the actual online pipeline.

The scripted client reads only actual prompt material. Its prose is a control
input, not a real-model quality judgment; the separately authored gold scores
the final original-source supply and the user-visible cumulative inventory.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).resolve().parents[1])]
from global_formula_oracle import FIXTURE, delivered_coverage, lineage_retention, load_gold, supplied_coverage
from business_index import build_business_index
from company_api import CompanyAPIConfig, OpenAICompatibleChatClient, TransportResponse
from online_investigation import run_investigation
from online_material_export import export_material
from online_material_query import MaterialQuery
from online_material_reader import DirectoryObjectStore, MaterialReader


class OnlineGlobalFormulaDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / "source"
        shutil.copytree(FIXTURE / "source", source)
        self.originals = {p.name: p.read_bytes() for p in source.iterdir()}
        database = self.root / "index.sqlite"
        report = build_business_index(source, database, source_format="free", verify_content=True,
                                      framework_reference_path="", quiet=True)
        material = self.root / "material"
        export_material(database, source, material, repository_id="example/calculation-source",
                        commit="c" * 40, build_report=report)
        shutil.rmtree(source)
        for path in self.root.glob("index.sqlite*"):
            path.unlink()
        self.query = MaterialQuery(MaterialReader(DirectoryObjectStore(material)))
        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "synthetic-model", api_key="synthetic-only")
        self.gold = load_gold()
        self.contexts, self.pages, self.actions, self.failures = [], [], [], []
        self.relation_paths = set()
        self.search_started = False
        self.checkpoints = []

    @staticmethod
    def source_lines(context):
        """Assemble only original lines actually present in this model prompt."""
        result = {}
        for page in context["source_context"]:
            path, start = page["citation"]["path"], page["citation"]["start"]
            for number, line in enumerate(page["text"].splitlines(), start):
                result.setdefault(path, {})[number] = line
        return result

    @staticmethod
    def cited_pages(context, path, first, last):
        pages = [page for page in context["source_context"] if page["citation"]["path"] == path
                 and page["citation"]["start"] <= last and page["citation"]["end"] >= first
                 and not page["truncated"]]
        covered = {number for page in pages
                   for number in range(page["citation"]["start"], page["citation"]["end"] + 1)}
        return [page["id"] for page in pages] if set(range(first, last + 1)) <= covered else []

    @staticmethod
    def relationship_lines(lines):
        return [line.strip() for _, line in sorted(lines.items()) if re.search(
            r"PROGRAM-ID\.|PROCEDURE DIVISION USING|\bCALL\b|\bCOPY\b|ASSIGN TO|PFILE\(", line)]

    @staticmethod
    def related_paths(context, path):
        # Names come exclusively from retained source summaries generated from
        # actual earlier prompts, not from gold or the fixture's directory.
        summaries = {row["path"]: row for row in context["reviewed_source_summaries"]}
        lookup = {Path(name).stem.upper(): name for name in summaries}
        for name, row in summaries.items():
            match = re.search(r"PROGRAM-ID\.\s*([\w-]+)", row["summary"])
            if match:
                lookup[match.group(1).upper()] = name
        edges = {name: set() for name in summaries}
        for name, row in summaries.items():
            for target in re.findall(r"(?:CALL\s+['\"]?|COPY\s+|ASSIGN TO\s+['\"]?|PFILE\()([\w-]+)", row["summary"]):
                other = lookup.get(target.upper())
                if other and other != name:
                    edges[name].add(other)
                    edges[other].add(name)
        related, frontier = {path}, [path]
        while frontier:
            for other in edges.get(frontier.pop(), ()) - related:
                related.add(other)
                frontier.append(other)
        return related

    def plan(self, context, *, premature=False):
        pages = context["source_context"]
        self.pages.extend(pages)
        pending = context["work_ledger"]["pending"]
        if premature and pages:
            return {"answer": "All system premium formulas have been checked. [" + pages[0]["id"] + "]", "complete": True,
                    "citations": [pages[0]["id"]], "remaining": []}
        if not self.search_started and not premature:
            self.search_started = True
            return {"actions": [{"tool": "search", "text": "premium", "limit": 1}]}
        # Intentionally defer a valid unfinished search until after every file
        # review. It must survive both prompt and checkpoint observation limits.
        pending = [row for row in pending if row["kind"] != "query_page"] + [
            row for row in pending if row["kind"] == "query_page"]
        lines_by_path = self.source_lines(context)
        for row in pending:
            if row.get("action"):
                action = deepcopy(row["action"])
                if action["tool"] == "read":
                    # Two physical pages per file exercise real range coverage.
                    first, last = action["start_line"], action["end_line"]
                    if first == 1 and last > 1:
                        action["end_line"] = (first + last) // 2
                return {"actions": [action]}
            if row["kind"] == "source_review":
                path = row["path"]
                if path not in self.relation_paths:
                    self.relation_paths.add(path)
                    return {"actions": [{"tool": "relations", "path": path}]}
                citations = self.cited_pages(context, path, row["start_line"], row["end_line"])
                if not citations:
                    continue
                return {"actions": [{"tool": "review_file", "path": path,
                    "summary": "Reviewed source relationships: " + " ; ".join(self.relationship_lines(lines_by_path[path])),
                    "start_line": row["start_line"], "end_line": row["end_line"], "citations": citations}]}
            if row["kind"] == "candidate_review":
                path = row["path"]
                lines = lines_by_path.get(path, {})
                related = self.related_paths(context, path)
                summaries = [summary for summary in context["reviewed_source_summaries"] if summary["path"] in related]
                if summaries and any(number not in lines_by_path.get(summary["path"], {}) for summary in summaries
                                     for number in range(summary["start_line"], summary["end_line"] + 1)):
                    ids = list(dict.fromkeys(identifier for summary in summaries for identifier in summary["citations"]))
                    return {"actions": [{"tool": "recall", "evidence_ids": ids[:8]}]}
                physical = [number for number in sorted(lines)
                    if row["start_line"] <= number <= row["end_line"]
                    and re.match(r"\s*" + re.escape(row["rule_kind"]) + r"\b", lines[number])]
                if len(physical) != row["occurrence_count"]:
                    continue
                ordinal = row["missing_occurrences"][0]
                number = physical[ordinal - 1]
                citations = self.cited_pages(context, path, number, number)
                if not citations:
                    continue
                labels = [match.group(1) for n in sorted(lines) if n <= number
                          and (match := re.search(r"Business calculation:\s*(.+)", lines[n]))]
                if not labels and 1 not in lines:
                    continue
                included = bool(labels) and row["rule_kind"] in {"COMPUTE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE"}
                summary = ((labels[-1] + ": " + lines[number].strip()) if included else
                           "This arithmetic updates a diagnostic counter, not a premium business amount.")
                if included and len(related) > 1:
                    summary += " Source associations: " + " ; ".join(name + " " + " / ".join(
                        self.relationship_lines(lines_by_path[name])) for name in sorted(related))
                    for source_summary in summaries:
                        citations.extend(self.cited_pages(context, source_summary["path"],
                            source_summary["start_line"], source_summary["end_line"]))
                    citations = list(dict.fromkeys(citations))
                return {"actions": [{"tool": "review", "candidate_id": row["candidate_id"],
                    "occurrence": ordinal, "start_line": number, "end_line": number,
                    "disposition": "included" if included else "excluded", "summary": summary,
                    "citations": citations}]}
        if pending:
            raise AssertionError("The public pending-work contract exposes no executable next step: " + str(pending))
        return {"answer": "The selected snapshot inventory preserves each reviewed business calculation below; runtime binding remains unproved. [" + pages[0]["id"] + "]",
                "complete": True, "citations": [pages[0]["id"]], "remaining": []}

    def run_question(self, checkpoint=None, *, premature=False):
        def transport(request):
            context = json.loads(json.loads(request.body)["messages"][-1]["content"])
            self.contexts.append(context)
            try:
                response = self.plan(context, premature=premature)
            except Exception as error:
                self.failures.append(repr(error))
                raise
            self.actions.extend(response.get("actions", []))
            return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant",
                "content": json.dumps(response, ensure_ascii=False)}, "finish_reason": "stop"}]}))
        result = run_investigation(self.query, lambda path, **kwargs: self.originals[path],
            "列出系统所有计算所有premium formula 的地方", allow_source_transfer=True,
            client=OpenAICompatibleChatClient(self.config, transport=transport), checkpoint=checkpoint,
            max_model_calls=10, max_actions=16)
        self.checkpoints.append(deepcopy(result["checkpoint"]))
        self.assertFalse(self.failures, self.failures)
        return result

    def test_real_online_batches_deliver_every_gold_site_after_observation_eviction(self):
        with patch.object(self.query, "inventory", wraps=self.query.inventory) as inventory:
            result = self.run_question()
            first_checkpoint = deepcopy(result["checkpoint"])
            for _ in range(30):
                if result["status"] == "answered":
                    break
                self.assertEqual(result["status"], "needs_continuation", result.get("error"))
                result = self.run_question(result["checkpoint"])
            self.assertEqual(result["status"], "answered", result.get("error"))
            self.assertEqual(inventory.call_count, 1, "Fixed snapshot continuation must reuse enumerated files")
        self.assertEqual(self.checkpoints[0], first_checkpoint)
        self.assertTrue(result["coverage_ledger"]["complete"])
        self.assertGreater(result["checkpoint"]["observations_total"], 96)
        self.assertTrue(any(context["observations_omitted"] > 96 and any(
            row["kind"] == "query_page" for row in context["work_ledger"]["pending"])
            for context in self.contexts), "An unfinished query must outlive the bounded observation log")
        reviewed = [json.dumps(action, sort_keys=True) for action in self.actions
                    if action["tool"] in {"review", "review_file"}]
        self.assertEqual(len(reviewed), len(set(reviewed)), "Completed reviews must not replay on resume")
        supply = supplied_coverage(self.gold, self.pages)
        self.assertTrue(supply["complete"], supply)
        delivery = delivered_coverage(self.gold, result["findings"], self.pages, result["answer"])
        self.assertTrue(delivery["complete"], delivery)
        lineage = lineage_retention(self.gold, result["findings"], self.pages, result["answer"])
        self.assertTrue(lineage["complete"], lineage)
        self.assertIn(self.gold["sites"][0]["business_label"], result["answer"])
        self.assertFalse(result["coverage_ledger"]["full_repository_or_runtime_proof"])

    def test_premature_model_complete_cannot_clear_unreviewed_global_work(self):
        result = self.run_question(premature=True)
        self.assertNotEqual(result["status"], "answered")
        self.assertGreater(result["coverage_ledger"]["pending_count"], 0)
        self.assertFalse(result["coverage_ledger"]["complete"])
        self.assertTrue(result["checkpoint"]["work_ledger"])
        self.assertFalse(delivered_coverage(self.gold, result["findings"], self.pages, result["answer"])["complete"])


if __name__ == "__main__":
    unittest.main()
