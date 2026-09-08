"""Independent T01 return-flow regressions over neutral temporary source files."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))
from error_paths import ErrorContract
from interprogram_paths import audit_interprogram_paths
from structural_index import build_structural_index


ROOT_FIELDS = """01 INPUT-A PIC 9(4) COMP-5.
01 INPUT-B PIC 9(4) COMP-5.
01 OUTPUT-A PIC 9(4) COMP-5.
01 OUTPUT-B PIC 9(4) COMP-5.
01 STATUS-A PIC 9(4) COMP-5.
01 STATUS-B PIC 9(4) COMP-5.
01 NORMAL-FLAG PIC 9(4) COMP-5.
"""
ROOT_CONTRACT = ErrorContract("ROOTFLOW", ("STATUS-A",), ("OUTPUT-A",))
LEAF_CONTRACT = ErrorContract("LEAFWORK", ("STATUS-VAL",), ("OUTPUT-VAL",))
WRAP_CONTRACT = ErrorContract("WRAPWORK", ("WRAP-STATUS",), ("WRAP-OUTPUT",))


def _program(name, body, *, storage="", linkage="", using=""):
    return ("       >>SOURCE FORMAT FREE\nIDENTIFICATION DIVISION.\nPROGRAM-ID. " + name + ".\n"
            "DATA DIVISION.\n" + ("WORKING-STORAGE SECTION.\n" + storage if storage else "")
            + ("LINKAGE SECTION.\n" + linkage if linkage else "")
            + "PROCEDURE DIVISION" + (" USING " + using if using else "") + ".\n"
            "MAIN-ENTRY.\n" + body)


def _root(body, *, extra_storage=""):
    return _program("ROOTFLOW", body, storage=ROOT_FIELDS + extra_storage)


def _leaf(body=None, *, storage=""):
    return _program("LEAFWORK", body or """    MOVE 21 TO STATUS-VAL.
    MOVE ZERO TO OUTPUT-VAL.
    GOBACK.
""", storage=storage, linkage="""01 INPUT-VAL PIC 9(4) COMP-5.
01 OUTPUT-VAL PIC 9(4) COMP-5.
01 STATUS-VAL PIC 9(4) COMP-5.
""", using="INPUT-VAL OUTPUT-VAL STATUS-VAL")


def _wrapper(*, status_value=False):
    return _program("WRAPWORK", """    CALL 'LEAFWORK' USING BY CONTENT WRAP-INPUT
        BY REFERENCE WRAP-OUTPUT WRAP-STATUS
        ON EXCEPTION
            MOVE 92 TO WRAP-STATUS
            MOVE ZERO TO WRAP-OUTPUT
    END-CALL.
    GOBACK.
""", linkage="""01 WRAP-INPUT PIC 9(4) COMP-5.
01 WRAP-OUTPUT PIC 9(4) COMP-5.
01 WRAP-STATUS PIC 9(4) COMP-5.
""", using="WRAP-INPUT WRAP-OUTPUT " + ("BY VALUE " if status_value else "") + "WRAP-STATUS")


def _call(target="LEAFWORK", *, input_name="INPUT-A", output_name="OUTPUT-A",
          status_name="STATUS-A", status_mode="REFERENCE", normal=""):
    return (f"    CALL '{target}' USING BY CONTENT {input_name}\n"
            f"        BY REFERENCE {output_name} BY {status_mode} {status_name}\n"
            "        ON EXCEPTION\n"
            f"            MOVE 99 TO {status_name}\n"
            f"            MOVE ZERO TO {output_name}\n"
            + ("        NOT ON EXCEPTION\n" + normal if normal else "")
            + "    END-CALL.\n")


def _base_sources(*, call=None, after="", leaf=None):
    return {"root.cbl": _root("""    MOVE 1 TO INPUT-A.
    MOVE 9 TO OUTPUT-A.
    MOVE ZERO TO STATUS-A NORMAL-FLAG.
""" + (call or _call()) + after + "    GOBACK.\n"),
            "leaf.cbl": leaf or _leaf()}


class InterprogramPathAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sources = self.root / "sources"
        self.sources.mkdir()
        self.database = self.root / "index.sqlite"

    def build(self, sources):
        for name, source in sources.items():
            (self.sources / name).write_text(source, encoding="utf-8")
        build_structural_index(self.sources, self.database, quiet=True)

    def audit(self, sources, contracts=None, **options):
        self.build(sources)
        return audit_interprogram_paths(self.database, "ROOTFLOW",
            contracts or (ROOT_CONTRACT, LEAF_CONTRACT),
            call_policy=options.pop("call_policy", "normal_return_only"), **options)

    @staticmethod
    def exits(result, field, *, status=False):
        key = "status_values" if status else "output_values"
        return {exit_record[key].get(field) for exit_record in result["root_exits"]}

    @staticmethod
    def leaf_events(result):
        return [event for event in result["events"]
                if event["program_name"] == "LEAFWORK" and event["event_kind"] == "business_error_return"]

    def test_same_callee_two_calls_do_not_cross_status_or_output(self):
        sources = {"root.cbl": _root("""    MOVE ZERO TO INPUT-A STATUS-A STATUS-B.
    MOVE 5 TO INPUT-B.
    MOVE 9 TO OUTPUT-A OUTPUT-B.
""" + _call() + _call(input_name="INPUT-B", output_name="OUTPUT-B", status_name="STATUS-B")
                         + "    GOBACK.\n"),
                   "leaf.cbl": _leaf("""    IF INPUT-VAL = ZERO
        MOVE 21 TO STATUS-VAL
        MOVE ZERO TO OUTPUT-VAL
    ELSE
        MOVE ZERO TO STATUS-VAL
        MOVE INPUT-VAL TO OUTPUT-VAL
    END-IF.
    GOBACK.
""")}
        contract = ErrorContract("ROOTFLOW", ("STATUS-A", "STATUS-B"), ("OUTPUT-A", "OUTPUT-B"))
        result = self.audit(sources, (contract, LEAF_CONTRACT))
        self.assertEqual(self.exits(result, "OUTPUT-A"), {"0"})
        self.assertEqual(self.exits(result, "OUTPUT-B"), {"5"})
        self.assertEqual(self.exits(result, "STATUS-A", status=True), {"21"})
        self.assertEqual(self.exits(result, "STATUS-B", status=True), {"0"})
        self.assertEqual(len({event["context_id"] for event in self.leaf_events(result)}), 1)

    def test_reference_status_returns_through_wrapper_to_root(self):
        sources = _base_sources(call=_call("WRAPWORK"))
        sources["wrapper.cbl"] = _wrapper()
        result = self.audit(sources, (ROOT_CONTRACT, WRAP_CONTRACT, LEAF_CONTRACT))
        self.assertEqual(self.exits(result, "OUTPUT-A"), {"0"})
        self.assertEqual(self.exits(result, "STATUS-A", status=True), {"21"})
        self.assertTrue(self.leaf_events(result))

    def test_two_nonzero_returns_keep_distinct_contexts_and_statuses(self):
        sources = {"root.cbl": _root("""    MOVE ZERO TO INPUT-A STATUS-A STATUS-B.
    MOVE 5 TO INPUT-B.
    MOVE 9 TO OUTPUT-A OUTPUT-B.
""" + _call() + _call(input_name="INPUT-B", output_name="OUTPUT-B", status_name="STATUS-B")
                         + "    GOBACK.\n"),
                   "leaf.cbl": _leaf("""    IF INPUT-VAL = ZERO
        MOVE 21 TO STATUS-VAL
        MOVE ZERO TO OUTPUT-VAL
    ELSE
        MOVE 22 TO STATUS-VAL
        MOVE 7 TO OUTPUT-VAL
    END-IF.
    GOBACK.
""")}
        contract = ErrorContract("ROOTFLOW", ("STATUS-A", "STATUS-B"), ("OUTPUT-A", "OUTPUT-B"))
        result = self.audit(sources, (contract, LEAF_CONTRACT))
        self.assertEqual(self.exits(result, "STATUS-A", status=True), {"21"})
        self.assertEqual(self.exits(result, "STATUS-B", status=True), {"22"})
        self.assertEqual(self.exits(result, "OUTPUT-A"), {"0"})
        self.assertEqual(self.exits(result, "OUTPUT-B"), {"7"})
        events = self.leaf_events(result)
        self.assertEqual({event["status_value"] for event in events}, {"21", "22"})
        self.assertEqual(len({event["context_id"] for event in events}), 2)

    def test_perform_repeated_callsite_retains_local_instance_identity(self):
        sources = _base_sources()
        sources["root.cbl"] = _root("""    MOVE ZERO TO INPUT-A STATUS-A.
    MOVE 9 TO OUTPUT-A.
    PERFORM CALL-WORK.
    MOVE 5 TO INPUT-A.
    PERFORM CALL-WORK.
    GOBACK.
CALL-WORK.
""" + _call() + "    EXIT.\n")
        sources["leaf.cbl"] = _leaf("""    IF INPUT-VAL = ZERO
        MOVE 21 TO STATUS-VAL
        MOVE ZERO TO OUTPUT-VAL
    ELSE
        MOVE 22 TO STATUS-VAL
        MOVE 7 TO OUTPUT-VAL
    END-IF.
    GOBACK.
""")
        result = self.audit(sources)
        self.assertEqual(self.exits(result, "STATUS-A", status=True), {"22"})
        self.assertEqual(self.exits(result, "OUTPUT-A"), {"7"})
        events = self.leaf_events(result)
        self.assertEqual({event["status_value"] for event in events}, {"21", "22"})
        self.assertEqual(len({event["context_id"] for event in events}), 2)

    def test_unbound_linkage_write_does_not_become_supported_local_storage(self):
        sources = _base_sources(leaf=_leaf("""    MOVE ZERO TO UNBOUND-AREA.
    MOVE UNBOUND-AREA TO OUTPUT-VAL.
    MOVE 21 TO STATUS-VAL.
    GOBACK.
"""))
        sources["leaf.cbl"] = sources["leaf.cbl"].replace(
            "LINKAGE SECTION.\n", "LINKAGE SECTION.\n01 UNBOUND-AREA PIC 9(4) COMP-5.\n")
        result = self.audit(sources)
        self.assertTrue(result["boundaries"])
        self.assertEqual(result["root_exits"], [])

    def test_fixed_group_numeric_members_return_without_losing_status(self):
        sources = {
            "root.cbl": _root("""    MOVE 9 TO GROUP-OUTPUT.
    MOVE ZERO TO GROUP-STATUS.
    CALL 'GROUPWORK' USING BY REFERENCE RESULT-AREA
        ON EXCEPTION
            MOVE 99 TO GROUP-STATUS
            MOVE ZERO TO GROUP-OUTPUT
    END-CALL.
    GOBACK.
""", extra_storage="""01 RESULT-AREA.
   05 GROUP-OUTPUT PIC 9(4) COMP-5.
   05 GROUP-STATUS PIC 9(4) COMP-5.
"""),
            "group.cbl": _program("GROUPWORK", """    MOVE 21 TO MEMBER-STATUS.
    MOVE ZERO TO MEMBER-OUTPUT.
    GOBACK.
""", linkage="""01 PARAMETER-AREA.
   05 MEMBER-OUTPUT PIC 9(4) COMP-5.
   05 MEMBER-STATUS PIC 9(4) COMP-5.
""", using="PARAMETER-AREA"),
        }
        root_contract = ErrorContract("ROOTFLOW", ("GROUP-STATUS",), ("GROUP-OUTPUT",))
        callee_contract = ErrorContract("GROUPWORK", ("MEMBER-STATUS",), ("MEMBER-OUTPUT",))
        result = self.audit(sources, (root_contract, callee_contract))
        self.assertEqual(self.exits(result, "GROUP-OUTPUT"), {"0"})
        self.assertEqual(self.exits(result, "GROUP-STATUS", status=True), {"21"})

    def test_copy_defined_parameters_keep_storage_and_return_values(self):
        sources = _base_sources()
        leaf_fields = """01 INPUT-VAL PIC 9(4) COMP-5.
01 OUTPUT-VAL PIC 9(4) COMP-5.
01 STATUS-VAL PIC 9(4) COMP-5.
"""
        sources["root.cbl"] = sources["root.cbl"].replace(ROOT_FIELDS, "COPY ROOTDATA.\n")
        sources["leaf.cbl"] = sources["leaf.cbl"].replace(leaf_fields, "COPY LEAFDATA.\n")
        sources["rootdata.cpy"] = ROOT_FIELDS
        sources["leafdata.cpy"] = leaf_fields
        result = self.audit(sources)
        self.assertEqual(result["boundaries"], [])
        self.assertEqual(self.exits(result, "OUTPUT-A"), {"0"})
        self.assertEqual(self.exits(result, "STATUS-A", status=True), {"21"})

    def test_zero_witness_budget_does_not_invent_a_trace_or_erase_exit(self):
        result = self.audit(_base_sources(), max_witnesses=0)
        self.assertEqual(result["witnesses"], [])
        self.assertTrue(result["summary"]["witnesses_truncated"])
        self.assertEqual(self.exits(result, "OUTPUT-A"), {"0"})
        self.assertEqual(self.exits(result, "STATUS-A", status=True), {"21"})

    def test_content_and_value_cut_off_outer_status_writeback(self):
        for mode in ("CONTENT", "VALUE"):
            with self.subTest(mode=mode):
                sources = _base_sources(call=_call("WRAPWORK", status_mode=mode))
                sources["root.cbl"] = sources["root.cbl"].replace(
                    "MOVE ZERO TO STATUS-A NORMAL-FLAG.", "MOVE 4 TO STATUS-A.\n    MOVE ZERO TO NORMAL-FLAG.")
                sources["wrapper.cbl"] = _wrapper(status_value=mode == "VALUE")
                result = self.audit(sources, (ROOT_CONTRACT, WRAP_CONTRACT, LEAF_CONTRACT))
                self.assertEqual(self.exits(result, "OUTPUT-A"), {"0"})
                self.assertEqual(self.exits(result, "STATUS-A", status=True), {"4"})
                self.assertTrue(self.leaf_events(result))

    def test_business_error_normal_return_executes_not_on_exception(self):
        call = _call(normal="            MOVE 5 TO NORMAL-FLAG\n")
        contract = ErrorContract("ROOTFLOW", ("STATUS-A",), ("OUTPUT-A", "NORMAL-FLAG"))
        result = self.audit(_base_sources(call=call), (contract, LEAF_CONTRACT))
        self.assertEqual(self.exits(result, "STATUS-A", status=True), {"21"})
        self.assertEqual(self.exits(result, "NORMAL-FLAG"), {"5"})

    def test_explored_invocation_failure_and_normal_return_do_not_share_mutable_state(self):
        call = _call(normal="            MOVE 5 TO NORMAL-FLAG\n")
        contract = ErrorContract("ROOTFLOW", ("STATUS-A",), ("OUTPUT-A", "NORMAL-FLAG"))
        result = self.audit(_base_sources(call=call), (contract, LEAF_CONTRACT), call_policy="explore")
        values = {(exit_record["output_values"]["OUTPUT-A"],
                   exit_record["status_values"]["STATUS-A"],
                   exit_record["output_values"]["NORMAL-FLAG"])
                  for exit_record in result["root_exits"]}
        self.assertEqual(values, {("0", "21", "5"), ("0", "99", "0")})

    def test_parent_overwrite_after_return_remains_visible_at_root(self):
        result = self.audit(_base_sources(after="    MOVE 7 TO OUTPUT-A.\n"))
        self.assertEqual(self.exits(result, "OUTPUT-A"), {"7"})
        self.assertTrue(any(event["outputs"]["OUTPUT-A"]["finding"] == "nonzero_exit_possible_in_model"
                            for event in self.leaf_events(result)))

    def test_callee_overwrite_after_clear_is_not_discarded_on_return(self):
        leaf = _leaf("""    MOVE 21 TO STATUS-VAL.
    MOVE ZERO TO OUTPUT-VAL.
    MOVE 7 TO OUTPUT-VAL.
    GOBACK.
""")
        result = self.audit(_base_sources(leaf=leaf))
        self.assertEqual(self.exits(result, "OUTPUT-A"), {"7"})
        self.assertEqual(self.exits(result, "STATUS-A", status=True), {"21"})

    def test_stop_run_in_callee_cannot_return_to_parent_overwrite(self):
        leaf = _leaf("""    MOVE 21 TO STATUS-VAL.
    MOVE ZERO TO OUTPUT-VAL.
    STOP RUN.
""")
        result = self.audit(_base_sources(after="    MOVE 7 TO OUTPUT-A.\n", leaf=leaf))
        self.assertTrue(result["boundaries"])
        self.assertEqual(result["root_exits"], [])

    def test_same_reference_actual_cannot_be_copied_back_as_independent_fields(self):
        result = self.audit(_base_sources(call=_call(status_name="OUTPUT-A")))
        self.assertTrue(result["boundaries"])
        self.assertEqual(result["root_exits"], [])

    def test_working_storage_is_not_assumed_fresh_zero_for_every_call(self):
        sources = _base_sources(call=_call() + _call())
        sources["leaf.cbl"] = _leaf("""    IF SAVED-FLAG = ZERO
        MOVE 1 TO SAVED-FLAG
        MOVE ZERO TO STATUS-VAL OUTPUT-VAL
    ELSE
        MOVE 21 TO STATUS-VAL
        MOVE 7 TO OUTPUT-VAL
    END-IF.
    GOBACK.
""", storage="01 SAVED-FLAG PIC 9(4) COMP-5 VALUE ZERO.\n")
        result = self.audit(sources)
        self.assertTrue(result["boundaries"] or "7" in self.exits(result, "OUTPUT-A"))
        self.assertNotEqual(self.exits(result, "OUTPUT-A"), {"0"})

    def test_shared_storage_clauses_cannot_hide_global_side_effects(self):
        for clause in ("EXTERNAL", "GLOBAL"):
            with self.subTest(clause=clause):
                sources = _base_sources(leaf=_leaf(storage=
                    "01 SHARED-SLOT " + clause + " PIC 9(4) COMP-5.\n"))
                result = self.audit(sources)
                self.assertTrue(result["boundaries"])
                self.assertEqual(result["root_exits"], [])

    def test_source_change_gets_new_snapshot_and_new_return_result(self):
        sources = _base_sources()
        first = self.audit(sources)
        sources["leaf.cbl"] = sources["leaf.cbl"].replace("MOVE ZERO TO OUTPUT-VAL", "MOVE 7 TO OUTPUT-VAL")
        second = self.audit(sources)
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(self.exits(first, "OUTPUT-A"), {"0"})
        self.assertEqual(self.exits(second, "OUTPUT-A"), {"7"})

    def test_missing_evidence_fails_closed_instead_of_reusing_previous_results(self):
        self.audit(_base_sources())
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE evidence_spans SET source_sha256 = ?", ("0" * 64,))
        with self.assertRaises(ValueError):
            audit_interprogram_paths(self.database, "ROOTFLOW", (ROOT_CONTRACT, LEAF_CONTRACT),
                                     call_policy="normal_return_only")

    def test_state_depth_and_context_budgets_cannot_claim_root_return(self):
        sources = _base_sources(call=_call("WRAPWORK"))
        sources["wrapper.cbl"] = _wrapper()
        for budget in ({"max_states": 2}, {"max_depth": 1}, {"max_contexts": 1}):
            with self.subTest(budget=budget):
                result = self.audit(sources, (ROOT_CONTRACT, WRAP_CONTRACT, LEAF_CONTRACT), **budget)
                self.assertTrue(result["boundaries"])
                self.assertEqual(result["root_exits"], [])

    def test_recursive_call_or_missing_target_has_no_invented_return(self):
        variants = (
            _base_sources(call=_call("MISSINGWORK")),
            _base_sources(leaf=_leaf("""    CALL 'LEAFWORK' USING INPUT-VAL OUTPUT-VAL STATUS-VAL
        ON EXCEPTION
            MOVE ZERO TO OUTPUT-VAL
    END-CALL.
    GOBACK.
""")),
        )
        for sources in variants:
            with self.subTest(source=sources["root.cbl"]):
                result = self.audit(sources)
                self.assertTrue(result["boundaries"])
                self.assertEqual(result["root_exits"], [])


if __name__ == "__main__":
    unittest.main()
