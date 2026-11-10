"""Large inventory oracle controls; pipeline acceptance uses the same gold.

These tests validate the independently specified 10,001-file benchmark and the
oracle's ability to reject an incomplete export. They do not claim a model has
analyzed those files; application/store acceptance is checked separately.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).resolve().parents[1])]
from large_repository_fixture import create_large_fixture, evaluate_delivery, read_gold


def exercise_local_store(database, source, snapshot, *, progress=None):
    """Drive real inventory batches from actual source, without reading gold.

    This controls the interpretation response only. Strict independent model
    review is deliberately left pending and must not be reported complete.
    """
    from repository_inventory import (prepare_inventory, inventory_batch, inventory_checkpoint,
        accept_inventory_response, persist_inventory, set_inventory_requirements)
    from repository_inventory_store import InventoryStore
    question = "列出系统所有计算所有premium formula 的地方"
    state = prepare_inventory(database, snapshot, question)
    set_inventory_requirements(state, "zh-CN", "")
    seen, read_paths, checkpoints, restored, first_page, first_handle = set(), set(), [], [], None, None
    batches = 0
    while batch := inventory_batch(state):
        identifiers = [row["candidate_id"] for row in batch]
        if seen.intersection(identifiers):
            raise AssertionError("Completed source/formula items repeated after a checkpoint")
        pages, page_by_path = [], {}
        for path in dict.fromkeys(row["relative_path"] for row in batch):
            raw = (Path(source) / path).read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            lines = raw.decode("utf-8").splitlines()
            page = {"evidence_id": "ev_scale_" + hashlib.sha256((path + digest).encode()).hexdigest()[:24],
                "relative_path": path, "source_sha256": digest, "source_text": "\n".join(lines),
                "start_line": 1, "end_line": len(lines), "span_truncated": False}
            pages.append(page)
            page_by_path[path] = page
            read_paths.add(path)
        findings = []
        for item in batch:
            page = page_by_path[item["relative_path"]]
            row = {"candidate_id": item["candidate_id"], "evidence_ids": [page["evidence_id"]]}
            if item["kind"] == "source_review":
                row.update(disposition="reviewed", summary="Reviewed the complete supplied source window and its arithmetic role.")
            else:
                lines = page["source_text"].splitlines()
                labels = [match.group(1) for line in lines[:item["start_line"]]
                          if (match := re.search(r"Business calculation:\s*(.+)", line))]
                if not labels:
                    raise AssertionError("Synthetic reviewer has no supplied business-role label")
                row.update(disposition="included", summary=labels[-1] + " determines the charge for this business entry.",
                    formula="\n".join(lines[item["start_line"] - 1:item["end_line"]]).strip(),
                    conditions="Applies when the shown paragraph executes; actual runtime inputs remain unverified.")
            findings.append(row)
        response = json.dumps({"answer": "This batch preserves its original-source business calculations.",
                               "inventory_findings": findings}, ensure_ascii=False)
        accepted = accept_inventory_response(state, response, pages, requested_items=batch)
        if accepted != len(batch):
            raise AssertionError(f"Valid supplied batch did not advance: {accepted}/{len(batch)}")
        persist_inventory(state, identifiers, pages, run_id="neutral-scale-audit",
            update_key=hashlib.sha256(response.encode()).hexdigest())
        batches += 1
        seen.update(identifiers)
        saved = json.loads(json.dumps(inventory_checkpoint(state)))
        checkpoints.append(len(json.dumps(saved).encode()))
        if first_page is None:
            first_handle = state["store_handle"]
            first_page = InventoryStore(database).page(first_handle, page_size=100)
        for threshold in (16, 10000, 18000):
            if len(seen) >= threshold and threshold not in restored:
                state = prepare_inventory(database, snapshot, question, saved=saved)
                restored.append(threshold)
        if progress and batches % 100 == 0:
            progress({"batches": batches, "reviewed_items": len(seen), "source_files_read": len(read_paths)})
    store = InventoryStore(database)
    if store.page(first_handle, page_size=100) != first_page:
        raise AssertionError("Continuing mutated an already published older handle")
    return {"state": state, "store": store, "first_handle": first_handle,
        "statistics": {"batches": batches, "unique_review_items": len(seen), "source_files_read": len(read_paths),
            "restores": len(restored), "maximum_checkpoint_bytes": max(checkpoints),
            "catalog_builds": state["catalog_builds"], "older_handle_unchanged": True,
            "strict_model_review_executed": False}}


class LargeRepositoryOracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.manifest = create_large_fixture(cls.temp.name, 10001)
        cls.gold = list(read_gold(cls.manifest["gold"]))

    def delivered(self):
        return [{**row, "disposition": "included", "summary": row["business_label"]}
                for row in self.gold]

    def test_authored_10001_roots_and_positions_match_original_bytes(self):
        source = Path(self.manifest["source"])
        self.assertEqual(len(list(source.iterdir())), 10001)
        self.assertEqual(len({row["program_id"] for row in self.gold}), 10001)
        self.assertEqual(len({row["path"] for row in self.gold}), 10001)
        for row in self.gold:
            raw = (source / row["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), row["source_sha256"])
            self.assertEqual(raw.decode().splitlines()[row["start_line"] - 1], row["formula"])
        self.assertNotIn("premium", (source / "unit-05000.cbl").read_text().lower())
        self.assertTrue(evaluate_delivery(self.manifest["gold"], self.delivered())["complete"])

    def test_equal_counts_cannot_hide_a_missing_middle_and_duplicate_neighbor(self):
        rows = self.delivered()
        rows[5000] = deepcopy(rows[5001])
        result = evaluate_delivery(self.manifest["gold"], iter(rows))
        self.assertEqual(len(rows), 10001)
        self.assertEqual(result["missing_count"], 1)
        self.assertEqual(result["duplicate_count"], 1)
        self.assertFalse(result["first_middle_last_preserved"])
        self.assertFalse(result["complete"])

    def test_directory_only_or_wrong_source_export_is_not_complete_business_delivery(self):
        rows = self.delivered()
        for row in rows:
            row.pop("formula")
        result = evaluate_delivery(self.manifest["gold"], rows)
        self.assertEqual(result["unique_delivered_positions"], 10001)
        self.assertEqual(result["invalid_business_rows"], 10001)
        self.assertFalse(result["complete"])
        rows = self.delivered()
        rows[-1]["source_sha256"] = "0" * 64
        self.assertEqual(evaluate_delivery(self.manifest["gold"], rows)["invalid_business_rows"], 1)
        rows[-1].pop("source_sha256")
        self.assertEqual(evaluate_delivery(self.manifest["gold"], rows)["invalid_business_rows"], 1)


class LargeRepositoryRuntimeTests(unittest.TestCase):
    """Actual production indexing and material traversal, with independent gold."""
    @classmethod
    def setUpClass(cls):
        from business_index import build_business_index
        from online_material_export import export_material
        from repository_discovery import ensure_repository_search
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.manifest = create_large_fixture(cls.root, 10001)
        cls.gold = list(read_gold(cls.manifest["gold"]))
        cls.source = Path(cls.manifest["source"])
        cls.database = cls.root / "index.sqlite"
        cls.report = build_business_index(cls.source, cls.database, source_format="free", verify_content=True,
                                         framework_reference_path="", quiet=True)
        ensure_repository_search(cls.database, cls.source)
        cls.material = cls.root / "material"
        cls.material_manifest = export_material(cls.database, cls.source, cls.material,
            repository_id="example/large-calculation-source", commit="d" * 40, build_report=cls.report)

    def test_real_index_and_exported_directory_cover_all_10001_roots_exactly_once(self):
        import sqlite3
        from online_material_query import MaterialQuery
        from online_material_reader import DirectoryObjectStore, MaterialReader
        expected = {(row["path"], row["start_line"], row["end_line"], row["source_sha256"]) for row in self.gold}
        with sqlite3.connect(self.database) as db:
            indexed = list(db.execute("SELECT o.relative_path,o.start_line,o.end_line,o.source_sha256 "
                "FROM business_rule_occurrences o JOIN business_rules b USING(rule_id) "
                "WHERE b.rule_kind='COMPUTE'"))
        self.assertEqual(len(indexed), 10001)
        self.assertEqual(set(indexed), expected)
        query = MaterialQuery(MaterialReader(DirectoryObjectStore(self.material)))
        observed, cursors, cursor = {}, set(), None
        while True:
            page = query.inventory(100, cursor=cursor)
            self.assertEqual(page["expected_files"], 10001)
            for row in page["files"]:
                self.assertNotIn(row["relative_path"], observed, "Directory pages repeated a file")
                observed[row["relative_path"]] = row["sha256"]
            if page["complete"]:
                self.assertIsNone(page["next_cursor"])
                break
            cursor = page["next_cursor"]
            marker = repr(sorted(cursor.items()))
            self.assertNotIn(marker, cursors, "Directory cursor failed to advance")
            cursors.add(marker)
        self.assertEqual(observed, {row["path"]: row["source_sha256"] for row in self.gold})
        for ordinal in self.manifest["sentinel_ordinals"]:
            row = self.gold[ordinal]
            candidates = query.arithmetic_candidates(row["path"], limit=100)
            self.assertEqual(candidates["total_matches"], 1)
            self.assertEqual((candidates["candidates"][0]["start_line"], candidates["candidates"][0]["end_line"]),
                             (row["start_line"], row["end_line"]))

    def test_explicit_single_file_question_does_not_expand_to_10001_roots(self):
        from business_map import build_business_map
        result = build_business_map(self.database, self.source, "unit-05000.cbl 的保费公式有哪些？")
        self.assertFalse(result.get("repository_inventory_requested"))
        self.assertEqual(result["direct_paths"], ["unit-05000.cbl"])
        self.assertEqual(result["selected_paths"], ["unit-05000.cbl"])

    def test_local_10001_file_batches_resume_and_publish_every_formula_without_large_checkpoints(self):
        import sqlite3
        from unittest.mock import patch
        real_connect, catalog_scans = sqlite3.connect, []
        def connect(*args, **kwargs):
            db = real_connect(*args, **kwargs)
            db.set_trace_callback(lambda sql: catalog_scans.append(sql) if
                "SELECT RELATIVE_PATH,SHA256,LINE_COUNT FROM SOURCE_FILES ORDER BY RELATIVE_PATH" in sql.upper() else None)
            return db
        with patch("sqlite3.connect", side_effect=connect):
            exercised = exercise_local_store(self.database, self.source, self.report["snapshot_id"])
        self.assertEqual(len(catalog_scans), 1, "Restoring a fixed inventory must not rescan its source directory")
        state, store, stats = exercised["state"], exercised["store"], exercised["statistics"]
        self.assertEqual(stats["unique_review_items"], 20002)
        self.assertEqual(stats["source_files_read"], 10001)
        self.assertEqual(stats["restores"], 3)
        self.assertLess(stats["maximum_checkpoint_bytes"], 4096)
        self.assertEqual(stats["catalog_builds"], 1)
        records, cursor, seen_cursors = [], 0, set()
        while True:
            page = store.page(state["store_handle"], cursor=cursor, page_size=73)
            self.assertEqual(page["total"], 10001)
            self.assertFalse(page["complete"], "Accepted synthetic interpretations cannot replace independent strict review")
            records.extend(page["rows"])
            if page["next_cursor"] is None:
                break
            cursor = page["next_cursor"]
            self.assertNotIn(cursor, seen_cursors)
            seen_cursors.add(cursor)
        self.assertTrue(evaluate_delivery(self.manifest["gold"], records)["complete"])
        exported = store.export(state["store_handle"])
        rows = [json.loads(line) for line in exported["jsonl"].splitlines()]
        formulas = [row for row in rows if row.get("record_type") == "formula" and row.get("disposition") == "included"]
        self.assertTrue(evaluate_delivery(self.manifest["gold"], formulas)["complete"])
        self.assertTrue(any(row.get("record_type") == "business_narrative" for row in rows))
        self.assertEqual(exported["snapshot_id"], self.report["snapshot_id"])

    def test_online_10001_file_reviews_restore_and_deliver_complete_paged_business_inventory(self):
        from large_repository_online_driver import exercise_online_store
        exercised = exercise_online_store(self.material, self.source, self.root / "online-inventory")
        result, stats, publication = exercised["result"], exercised["statistics"], exercised["publication"]
        self.assertEqual(result["status"], "answered")
        self.assertTrue(result["coverage_ledger"]["complete"])
        self.assertFalse(result["coverage_ledger"]["full_repository_or_runtime_proof"])
        self.assertEqual(stats["inventory_files"], 10001)
        self.assertEqual(stats["reviewed_files"], 10001)
        self.assertGreaterEqual(stats["restores"], 40)
        self.assertLess(stats["maximum_checkpoint_bytes"], 100000)
        self.assertEqual(stats["final_model_requests"], 2)
        self.assertTrue(stats["first_final_request_recalled_source"])
        self.assertEqual(len(stats["recall_requests"]), 1)
        self.assertEqual(stats["final_answer_citations"], stats["recall_requests"])
        self.assertEqual(stats["source_reads_before_delivery"], 10001)
        self.assertTrue(stats["older_handle_unchanged"])
        self.assertGreater(stats["durable_completed_actions"], 40000)
        self.assertTrue(stats["initial_completed_action_retained"])
        rows, offset, seen_offsets = [], 0, set()
        while True:
            page = publication.page(offset, 73)
            self.assertTrue(page["complete"])
            self.assertEqual(page["total"], 10001)
            rows.extend(page["findings"])
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
            self.assertNotIn(offset, seen_offsets)
            seen_offsets.add(offset)
        self.assertTrue(evaluate_delivery(self.manifest["gold"], rows)["complete"])
        exported = publication.export_data()
        self.assertTrue(evaluate_delivery(self.manifest["gold"], exported["findings"])["complete"])
        self.assertEqual(len(exported["source_reviews"]), 10001)
        self.assertEqual({row["path"] for row in exported["source_reviews"]}, {row["path"] for row in self.gold})
        self.assertIn("business roles", exported["answer"])
        self.assertEqual(exported["revision"], self.material_manifest["revision"])


if __name__ == "__main__":
    unittest.main()
