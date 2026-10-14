"""Large records keep explicit alias semantics without pairwise expansion."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parser_adapter import BuiltinParserAdapter, _regions
from agent_policy import AgentPolicy
from business_index import build_business_index
from benchmark_chat_latency import measure_first_request
from repository_discovery import ensure_repository_search
from semantic_ir import compilation_ir_from_dict, overlapping_region_ids, unbounded_region_ids
from semantic_scope import prepare_semantic_scope
from semantic_service import FactStore, SemanticService
from source_session import QuestionSourceSession
from statement_parser import parse_statements
from structural_index import build_structural_index


def record_source(count, *, body=None, extra=""):
    return ("IDENTIFICATION DIVISION.\nPROGRAM-ID. RECORD-RULE.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 SOURCE-RECORD.\n" + "".join(
                f"05 VALUE-{number:05d} PIC 9(9).\n" for number in range(count)) + extra +
            "PROCEDURE DIVISION.\nMAIN.\n" + (body or "COMPUTE VALUE-00000 = VALUE-00001 * 2.\nGOBACK.\n"))


class SemanticLayoutScaleTests(unittest.TestCase):
    def test_ten_thousand_siblings_use_linear_ir_and_remain_separate(self):
        ir = BuiltinParserAdapter().parse(record_source(10000), relative_path="record.cbl")
        self.assertEqual(len(ir.storage_regions), 10001)
        self.assertEqual(sum(len(region.overlaps) for region in ir.storage_regions), 0)
        encoded = json.dumps(ir.to_dict())
        self.assertLess(len(encoded), 15_000_000)
        restored = compilation_ir_from_dict(json.loads(encoded))
        self.assertEqual(restored.fingerprint, ir.fingerprint)
        by_name = {item.name: item.item_id for item in ir.data_items}
        regions = {region.item_id: region for region in ir.storage_regions}
        leaf = regions[by_name["VALUE-00000"]]
        self.assertEqual(overlapping_region_ids(ir.storage_regions, leaf.region_id),
                         (regions[by_name["SOURCE-RECORD"]].region_id,))
        self.assertEqual(len(overlapping_region_ids(ir.storage_regions,
            regions[by_name["SOURCE-RECORD"]].region_id)), 10000)
        computation = next(row for row in ir.statements if row.statement_type == "COMPUTE")
        self.assertEqual(computation.binding_status, "bound")
        self.assertEqual(computation.expressions[0][1].operands[1].value, "2")

    def test_alias_and_unknown_layout_expand_only_when_queried(self):
        extra = ("01 ALIAS-RECORD REDEFINES SOURCE-RECORD.\n05 RAW-VALUE PIC X(18).\n"
                 "01 INDEPENDENT-VALUE PIC 9.\n")
        ir = BuiltinParserAdapter().parse(record_source(2, extra=extra), relative_path="record.cbl")
        by_name = {item.name: item.item_id for item in ir.data_items}
        regions = {region.item_id: region for region in ir.storage_regions}
        actual = set(overlapping_region_ids(ir.storage_regions, regions[by_name["VALUE-00000"]].region_id))
        self.assertIn(regions[by_name["RAW-VALUE"]].region_id, actual)
        self.assertIn(regions[by_name["ALIAS-RECORD"]].region_id, actual)
        self.assertNotIn(regions[by_name["INDEPENDENT-VALUE"]].region_id, actual)
        self.assertEqual(sum(len(region.overlaps) for region in ir.storage_regions), 2)
        unknown = BuiltinParserAdapter().parse(record_source(2, extra=
            "01 UNKNOWN-RECORD REDEFINES ABSENT-RECORD.\n05 RAW-VALUE PIC X(18).\n"), relative_path="record.cbl")
        unresolved = next(region for region in unknown.storage_regions if region.layout_status == "unresolved")
        self.assertEqual(unresolved.overlap_scope, "all_visible_storage")
        self.assertEqual(len(overlapping_region_ids(unknown.storage_regions, unresolved.region_id)),
                         len(unknown.storage_regions) - 1)
        self.assertEqual(sum(len(region.overlaps) for region in unknown.storage_regions), 0)

    def test_region_construction_and_frontend_preserve_budget_exception(self):
        ir = BuiltinParserAdapter().parse(record_source(1000), relative_path="record.cbl")
        failure = RuntimeError("LOCAL_BUDGET_EXHAUSTED")
        checks = []
        def interrupted():
            checks.append(True)
            if len(checks) == 3:
                raise failure
        with self.assertRaises(RuntimeError) as caught:
            _regions(ir.data_items, check_cancel=interrupted)
        self.assertIs(caught.exception, failure)
        checks.clear()
        with self.assertRaises(RuntimeError) as caught:
            BuiltinParserAdapter().parse(record_source(10000), relative_path="record.cbl", check_cancel=interrupted)
        self.assertIs(caught.exception, failure)
        checks.clear()
        with self.assertRaises(RuntimeError) as caught:
            parse_statements('DISPLAY "' + 'x' * 500000 + '".', check_cancel=interrupted)
        self.assertIs(caught.exception, failure)

    def test_unknown_group_descendants_and_aliases_keep_global_uncertainty(self):
        source = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. UNKNOWN-LAYOUT.\nDATA DIVISION.\n"
                  "WORKING-STORAGE SECTION.\n01 FIRST-AREA.\n"
                  "05 UNKNOWN-GROUP REDEFINES MISSING-GROUP.\n"
                  "10 CHILD-VALUE PIC 9 OCCURS 3 TIMES.\n"
                  "05 ALIAS-GROUP REDEFINES UNKNOWN-GROUP.\n10 ALIAS-VALUE PIC 9.\n"
                  "01 PARENT-ALIAS REDEFINES FIRST-AREA.\n05 PARENT-ALIAS-VALUE PIC 9.\n"
                  "01 OTHER-AREA.\n05 OTHER-VALUE PIC 9.\n"
                  "PROCEDURE DIVISION.\nMOVE 1 TO CHILD-VALUE(1).\nDISPLAY OTHER-VALUE.\n")
        ir = BuiltinParserAdapter().parse(source, relative_path="record.cbl")
        names = {item.item_id: item.name for item in ir.data_items}
        regions = {names[region.item_id]: region for region in ir.storage_regions}
        for name in ("FIRST-AREA", "UNKNOWN-GROUP", "CHILD-VALUE", "ALIAS-GROUP", "ALIAS-VALUE",
                     "PARENT-ALIAS", "PARENT-ALIAS-VALUE"):
            with self.subTest(name=name):
                self.assertEqual(len(overlapping_region_ids(ir.storage_regions, regions[name].region_id)),
                                 len(ir.storage_regions) - 1)
        self.assertIn(regions["CHILD-VALUE"].region_id,
                      overlapping_region_ids(ir.storage_regions, regions["OTHER-VALUE"].region_id))
        self.assertIn(regions["PARENT-ALIAS-VALUE"].region_id,
                      overlapping_region_ids(ir.storage_regions, regions["OTHER-VALUE"].region_id))
        self.assertEqual(sum(len(region.overlaps) for region in ir.storage_regions), 4)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "record.cbl").write_text(source)
            database = root / "facts.sqlite"
            build_structural_index(root, database, source_format="free")
            facts = FactStore(database, root, source_format="free")
            for name in ("FIRST-AREA", "CHILD-VALUE(1)", "ALIAS-VALUE", "PARENT-ALIAS-VALUE"):
                with self.subTest(reference=name):
                    result = facts.storage_reference("record.cbl", name)
                    self.assertFalse(result["exact"])
                    self.assertEqual(set(result["affected_storage"]), set(names.values()))
                    self.assertIn("unresolved_layout_broad_overlap", result["diagnostics"])
            independent = facts.storage_reference("record.cbl", "OTHER-VALUE")
            self.assertFalse(independent["exact"])
            self.assertIn("CHILD-VALUE", independent["affected_storage"])
            self.assertIn("PARENT-ALIAS-VALUE", independent["affected_storage"])
            service = SemanticService(facts).build("record.cbl")
            write = next(key for key, node in service.nodes.items() if node.kind == "MOVE")
            read = next(key for key, node in service.nodes.items() if node.kind == "DISPLAY")
            self.assertIn(write, service.reaching_in[read]["OTHER-VALUE"])
            self.assertEqual(service.nodes[write].expressions["CHILD-VALUE"], "1")

    def test_unknown_layout_cancellation_does_not_cache_incomplete_program(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "record.cbl").write_text(record_source(1000,
                extra="01 UNKNOWN-RECORD REDEFINES ABSENT-RECORD.\n05 RAW-VALUE PIC X.\n"))
            database = root / "facts.sqlite"
            build_structural_index(root, database, source_format="free")
            expanding, checks = False, []
            failure = RuntimeError("LOCAL_BUDGET_EXHAUSTED")
            def interrupted():
                if expanding:
                    checks.append(True)
                    if len(checks) == 2:
                        raise failure
            def expand(*args, **kwargs):
                nonlocal expanding
                expanding = True
                return unbounded_region_ids(*args, **kwargs)
            facts = FactStore(database, root, source_format="free", check_cancel=interrupted)
            with mock.patch("semantic_service.unbounded_region_ids", side_effect=expand):
                with self.assertRaises(RuntimeError) as caught:
                    facts.storage_reference("record.cbl", "RAW-VALUE")
            self.assertIs(caught.exception, failure)
            self.assertFalse(facts._programs)
            self.assertFalse(facts._storage_refs)
            facts.check_cancel = None
            recovered = facts.storage_reference("record.cbl", "RAW-VALUE")
            self.assertIn("VALUE-00999", recovered["affected_storage"])

    def test_field_binding_reuses_per_program_layout_indexes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "record.cbl").write_text(record_source(1000))
            database = root / "facts.sqlite"
            build_structural_index(root, database, source_format="free")
            facts = FactStore(database, root, source_format="free")
            program = facts.program("record.cbl")
            import semantic_service
            with mock.patch("semantic_service.bind_reference", wraps=semantic_service.bind_reference) as bind:
                for index in range(1000):
                    result = facts.storage_reference("record.cbl", f"VALUE-{index:05d}")
                    self.assertTrue(result["exact"])
                    self.assertEqual(result["affected_storage"], [f"VALUE-{index:05d}"])
                self.assertEqual(sum(len(call.args[1]) for call in bind.call_args_list), 1000)
            self.assertIs(facts.program("record.cbl")["regions"], program["regions"])

    def test_sidecar_budget_reaches_ir_and_removes_incomplete_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "record.cbl").write_text(record_source(1000))
            database = root / "facts.sqlite"
            build_business_index(source, database, source_format="free")
            ensure_repository_search(database, source)
            parsing, checks = False, []
            failure = RuntimeError("LOCAL_BUDGET_EXHAUSTED")
            original = BuiltinParserAdapter.parse
            def parse(adapter, *args, **kwargs):
                nonlocal parsing
                parsing = True
                return original(adapter, *args, **kwargs)
            def interrupted():
                if parsing:
                    checks.append(True)
                    if len(checks) == 2:
                        raise failure
            with QuestionSourceSession(database, source) as session:
                with mock.patch.object(BuiltinParserAdapter, "parse", parse):
                    with self.assertRaises(RuntimeError) as caught:
                        prepare_semantic_scope(database, session,
                            anchors=[{"relative_path": "record.cbl", "line": 1008}],
                            policy=AgentPolicy(), check_cancel=interrupted)
            self.assertIs(caught.exception, failure)
            self.assertEqual(checks, [True, True])
            self.assertEqual(list((root / "semantic-scopes").glob("*.sqlite")), [])

    def test_repeated_offline_probe_reaches_request_with_warm_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "record.cbl").write_text(record_source(2))
            database = root / "facts.sqlite"
            build_business_index(source, database, source_format="free")
            ensure_repository_search(database, source)
            runs = [measure_first_request(database, source,
                "RECORD-RULE 中 VALUE-00000 是怎么计算的？") for _ in range(2)]
            self.assertTrue(all(row["first_request_reached"] for row in runs))
            self.assertTrue(all(row["source_characters"] > 0 for row in runs))


if __name__ == "__main__":
    unittest.main()
