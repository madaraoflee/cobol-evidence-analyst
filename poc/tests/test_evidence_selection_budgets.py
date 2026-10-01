from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_index import build_business_index
from business_chat import _fit_request
from company_api import CompanyAPIConfig
from evidence_context import EvidenceContext, EvidenceGroup, Observation, SourceRef
from repository_discovery import ensure_repository_search
from semantic_scope import build_business_evidence, prepare_semantic_scope
from source_session import QuestionSourceSession


def page(identifier, text, role=None, group=None, path="entry.cbl", line=1):
    value = {"evidence_id": identifier, "relative_path": path, "start_line": line,
             "end_line": line, "source_sha256": "a" * 64, "source_text": text}
    if role:
        value["semantic_roles"] = [role]
    if group:
        value["group_id"] = group
    return value


class EvidenceSelectionBudgetTests(unittest.TestCase):
    def test_core_group_is_preserved_across_paths_before_unrelated_pages(self):
        pool = EvidenceContext()
        core = [page("result", "COMPUTE VALUE", "result", "core", "z.cbl"),
                page("condition", "IF ENABLED", "condition", "core", "m.cbl"),
                page("input", "MOVE INPUT", "input", "core", "n.cbl")]
        pool.accept({"pages": core}, "business_context")
        pool.accept({"pages": [page("noise", "C" * 30, path="a.cbl")]}, "search")
        selected = pool.selected_pages(sum(len(item["source_text"]) for item in core))
        self.assertEqual({item["evidence_id"] for item in selected}, {"result", "condition", "input"})
        self.assertEqual(pool.selection_trim_events[0]["evidence_id"], "noise")

    def test_new_search_target_displaces_old_material_without_slicing(self):
        pool = EvidenceContext()
        old = page("old", "O" * 2414, "result", "old", line=1)
        target = page("target", "T" * 2449, line=90)
        pool.accept({"pages": [old]}, "business_context")
        pool.accept({"pages": [target]}, "search")
        selected = pool.selected_pages(3600, priority_targets=[{"relative_path": "entry.cbl", "line": 90}])
        self.assertEqual([item["evidence_id"] for item in selected], ["target"])
        self.assertEqual(selected[0]["source_text"], target["source_text"])
        self.assertEqual(selected[0]["source_sha256"], target["source_sha256"])
        self.assertEqual(pool.selection_trim_events[0]["reason"], "source_characters")
        self.assertEqual(pool.bundle(selected)[0]["open_frontier"][0]["evidence_id"], "old")

    def test_group_observations_supply_core_priority_for_legacy_pages(self):
        pool = EvidenceContext()
        values = [page("result", "R" * 10), page("condition", "C" * 10, line=2),
                  page("input", "I" * 10, line=3)]
        pool.accept({"pages": values}, "search")
        pool.accept({"pages": [page("noise", "N" * 20, line=4, role="declaration")]}, "search")
        observations = [Observation(item["evidence_id"], role, [SourceRef("source", "entry.cbl", "a" * 64,
            item["start_line"], item["end_line"], "b" * 64, item["evidence_id"])])
            for item, role in zip(values, ("result", "condition", "input"))]
        group = EvidenceGroup("core", {"relative_path": "entry.cbl", "line": 1}, "explain", observations)
        selected = pool.selected_pages(30, evidence_groups=[group])
        self.assertEqual({item["evidence_id"] for item in selected}, {"result", "condition", "input"})

    def test_duplicate_id_merges_roles_reasons_and_groups(self):
        pool = EvidenceContext()
        first = page("same", "COMPUTE VALUE", "related_statement", "early")
        first["selection_reasons"] = ["question_match"]
        pool.accept({"pages": [first]}, "search")
        later = page("same", "COMPUTE VALUE", "result", "focused")
        later["selection_reasons"] = ["business_rule"]
        self.assertEqual(pool.accept({"pages": [later]}, "business_context"), [])
        stored = pool.pages["same"]
        self.assertEqual(set(stored["semantic_roles"]), {"result", "related_statement"})
        self.assertEqual(set(stored["selection_reasons"]), {"question_match", "business_rule"})
        self.assertEqual(set(stored["group_ids"]), {"early", "focused"})
        self.assertEqual(stored["source_text"], first["source_text"])

    def test_conflicting_same_id_source_is_rejected(self):
        pool = EvidenceContext()
        pool.accept({"pages": [page("same", "original")]}, "search")
        with self.assertRaisesRegex(ValueError, "EVIDENCE_ID_CONTENT_MISMATCH"):
            pool.accept({"pages": [page("same", "changed", "result")]}, "business_context")
        self.assertEqual(pool.pages["same"]["source_text"], "original")

    def test_insufficient_group_budget_records_all_missing_members(self):
        pool = EvidenceContext()
        core = [page("result", "R" * 20, "result", "core"),
                page("condition", "C" * 20, "condition", "core", line=2),
                page("input", "I" * 20, "input", "core", line=3)]
        pool.accept({"pages": core}, "business_context")
        self.assertEqual(pool.selected_pages(59), [])
        self.assertEqual({item["evidence_id"] for item in pool.selection_frontier}, {"result", "condition", "input"})
        self.assertEqual(sum(item["dropped_characters"] for item in pool.selection_trim_events), 60)

    def test_overlapping_old_group_does_not_split_new_anchor_group(self):
        pool = EvidenceContext()
        pool.accept({"pages": [page("shared", "R" * 20, "result", "old"),
                               page("old-input", "O" * 100, "input", "old", line=2)]}, "business_context")
        pool.accept({"pages": [page("shared", "R" * 20, "result", "new"),
                               page("condition", "C" * 10, "condition", "new", line=3),
                               page("input", "I" * 10, "input", "new", line=4)]}, "business_context")
        selected = pool.selected_pages(40, priority_targets=["shared"])
        self.assertEqual({item["evidence_id"] for item in selected}, {"shared", "condition", "input"})

    def test_visibility_frontier_clears_after_required_evidence_is_restored(self):
        payload = {"source_context": [{"pages": [], "call_chain": {}, "outline": []}],
                   "evidence_groups": [{"group_id": "core", "required_evidence_ids": ["result"],
                                        "open_frontier": []}]}
        EvidenceContext.reconcile_payload(payload)
        self.assertFalse(payload["evidence_groups"][0]["complete_text_supplied"])
        payload["source_context"][0]["pages"] = [page("result", "VALUE")]
        EvidenceContext.reconcile_payload(payload)
        self.assertTrue(payload["evidence_groups"][0]["complete_text_supplied"])

    def test_many_source_omissions_have_bounded_payload_and_complete_local_log(self):
        pool = EvidenceContext()
        pool.accept({"pages": [page("result", "COMPUTE VALUE", "result", "core")]}, "business_context")
        noise = [page(f"noise-{index}", "N" * 1000, path=f"member-{index}.cbl") for index in range(500)]
        pool.accept({"pages": noise}, "search")
        selected = pool.selected_pages(512)
        self.assertEqual(len(pool.selection_trim_events), 500)
        bundle = pool.bundle(selected)[0]
        self.assertEqual(len(bundle["open_frontier"]), 24)
        self.assertEqual(bundle["open_frontier_count"], 500)
        self.assertEqual(bundle["open_frontier_omitted"], 476)
        payload = {"question": "Explain VALUE", "repository": {}, "business_map": {},
                   "source_context": [bundle], "framework_references": [], "evidence_groups": []}
        config = CompanyAPIConfig("https://gateway.example.invalid/v1", "offline-model", api_key="offline-only")
        _, size = _fit_request(config, payload, [], AgentPolicy(max_request_bytes=32768))
        self.assertLessEqual(size, 32768)
        self.assertEqual([item["evidence_id"] for item in payload["source_context"][0]["pages"]], ["result"])

    def test_missing_group_counts_remain_complete_with_bounded_gap_preview(self):
        required = [f"required-{index}" for index in range(100)]
        payload = {"source_context": [{"pages": [], "call_chain": {}, "outline": []}],
                   "evidence_groups": [{"group_id": "core", "required_evidence_ids": required,
                                        "open_frontier": []}]}
        EvidenceContext.reconcile_payload(payload)
        group = payload["evidence_groups"][0]
        self.assertEqual(group["missing_evidence_count"], 100)
        self.assertEqual(group["omitted_visibility_frontier_count"], 76)
        self.assertEqual(len(group["open_frontier"]), 24)
        self.assertFalse(group["complete_text_supplied"])

    def test_late_input_candidate_survives_many_earlier_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            lines = ["IDENTIFICATION DIVISION.", "PROGRAM-ID. ENTRY.", "DATA DIVISION.",
                     "WORKING-STORAGE SECTION.", "01 SOURCE-VALUE PIC 9(5).",
                     "01 RESULT-VALUE PIC 9(6).", "01 ENABLED PIC X.",
                     "PROCEDURE DIVISION.", "MAIN."]
            lines += [f"MOVE {number} TO SOURCE-VALUE." for number in range(15)]
            lines += ["*> neutral explanation" for _ in range(40)]
            late_line = len(lines) + 1
            lines += ["MOVE 777 TO SOURCE-VALUE."]
            lines += ["*> later neutral explanation" for _ in range(24)]
            condition_line = len(lines) + 1
            lines += ["IF ENABLED = 'Y'",
                      "COMPUTE RESULT-VALUE = SOURCE-VALUE * 3", "END-IF.", "GOBACK."]
            anchor_line = condition_line + 1
            (source / "entry.cbl").write_text("\n".join(lines) + "\n")
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", verify_content=True)
            ensure_repository_search(database, source)
            with QuestionSourceSession(database, source) as session:
                with prepare_semantic_scope(database, session,
                        anchors=[{"relative_path": "entry.cbl", "line": anchor_line}], policy=AgentPolicy()) as scope:
                    group = build_business_evidence(scope, session,
                        anchor={"relative_path": "entry.cbl", "line": anchor_line}, policy=AgentPolicy())
            supplied = {item["start_line"]: item for item in group.supplied_locations}
            self.assertIn(late_line, supplied)
            self.assertIn("input", supplied[late_line]["semantic_roles"])
            self.assertEqual(supplied[late_line]["interpretation_basis"], "same_symbol_candidate_not_reaching_definition")
            self.assertIn("result", supplied[anchor_line]["semantic_roles"])
            self.assertIn("condition", supplied[condition_line]["semantic_roles"])
            self.assertTrue(any(item["reason"] == "same_symbol_candidates_limited" and item["omitted_count"] > 0
                                for item in group.open_frontier))


if __name__ == "__main__":
    unittest.main()
