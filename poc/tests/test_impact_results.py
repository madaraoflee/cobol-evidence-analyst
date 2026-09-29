from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
from impact_results import impact_jsonl, impact_page, list_impact
from repository_discovery import ensure_repository_search


class ImpactResultsTests(unittest.TestCase):
    def test_static_caller_has_a_path_and_unresolved_target_is_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            (source / "target.cbl").write_text(
                "PROGRAM-ID. TARGET.\nPROCEDURE DIVISION.\nMOVE TARGET-FLAG TO RESULT-FLAG.\n",
                encoding="utf-8")
            (source / "caller.cbl").write_text(
                'PROGRAM-ID. CALLER.\nPROCEDURE DIVISION.\nCALL "TARGET".\nCALL "MISSING".\n',
                encoding="utf-8")
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", verify_content=True)
            ensure_repository_search(database, source)
            result = list_impact(database, "TARGET-FLAG", page_size=20)
            caller = next(row for row in result["rows"] if row["name"] == "CALLER")
            self.assertEqual(caller["match_type"], "static_caller_candidate")
            self.assertEqual(caller["static_relation_path"][0]["target_path"], "target.cbl")
            missing = list_impact(database, "MISSING")
            self.assertEqual(missing["counts"]["external_target"], 1)

    def test_full_list_is_independent_of_preview_and_counts_real_object_types(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            for index in range(650):
                (source / f"process-{index:03d}.cbl").write_text(
                    f"PROGRAM-ID. PROCESS-{index:03d}.\nPROCEDURE DIVISION.\n"
                    "MOVE SHARED-LEVEL TO OUTPUT-LEVEL.\n", encoding="utf-8")
            (source / "multi.cbl").write_text("PROGRAM-ID. FIRST.\nMOVE SHARED-LEVEL TO OUTPUT-LEVEL.\n"
                "PROGRAM-ID. SECOND.\nMOVE SHARED-LEVEL TO OUTPUT-LEVEL.\n", encoding="utf-8")
            (source / "record.cpy").write_text("01 SHARED-LEVEL PIC 9.\n", encoding="utf-8")
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", verify_content=True)
            ensure_repository_search(database, source)
            first = list_impact(database, "SHARED-LEVEL", page_size=20)
            self.assertEqual(len(first["rows"]), 20)
            self.assertEqual(first["counts"]["program"], 652)
            self.assertEqual(first["counts"]["copybook"], 1)
            self.assertEqual(first["total"], 653)
            page = impact_page(database, first["handle"], cursor=600, page_size=100)
            self.assertEqual(len(page["rows"]), 53)
            exported = [json.loads(line) for line in impact_jsonl(database, first["handle"]).splitlines()]
            self.assertEqual(len(exported), first["total"])
            self.assertEqual(len({row["id"] for row in exported}), first["total"])


if __name__ == "__main__":
    unittest.main()
