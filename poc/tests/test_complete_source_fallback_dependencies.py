"""Offline regressions for retaining dependencies during whole-root fallback."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import _prompt_payload, run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from evidence_context import EvidenceContext
from repository_discovery import ensure_repository_search
from source_reading import _identify_page


HEADER = (
    "IDENTIFICATION DIVISION.\nPROGRAM-ID. ROOT-RULE.\n"
    "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
    "01 BASE-AMOUNT PIC 9(5) VALUE 10.\n01 RESULT-AMOUNT PIC 9(6).\n"
    "PROCEDURE DIVISION.\nMAIN.\nMOVE BASE-AMOUNT TO RESULT-AMOUNT.\n"
    "CALL 'DETAIL-RULE' USING RESULT-AMOUNT.\nDISPLAY RESULT-AMOUNT.\nGOBACK.\n"
)
COMMENT = "*> 中性业务原文保留全部条件和金额计算步骤\n"
DETAIL = (
    "IDENTIFICATION DIVISION.\nPROGRAM-ID. DETAIL-RULE.\n"
    "DATA DIVISION.\nLINKAGE SECTION.\n01 RESULT-AMOUNT PIC 9(6).\n"
    "PROCEDURE DIVISION USING RESULT-AMOUNT.\nMAIN.\n"
    "ADD 5 TO RESULT-AMOUNT.\nGOBACK.\n"
)


def source_page(path, text, *, first=1, last=None, whole_text=None, reasons=()):
    lines = text.rstrip("\n").splitlines()
    page = {"relative_path": path, "source_sha256": hashlib.sha256(
        (whole_text or text).encode("utf-8")).hexdigest(), "start_line": first,
        "end_line": last or first + len(lines) - 1, "source_text": "\n".join(lines),
        "span_truncated": False, "include_chain": [], "selection_reasons": list(reasons)}
    page["evidence_id"] = _identify_page(page)
    return page


class CompleteSourceFallbackDependencyTests(unittest.TestCase):
    def test_merged_complete_label_does_not_discard_a_short_dependency(self):
        pool = EvidenceContext()
        text = HEADER + COMMENT * 4300
        root = source_page("root.cbl", text, reasons=["complete_working_set"])
        detail = source_page("detail.cbl", DETAIL, reasons=["question_match"])
        pool.accept({"pages": [detail]}, "search")
        pool.accept({"pages": [root, {**detail, "selection_reasons": ["complete_working_set"]}],
                     "metadata": {"root_paths": ["root.cbl"], "status": "supplied"}}, "complete_working_set")
        excerpt = source_page("root.cbl", HEADER, whole_text=text, reasons=["question_match"])
        pool.accept({"pages": [excerpt]}, "search")
        selected = pool.fallback_pages(36000, priority_targets=[{"relative_path": "root.cbl", "line": 10}])
        by_path = {page["relative_path"]: page for page in selected}
        self.assertEqual(set(by_path), {"root.cbl", "detail.cbl"})
        self.assertEqual(by_path["detail.cbl"]["evidence_id"], detail["evidence_id"])
        self.assertEqual(by_path["detail.cbl"]["source_sha256"], detail["source_sha256"])
        self.assertEqual(by_path["detail.cbl"]["source_text"], DETAIL.rstrip("\n"))
        self.assertNotEqual(by_path["root.cbl"]["evidence_id"], root["evidence_id"])
        self.assertLessEqual(sum(len(page["source_text"]) for page in selected), 36000)
        pool.accept({"pages": [detail]}, "read")
        self.assertIn(detail["evidence_id"], {page["evidence_id"] for page in pool.fallback_pages(36000)})

    def test_all_complete_pool_retains_dependency_and_creates_an_exact_root_anchor(self):
        pool = EvidenceContext()
        root_lines = ["*> neutral source context"] * 500
        root_lines[399] = "CALL 'DETAIL-RULE' USING RESULT-AMOUNT."
        text = "\n".join(root_lines)
        root = source_page("root.cbl", text, reasons=["complete_working_set"])
        detail = source_page("detail.cbl", DETAIL, reasons=["complete_working_set"])
        pool.accept({"pages": [root, detail], "metadata": {
            "root_paths": ["root.cbl"], "status": "supplied"}}, "complete_working_set")
        selected = pool.fallback_pages(36000, priority_targets=[{"relative_path": "root.cbl", "line": 400}])
        by_path = {page["relative_path"]: page for page in selected}
        self.assertEqual(set(by_path), {"root.cbl", "detail.cbl"})
        excerpt = by_path["root.cbl"]
        self.assertLessEqual(excerpt["start_line"], 400)
        self.assertGreaterEqual(excerpt["end_line"], 400)
        self.assertEqual(excerpt["source_text"], "\n".join(root_lines[excerpt["start_line"] - 1:excerpt["end_line"]]))
        self.assertEqual(excerpt["source_sha256"], root["source_sha256"])
        self.assertEqual(excerpt["evidence_id"], _identify_page(excerpt))
        self.assertEqual(by_path["detail.cbl"]["evidence_id"], detail["evidence_id"])

    def test_narrowing_retains_initialization_and_retries_if_anchor_context_is_too_large(self):
        pool = EvidenceContext()
        text = HEADER + COMMENT * 200
        original = source_page("root.cbl", text)
        narrowed = pool.narrow_page(original, 4096,
            priority_targets=[{"relative_path": "root.cbl", "line": 9}])
        self.assertIn("VALUE 10", narrowed["source_text"])
        self.assertIn("CALL 'DETAIL-RULE'", narrowed["source_text"])
        text = "\n".join(["*> " + "N" * 1000] * 20 + ["MOVE 10 TO RESULT-AMOUNT.", "GOBACK."])
        original = source_page("root.cbl", text)
        narrowed = pool.narrow_page(original, 64,
            priority_targets=[{"relative_path": "root.cbl", "line": 21}])
        self.assertEqual(narrowed["start_line"], 21)
        self.assertIn("MOVE 10 TO RESULT-AMOUNT", narrowed["source_text"])
        self.assertEqual(narrowed["evidence_id"], _identify_page(narrowed))

    def test_budget_wire_projection_preserves_canonical_evidence_and_real_gaps(self):
        page = source_page("root.cbl", HEADER)
        manifest = [{"relative_path": "root.cbl", "sha256": page["source_sha256"],
                     "line_count": page["end_line"], "evidence_id": page["evidence_id"]}]
        gap = {"kind": "dependencies", "reason": "runtime_target_unresolved"}
        payload = {"question": "请说明 root.cbl 的流程。", "budget_wire_compact": True,
            "source_context": [{"pages": [page], "working_set": {
                "status": "partial", "physical_complete": False, "closure_complete": False,
                "root_paths": ["root.cbl"], "source_manifest": manifest,
                "transmission_status": "partial", "frontier": [gap]}}],
            "business_map": {"source_identity": {"status": "resolved", "hard_constraint": True}},
            "question_investigation": {"required_items": [{"kind": "business_steps", "status": "SATISFIED",
                "evidence_ids": [page["evidence_id"]]}], "open_gaps": [gap],
                "state": "bounded_partial", "analysis_plan": {"intents": ["flow"]},
                "candidate_cache": {"hit": True}},
            "investigation_budget": {"remaining_model_requests": 2, "reads_per_turn": 3}}
        original = deepcopy(payload)
        projected = _prompt_payload(payload)
        self.assertEqual(payload, original)
        self.assertEqual(projected["question"], payload["question"])
        self.assertEqual(projected["source_context"][0]["pages"], payload["source_context"][0]["pages"])
        self.assertTrue(projected["source_selection"]["source_supplied"])
        working_set = projected["source_context"][0]["working_set"]
        self.assertNotIn("source_manifest", working_set)
        self.assertEqual(working_set["omitted_source_manifest_count"], 1)
        self.assertEqual(working_set["frontier"], [gap])
        self.assertEqual(working_set["transmission_status"], "partial")
        self.assertEqual(projected["question_investigation"]["required_items"],
                         payload["question_investigation"]["required_items"])
        self.assertEqual(projected["question_investigation"]["open_gaps"], [gap])
        self.assertEqual(projected["investigation_budget"], payload["investigation_budget"])
        self.assertNotIn("budget_wire_compact", projected)

    def test_whole_root_transmission_fallback_supplies_the_readable_dependency(self):
        for comments, limit, newline in ((4300, 230000, "\n"), (1000, 32768, "\r\n")):
            with self.subTest(comments=comments, max_request_bytes=limit, newline=repr(newline)), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "source"
                source.mkdir()
                original = {"root.cbl": HEADER + COMMENT * comments, "detail.cbl": DETAIL}
                for path, text in original.items():
                    (source / path).write_bytes(text.replace("\n", newline).encode("utf-8"))
                source_hashes = {path: hashlib.sha256((source / path).read_bytes()).hexdigest()
                                 for path in original}
                database = root / "index.sqlite"
                build_business_index(source, database, source_format="free", framework_reference_path="", quiet=True)
                ensure_repository_search(database, source)
                policy = replace(AgentPolicy(), max_request_bytes=limit)
                requests = []

                def transport(request):
                    self.assertLessEqual(len(request.body), limit)
                    payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
                    requests.append(payload)
                    pages = payload["source_context"][0]["pages"]
                    detail = next((page for page in pages if page["relative_path"] == "detail.cbl"
                                   and "ADD 5 TO RESULT-AMOUNT" in page["source_text"]), None)
                    caller = next((page for page in pages if page["relative_path"] == "root.cbl"
                                   and "CALL 'DETAIL-RULE'" in page["source_text"]), None)
                    if detail and caller:
                        answer = ("主程序先把基础金额 10 移入 RESULT-AMOUNT，再以引用参数调用 DETAIL-RULE；"
                                  "被调用程序将 RESULT-AMOUNT 加 5，返回后显示金额 15。"
                                  f"[{caller['evidence_id']}][{detail['evidence_id']}]")
                    elif len(requests) < 3:
                        answer = json.dumps({"read": [{"relative_path": "detail.cbl", "start_line": 1, "end_line": 9}]})
                    else:
                        answer = "调用前金额为 10；DETAIL-RULE 本轮未提供，无法确认调用后的值。"
                    return TransportResponse(200, json.dumps({"choices": [{"message": {"content": answer},
                        "finish_reason": "stop"}]}, ensure_ascii=False))

                config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model", api_key="offline-only")
                with mock.patch.object(socket, "create_connection", side_effect=AssertionError("Network forbidden")), \
                        mock.patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")), \
                        mock.patch.object(socket.socket, "connect_ex", side_effect=AssertionError("Network forbidden")):
                    result = run_business_chat("请说明 root.cbl 的完整业务流程。", database, source, config,
                        transport=transport, framework_reference_path="", policy=policy)["agent_result"]
                self.assertIn("15", result["answer"])
                self.assertNotIn("本轮未提供", result["answer"])
                self.assertLessEqual(len(requests), 2)
                for payload in requests:
                    pages = payload["source_context"][0]["pages"]
                    self.assertIn("detail.cbl", {page["relative_path"] for page in pages})
                    for page in pages:
                        lines = original[page["relative_path"]].splitlines()
                        self.assertEqual(page["source_text"], "\n".join(lines[page["start_line"] - 1:page["end_line"]]))
                        self.assertEqual(page["source_sha256"], source_hashes[page["relative_path"]])
                        self.assertEqual(page["evidence_id"], _identify_page(page))
                self.assertNotIn("retrieved_source_not_visible", {gap.get("reason") for gap in
                    result["investigation_state"]["question_investigation"]["open_gaps"]})


if __name__ == "__main__":
    unittest.main()
