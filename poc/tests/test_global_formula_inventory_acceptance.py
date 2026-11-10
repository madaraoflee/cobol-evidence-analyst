"""Independent whole-inventory oracle and actual delivery acceptance."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).resolve().parents[1])]
from global_formula_oracle import FIXTURE, delivered_coverage, lineage_retention, load_gold, supplied_coverage


class GlobalFormulaOracleTests(unittest.TestCase):
    def setUp(self):
        self.gold = load_gold()
        self.pages = []
        for row in self.gold["files"]:
            text = (FIXTURE / "source" / row["path"]).read_text(encoding="utf-8")
            self.pages.append({"id": "evidence-" + row["path"], "citation": {"path": row["path"],
                "hash": row["sha256"], "start": 1, "end": row["lines"]}, "text": "\n".join(text.splitlines())})
        self.findings = [{"path": site["path"], "start_line": site["start_line"], "end_line": site["end_line"],
            "disposition": "included", "summary": site["business_label"] + ": " + site["formula"],
            "citations": ["evidence-" + site["path"]]} for site in self.gold["sites"]]
        self.answer = "\n".join(row["summary"] for row in self.findings)

    def test_authored_fixture_has_independent_roots_alias_copy_view_and_negative_controls(self):
        self.assertEqual(self.gold["metrics"], {"business_sites": 14, "independent_entry_roots": 12, "source_files": 18})
        self.assertNotIn("premium", (FIXTURE / "source/z-local-language.cbl").read_text().lower())
        self.assertNotIn("premium", (FIXTURE / "source/value-adjust.cbl").read_text().lower())
        self.assertEqual(len(self.gold["excluded_sites"]), 2)
        self.assertTrue(supplied_coverage(self.gold, self.pages)["complete"])
        self.assertTrue(delivered_coverage(self.gold, self.findings, self.pages, self.answer)["complete"])

    def test_formula_pages_without_original_copy_lv_and_call_bindings_are_not_full_supply(self):
        for path in ("copy-entry.cbl", "RATELV.dds", "RATEPF.pf", "issue.cbl"):
            with self.subTest(path=path):
                pages = [page for page in self.pages if page["citation"]["path"] != path]
                result = supplied_coverage(self.gold, pages)
                self.assertFalse(result["complete"])
                self.assertIn(path, result["dependency_paths_missing"])

    def test_first_batch_claim_of_everything_cannot_pass_the_independent_oracle(self):
        first = self.findings[:8]
        result = delivered_coverage(self.gold, first, self.pages, "All calculations are listed.\n" + "\n".join(x["summary"] for x in first))
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["missing"]), 6)
        self.assertTrue(supplied_coverage(self.gold, self.pages)["complete"], "Having read every source is insufficient for delivery")

    def test_retained_metadata_cannot_hide_omission_from_the_visible_final_answer(self):
        for index in (0, 9, 10, 11, 12):
            with self.subTest(index=index):
                answer = "\n".join(row["summary"] for i, row in enumerate(self.findings) if i != index)
                result = delivered_coverage(self.gold, self.findings, self.pages, answer)
                self.assertFalse(result["complete"])
                self.assertEqual(result["missing_from_visible_answer"], [self.gold["sites"][index]["site_id"]])

    def test_forged_clipped_wrong_source_and_irrelevant_calculations_do_not_inflate_coverage(self):
        for change in ({"text": "forged"}, {"truncated": True}):
            broken = deepcopy(self.pages)
            broken[0].update(change)
            self.assertFalse(supplied_coverage(self.gold, broken)["complete"])
        broken = deepcopy(self.findings)
        broken[0]["citations"] = ["evidence-report-only.cbl"]
        self.assertFalse(delivered_coverage(self.gold, broken, self.pages, self.answer)["complete"])
        extra = {"path": "unrelated-counter.cbl", "start_line": 9, "end_line": 9,
                 "disposition": "included", "summary": "Report counter", "citations": []}
        self.assertFalse(delivered_coverage(self.gold, self.findings + [extra], self.pages, self.answer)["complete"])
        excluded = deepcopy(self.findings)
        excluded[0]["disposition"] = "excluded"
        self.assertFalse(delivered_coverage(self.gold, excluded, self.pages, self.answer)["complete"])

    def test_location_recall_does_not_count_as_preserved_call_copy_or_view_lineage(self):
        self.assertFalse(lineage_retention(self.gold, self.findings, self.pages, self.answer)["complete"])
        linked = deepcopy(self.findings)
        for site, finding in zip(self.gold["sites"], linked):
            finding["summary"] += " Source associations: " + ", ".join(site["dependencies"])
            finding["citations"].extend("evidence-" + path for path in site["dependencies"])
        answer = "\n".join(row["summary"] for row in linked)
        self.assertTrue(lineage_retention(self.gold, linked, self.pages, answer)["complete"])
        for dependency in ("issue.cbl", "copy-entry.cbl", "RATELV.dds", "RATEPF.pf"):
            with self.subTest(dependency=dependency):
                broken = deepcopy(linked)
                for row in broken:
                    row["citations"] = [identifier for identifier in row["citations"]
                                        if identifier != "evidence-" + dependency]
                self.assertFalse(lineage_retention(self.gold, broken, self.pages, answer)["complete"])


class LocalGlobalFormulaDeliveryTests(unittest.TestCase):
    """Scripted reviewer reads actual wire pages, never the gold file or source disk."""
    def setUp(self):
        import json
        import shutil
        import tempfile
        from agent_policy import AgentPolicy
        from business_index import build_business_index
        from company_api import CompanyAPIConfig
        from repository_discovery import ensure_repository_search
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        shutil.copytree(FIXTURE / "source", self.source)
        self.database = self.root / "index.sqlite"
        build_business_index(self.source, self.database, source_format="free", verify_content=True,
                             quiet=True, framework_reference_path="")
        ensure_repository_search(self.database, self.source)
        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "synthetic-model", api_key="synthetic-only")
        self.policy = AgentPolicy(max_model_requests=2, max_answer_revisions=0, max_semantic_files=3,
            max_source_characters=1800, max_complete_source_characters=1800, initial_source_characters=1800)
        self.gold = load_gold()
        self.requests, self.envelopes, self.pages, self.reviewed_ids, self.batches = [], [], [], set(), []
        self.compactions = 0
        self.narrative = "This batch records the supported business calculation locations; it does not establish runtime configuration."
        self.json = json

    @staticmethod
    def wire_pages(payload):
        return [page for bundle in payload.get("source_context", []) for page in bundle.get("pages", [])]

    def review(self, payload, *, omit_formula=False):
        import re
        pages = self.wire_pages(payload)
        self.pages.extend(pages)
        if payload.get("schema") == "semantic-answer-review/v1":
            # This controls the existing review protocol only. The independent
            # location/supply/delivery oracle below decides acceptance; no
            # synthetic verdict is evidence of real-model semantic quality.
            evidence = payload["evidence"]
            reviews = []
            for unit in payload["units"]:
                references = [row for row in evidence if row["path"] in unit["text"]]
                references = references or evidence[:1]
                reviews.append({"unit_id": unit["unit_id"], "verdict": "supported",
                    "reason": "Synthetic protocol response; source position coverage is checked separately.",
                    "evidence": [{"reference_id": row["reference_id"], "quote": row["text"][:1000]}
                                 for row in references[:4]]})
            return {"answer_sha256": payload["answer_sha256"], "evidence_sha256": payload["evidence_sha256"],
                    "reviews": reviews, "omissions": []}
        if payload.get("schema") == "business-analysis-compaction/v1":
            self.compactions += 1
            task = payload["analysis_compaction"]
            entries = [{"kind": row["kind"], "text": row["text"], "evidence_ids": row["evidence_ids"],
                        "covers_entry_ids": [row["entry_id"]]} for row in task["previous_entries"]]
            entries.append({"kind": "rules", "text": "Retain reviewed source positions and their calculation or input roles.",
                            "evidence_ids": task["source_ids"], "covers_entry_ids": []})
            return {"schema": "analysis-ledger-result/v1", "task_id": task["task_id"],
                    "entries": entries, "deferred_source_ids": []}
        inventory = payload.get("repository_inventory", {})
        items = inventory.get("items", [])
        self.batches.append([item["candidate_id"] for item in items])
        self.assertFalse(self.reviewed_ids.intersection(self.batches[-1]), "Completed items were sent for review again")
        findings = []
        for item in items:
            covering = [p for p in pages if p["relative_path"] == item["relative_path"]
                        and p["start_line"] <= item["start_line"] and p["end_line"] >= item["end_line"]
                        and not p.get("span_truncated")]
            if not covering:
                continue
            page = max(covering, key=lambda p: len(p["source_text"]))
            lines = page["source_text"].splitlines()
            if item["kind"] == "source_review":
                finding = {"candidate_id": item["candidate_id"], "disposition": "reviewed",
                    "summary": "Reviewed original source window for business formulas, declarations and source relationships.",
                    "evidence_ids": [page["evidence_id"]]}
            else:
                if omit_formula:
                    continue
                offset = item["start_line"] - page["start_line"]
                formula = "\n".join(lines[offset:item["end_line"] - page["start_line"] + 1]).strip()
                labels = [m.group(1) for line in lines[:offset + 1]
                          if (m := re.search(r"Business calculation:\s*(.+)", line))]
                if not labels and page["start_line"] != 1:
                    # A statement-only page cannot establish absence of its
                    # business label. Leave it pending until original context
                    # arrives instead of treating missing context as exclusion.
                    continue
                included = bool(labels)
                finding = {"candidate_id": item["candidate_id"], "disposition": "included" if included else "excluded",
                    "summary": labels[-1] + " determines the amount for this business step." if included else "A diagnostic counter, not a business premium amount.",
                    "formula": formula, "conditions": "Applies on the shown source path; actual runtime rate and library binding are not established.",
                    "evidence_ids": [page["evidence_id"]]}
            findings.append(finding)
            self.reviewed_ids.add(item["candidate_id"])
        return {"answer": self.narrative,
                "inventory_findings": findings}

    def run_question(self, previous=None, *, omit_formula=False, answer_instructions="", locale=None):
        from business_chat import run_business_chat
        from company_api import TransportResponse
        def transport(request):
            envelope = self.json.loads(request.body)
            self.envelopes.append(envelope)
            payload = self.json.loads(envelope["messages"][-1]["content"])
            self.requests.append(payload)
            body = self.review(payload, omit_formula=omit_formula)
            return TransportResponse(200, self.json.dumps({"choices": [{"message": {"role": "assistant",
                "content": self.json.dumps(body, ensure_ascii=False)}, "finish_reason": "stop"}]}, ensure_ascii=False))
        options = {"continue_investigation": True, "resume_run_id": previous["run_id"]} if previous else {}
        return run_business_chat("列出系统所有计算所有premium formula 的地方", self.database, self.source,
            self.config, transport=transport, allow_network=False, framework_reference_path="", policy=self.policy,
            conversation_id="inventory-acceptance", answer_instructions=answer_instructions, locale=locale,
            **options)["agent_result"]

    def test_real_local_batches_resume_to_all_gold_sites_without_replaying_reviewed_items(self):
        from run_store import RunStore
        result = self.run_question()
        first = deepcopy(result)
        inventory = result.get("repository_inventory", {})
        self.assertGreater(inventory.get("reviewed_count", 0), 0, result.get("diagnostics"))
        self.assertGreater(inventory.get("pending_count", 0), 0)
        previous_count = inventory["reviewed_count"]
        saved_first = RunStore(self.root / "investigation-runs.sqlite").load(result["run_id"])
        for _ in range(12):
            if inventory["complete"] and result["status"] == "ANALYZED":
                break
            was_complete = inventory["complete"]
            result = self.run_question(result)
            inventory = result["repository_inventory"]
            if was_complete:
                self.assertEqual(inventory["reviewed_count"], previous_count)
            else:
                self.assertGreater(inventory["reviewed_count"], previous_count, result.get("diagnostics"))
            previous_count = inventory["reviewed_count"]
        self.assertTrue(inventory["complete"], inventory)
        self.assertEqual(result["status"], "ANALYZED", result.get("diagnostics"))
        self.assertTrue(supplied_coverage(self.gold, self.pages)["complete"], supplied_coverage(self.gold, self.pages))
        delivery = delivered_coverage(self.gold, inventory["findings"], self.pages, result["answer"])
        self.assertTrue(delivery["complete"], delivery)
        self.assertEqual(RunStore(self.root / "investigation-runs.sqlite").load(first["run_id"]), saved_first)
        final_checkpoint = RunStore(self.root / "investigation-runs.sqlite").load(result["run_id"])["checkpoint"]
        self.assertEqual(final_checkpoint["repository_inventory_state"]["catalog_builds"], 1)
        early = self.gold["sites"][0]
        self.assertIn(early["business_label"], result["answer"])
        self.assertGreater(self.compactions, 0, "The fixture must exercise actual context compaction")
        final_batch = [payload for payload in self.requests if payload.get("repository_inventory", {}).get("items")][-1]
        final_pages = self.wire_pages(final_batch)
        self.assertNotIn(early["path"], {p["relative_path"] for p in final_pages}, "Fixture must exercise a source that left the working window")

    def test_omitted_formula_reviews_cannot_finish_even_when_model_claims_batch_completion(self):
        result = self.run_question(omit_formula=True)
        inventory = result.get("repository_inventory", {})
        self.assertFalse(inventory.get("complete", True))
        self.assertGreater(inventory["pending_count"], 0)
        self.assertNotEqual(result["status"], "ANALYZED")
        self.assertFalse(delivered_coverage(self.gold, inventory["findings"], self.pages, result["answer"])["complete"])

    def test_saved_business_requirements_and_traditional_chinese_survive_inventory_delivery(self):
        requirements = "請用繁體中文，保留『業務目的』和『適用條件』兩個章節，詳述業務影響。"
        self.narrative = "### 業務目的\n\n本輪核對已提供原文的保費計算位置。\n\n### 適用條件\n\n實際執行仍取決於所示條件與執行時輸入。"
        result = self.run_question(answer_instructions=requirements, locale="zh-HK")
        for _ in range(12):
            if result["repository_inventory"]["complete"] and result["status"] == "ANALYZED":
                break
            result = self.run_question(result, answer_instructions=requirements, locale="zh-HK")
        self.assertTrue(result["repository_inventory"]["complete"])
        self.assertEqual(result["status"], "ANALYZED", result.get("diagnostics"))
        self.assertIn(self.narrative, result["answer"])
        self.assertIn("累計公式位置與業務規則", result["answer"])
        self.assertNotIn("累计公式位置与业务规则", result["answer"])
        inventory_requests = [envelope for envelope, payload in zip(self.envelopes, self.requests)
                              if payload.get("repository_inventory", {}).get("items")]
        self.assertTrue(inventory_requests)
        for envelope in inventory_requests:
            self.assertIn(requirements, envelope["messages"][0]["content"])
            self.assertIn("繁體", envelope["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
