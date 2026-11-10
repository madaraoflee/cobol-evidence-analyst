"""Offline global-scope and cumulative delivery contracts; no model-quality claim."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_policy import AgentPolicy
from business_chat import _continued_investigation_seed, run_business_chat
from business_index import build_business_index
from business_map import build_business_map
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import discover_repository, ensure_repository_search
from repository_inventory import (prepare_inventory, inventory_batch, inventory_investigation,
    inventory_report, accept_inventory_response, render_inventory, missing_deliveries, set_inventory_requirements, inventory_review_parts, restore_inventory_deliveries)

QUESTION = "列出系统所有计算所有premium formula 的地方"


def program(name, body, field="PREMIUM-AMT"):
    return (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\nWORKING-STORAGE SECTION.\n"
        f"01 {field} PIC 9(6)V99.\n01 BASIS PIC 9(5) VALUE 100.\n01 FACTOR PIC 9V99 VALUE 1.25.\n"
        f"PROCEDURE DIVISION.\nMAIN.\n{body}\nGOBACK.\n")


class RepositoryFormulaInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "synthetic-model", api_key="synthetic-only")

    def prepare(self, files):
        for path, text in files.items():
            (self.source / path).write_text(text)
        build_business_index(self.source, self.database, source_format="free", verify_content=True,
            quiet=True, framework_reference_path="")
        ensure_repository_search(self.database, self.source)
        mapping = build_business_map(self.database, self.source, QUESTION)
        self.state = prepare_inventory(self.database, mapping["snapshot_id"], QUESTION)
        self.pages = [{"relative_path": path, "start_line": 1, "end_line": len(text.splitlines()),
            "source_sha256": hashlib.sha256(text.encode()).hexdigest(), "source_text": text,
            "evidence_id": "ev_page_" + hashlib.sha256(path.encode()).hexdigest()[:24], "span_truncated": False}
            for path, text in files.items()]
        return mapping

    def findings(self, items, pages):
        result = []
        for item in items:
            page = next((p for p in pages if p["relative_path"] == item["relative_path"]
                and p["start_line"] <= item["start_line"] and p["end_line"] >= item["end_line"]), None)
            if page:
                result.append({"candidate_id": item["candidate_id"],
                    "disposition": "included" if item["kind"] == "formula" else "reviewed",
                    "summary": "保费金额按约定的基础金额和计费系数确定。",
                    "formula": "保费 = 基础金额 × 计费系数。", "conditions": "到达本段计算时适用；执行调度未由此确定。",
                    "evidence_ids": [page["evidence_id"]]})
        return result

    def test_explicit_global_catalog_includes_disconnected_chinese_business_alias(self):
        mapping = self.prepare({"a.cbl": program("ENTRY-A", "COMPUTE PREMIUM-AMT = BASIS * FACTOR."),
            "c.cbl": program("ENTRY-C", "*> 此项为保费金额。\nCOMPUTE PRM-AMT = BASIS * FACTOR.", "PRM-AMT"),
            "layout.cpy": "01 POLICY-RECORD.\n   05 PRM-AMT PIC 9(6)V99.\n"})
        self.assertEqual(set(mapping["direct_paths"]), {"a.cbl", "c.cbl", "layout.cpy"})
        self.assertEqual(mapping["direct_path_selection"]["basis"], "fixed_repository_inventory")
        self.assertEqual({row["relative_path"] for row in self.state["items"]}, set(mapping["direct_paths"]))
        self.assertEqual(len([r for r in self.state["items"] if r["kind"] == "formula"]), 2)
        local = build_business_map(self.database, self.source, "只看 a.cbl 的 PREMIUM-AMT 计算规则")
        self.assertFalse(local["repository_inventory_requested"])
        self.assertEqual(local["direct_paths"], ["a.cbl"])

    def test_global_question_releases_previous_exclusive_scope(self):
        self.prepare({"a.cbl": program("ENTRY-A", "COMPUTE PREMIUM-AMT = BASIS * FACTOR."),
            "b.cbl": program("ENTRY-B", "COMPUTE PREMIUM-AMT = BASIS * FACTOR.")})
        old_question = "只看 a.cbl 的 PREMIUM-AMT 怎样计算？"
        old = build_business_map(self.database, self.source, old_question)
        history = [{"role": "user", "content": old_question}, {"role": "assistant", "content": "已解释局部计算。",
            "investigation_state": {"session_subject": old["session_subject"]}}]
        result = discover_repository(self.database, QUESTION, history=history, fallback_to_repository=False)
        self.assertEqual(result["selected_paths"], ["a.cbl", "b.cbl"])
        self.assertFalse(result["source_identity"].get("exclusive_source_scope"))

    def test_catalog_contains_every_physical_occurrence_and_every_source_window(self):
        body = "\n".join("ADD 1 TO PREMIUM-AMT." for _ in range(70))
        self.prepare({"many.cbl": program("MANY-ENTRY", body + "\n" * 140)})
        formulas = [r for r in self.state["items"] if r["kind"] == "formula"]
        self.assertEqual(len(formulas), 70)
        self.assertEqual(len({r["candidate_id"] for r in formulas}), 70)
        self.assertEqual(len([r for r in self.state["items"] if r["kind"] == "source_review"]), 3)
        self.assertEqual(len(inventory_batch(self.state)), 32)

    def test_same_snapshot_resumes_directory_without_database_scan(self):
        self.prepare({"entry.cbl": program("ENTRY", "COMPUTE PREMIUM-AMT = BASIS * FACTOR.")})
        with patch("repository_inventory.sqlite3.connect", side_effect=AssertionError("catalog rebuilt")):
            self.assertIs(prepare_inventory(self.database, self.state["snapshot_id"], QUESTION, self.state), self.state)
        rebuilt = prepare_inventory(self.database, "different-snapshot", QUESTION, self.state)
        self.assertNotEqual(rebuilt["scope_key"], self.state["scope_key"])
        self.assertEqual(rebuilt["findings"], {})

    def test_partial_or_wrong_version_citation_cannot_complete_review(self):
        self.prepare({"entry.cbl": program("ENTRY", "COMPUTE PREMIUM-AMT = BASIS * FACTOR.")})
        items = inventory_batch(self.state)
        findings = self.findings(items, self.pages)
        for changed in ({"source_sha256": "0" * 64}, {"span_truncated": True}, {"end_line": 1}):
            with self.subTest(changed=changed):
                self.assertEqual(accept_inventory_response(self.state, json.dumps({"inventory_findings": findings}),
                    [{**self.pages[0], **changed}], requested_items=items), 0)
        self.assertFalse(inventory_report(self.state)["complete"])

    def test_uncertain_classification_does_not_block_unseen_batch_or_count_complete(self):
        self.prepare({f"entry-{n:02}.cbl": program(f"ENTRY-{n:02}", "COMPUTE PREMIUM-AMT = BASIS * FACTOR.")
            for n in range(12)})
        items = inventory_batch(self.state)
        findings = self.findings(items, self.pages)
        findings[1]["disposition"] = "uncertain"
        accept_inventory_response(self.state, json.dumps({"inventory_findings": findings}), self.pages, requested_items=items)
        remaining = inventory_batch(self.state)
        self.assertFalse({r["candidate_id"] for r in items} & {r["candidate_id"] for r in remaining})
        self.assertEqual(self.state["reviewed_count"], len(findings) - 1)
        self.assertFalse(inventory_report(self.state)["complete"])

    def test_render_retains_business_sections_and_traditional_language(self):
        self.prepare({"entry.cbl": program("ENTRY", "COMPUTE PREMIUM-AMT = BASIS * FACTOR.")})
        items = inventory_batch(self.state)
        accept_inventory_response(self.state, json.dumps({"answer": "## 業務因果\n\n依契約計費條件產生保費。",
            "inventory_findings": self.findings(items, self.pages)}), self.pages, requested_items=items)
        answer = render_inventory(self.state, language="zh-HK")
        self.assertIn("## 業務因果", answer)
        self.assertIn("已核對固定索引快照", answer)
        self.assertIn("累計公式位置與業務規則", answer)
        self.assertFalse(missing_deliveries(self.state, answer))
        self.assertTrue(missing_deliveries(self.state, "只保留最终批次"))

    def test_directory_and_citation_without_business_formula_is_not_final_delivery(self):
        self.prepare({"entry.cbl": program("ENTRY", "COMPUTE PREMIUM-AMT = BASIS * FACTOR.")})
        items = inventory_batch(self.state)
        accept_inventory_response(self.state, json.dumps({"inventory_findings": self.findings(items, self.pages)}),
            self.pages, requested_items=items)
        formula = next(row for row in self.state["findings"].values() if row["kind"] == "formula")
        directory_only = f"entry.cbl:10-10 [{formula['evidence_ids'][0]}]"
        self.assertEqual(missing_deliveries(self.state, directory_only), [formula["candidate_id"]])
        paraphrase = "改写后的业务说明仍予保留。\n\n" + directory_only
        restored = restore_inventory_deliveries(self.state, paraphrase)
        self.assertIn("改写后的业务说明仍予保留", restored)
        for field in ("summary", "formula", "conditions"):
            self.assertIn(formula[field], restored)
        self.assertFalse(missing_deliveries(self.state, restored))

    def test_additional_formula_is_bound_to_reviewed_original_window(self):
        self.prepare({"entry.cbl": program("ENTRY", "EXEC SQL SET :PREMIUM-AMT = :BASIS * :FACTOR END-EXEC.")})
        items = inventory_batch(self.state)
        extra = {"relative_path": "entry.cbl", "start_line": 10, "end_line": 10,
            "summary": "补录保费计算。", "formula": "保费按基础金额和系数计算。", "conditions": "本源文路径适用。",
            "evidence_ids": [self.pages[0]["evidence_id"]]}
        accept_inventory_response(self.state, json.dumps({"inventory_findings": self.findings(items, self.pages),
            "additional_findings": [extra]}), self.pages, requested_items=items)
        self.assertEqual(len(self.state["additional_findings"]), 1)
        self.assertIn("补录保费计算。", render_inventory(self.state))

    def test_changed_language_rephrases_saved_findings_without_discarding_source_review(self):
        self.prepare({"entry.cbl": program("ENTRY", "COMPUTE PREMIUM-AMT = BASIS * FACTOR.")})
        set_inventory_requirements(self.state, "zh-CN", "")
        items = inventory_batch(self.state)
        accept_inventory_response(self.state, json.dumps({"answer": "旧业务段落。",
            "inventory_findings": self.findings(items, self.pages)}), self.pages, requested_items=items)
        old_findings = dict(self.state["findings"])
        set_inventory_requirements(self.state, "en", "Use business headings")
        self.assertEqual(self.state["findings"], old_findings)
        self.assertFalse(inventory_report(self.state)["complete"])
        self.assertTrue(all(item["kind"] == "formula" for item in inventory_batch(self.state)))
        self.assertNotIn("旧业务段落", render_inventory(self.state, language="en"))
        revised = [{"candidate_id": item["candidate_id"], "disposition": "included", "summary": "Business premium calculation.",
            "formula": "Premium = base amount times rating factor.", "conditions": "Applies at this source location.",
            "evidence_ids": [self.pages[0]["evidence_id"]]} for item in inventory_batch(self.state)]
        accept_inventory_response(self.state, json.dumps({"answer": "## Business result\n\nThe source determines the premium amount.",
            "inventory_findings": revised}), self.pages, requested_items=inventory_batch(self.state))
        self.assertTrue(inventory_report(self.state)["complete"])
        self.assertIn("## Business result", render_inventory(self.state, language="en"))
        self.assertNotIn("保费金额按", render_inventory(self.state, language="en"))
        self.assertEqual(self.state["catalog_builds"], 1)

    def test_review_partition_preserves_every_character_beyond_thirty_two_paragraphs(self):
        answer = "\n\n".join(f"Business rule {number}. " * 100 for number in range(70))
        parts = inventory_review_parts(answer)
        self.assertGreater(len(parts), 2)
        self.assertEqual("".join(parts), answer)
        self.assertTrue(all(len(part) <= 8000 for part in parts))

    def test_explicit_continue_seed_retains_inventory_and_old_checkpoint_stays_compatible(self):
        self.prepare({"entry.cbl": program("ENTRY", "COMPUTE PREMIUM-AMT = BASIS * FACTOR.")})
        from quality_trace import PIPELINE_VERSION, PROMPT_VERSION
        saved = {"checkpoint": {"repository_inventory_state": self.state},
            "metadata": {"pipeline_version": PIPELINE_VERSION, "prompt_version": PROMPT_VERSION},
            "run_id": "saved"}
        seed = _continued_investigation_seed(saved)
        self.assertEqual(seed["repository_inventory_state"], self.state)
        self.assertIsNot(seed["repository_inventory_state"], self.state)
        saved["checkpoint"] = {}
        self.assertNotIn("repository_inventory_state", _continued_investigation_seed(saved))

    def test_original_question_positive_delivers_first_and_last_batches_in_final_answer(self):
        files = {f"calc-{n:02}.cbl": program(f"ENTRY-{n:02}", "COMPUTE PREMIUM-AMT = BASIS * FACTOR.") for n in range(12)}
        self.prepare(files)
        batches = []
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            if payload.get("schema") == "semantic-answer-review/v1":
                evidence = payload["evidence"][0]
                response = {"answer_sha256": payload["answer_sha256"], "evidence_sha256": payload["evidence_sha256"],
                    "reviews": [{"unit_id": unit["unit_id"], "verdict": "supported", "reason": "Offline protocol fixture.",
                        "evidence": [{"reference_id": evidence["reference_id"], "quote": evidence["text"].splitlines()[0]}]}
                        for unit in payload["units"]], "omissions": []}
            else:
                items = payload["repository_inventory"]["items"]
                batches.append({item["candidate_id"] for item in items})
                pages = [p for bundle in payload["source_context"] for p in bundle["pages"]]
                response = {"answer": "本批次已核对各项业务保费公式与适用条件。", "inventory_findings": self.findings(items, pages)}
            return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": json.dumps(response)}, "finish_reason": "stop"}]}))
        with patch("socket.create_connection", side_effect=AssertionError("network forbidden")):
            result = run_business_chat(QUESTION, self.database, self.source, self.config, transport=transport,
                allow_network=False, framework_reference_path="", policy=AgentPolicy(max_model_requests=8, max_answer_revisions=0),
                answer_instructions="")["agent_result"]
        self.assertEqual(result["status"], "ANALYZED", result["answer_completion"])
        self.assertEqual(len(batches), 2)
        self.assertFalse(batches[0] & batches[1])
        self.assertTrue(result["repository_inventory"]["complete"])
        self.assertEqual(len([r for r in result["repository_inventory"]["findings"] if r["disposition"] == "included"]), 12)
        for path in files:
            self.assertIn(path + ":10-10", result["answer"])
        self.assertFalse(result["investigation"]["full_repository_semantics_verified"])

    def test_source_review_can_correct_wrong_formula_without_restoring_the_wrong_canonical_text(self):
        self.prepare({f"calc-{number}.cbl": program(f"ENTRY-{number}", "COMPUTE PREMIUM-AMT = BASIS * 2.")
            for number in range(8)})
        phases = []
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            if payload.get("schema") == "semantic-answer-review/v1":
                phases.append("review")
                evidence = payload["evidence"][0]
                quote = "COMPUTE PREMIUM-AMT = BASIS * 2."
                response = {"answer_sha256": payload["answer_sha256"], "evidence_sha256": payload["evidence_sha256"],
                    "reviews": [{"unit_id": unit["unit_id"], "verdict": "contradicted" if "乘以五" in unit["text"] else "supported",
                        "reason": "The original source multiplies the base by two.",
                        "evidence": [{"reference_id": evidence["reference_id"], "quote": quote}]}
                        for unit in payload["units"]], "omissions": []}
            elif payload.get("repository_inventory_revision"):
                phases.append("correction")
                corrections = [{**row, "formula": "保费按基础金额乘以二计算。"}
                    for row in payload["repository_inventory_revision"]["items"]]
                response = {"answer": "业务公式已按实际源码修正，仍保留全部位置与适用条件。",
                    "inventory_corrections": corrections}
            else:
                phases.append("analysis")
                items = payload["repository_inventory"]["items"]
                pages = [page for bundle in payload["source_context"] for page in bundle["pages"]]
                findings = self.findings(items, pages)
                for row in findings:
                    if row["disposition"] == "included":
                        row["formula"] = "保费按基础金额乘以五计算。"
                response = {"answer": "逐项解释业务计算、适用条件与实际源码位置。", "inventory_findings": findings}
            return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": json.dumps(response)}, "finish_reason": "stop"}]}))
        result = run_business_chat(QUESTION, self.database, self.source, self.config, transport=transport,
            allow_network=False, framework_reference_path="", policy=AgentPolicy(max_model_requests=8, max_answer_revisions=1))["agent_result"]
        self.assertEqual(result["status"], "ANALYZED", result["answer_completion"])
        self.assertEqual(phases, ["analysis", "review", "correction", "review"])
        self.assertNotIn("乘以五", result["answer"])
        self.assertIn("乘以二", result["answer"])
        from run_store import RunStore
        checkpoint = RunStore(self.root / "investigation-runs.sqlite").load(result["run_id"])["checkpoint"]
        history = checkpoint["repository_inventory_state"]["finding_history"]
        self.assertEqual(len(history), 8)
        self.assertTrue(all(rows[0]["formula"] == "保费按基础金额乘以五计算。" for rows in history.values()))
        self.assertTrue(all(row["formula"] == "保费按基础金额乘以二计算。" for row in
            result["repository_inventory"]["findings"] if row["disposition"] == "included"))

    def test_seventy_formulas_complete_review_in_persisted_batches_without_rechecking_finished_parts(self):
        self.prepare({"many.cbl": program("MANY-ENTRY", "\n".join(
            f"ADD {number + 1} TO PREMIUM-AMT." for number in range(70)))})
        reviewed_parts, reviewed_candidates = set(), set()
        calls = {"analysis": 0, "review": 0}
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            if payload.get("schema") == "semantic-answer-review/v1":
                key = (payload["answer_sha256"], payload["evidence_sha256"])
                self.assertNotIn(key, reviewed_parts)
                reviewed_parts.add(key)
                calls["review"] += 1
                evidence = payload["evidence"][0]
                response = {"answer_sha256": payload["answer_sha256"], "evidence_sha256": payload["evidence_sha256"],
                    "reviews": [{"unit_id": unit["unit_id"], "verdict": "supported", "reason": "Offline protocol fixture.",
                        "evidence": [{"reference_id": evidence["reference_id"], "quote": evidence["text"].splitlines()[0]}]}
                        for unit in payload["units"]], "omissions": []}
            else:
                items = payload["repository_inventory"]["items"]
                ids = {row["candidate_id"] for row in items}
                self.assertTrue(items)
                self.assertFalse(ids & reviewed_candidates)
                reviewed_candidates.update(ids)
                calls["analysis"] += 1
                pages = [p for bundle in payload["source_context"] for p in bundle["pages"]]
                response = {"answer": "业务清点保留每个实际计算位置与适用条件。", "inventory_findings": self.findings(items, pages)}
            return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": json.dumps(response)}, "finish_reason": "stop"}]}))
        policy = AgentPolicy(max_model_requests=3, max_answer_revisions=0)
        result = None
        counts = []
        for _ in range(8):
            options = {"continue_investigation": True, "resume_run_id": result["run_id"]} if result else {}
            result = run_business_chat(QUESTION, self.database, self.source, self.config, transport=transport,
                allow_network=False, framework_reference_path="", policy=policy, conversation_id="many-formula-review",
                **options)["agent_result"]
            counts.append(len(reviewed_parts))
            if result["status"] == "ANALYZED":
                break
        self.assertEqual(result["status"], "ANALYZED", result["diagnostics"])
        self.assertEqual(calls["analysis"], 3)
        self.assertGreater(calls["review"], 2)
        self.assertEqual(len(reviewed_candidates), 71)
        self.assertEqual(len([r for r in result["repository_inventory"]["findings"] if r["disposition"] == "included"]), 70)
        self.assertTrue(result["answer_completion"]["semantic_review"]["coverage"]["complete"])
        for line in range(10, 80):
            self.assertIn(f"many.cbl:{line}-{line}", result["answer"])
        self.assertEqual(counts, sorted(counts))

    def test_unstructured_claim_of_global_completion_cannot_finish(self):
        self.prepare({"entry.cbl": program("ENTRY", "COMPUTE PREMIUM-AMT = BASIS * FACTOR.")})
        def transport(request):
            return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": "这是全部保费计算位置，已经完整完成。"}, "finish_reason": "stop"}]}))
        result = run_business_chat(QUESTION, self.database, self.source, self.config, transport=transport,
            allow_network=False, framework_reference_path="", policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0))["agent_result"]
        self.assertNotEqual(result["status"], "ANALYZED")
        self.assertFalse(result["repository_inventory"]["complete"])
        self.assertGreater(result["repository_inventory"]["pending_count"], 0)
        self.assertNotIn("已经完整完成", result["answer"])


if __name__ == "__main__":
    unittest.main()
