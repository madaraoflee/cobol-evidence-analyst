"""Independent examples for the declared, bounded access model; no real file I/O."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import framework_io
from framework_io import apply_io_call, initial_io_state, validate_runtime_contract


def contract():
    operations = [{"value": "FIRST", "action": "first", "reads_record": True},
                  {"value": "POSITION", "action": "first", "reads_record": False}]
    operations += [{"value": name.upper(), "action": name} for name in
                   ("next", "get", "save", "close", "commit", "rollback", "checkpoint", "resume")]
    return {
        "schema_version": "1.0", "contract_id": "request-access", "contract_version": "1",
        "provenance": {"kind": "synthetic", "reference": "independent test model", "target_version": "test-1"},
        "dataset": {"id": "REQUESTS", "fields": {"ID": "string", "TYPE": "string", "STATE": "string", "COUNT": "integer"},
                    "primary_key": ["ID"]},
        "access_paths": [{"id": "SELECTED", "key_fields": ["ID"], "select": {"TYPE": "WORK"}, "join": False}],
        "services": [{"program": "ACCESS", "argument": "AREA", "function_field": "FUNCTION", "status_field": "STATUS",
                      "session": "MAIN", "access_path": "SELECTED", "key_fields": {"ID": "RECORD-ID"},
                      "record_fields": {"ID": "RECORD-ID", "TYPE": "RECORD-TYPE", "STATE": "RECORD-STATE", "COUNT": "RECORD-COUNT"},
                      "operations": operations}],
        "statuses": {"ok": "OK", "eof": "END", "not_found": "MISS", "locked": "HELD", "invalid": "FAIL"},
        "locking": {"read": "none", "save_requires_lock": False, "release_on_advance": True, "release_on_close": True},
        "transaction": {"mode": "explicit", "scope": "all_services", "read_visibility": "own_pending",
                        "commit_cursor": "preserve", "rollback_cursor": "close", "commit_locks": "release", "rollback_locks": "release"},
        "restart": {"enabled": True, "checkpoint": "committed_key", "resume": "strictly_after"},
    }


def records():
    return [{"ID": "A001", "TYPE": "WORK", "STATE": "PENDING", "COUNT": 0},
            {"ID": "A002", "TYPE": "OTHER", "STATE": "PENDING", "COUNT": 0},
            {"ID": "A003", "TYPE": "WORK", "STATE": "REVIEW", "COUNT": 0},
            {"ID": "A004", "TYPE": "WORK", "STATE": "PENDING", "COUNT": 0}]


class FrameworkIOTests(unittest.TestCase):
    def setUp(self):
        self.contract = contract()
        self.start()

    def start(self, **scenario):
        self.state = initial_io_state(self.contract, {"records": records(), **scenario})
        self.values = {"RECORD-ID": "", "RECORD-TYPE": "", "RECORD-STATE": "", "RECORD-COUNT": 0}

    def call(self, function, program="ACCESS", **values):
        self.values.update(values)
        self.values["FUNCTION"] = function
        result = apply_io_call(self.contract, self.state, program, self.values)[0]
        self.state = result["state"]
        self.values.update(result["values"])
        self.assertFalse(result["runtime_verified"])
        self.assertFalse(result["event"]["runtime_verified"])
        self.assertEqual(result["event"]["semantics"], "declared_contract")
        return result

    def status(self, function, expected="OK", **values):
        result = self.call(function, **values)
        self.assertNotIn("boundary", result)
        self.assertEqual(result["values"]["STATUS"], expected)
        return result

    def add_service(self, name, session="MAIN", path="SELECTED"):
        service = deepcopy(self.contract["services"][0])
        service.update(program=name, session=session, access_path=path)
        self.contract["services"].append(service)

    def checkpoint(self):
        self.status("FIRST")
        self.status("SAVE", **{"RECORD-STATE": "DONE"})
        self.status("COMMIT")
        self.status("CHECKPOINT")
        return deepcopy(self.state["restart_checkpoint"]), deepcopy(self.state["committed_records"])

    def test_validation_does_not_mutate_contract_and_provenance_is_not_verification(self):
        for kind in ("synthetic", "public_candidate", "company_validated"):
            c = contract()
            c["provenance"]["kind"] = kind
            before = deepcopy(c)
            self.assertEqual(validate_runtime_contract(c), before)
            state = initial_io_state(c, {"records": records()})
            result = apply_io_call(c, state, "ACCESS", {"FUNCTION": "FIRST", "RECORD-ID": ""})[0]
            self.assertFalse(state["runtime_verified"])
            self.assertFalse(result["runtime_verified"])
            self.assertEqual(c, before)

    def test_malformed_nested_enums_raise_value_error_not_type_error(self):
        paths = [("provenance", "kind"), ("dataset", "fields", "ID"), ("locking", "read"),
                 ("transaction", "mode"), ("transaction", "commit_cursor"), ("transaction", "commit_locks"),
                 ("services", 0, "access_path"), ("services", 0, "operations", 0, "action")]
        for path in paths:
            for invalid in ([], {}, None, True):
                with self.subTest(path=path, invalid=invalid):
                    c = contract()
                    parent = c
                    for key in path[:-1]:
                        parent = parent[key]
                    parent[path[-1]] = invalid
                    with self.assertRaises(ValueError):
                        validate_runtime_contract(c)

    def test_invalid_top_level_inputs_have_a_consistent_exception_type(self):
        for invalid in (None, [], "invalid", 1):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_runtime_contract(invalid)
            with self.subTest(scenario=invalid), self.assertRaises(ValueError):
                initial_io_state(self.contract, invalid)
            with self.subTest(state=invalid), self.assertRaises(ValueError):
                apply_io_call(self.contract, invalid, "ACCESS", {})
        for invalid in (None, [], "", "NOT AN IDENTIFIER"):
            with self.subTest(program=invalid), self.assertRaises(ValueError):
                apply_io_call(self.contract, self.state, invalid, {})
        with self.assertRaises(ValueError):
            apply_io_call(self.contract, self.state, "ACCESS", {}, call_failure=1)

    def test_schema_key_and_mapping_mistakes_are_rejected(self):
        mutations = [lambda c: c.update(schema_version="2.0"),
                     lambda c: c["dataset"].update(primary_key=[["ID"]]),
                     lambda c: c["access_paths"][0].update(key_fields=["TYPE"]),
                     lambda c: c["services"][0]["operations"][0].pop("reads_record"),
                     lambda c: c["services"][0].update(status_field="FUNCTION"),
                     lambda c: c["services"][0]["record_fields"].update(STATE="RECORD-ID"),
                     lambda c: c["services"][0]["key_fields"].update(ID="RECORD-STATE"),
                     lambda c: c["statuses"].update(eof="OK"),
                     lambda c: c["locking"].update(save_requires_lock=True),
                     lambda c: c["transaction"].update(scope="one_service")]
        for mutation in mutations:
            c = contract()
            mutation(c)
            with self.subTest(contract=c), self.assertRaises(ValueError):
                validate_runtime_contract(c)

    def test_bounded_scenario_records_and_external_lock_keys(self):
        invalid = [records() * 2, records() * 33, [{**records()[0], "COUNT": 10**18}],
                   [{**records()[0], "COUNT": True}], [{**records()[0], "STATE": "X" * 2049}]]
        for rows in invalid:
            with self.subTest(records=rows), self.assertRaises(ValueError):
                initial_io_state(self.contract, {"records": rows})
        for locks in ([{"key": ["MISSING"], "owner": "OTHER"}],
                      [{"key": ["A001"], "owner": "OTHER"}] * 2,
                      [{"key": [1], "owner": "OTHER"}]):
            with self.subTest(locks=locks), self.assertRaises(ValueError):
                initial_io_state(self.contract, {"records": records(), "external_locks": locks})

    def test_file_equality_filter_does_not_apply_business_record_decision(self):
        self.status("FIRST")
        self.assertEqual(self.values["RECORD-ID"], "A001")
        self.status("NEXT")
        self.assertEqual(self.values["RECORD-ID"], "A003")
        self.assertEqual(self.values["RECORD-STATE"], "REVIEW")
        self.status("GET", "MISS", **{"RECORD-ID": "A002"})
        self.assertFalse(self.state["pending_writes"])

    def test_reading_first_then_next_advances_past_first_record(self):
        self.status("FIRST")
        self.assertEqual(self.values["RECORD-ID"], "A001")
        self.status("NEXT")
        self.assertEqual(self.values["RECORD-ID"], "A003")

    def test_position_only_then_next_includes_start_record(self):
        result = self.status("POSITION", **{"RECORD-ID": "A003"})
        self.assertEqual(set(result["values"]), {"STATUS"})
        self.status("NEXT")
        self.assertEqual(self.values["RECORD-ID"], "A003")
        self.status("NEXT")
        self.assertEqual(self.values["RECORD-ID"], "A004")

    def test_keyed_get_positions_this_explicitly_shared_cursor(self):
        self.status("GET", **{"RECORD-ID": "A003"})
        self.status("NEXT")
        self.assertEqual(self.values["RECORD-ID"], "A004")
        self.status("NEXT", "END")
        self.status("NEXT", "END")
        self.status("SAVE", "FAIL")

    def test_services_share_only_the_same_declared_path_and_session(self):
        self.add_service("SAME")
        self.add_service("SEPARATE", session="SECOND")
        self.contract["access_paths"].append({"id": "OTHERPATH", "key_fields": ["ID"], "select": {}, "join": False})
        self.add_service("OTHERPATHSERVICE", path="OTHERPATH")
        self.start()
        self.status("FIRST")
        result = self.call("NEXT", program="SAME")
        self.assertEqual(result["values"]["RECORD-ID"], "A003")
        for program in ("SEPARATE", "OTHERPATHSERVICE"):
            result = self.call("NEXT", program=program)
            self.assertEqual(result["values"]["STATUS"], "FAIL")

    def test_unknown_function_and_unresolved_key_are_boundaries(self):
        for value in ("first", "FIRST ", "UNDECLARED", 1, None):
            with self.subTest(value=value):
                result = self.call(value)
                self.assertIn("boundary", result)
                self.assertEqual(result["values"], {})
                self.assertFalse(self.state["cursors"])
        self.assertEqual(self.call("FIRST", **{"RECORD-ID": None})["boundary"]["reason"], "io_request_key_not_resolved")

    def test_join_is_a_boundary_and_never_silently_an_equality_scan(self):
        self.contract["access_paths"][0]["join"] = True
        self.start()
        result = self.call("FIRST")
        self.assertEqual(result["boundary"]["reason"], "io_join_access_path_not_modeled")
        self.assertFalse(self.state["cursors"])

    def test_calls_do_not_mutate_input_contract_state_or_values(self):
        self.values["FUNCTION"] = "FIRST"
        before = deepcopy((self.contract, self.state, self.values))
        result = apply_io_call(self.contract, self.state, "ACCESS", self.values)[0]
        self.assertEqual((self.contract, self.state, self.values), before)
        self.assertNotEqual(result["state"], self.state)

    def test_read_lock_conflict_preserves_previous_position_and_lock(self):
        self.contract["locking"].update(read="exclusive", save_requires_lock=True)
        self.start(external_locks=[{"key": ["A003"], "owner": "OTHER"}])
        self.status("FIRST")
        cursor = deepcopy(self.state["cursors"])
        locks = deepcopy(self.state["locks"])
        self.status("NEXT", "HELD")
        self.assertEqual(self.state["cursors"], cursor)
        self.assertEqual(self.state["locks"], locks)
        self.status("NEXT", "HELD")

    def test_advance_and_close_release_policies_are_explicit(self):
        for release_advance in (False, True):
            for release_close in (False, True):
                with self.subTest(advance=release_advance, close=release_close):
                    self.contract = contract()
                    self.contract["locking"].update(read="exclusive", release_on_advance=release_advance, release_on_close=release_close)
                    self.start()
                    self.status("FIRST")
                    self.status("NEXT")
                    self.assertEqual(len(self.state["locks"]), 1 if release_advance else 2)
                    self.status("CLOSE")
                    self.assertFalse(self.state["cursors"])
                    self.assertEqual(len(self.state["locks"]), 0 if release_close else (1 if release_advance else 2))

    def test_cursor_lock_owner_is_not_inferred_from_same_program_or_session(self):
        self.contract["locking"]["read"] = "exclusive"
        self.add_service("OTHER", session="SECOND")
        self.start()
        self.status("FIRST")
        result = self.call("GET", program="OTHER", **{"RECORD-ID": "A001"})
        self.assertEqual(result["values"]["STATUS"], "HELD")

    def test_external_lock_can_allow_nonlocking_read_but_block_save(self):
        self.start(external_locks=[{"key": ["A001"], "owner": "OTHER"}])
        self.status("FIRST")
        self.status("SAVE", "HELD", **{"RECORD-STATE": "DONE"})
        self.assertFalse(self.state["pending_writes"])

    def test_save_requiring_owned_lock_fails_after_commit_releases_it(self):
        self.contract["locking"].update(read="exclusive", save_requires_lock=True)
        self.start()
        self.status("FIRST")
        self.status("COMMIT")
        self.status("SAVE", "HELD", **{"RECORD-STATE": "DONE"})

    def test_pending_writes_are_visible_to_own_reads_but_not_committed(self):
        self.status("FIRST")
        self.status("SAVE", **{"RECORD-STATE": "DONE"})
        self.assertEqual(self.state["committed_records"][0]["STATE"], "PENDING")
        self.assertEqual(self.state["pending_writes"][0]["STATE"], "DONE")
        self.status("GET", **{"RECORD-ID": "A001"})
        self.assertEqual(self.values["RECORD-STATE"], "DONE")
        self.status("CLOSE")
        self.assertEqual(len(self.state["pending_writes"]), 1)
        self.status("COMMIT")
        self.assertFalse(self.state["pending_writes"])
        self.assertEqual(self.state["committed_records"][0]["STATE"], "DONE")

    def test_rollback_discards_pending_work_but_preserves_prior_commit(self):
        self.status("FIRST")
        self.status("SAVE", **{"RECORD-STATE": "DONE"})
        self.status("COMMIT")
        self.status("NEXT")
        self.status("SAVE", **{"RECORD-STATE": "DONE"})
        self.status("ROLLBACK")
        self.assertEqual([row["STATE"] for row in self.state["committed_records"]], ["DONE", "PENDING", "REVIEW", "PENDING"])
        self.assertFalse(self.state["pending_writes"])
        self.assertFalse(self.state["cursors"])
        self.assertFalse(self.state["locks"])

    def test_commit_status_failure_preserves_pending_work_until_rollback(self):
        self.start(faults=[{"program": "ACCESS", "function": "COMMIT", "occurrence": 1, "outcome": "status", "status": "FAIL"}])
        self.status("FIRST")
        self.status("SAVE", **{"RECORD-STATE": "DONE"})
        result = self.status("COMMIT", "FAIL")
        self.assertEqual(result["outcome"], "normal")
        self.assertEqual(self.state["committed_records"], records())
        self.assertEqual(len(self.state["pending_writes"]), 1)
        self.assertFalse(self.state["committed_progress"])
        self.status("ROLLBACK")
        self.assertFalse(self.state["pending_writes"])

    def test_rollback_failure_leaves_pending_work_and_original_commit_unchanged(self):
        self.start(faults=[{"program": "ACCESS", "function": "ROLLBACK", "occurrence": 1, "outcome": "status", "status": "FAIL"}])
        self.status("FIRST")
        self.status("SAVE", **{"RECORD-STATE": "DONE"})
        self.status("ROLLBACK", "FAIL")
        self.assertEqual(len(self.state["pending_writes"]), 1)
        self.assertEqual(self.state["committed_records"], records())

    def test_fault_exception_is_distinct_from_status_and_occurrence_is_exact(self):
        self.start(faults=[{"program": "ACCESS", "function": "NEXT", "occurrence": 2, "outcome": "exception"}])
        self.status("FIRST")
        self.status("NEXT")
        cursor = deepcopy(self.state["cursors"])
        result = self.call("NEXT")
        self.assertEqual(result["outcome"], "exception")
        self.assertEqual(result["values"], {})
        self.assertEqual(self.state["cursors"], cursor)
        self.status("NEXT")
        self.assertEqual(self.values["RECORD-ID"], "A004")

    def test_explicit_call_failure_has_no_service_effects(self):
        self.values["FUNCTION"] = "FIRST"
        result = apply_io_call(self.contract, self.state, "ACCESS", self.values, call_failure=True)[0]
        self.assertEqual(result["outcome"], "exception")
        self.assertFalse(result["state"]["cursors"])
        self.assertEqual(result["values"], {})

    def test_autocommit_applies_cursor_and_lock_policy(self):
        self.contract["locking"].update(read="exclusive", save_requires_lock=True)
        self.contract["transaction"].update(mode="autocommit", commit_cursor="close")
        self.start()
        self.status("FIRST")
        result = self.status("SAVE", **{"RECORD-STATE": "DONE"})
        self.assertEqual(result["event"]["write_visibility"], "committed")
        self.assertEqual(self.state["committed_records"][0]["STATE"], "DONE")
        self.assertFalse(self.state["cursors"])
        self.assertFalse(self.state["locks"])
        self.status("CHECKPOINT")
        self.assertEqual(self.state["restart_checkpoint"]["key"], ["A001"])

    def test_commit_can_preserve_locks_as_declared(self):
        self.contract["locking"].update(read="exclusive", save_requires_lock=True)
        self.contract["transaction"]["commit_locks"] = "preserve"
        self.start()
        self.status("FIRST")
        self.status("COMMIT")
        self.assertEqual(len(self.state["locks"]), 1)
        self.status("SAVE", **{"RECORD-STATE": "DONE"})

    def test_save_does_not_allow_new_identity_or_ordering_change(self):
        self.contract["access_paths"].append({"id": "BYCOUNT", "key_fields": ["COUNT", "ID"], "select": {}, "join": False})
        self.start()
        self.status("FIRST")
        self.status("SAVE", "FAIL", **{"RECORD-ID": "NEW"})
        self.status("SAVE", "FAIL", **{"RECORD-ID": "A001", "RECORD-COUNT": 2})
        self.assertFalse(self.state["pending_writes"])

    def test_save_scalar_size_limit_matches_scenario_limit(self):
        self.status("FIRST")
        for value in (10**18, -(10**18), 10**100, True, None):
            with self.subTest(value=value):
                result = self.call("SAVE", **{"RECORD-COUNT": value})
                self.assertEqual(result["boundary"]["reason"], "io_write_record_not_resolved")
                self.assertFalse(self.state["pending_writes"])

    def test_checkpoint_requires_committed_progress_and_no_pending_write(self):
        self.assertEqual(self.call("CHECKPOINT")["boundary"]["reason"], "restart_checkpoint_has_no_committed_progress")
        self.status("FIRST")
        self.assertEqual(self.call("CHECKPOINT")["boundary"]["reason"], "restart_checkpoint_has_no_committed_progress")
        self.status("SAVE", **{"RECORD-STATE": "DONE"})
        self.assertEqual(self.call("CHECKPOINT")["boundary"]["reason"], "restart_checkpoint_has_uncommitted_writes")
        self.assertEqual(self.call("RESUME")["boundary"]["reason"], "restart_resume_has_uncommitted_writes")
        self.status("COMMIT")
        self.status("CHECKPOINT")

    def test_checkpoint_failure_does_not_undo_commit_or_create_checkpoint(self):
        self.start(faults=[{"program": "ACCESS", "function": "CHECKPOINT", "occurrence": 1, "outcome": "status", "status": "FAIL"}])
        self.status("FIRST")
        self.status("SAVE", **{"RECORD-STATE": "DONE"})
        self.status("COMMIT")
        self.status("CHECKPOINT", "FAIL")
        self.assertEqual(self.state["committed_records"][0]["STATE"], "DONE")
        self.assertIsNone(self.state["restart_checkpoint"])

    def test_valid_checkpoint_resumes_strictly_after_committed_key(self):
        checkpoint, committed = self.checkpoint()
        self.start(records=committed, restart_checkpoint=checkpoint)
        self.status("RESUME")
        self.assertEqual(self.values["RECORD-ID"], "A003")
        self.assertEqual(self.state["committed_records"][0]["STATE"], "DONE")
        self.assertFalse(self.state["runtime_verified"])

    def test_restart_rejects_contract_version_data_path_and_key_mismatch(self):
        checkpoint, committed = self.checkpoint()
        cases = [("schema_version", "2", "restart_contract_mismatch"),
                 ("contract_version", "other", "restart_contract_mismatch"),
                 ("contract_hash", "other", "restart_contract_mismatch"),
                 ("dataset_hash", "other", "restart_dataset_mismatch"),
                 ("access_path", "OTHER", "restart_access_path_mismatch"),
                 ("key", [1], "restart_key_type_mismatch"),
                 ("key", [], "restart_key_type_mismatch"),
                 ("key", ["Z999"], "restart_committed_key_not_found")]
        for field, value, reason in cases:
            with self.subTest(field=field, value=value):
                supplied = {**checkpoint, field: value}
                self.start(records=committed, restart_checkpoint=supplied)
                result = self.call("RESUME")
                self.assertEqual(result["boundary"]["reason"], reason)
                self.assertEqual(result["values"], {})
                self.assertFalse(self.state["cursors"])

    def test_restart_does_not_require_processed_record_to_still_pass_filter(self):
        self.contract["access_paths"][0]["select"] = {"TYPE": "WORK", "STATE": "PENDING"}
        self.start()
        checkpoint, committed = self.checkpoint()
        self.start(records=committed, restart_checkpoint=checkpoint)
        self.status("RESUME")
        self.assertEqual(self.values["RECORD-ID"], "A004")

    def test_missing_and_disabled_restart_contract_are_boundaries(self):
        self.assertEqual(self.call("RESUME")["boundary"]["reason"], "restart_checkpoint_missing")
        self.contract["restart"]["enabled"] = False
        self.start()
        for function in ("RESUME", "CHECKPOINT"):
            self.assertEqual(self.call(function)["boundary"]["reason"], "io_restart_not_enabled")

    def test_model_state_edits_fail_integrity_and_are_not_resealed(self):
        original = deepcopy(self.state)
        edits = [lambda s: s["committed_records"][0].update(STATE="DONE"),
                 lambda s: s.update(event_count=-1), lambda s: s.update(state_hash="wrong")]
        for edit in edits:
            with self.subTest(edit=edit):
                state = deepcopy(original)
                edit(state)
                result = apply_io_call(self.contract, state, "ACCESS", {"FUNCTION": "FIRST", "RECORD-ID": ""})[0]
                self.assertEqual(result["boundary"]["reason"], "io_state_integrity_mismatch")
                self.assertEqual(result["state"], state)
                again = apply_io_call(self.contract, result["state"], "ACCESS", {"FUNCTION": "FIRST", "RECORD-ID": ""})[0]
                self.assertEqual(again["boundary"]["reason"], "io_state_integrity_mismatch")

    def test_recomputed_checksum_cannot_hide_invalid_state_shape(self):
        edits = [lambda s: s.update(event_count=-1),
                 lambda s: s.update(pending_writes=[{**records()[0], "ID": "NEW"}]),
                 lambda s: s.update(cursors={"UNDECLARED": {}}),
                 lambda s: s.update(call_counts={"unknown": 1}),
                 lambda s: s.update(locks=[{"key": ["A001"], "owner": "UNDECLARED"}]),
                 lambda s: s.update(committed_progress={"UNDECLARED": ["A001"]})]
        for edit in edits:
            with self.subTest(edit=edit):
                state = deepcopy(self.state)
                edit(state)
                framework_io._seal(state)
                result = apply_io_call(self.contract, state, "ACCESS", {"FUNCTION": "FIRST", "RECORD-ID": ""})[0]
                self.assertEqual(result["boundary"]["reason"], "io_state_shape_invalid")

    def test_state_cannot_upgrade_verification_or_cross_contract_versions(self):
        for name, value in (("runtime_verified", True), ("contract_hash", "other")):
            state = {**self.state, name: value}
            result = apply_io_call(self.contract, state, "ACCESS", {"FUNCTION": "FIRST", "RECORD-ID": ""})[0]
            self.assertEqual(result["boundary"]["reason"], "io_state_contract_mismatch")
            self.assertFalse(result["runtime_verified"])
        self.contract["contract_version"] = "2"
        self.assertEqual(self.call("FIRST")["boundary"]["reason"], "io_state_contract_mismatch")

    def test_event_budget_stops_before_another_effect(self):
        with mock.patch.object(framework_io, "MAX_EVENTS", 2):
            self.status("FIRST")
            self.status("NEXT")
            state = deepcopy(self.state)
            result = self.call("NEXT")
            self.assertEqual(result["boundary"]["reason"], "io_event_budget_exhausted")
            self.assertEqual(result["state"], state)


if __name__ == "__main__":
    unittest.main()
