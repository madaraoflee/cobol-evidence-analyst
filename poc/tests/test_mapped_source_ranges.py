"""Source-map compaction preserves physical provenance and bounded spans."""

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from semantic_service import FactStore
from statement_parser import SourceSpan
from structural_index import build_structural_index


class MappedSourceRangeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mapped-source-range-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "facts.sqlite"
        self.root.joinpath("host.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. HOST.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 AMOUNT PIC 9(5).\n"
            "PROCEDURE DIVISION.\nMAIN.\nMOVE 2 TO AMOUNT.\nGOBACK.\n", encoding="utf-8")
        self.root.joinpath("shared.cpy").write_text("01 BASIS PIC 9(5) VALUE 2.\n", encoding="utf-8")
        build_structural_index(self.root, self.database, source_format="free")
        self.source_hash = hashlib.sha256(self.root.joinpath("shared.cpy").read_bytes()).hexdigest()

    def mapping(self, line, end=None, *, path="shared.cpy", source_hash=None, chain=None, trace=None):
        return {"origin": {"relative_path": path, "source_hash": source_hash or self.source_hash,
                           "line": line, "end_line": end or line},
                "include_chain": chain or [], "replacement_trace": trace or []}

    def refs(self, mapping):
        store = FactStore(self.database, self.root, source_format="free", source_map={"host.cbl": mapping})
        return store.source_ref("host.cbl", SourceSpan(1, len(mapping), 1, 20, 0, 100))

    def test_adjacent_and_overlapping_origins_form_one_exact_contiguous_range(self):
        chain = [{"relative_path": "host.cbl", "line": 5, "target": "shared.cpy"}]
        trace = [{"from": "BASIS", "to": "AMOUNT"}]
        refs = self.refs([self.mapping(10, 11, chain=chain, trace=trace),
                          self.mapping(11, 12, chain=chain, trace=trace),
                          self.mapping(13, chain=chain, trace=trace)])
        self.assertEqual(len(refs), 1)
        self.assertEqual((refs[0]["start_line"], refs[0]["end_line"]), (10, 13))
        self.assertEqual(refs[0]["source_sha256"], self.source_hash)
        self.assertEqual(refs[0]["include_chain"], chain)
        self.assertEqual(refs[0]["replacement_trace"], trace)

    def test_path_hash_copy_instance_and_replacement_differences_stay_separate(self):
        first = self.mapping(1, chain=[{"line": 5}], trace=[{"to": "FIRST"}])
        for second in (self.mapping(2, path="other.cpy", chain=[{"line": 5}], trace=[{"to": "FIRST"}]),
                       self.mapping(2, source_hash="different-version", chain=[{"line": 5}], trace=[{"to": "FIRST"}]),
                       self.mapping(2, chain=[{"line": 8}], trace=[{"to": "FIRST"}]),
                       self.mapping(2, chain=[{"line": 5}], trace=[{"to": "SECOND"}])):
            with self.subTest(second=second):
                refs = self.refs([first, second])
                self.assertEqual(len(refs), 2)
                self.assertEqual([(ref["start_line"], ref["end_line"]) for ref in refs], [(1, 1), (2, 2)])

    def test_missing_physical_lines_are_not_invented_by_coalescing(self):
        refs = self.refs([self.mapping(1), self.mapping(3)])
        self.assertEqual([(ref["start_line"], ref["end_line"]) for ref in refs], [(1, 1), (3, 3)])

    def test_long_contiguous_span_stays_long_for_downstream_budget_checks(self):
        refs = self.refs([self.mapping(line) for line in range(1, 401)])
        self.assertEqual(len(refs), 1)
        self.assertEqual((refs[0]["start_line"], refs[0]["end_line"]), (1, 400))
        self.assertGreaterEqual(refs[0]["end_line"] - refs[0]["start_line"], 96)


if __name__ == "__main__":
    unittest.main()
