"""Conversation subjects come from user objects and local indexed definitions."""

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
from conversation_focus import build_session_subject, resolve_conversation_subject, resolve_question_field_subject
from repository_identity import resolve_source_identity


def program(name, declarations, body="GOBACK."):
    return ("IDENTIFICATION DIVISION.\nPROGRAM-ID. " + name + ".\nDATA DIVISION.\n"
        "WORKING-STORAGE SECTION.\n" + declarations + "\nPROCEDURE DIVISION.\nMAIN.\n" + body + "\n")


FIRST_QUESTION = "ENTRYPOINT 第一种页合计 WS-PAGE-TOTAL 和第二种账户合计 WS-ACCOUNT-TOTAL，何时清零？"


class ConversationFocusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.main_source = program("ENTRYPOINT", "01 WS-PAGE-TOTAL PIC 9(5) VALUE 0.\n"
            "01 WS-ACCOUNT-TOTAL PIC 9(5) VALUE 0.\n01 WS-GRAND-TOTAL PIC 9(5) VALUE 0.\n"
            "01 INPUT-REC.\n05 AMOUNT PIC 9(5).\n01 OUTPUT-REC.\n05 AMOUNT PIC 9(5).",
            "ADD 1 TO WS-PAGE-TOTAL WS-ACCOUNT-TOTAL.\nGOBACK.")
        self.source.joinpath("entry.cbl").write_text(self.main_source, encoding="utf-8")
        self.source.joinpath("other.cbl").write_text(program("OTHERPOINT", "01 OTHER-AMOUNT PIC 9(5)."), encoding="utf-8")
        for letter in ("a", "b"):
            self.source.joinpath("duplicate-" + letter + ".cbl").write_text(
                program("SHAREDPOINT", "01 SHARED-AMOUNT PIC 9(5)."), encoding="utf-8")
        self.rebuild()
        self.connection = sqlite3.connect(self.database)
        self.addCleanup(self.connection.close)

    def rebuild(self):
        build_business_index(self.source, self.database, source_format="free", verify_content=True, quiet=True)

    def subject(self, question=FIRST_QUESTION, previous=None):
        identity = resolve_source_identity(self.connection, question)
        return build_session_subject(self.connection, question, identity, previous=previous)

    def history(self, subject=None, question=FIRST_QUESTION, answer="OTHERPOINT 也许是模型猜测的主体。"):
        return [{"role": "user", "content": question}, {"role": "assistant", "content": answer,
            "investigation_state": {"session_subject": subject or self.subject(question)}}]

    def test_local_subject_has_exact_program_fields_and_user_ordinals(self):
        subject = self.subject()
        self.assertEqual(subject["schema"], "conversation-subject/v1")
        self.assertEqual(subject["status"], "resolved")
        self.assertEqual(subject["root_paths"], ["entry.cbl"])
        self.assertEqual(subject["entities"], ["WS-PAGE-TOTAL", "WS-ACCOUNT-TOTAL"])
        self.assertEqual(subject["ordinal_subjects"], [
            {"ordinal": 1, "entities": ["WS-PAGE-TOTAL"]},
            {"ordinal": 2, "entities": ["WS-ACCOUNT-TOTAL"]}])
        self.assertEqual(subject["programs"][0]["source_sha256"], hashlib.sha256(
            self.source.joinpath("entry.cbl").read_bytes()).hexdigest())

    def test_generic_followup_preserves_program_without_using_model_prose(self):
        result = resolve_conversation_subject(self.connection, "再解释一下流程。", self.history())
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["direct_paths"], ["entry.cbl"])
        self.assertFalse(result["hard_constraint"])
        self.assertEqual(result["selection_source"], "conversation_subject")

    def test_second_subject_then_pronoun_retains_only_the_second_field_focus(self):
        result = resolve_conversation_subject(self.connection, "刚才第二种情况呢？", self.history())
        self.assertEqual(result["session_subject"]["focus_entities"], ["WS-ACCOUNT-TOTAL"])
        history = self.history() + [{"role": "user", "content": "刚才第二种情况呢？"},
            {"role": "assistant", "content": "此处模型正文不是主体依据。",
             "investigation_state": {"session_subject": result["session_subject"]}}]
        followup = resolve_conversation_subject(self.connection, "它从哪里来？", history)
        self.assertEqual(followup["session_subject"]["focus_entities"], ["WS-ACCOUNT-TOTAL"])

    def test_ordinal_is_not_inferred_from_field_or_rule_order(self):
        initial = self.subject("ENTRYPOINT 的 WS-PAGE-TOTAL 和 WS-ACCOUNT-TOTAL 怎样计算？")
        self.assertEqual(initial["ordinal_subjects"], [])
        result = resolve_conversation_subject(self.connection, "第二种呢？", self.history(initial,
            "ENTRYPOINT 的 WS-PAGE-TOTAL 和 WS-ACCOUNT-TOTAL 怎样计算？"))
        self.assertEqual(result["session_subject"]["focus_entities"], [])
        self.assertIn("conversation_ordinal_not_established", result["session_subject"]["unresolved"])

    def test_current_explicit_program_path_entry_and_hard_scope_win(self):
        for question in ("OTHERPOINT 的业务逻辑？", "只分析 other.cbl，不读取其他文件。", "程序 UNKNOWNPOINT 的逻辑？"):
            with self.subTest(question=question):
                self.assertIsNone(resolve_conversation_subject(self.connection, question, self.history()))
        self.assertIsNone(resolve_conversation_subject(self.connection, "这个字段呢？", self.history(),
            current_identity={"status": "resolved", "direct_paths": ["other.cbl"], "hard_constraint": True}))

    def test_explicit_other_field_does_not_stick_to_old_program(self):
        self.assertIsNone(resolve_conversation_subject(self.connection, "OTHER-AMOUNT 从哪里来？", self.history()))
        for question in ("字段 UNKNOWN-AMOUNT 从哪里来？", "NEW-AMOUNT 怎么计算？"):
            with self.subTest(question=question):
                result = resolve_conversation_subject(self.connection, question, self.history())
                self.assertEqual(result["direct_paths"], ["entry.cbl"])
                self.assertEqual(result["session_subject"]["focus_entities"], [])
                self.assertIn("conversation_field_not_bound", result["session_subject"]["unresolved"])

    def test_explicit_topic_resets_do_not_inherit_old_subject(self):
        for question in ("换个问题。", "不看刚才程序了。", "全库有哪些程序？"):
            with self.subTest(question=question):
                self.assertIsNone(resolve_conversation_subject(self.connection, question, self.history()))

    def test_qualified_fields_are_exact_and_qualifiers_are_not_entities(self):
        subject = self.subject("ENTRYPOINT AMOUNT OF INPUT-REC 与 AMOUNT OF OUTPUT-REC 有什么关系？")
        self.assertEqual(subject["entities"], ["AMOUNT OF INPUT-REC", "AMOUNT OF OUTPUT-REC"])
        self.assertEqual(len(subject["entity_bindings"]), 2)
        result = resolve_conversation_subject(self.connection, "AMOUNT OF OUTPUT-REC 从哪里来？", self.history(subject,
            "ENTRYPOINT AMOUNT OF INPUT-REC 与 AMOUNT OF OUTPUT-REC 有什么关系？"))
        self.assertEqual(result["session_subject"]["focus_entities"], ["AMOUNT OF OUTPUT-REC"])

    def test_current_ambiguous_field_clears_old_field_focus_but_keeps_program(self):
        subject = build_session_subject(self.connection, "字段 AMOUNT 从哪里来？",
            {"status": "resolved", "direct_paths": ["entry.cbl"]}, previous=self.subject())
        self.assertEqual(subject["status"], "resolved")
        self.assertEqual(subject["focus_entities"], [])
        self.assertIn("conversation_field_ambiguous", subject["unresolved"])

    def test_explicit_same_program_preserves_ordinals_and_new_field_replaces_focus(self):
        second = build_session_subject(self.connection, "ENTRYPOINT WS-GRAND-TOTAL 从哪里来？",
            resolve_source_identity(self.connection, "ENTRYPOINT WS-GRAND-TOTAL 从哪里来？"), previous=self.subject())
        self.assertEqual(second["focus_entities"], ["WS-GRAND-TOTAL"])
        self.assertEqual(second["ordinal_subjects"][1]["entities"], ["WS-ACCOUNT-TOTAL"])

    def test_changed_source_rebinds_current_hash_and_locations_without_mutating_history(self):
        original = self.subject()
        saved = deepcopy(original)
        self.source.joinpath("entry.cbl").write_text("\n" + self.main_source.replace("VALUE 0", "VALUE 1"), encoding="utf-8")
        self.rebuild()
        result = resolve_conversation_subject(self.connection, "它从哪里来？", self.history(original))
        current = result["session_subject"]
        self.assertTrue(current["version_changed"])
        self.assertTrue(current["revalidated"])
        self.assertTrue(current["previous_positions_discarded"])
        self.assertNotEqual(current["programs"][0]["source_sha256"], original["programs"][0]["source_sha256"])
        self.assertEqual(current["programs"][0]["definition_line"], original["programs"][0]["definition_line"] + 1)
        self.assertEqual(original, saved)

    def test_unknown_and_ambiguous_latest_user_switches_block_old_subject(self):
        for question in ("分析程序 UNKNOWNPOINT。", "分析程序 SHAREDPOINT。", "OTHER-AMOUNT 从哪里来？"):
            with self.subTest(question=question):
                history = self.history() + [{"role": "user", "content": question}]
                self.assertIsNone(resolve_conversation_subject(self.connection, "继续解释。", history))

    def test_latest_completed_empty_or_unresolved_subject_is_a_barrier(self):
        for subject in ({}, None, {"schema": "conversation-subject/v1", "status": "unresolved"}):
            with self.subTest(subject=subject):
                history = self.history() + [{"role": "user", "content": "分析程序 UNKNOWNPOINT。"},
                    {"role": "assistant", "content": "尚未定位。", "investigation_state": {"session_subject": subject}}]
                self.assertIsNone(resolve_conversation_subject(self.connection, "继续解释。", history))

    def test_missing_legacy_state_reads_only_latest_explicit_user_question(self):
        history = [{"role": "user", "content": FIRST_QUESTION},
            {"role": "assistant", "content": "OTHERPOINT 是模型提出的另一个候选。",
             "evidence_refs": [{"relative_path": "other.cbl"}]}]
        result = resolve_conversation_subject(self.connection, "第二种呢？", history)
        self.assertEqual(result["direct_paths"], ["entry.cbl"])
        self.assertEqual(result["session_subject"]["focus_entities"], ["WS-ACCOUNT-TOTAL"])
        history += [{"role": "user", "content": "分析程序 UNKNOWNPOINT。"},
            {"role": "assistant", "content": "缺少资料。"}]
        self.assertIsNone(resolve_conversation_subject(self.connection, "继续解释。", history))

    def test_assistant_text_and_citations_alone_do_not_establish_subject(self):
        self.assertIsNone(resolve_conversation_subject(self.connection, "这个字段呢？", [
            {"role": "assistant", "content": "ENTRYPOINT 的 WS-PAGE-TOTAL。",
             "evidence_refs": [{"relative_path": "entry.cbl"}], "cited_evidence_ids": ["ev_placeholder"]}]))

    def test_new_explicit_user_program_can_replace_stale_saved_subject(self):
        history = self.history() + [{"role": "user", "content": "OTHERPOINT 的 OTHER-AMOUNT 从哪里来？"}]
        result = resolve_conversation_subject(self.connection, "继续解释。", history)
        self.assertEqual(result["direct_paths"], ["other.cbl"])
        self.assertEqual(result["session_subject"]["focus_entities"], ["OTHER-AMOUNT"])

    def test_different_repository_and_deleted_program_do_not_inherit_subject(self):
        subject = self.subject()
        subject["source_root_hash"] = "another-source-root"
        self.assertIsNone(resolve_conversation_subject(self.connection, "继续解释。", self.history(subject)))
        subject = self.subject()
        self.source.joinpath("entry.cbl").unlink()
        self.rebuild()
        self.assertIsNone(resolve_conversation_subject(self.connection, "继续解释。", self.history(subject)))

    def test_multi_program_file_does_not_bind_another_programs_field(self):
        self.source.joinpath("multi.cbl").write_text(program("FIRSTPOINT", "01 FIRST-AMOUNT PIC 9.")
            + program("SECONDPOINT", "01 SECOND-AMOUNT PIC 9."), encoding="utf-8")
        self.rebuild()
        subject = self.subject("FIRSTPOINT 字段 SECOND-AMOUNT 从哪里来？")
        self.assertEqual(subject["status"], "candidate")
        self.assertEqual(subject["focus_entities"], [])
        self.assertEqual(subject["entity_bindings"], [])

    def test_many_fields_keep_focus_and_fit_independent_subject_storage_budget(self):
        fields = ["RESULT-" + str(index).zfill(3) for index in range(32)]
        self.source.joinpath("many.cbl").write_text(program("MANYPOINT",
            "\n".join("01 " + name + " PIC 9(5)." for name in fields)), encoding="utf-8")
        self.rebuild()
        subject = self.subject("MANYPOINT " + "、".join(fields) + " 从哪里来？")
        self.assertEqual(subject["focus_entities"], fields)
        self.assertLessEqual(len(json.dumps(subject, ensure_ascii=False).encode("utf-8")), 8192)
        self.assertGreater(subject.get("omitted_entity_binding_count", 0), 0)
        self.assertEqual(len(subject["entity_bindings"]) + subject["omitted_entity_binding_count"], 32)

    def test_first_question_field_can_locate_its_unique_program(self):
        identity = resolve_question_field_subject(self.connection, "WS-ACCOUNT-TOTAL 怎么计算？")
        self.assertEqual(identity["status"], "resolved")
        self.assertEqual(identity["direct_paths"], ["entry.cbl"])
        self.assertEqual(identity["selection_source"], "question_fields")
        self.assertFalse(identity["hard_constraint"])
        self.assertIsNone(resolve_question_field_subject(self.connection, "AMOUNT 怎么计算？"))
        self.assertIsNone(resolve_question_field_subject(self.connection, "NEW-AMOUNT 怎么计算？"))
        identity = resolve_question_field_subject(self.connection, "AMOUNT OF OUTPUT-REC 怎么计算？")
        self.assertEqual(identity["direct_paths"], ["entry.cbl"])

    def test_first_question_fields_in_different_programs_remain_candidates(self):
        identity = resolve_question_field_subject(self.connection, "WS-ACCOUNT-TOTAL 与 OTHER-AMOUNT 有什么关系？")
        self.assertEqual(identity["status"], "ambiguous")
        self.assertEqual(set(identity["direct_paths"]), {"entry.cbl", "other.cbl"})

    def test_generic_text_and_standalone_copy_do_not_prove_program_ownership(self):
        self.assertIsNone(resolve_question_field_subject(self.connection, "解释这个计算。"))
        self.source.joinpath("layout.cpy").write_text("01 SHARED-RECORD.\n05 COPY-AMOUNT PIC 9.\n", encoding="utf-8")
        self.rebuild()
        self.assertIsNone(resolve_question_field_subject(self.connection, "COPY-AMOUNT 从哪里来？"))

    def test_latest_user_unknown_field_clears_old_focus_before_another_answer(self):
        history = self.history() + [{"role": "user", "content": "NEW-AMOUNT 怎么计算？"}]
        result = resolve_conversation_subject(self.connection, "它从哪里来？", history)
        self.assertEqual(result["session_subject"]["focus_entities"], [])

    def test_oversized_program_location_is_explicitly_unresolved_and_still_fits(self):
        location = "nested/" * 1000 + "entry.cbl"
        with self.connection:
            self.connection.execute("UPDATE code_units SET relative_path=? WHERE relative_path='entry.cbl'", (location,))
            self.connection.execute("UPDATE source_files SET relative_path=? WHERE relative_path='entry.cbl'", (location,))
        subject = build_session_subject(self.connection, "继续说明。", {"status": "resolved", "direct_paths": [location]})
        self.assertEqual(subject["status"], "unresolved")
        self.assertIn("conversation_subject_storage_budget", subject["unresolved"])
        self.assertLessEqual(len(json.dumps(subject, ensure_ascii=False).encode("utf-8")), 8192)


if __name__ == "__main__":
    unittest.main()
