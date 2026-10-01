from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
from business_map import _outgoing_paths, build_business_map
from repository_discovery import ensure_repository_search


class BusinessFileImpactRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"

    def write(self, relative, name, body, declarations="01 WORK-AMOUNT PIC 9(9)."):
        (self.source / relative).write_text(
            f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\n"
            f"DATA DIVISION.\nWORKING-STORAGE SECTION.\n{declarations}\n"
            "PROCEDURE DIVISION.\nMAIN.\n" + body + "\nGOBACK.\n", encoding="utf-8")

    def build(self):
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)

    def test_flow_impact_keeps_callers_and_reads_confirmed_outgoing_write_candidates(self):
        self.write("debit.cbl", "DEBITFLOW", "*> 扣减流程\nPERFORM APPLY-DEBIT.\n"
                   "CALL 'AUDITSTORE'.\nCALL 'MISSINGPOST'.\nAPPLY-DEBIT.\n"
                   "SUBTRACT 10 FROM WORK-AMOUNT.")
        self.write("audit.cbl", "AUDITSTORE", "WRITE AUDIT-REC FROM WORK-AMOUNT.")
        self.write("batch.cbl", "BATCHENTRY", "CALL 'DEBITFLOW'.\nCALL 'UNRELATEDWORK'.")
        self.write("unrelated.cbl", "UNRELATEDWORK", "WRITE UNRELATED-REC.")
        self.build()
        program_usage = build_business_map(self.database, self.source, "哪些程序用到 DEBITFLOW？")
        self.assertEqual(set(program_usage["selected_paths"]), {"debit.cbl", "batch.cbl"})
        self.assertEqual(program_usage["outgoing_dependency_paths"], [])
        for question in ("DEBITFLOW 扣减流程影响哪些 lf 和哪些 field？",
                         "扣减流程影响哪些 lf 和哪些 field？",
                         "DEBITFLOW affects which files and fields?"):
            with self.subTest(question=question):
                result = build_business_map(self.database, self.source, question)
                self.assertEqual(result["intent"], "impact")
                self.assertEqual(set(result["selected_paths"]), {"debit.cbl", "audit.cbl", "batch.cbl"})
                self.assertEqual(result["outgoing_dependency_paths"], ["audit.cbl"])
                self.assertEqual(result["outgoing_dependency_frontier"], [])
                self.assertTrue(any(rule["relative_path"] == "audit.cbl" and rule["rule_kind"] == "WRITE"
                                    for rule in result["rule_leads"]))
                self.assertTrue(any(edge["target_name"] == "AUDITSTORE" and edge["resolution"] == "confirmed"
                                    for edge in result["relations"]))
                self.assertTrue(any(edge["target_name"] == "MISSINGPOST" and edge["resolution"] == "unresolved"
                                    for edge in result["relations"]))

    @staticmethod
    def edge(caller, target, *, kind="CALLS", resolution="confirmed"):
        return {"caller_path": caller, "target_path": target,
                "relation_type": kind, "resolution": resolution}

    def test_outgoing_candidates_stop_at_depth_and_handle_cycles_without_runtime_claims(self):
        edges = [self.edge(f"file-{index}.cbl", f"file-{index + 1}.cbl") for index in range(6)]
        edges.extend([self.edge("file-2.cbl", "file-0.cbl"),
                      self.edge("file-0.cbl", "record.cpy", kind="INCLUDES_COPY"),
                      self.edge("file-0.cbl", "unknown.cbl", resolution="unresolved"),
                      self.edge("file-0.cbl", "runtime.cbl", kind="CALL_TARGET_FROM")])
        paths, frontier = _outgoing_paths(edges, {"file-0.cbl"}, None)
        self.assertEqual(set(paths), {*(f"file-{index}.cbl" for index in range(5)), "record.cpy"})
        self.assertEqual(frontier, [{"reason": "outgoing_dependency_depth_budget",
                                    "relative_path": "file-5.cbl", "caller_path": "file-4.cbl"}])

    def test_outgoing_fanout_is_bounded_and_reports_omitted_candidates(self):
        edges = [self.edge("root.cbl", f"leaf-{index:02d}.cbl") for index in range(40)]
        records = [_outgoing_paths(order, {"root.cbl"}, None) for order in (edges, list(reversed(edges)))]
        self.assertEqual(records[0], records[1])
        paths, frontier = records[0]
        self.assertEqual(len(paths), 32)
        self.assertEqual(len(frontier), 9)
        self.assertEqual({gap["reason"] for gap in frontier}, {"outgoing_dependency_path_budget"})
        self.assertEqual(set(paths) & {gap["relative_path"] for gap in frontier}, set())
        paths, frontier = _outgoing_paths([
            self.edge("root.cbl", f"leaf-{index:03d}.cbl") for index in range(100)], {"root.cbl"}, None)
        self.assertEqual(len(paths), 32)
        self.assertEqual(len(frontier), 32)
        self.assertEqual(frontier[-1], {"reason": "outgoing_dependency_frontier_budget", "omitted_count": 38})


if __name__ == "__main__":
    unittest.main()
