"""Offline CL facts: source commands stay distinct from job execution."""
from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
from cl_context import CLSourceCollector, bind_indexed_cl_context, collect_cl_context, nominate_cl_evidence
from repository_discovery import ensure_repository_search
from structural_index import build_structural_index

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "cl-batch-v1"


def parse(text, path="sample.clle"):
    sha = hashlib.sha256(text.encode()).hexdigest()
    collector = CLSourceCollector(path, sha)
    for number, raw in enumerate(text.splitlines(), 1):
        collector.consume(number, raw)
    collector.finish()
    return collector


def page(path, text, first=1, sha=None, eid=None):
    return {"relative_path": path, "start_line": first, "end_line": first + len(text.splitlines()) - 1,
            "source_text": text, "source_sha256": sha or hashlib.sha256(text.encode()).hexdigest(),
            "evidence_id": eid or f"source:{path}:{first}", "span_truncated": False}


class CLSourceFactTests(unittest.TestCase):
    def test_nested_submission_does_not_inherit_parent_overrides(self):
        collector = parse("OVRDBF FILE(INPUTLF) TOFILE(DATALIB/INPUTPF) OVRSCOPE(*JOB)\n"
            "SBMJOB CMD(CALL PGM(RUNLIB/NIGHTJOB) PARM('A B')) +\n"
            " JOB(BATCHONE) JOBD(CFGLIB/JOBDESC) INLLIBL(RUNLIB DATALIB)\n"
            "CALL PGM(DIRECTJOB) PARM(&DAY 'A B')\n")
        calls = [row for row in collector.facts if row["command"] == "CALL"]
        self.assertEqual([row["target_name"] for row in calls], ["NIGHTJOB", "DIRECTJOB"])
        submitted, direct = calls
        self.assertEqual(submitted["command_context"], "submitted_job_candidate")
        self.assertEqual(submitted["initial_library_list"], ["RUNLIB", "DATALIB"])
        self.assertEqual(submitted["environment_candidates"], [])
        self.assertEqual(submitted["arguments"], ["'A B'"])
        self.assertEqual((submitted["start_line"], submitted["end_line"]), (2, 3))
        self.assertEqual(direct["arguments"], ["&DAY", "'A B'"])
        self.assertEqual(len(direct["environment_candidates"]), 1)
        self.assertFalse(submitted["runtime_execution_verified"])

    def test_comments_literals_and_escaped_quote_never_make_commands(self):
        collector = parse("/* CALL PGM(COMMENTJOB)\n SBMJOB CMD(CALL PGM(ALSOFAKE)) */\n"
            "SNDMSG MSG('CALL PGM(STRINGJOB) /* not comment */ don''t') TOUSR(*SYSOPR)\n"
            "CALL PGM(REALJOB) PARM('CALL PGM(FAKEJOB)' '/* literal */')\n")
        self.assertEqual([row["target_name"] for row in collector.facts if row["command"] == "CALL"], ["REALJOB"])
        self.assertEqual(len(collector.facts), 1)

    def test_override_and_delete_keep_scope_and_order_without_effect_claim(self):
        collector = parse("CHGLIBL LIBL(RUNLIB DATALIB) CURLIB(*SAME)\n"
            "ADDLIBLE LIB(STAGELIB) POSITION(*FIRST)\n"
            "OVRDBF FILE(INPUTLF) TOFILE(*LIBL/INPUTPF) MBR(&MBR)\n"
            "CALL PGM(BATCHRUN)\nDLTOVR FILE(INPUTLF) LVL(*JOB)\nCALL PGM(BATCHRUN)\n")
        override = next(row for row in collector.facts if row["command"] == "OVRDBF")
        self.assertIsNone(override["declared_override_scope"])
        self.assertEqual(override["tofile"]["binding_status"], "library_list_candidate")
        self.assertFalse(override["effective_override_verified"])
        calls = [row for row in collector.facts if row["command"] == "CALL"]
        self.assertNotEqual(calls[0]["occurrence_id"], calls[1]["occurrence_id"])
        self.assertEqual([len(row["environment_candidates"]) for row in calls], [3, 4])
        self.assertFalse(calls[1]["environment_state_verified"])

    def test_dynamic_targets_and_monitor_are_bounded(self):
        collector = parse("CALL PGM(&TARGET) PARM(&ARG)\n"
            "MONMSG MSGID(CPF0000) EXEC(CALL PGM(LOGFAIL))\n"
            "SNDMSG MSG('neutral') TOUSR(*SYSOPR)\nMONMSG MSGID(CPF0000)\n")
        dynamic = collector.facts[0]
        self.assertIsNone(dynamic["target_name"])
        self.assertIn("cl_dynamic_program_target", dynamic["diagnostics"])
        monitor = collector.facts[1]
        self.assertEqual(monitor["monitored_command_occurrence_id"], dynamic["occurrence_id"])
        error_call = collector.facts[2]
        self.assertEqual(error_call["target_name"], "LOGFAIL")
        self.assertFalse(error_call["guards"][0]["error_occurred_verified"])
        self.assertIsNone(collector.facts[-1]["monitored_command_occurrence_id"])

    def test_if_and_loop_condition_are_preserved_not_evaluated(self):
        collector = parse("IF COND(&READY *EQ 'Y') THEN(DO)\nCALL PGM(BATCHRUN)\nENDDO\n"
                          "IF COND(&READY *EQ 'N') THEN(CALL PGM(SKIPRUN))\n")
        self.assertEqual([row["target_name"] for row in collector.facts], ["BATCHRUN", "SKIPRUN"])
        self.assertEqual(collector.facts[0]["guards"][0]["condition"], "&READY *EQ 'Y'")
        self.assertFalse(collector.facts[1]["guards"][0]["outcome_evaluated"])

    def test_positional_call_program_and_parameter_list_are_source_facts(self):
        collector = parse("CALL BATCHPAY\nCALL RUNLIB/BATCHPAY (&DAY 'A B')\n"
                          "CALL 'RUNLIB/BATCHPAY' PARM(&DAY 'X')\n")
        self.assertEqual([row["target_name"] for row in collector.facts], ["BATCHPAY"] * 3)
        self.assertEqual(collector.facts[1]["arguments"], ["&DAY", "'A B'"])
        self.assertEqual(collector.facts[2]["arguments"], ["&DAY", "'X'"])
        self.assertTrue(all(not row["diagnostics"] for row in collector.facts))

    def test_else_is_bound_to_preceding_if_with_false_outcome(self):
        collector = parse("IF COND(&READY *EQ 'Y') THEN(CALL YESJOB)\nELSE CMD(CALL NOJOB)\n"
                          "ELSE CMD(CALL ORPHANJOB)\n")
        self.assertEqual([row["target_name"] for row in collector.facts], ["YESJOB", "NOJOB", "ORPHANJOB"])
        positive, negative, orphan = collector.facts
        self.assertTrue(positive["guards"][0]["outcome"])
        self.assertFalse(negative["guards"][0]["outcome"])
        self.assertEqual(negative["guards"][0]["condition"], "&READY *EQ 'Y'")
        self.assertEqual(negative["guards"][0]["if_source_ref"]["start_line"], 1)
        self.assertEqual(orphan["status"], "candidate")
        self.assertIn("cl_control_scope_unresolved", orphan["diagnostics"])

    def test_literal_without_required_continuation_cannot_make_static_call(self):
        collector = parse("CALL PGM('BAT\nCHPAY')\nCALL REALJOB\n")
        self.assertEqual([row["target_name"] for row in collector.facts], ["REALJOB"])
        self.assertIn("cl_literal_continuation_missing", {row["reason"] for row in collector.boundaries})

    def test_unclosed_commands_are_diagnostics_not_relation_facts(self):
        collector = parse("CALL PGM(UNKNOWN +\n")
        self.assertEqual(collector.facts, [])
        self.assertIn("cl_unclosed_statement", {row["reason"] for row in collector.boundaries})

    def test_plus_and_minus_continuations_preserve_exact_literal_blanks(self):
        collector = parse("CALL PGM(NIGHT+\n   JOB) PARM('CONT+\n   INUED' 'TEXT -\n   WITH BLANKS')\n")
        self.assertEqual(len(collector.facts), 1)
        call = collector.facts[0]
        self.assertEqual(call["target_name"], "NIGHTJOB")
        self.assertEqual(call["arguments"], ["'CONTINUED'", "'TEXT    WITH BLANKS'"])
        self.assertEqual(call["end_line"], 4)

    def test_omitted_job_parameters_do_not_invent_library_configuration(self):
        collector = parse("SBMJOB CMD(CALL PGM(NIGHTJOB))\n")
        submit, call = collector.facts
        self.assertIsNone(submit["initial_library_list"])
        self.assertIsNone(call["initial_library_list"])
        self.assertEqual(submit["initial_library_list_origin"], "omitted_runtime_or_command_default")

    def test_only_supplied_consistent_physical_pages_become_evidence(self):
        text = "OVRDBF FILE(INPUTLF) TOFILE(DATALIB/INPUTPF)\nCALL PGM(BATCHRUN)"
        supplied = page("job.clle", text)
        result = collect_cl_context([supplied])
        self.assertEqual(len(result["observations"]), 2)
        self.assertTrue(all(row["evidence_ids"] == [supplied["evidence_id"]] for row in result["observations"]))
        self.assertEqual(result["observations"][1]["statement"], "CALL PGM(BATCHRUN)")
        self.assertEqual(collect_cl_context([supplied, {**supplied, "source_sha256": "other"}])["observations"], [])
        self.assertEqual(collect_cl_context([{**supplied, "span_truncated": True}])["observations"], [])
        gap = collect_cl_context([page("job.clle", "CALL PGM(BATCHRUN)", first=20)])
        self.assertIn("cl_source_scope_gap", {row["reason"] for row in gap["boundaries"]})
        self.assertEqual(gap["observations"], [])

    def test_gap_inside_comment_cannot_manufacture_a_call(self):
        sha = "same-file-version"
        result = collect_cl_context([page("job.clle", "PGM\n/* continued comment", sha=sha),
            page("job.clle", "CALL PGM(FAKEJOB)\n*/\nCALL PGM(REALJOB)", first=20, sha=sha)])
        self.assertEqual(result["observations"], [])
        self.assertIn("cl_lexical_prefix_unavailable", {row["reason"] for row in result["boundaries"]})


class CLIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cl-context-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        shutil.copytree(FIXTURE, self.source)
        self.database = self.root / "index.sqlite"

    def assert_index(self, builder):
        builder(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        candidates, frontier, hashes = nominate_cl_evidence(self.database, ["batchpay.cbl"], ["BATCHPAY"])
        self.assertEqual({row["relative_path"] for row in candidates}, {"launcher.clle", "nightjob.clle"})
        self.assertEqual(set(hashes), {"launcher.clle", "nightjob.clle"})
        self.assertEqual(frontier, [])
        for row in candidates:
            text = (self.source / row["relative_path"]).read_text()
            self.assertEqual(row["statement"], "\n".join(text.splitlines()[row["start_line"]-1:row["end_line"]]))
            self.assertEqual(row["source_sha256"], hashlib.sha256((self.source / row["relative_path"]).read_bytes()).hexdigest())
        with closing(sqlite3.connect(self.database)) as db, db:
            targets = {row[0] for row in db.execute("SELECT target_name FROM cl_command_facts WHERE command='CALL'")}
            self.assertNotIn("FAKEJOB", targets)
            self.assertNotIn("COMMENTJOB", targets)
            self.assertNotIn("STRINGJOB", targets)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM relations WHERE relative_path LIKE '%.clle'").fetchone()[0], 0)
            query_plan = db.execute("EXPLAIN QUERY PLAN SELECT relative_path FROM cl_command_facts WHERE target_name=?", ("BATCHPAY",)).fetchall()
            self.assertTrue(any("idx_cl_facts_target" in row[-1] for row in query_plan))
        return candidates

    def test_business_index_persists_bounded_facts_and_refreshes_changed_files(self):
        candidates = self.assert_index(build_business_index)
        source = self.source / "nightjob.clle"
        source.write_text(source.read_text().replace("PGM(BATCHPAY)", "PGM(NEWBATCH)"))
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        new, _, _ = nominate_cl_evidence(self.database, [], ["BATCHPAY"])
        self.assertEqual(new, [])
        newer, _, _ = nominate_cl_evidence(self.database, [], ["NEWBATCH"])
        self.assertTrue(newer)
        self.assertNotEqual(candidates[0]["source_sha256"], next(row for row in newer if row["relative_path"] == "nightjob.clle")["source_sha256"])

    def test_structural_index_uses_same_cl_frontend(self):
        self.assert_index(build_structural_index)

    def test_clp_import_and_late_target_are_selected_with_small_read_budget(self):
        long_job = self.source / "longjob.clp"
        long_job.write_text("PGM\n" + "CALL PGM(UNRELATED)\n" * 100 + "CALL PGM(TARGETPAY)\nENDPGM\n")
        build_business_index(self.source, self.database, source_format="free")
        candidates, frontier, _ = nominate_cl_evidence(self.database, [], ["TARGETPAY"], max_candidates=2)
        self.assertTrue(any("PGM(TARGETPAY)" in row["statement"] for row in candidates))
        self.assertTrue(all(row["relative_path"] == "longjob.clp" for row in candidates))
        self.assertIn("cl_candidate_budget", {row["reason"] for row in frontier})

    def late_fixture(self):
        lines = ["PGM", "OVRDBF FILE(INPUTLF) TOFILE(DATALIB/INPUTPF)",
                 "IF COND(&READY *EQ 'Y') THEN(DO)"]
        lines.extend(["/* neutral note */"] * 300)
        lines.extend(["/* multi-line comment", "CALL PGM(FAKEJOB)", "*/", "CALL BATCHPAY",
                      "CALL BATCHPAY", "ENDDO", "ENDPGM"])
        path = self.source / "latejob.clle"
        path.write_text("\n".join(lines) + "\n")
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        candidates, _, hashes = nominate_cl_evidence(self.database, [], ["BATCHPAY"])
        candidates = [row for row in candidates if row["relative_path"] == path.name]
        return lines, path, hashes[path.name], candidates

    def test_late_calls_bind_exact_spans_without_long_prefix_and_keep_occurrences(self):
        lines, path, sha, candidates = self.late_fixture()
        calls = [row for row in candidates if row["statement"] == "CALL BATCHPAY"]
        self.assertEqual(len(calls), 2)
        supplied = [page(path.name, row["statement"], row["start_line"], sha=sha,
                         eid="late:" + str(row["start_line"])) for row in calls]
        context = bind_indexed_cl_context(supplied, candidates)
        observations = context["observations"]
        self.assertEqual(len(observations), 2)
        self.assertNotEqual(observations[0]["occurrence_id"], observations[1]["occurrence_id"])
        self.assertTrue(all(row["start_line"] > 300 and row["target_name"] == "BATCHPAY" for row in observations))
        self.assertTrue(all(row["scope_unknown"] and row["status"] == "candidate" for row in observations))
        self.assertTrue(all(row["missing_guard_source_lines"] == [3] for row in observations))
        self.assertTrue(all(row["missing_environment_occurrence_ids"] for row in observations))
        self.assertFalse(any("FAKEJOB" == row.get("target_name") for row in observations))
        # Small separate source pages can cover the relevant context; the 300
        # unrelated comment lines never have to enter a model request.
        supplied.extend([page(path.name, "\n".join(lines[:3]), sha=sha, eid="prefix")])
        covered = bind_indexed_cl_context(supplied, candidates)
        bound_calls = [row for row in covered["observations"] if row["command"] == "CALL"]
        self.assertTrue(all(not row["scope_unknown"] for row in bound_calls))
        self.assertTrue(all(row["guard_evidence_ids"] == ["prefix"] for row in bound_calls))
        self.assertTrue(all(row["environment_evidence_ids"] == ["prefix"] for row in bound_calls))

    def test_indexed_binding_rejects_wrong_versions_raw_text_and_fake_literal_targets(self):
        _, path, sha, candidates = self.late_fixture()
        call = next(row for row in candidates if row["statement"] == "CALL BATCHPAY")
        valid = page(path.name, call["statement"], call["start_line"], sha=sha)
        for change in ({"source_sha256": "wrong-version"}, {"source_text": "CALL WRONGJOB"},
                       {"span_truncated": True}):
            self.assertEqual(bind_indexed_cl_context([{**valid, **change}], candidates)["observations"], [])
        forged = json.loads(json.dumps(call))
        forged["source_facts"][0]["target_name"] = "FAKEJOB"
        self.assertEqual(bind_indexed_cl_context([valid], [forged])["observations"], [])
        literal = "SNDMSG MSG('CALL BATCHPAY') TOUSR(*SYSOPR)"
        forged["statement"] = literal
        forged["source_facts"][0]["statement"] = literal
        self.assertEqual(bind_indexed_cl_context([page(path.name, literal, call["start_line"], sha=sha)],
            [forged])["observations"], [])

    def test_old_indexes_request_upgrade_and_budget_is_explicit(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE source_files(relative_path TEXT)")
        candidates, frontier, _ = nominate_cl_evidence(self.database, [], ["BATCHPAY"])
        self.assertEqual(candidates, [])
        self.assertEqual(frontier[0]["reason"], "cl_index_upgrade_required")
        self.database.unlink()
        build_business_index(self.source, self.database, source_format="free")
        candidates, frontier, _ = nominate_cl_evidence(self.database, [], ["BATCHPAY"], max_candidates=2)
        self.assertEqual(len(candidates), 2)
        self.assertIn("cl_candidate_budget", {row["reason"] for row in frontier})


if __name__ == "__main__":
    unittest.main()
