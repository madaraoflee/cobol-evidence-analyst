from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

import framework_demo  # noqa: E402
from framework_demo import FrameworkDemoError, load_framework_demo, select_framework_demo_case  # noqa: E402


def source(program: str) -> str:
    return f"""       IDENTIFICATION DIVISION.
       PROGRAM-ID. {program}.
       PROCEDURE DIVISION.
       MAIN-FLOW SECTION.
           COPY 'shared-flow.cpy'.
           DISPLAY 'CALL FAKE-PROGRAM'
      *    CALL 'COMMENT-TARGET'
       FINISH-FLOW SECTION.
           GOBACK.
"""


class FrameworkDemoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "fixture"
        self.sources = self.root / "source"
        self.sources.mkdir(parents=True)
        manifest = {"id": "framework-workbench", "title": "合成框架案例", "description": "本地源码导读。",
                    "default_case": "online", "cases": []}
        for case_id, program in (("online", "ONLINE-ENTRY"), ("client_server", "SCREEN-ENTRY"), ("batch", "BATCH-ENTRY")):
            relative = f"{case_id}.cbl"
            (self.sources / relative).write_text(source(program), encoding="utf-8")
            manifest["cases"].append({
                "id": case_id, "kind": case_id, "title": f"{case_id}案例", "entry_program": program,
                "entry_path": relative, "question": "解释实际控制流程。", "purpose": "演示源码阅读。",
                "steps": [{"title": "入口处理", "description": "读取源码中的 COPY 和调用。",
                           "path": relative, "anchor": "MAIN-FLOW SECTION."}],
                "metadata": [{"label": "来源", "value": "合成源码"}],
                "boundaries": ["生成的存储程序只有调用点，未提供实现。"],
            })
        (self.sources / "shared-flow.cpy").write_text("           CALL 'WORKER-ENTRY'.\n", encoding="utf-8")
        (self.sources / "worker.cbl").write_text(
            "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. WORKER-ENTRY.\n"
            "       PROCEDURE DIVISION.\n           CALL 'GENERATED-STORE'\n           GOBACK.\n", encoding="utf-8",
        )
        self.manifest = manifest
        self.save_manifest()
        patcher = mock.patch.object(framework_demo, "FIXTURE_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def save_manifest(self) -> None:
        (self.root / "manifest.json").write_text(json.dumps(self.manifest, ensure_ascii=False), encoding="utf-8")

    def test_current_source_drives_evidence_programs_and_calls(self) -> None:
        demo = load_framework_demo()
        case = select_framework_demo_case(demo, "online")
        self.assertEqual(demo["file_count"], 5)
        self.assertEqual(len(demo["programs"]), 4)
        self.assertEqual(demo["analysis_origin"], "synthetic_source_guide")
        self.assertFalse(demo["model_called"])
        self.assertEqual(case["trace"], [])
        self.assertEqual({(row["source_program"], row["target_name"]) for row in case["relations"]}, {
            ("ONLINE-ENTRY", "WORKER-ENTRY"), ("WORKER-ENTRY", "GENERATED-STORE"),
        })
        self.assertNotIn("FAKE-PROGRAM", {row["target_name"] for row in case["relations"]})
        self.assertNotIn("COMMENT-TARGET", {row["target_name"] for row in case["relations"]})
        evidence = {item["evidence_id"]: item for item in case["evidence"]}
        for item in case["evidence"]:
            raw = (self.sources / item["relative_path"]).read_bytes()
            lines = raw.decode().splitlines()
            self.assertEqual(item["source_text"], "\n".join(lines[item["start_line"] - 1:item["end_line"]]))
            self.assertEqual(item["source_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(item["integrity"], "SYNTHETIC_SOURCE")
        self.assertTrue(all(row["evidence_id"] in evidence for row in case["relations"]))
        first = evidence[case["steps"][0]["evidence_id"]]
        self.assertEqual(first["start_line"], 4)
        self.assertEqual(first["end_line"], 7)

    def test_file_mutation_changes_real_line_numbers_hashes_and_snapshot(self) -> None:
        original = load_framework_demo()
        path = self.sources / "online.cbl"
        path.write_text("\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
        updated = load_framework_demo()
        first = select_framework_demo_case(original, "online")["evidence"][0]
        second = select_framework_demo_case(updated, "online")["evidence"][0]
        self.assertEqual(second["start_line"], first["start_line"] + 1)
        self.assertNotEqual(second["source_sha256"], first["source_sha256"])
        self.assertNotEqual(second["evidence_id"], first["evidence_id"])
        self.assertNotEqual(original["snapshot_id"], updated["snapshot_id"])

    def test_manifest_content_participates_in_snapshot(self) -> None:
        original = load_framework_demo()
        self.manifest["description"] = "更新后的本地导读。"
        self.save_manifest()
        updated = load_framework_demo()
        self.assertNotEqual(original["snapshot_id"], updated["snapshot_id"])
        self.assertEqual(original["cases"][0]["evidence"][0]["source_sha256"], updated["cases"][0]["evidence"][0]["source_sha256"])

    def test_program_inventory_is_not_a_fixed_demo_list(self) -> None:
        (self.sources / "additional.cbl").write_text(source("ADDITIONAL-ENTRY"), encoding="utf-8")
        demo = load_framework_demo()
        self.assertEqual(len(demo["programs"]), 5)
        self.assertIn("ADDITIONAL-ENTRY", {program["program_name"] for program in demo["programs"]})

    def test_unknown_case_is_rejected_and_selected_case_is_a_copy(self) -> None:
        demo = load_framework_demo()
        for case_id in (None, "../online", "missing", {"id": "online"}):
            with self.subTest(case_id=case_id), self.assertRaises(FrameworkDemoError) as raised:
                select_framework_demo_case(demo, case_id)
            self.assertEqual(raised.exception.code, "FRAMEWORK_DEMO_CASE_UNKNOWN")
        case = select_framework_demo_case(demo, "online")
        case["steps"].clear()
        self.assertTrue(demo["cases"][0]["steps"])

    def test_missing_or_ambiguous_anchor_is_unavailable(self) -> None:
        self.manifest["cases"][0]["steps"][0]["anchor"] = "NO-SUCH-SECTION."
        self.save_manifest()
        with self.assertRaises(FrameworkDemoError) as raised:
            load_framework_demo()
        self.assertEqual(raised.exception.code, "FRAMEWORK_DEMO_ANCHOR_MISSING")
        self.manifest["cases"][0]["steps"][0]["anchor"] = "MAIN-FLOW SECTION."
        self.save_manifest()
        path = self.sources / "online.cbl"
        path.write_text(path.read_text() + "       MAIN-FLOW SECTION.\n", encoding="utf-8")
        with self.assertRaises(FrameworkDemoError) as raised:
            load_framework_demo()
        self.assertEqual(raised.exception.code, "FRAMEWORK_DEMO_ANCHOR_AMBIGUOUS")

    def test_manifest_step_cannot_escape_source_directory(self) -> None:
        outside = Path(self.temporary.name) / "outside.cbl"
        outside.write_text(source("OUTSIDE-ENTRY"), encoding="utf-8")
        for path in ("../outside.cbl", str(outside), "..\\outside.cbl", "C:/outside.cbl"):
            with self.subTest(path=path):
                self.manifest["cases"][0]["steps"][0]["path"] = path
                self.save_manifest()
                with self.assertRaises(FrameworkDemoError) as raised:
                    load_framework_demo()
                self.assertEqual(raised.exception.code, "FRAMEWORK_DEMO_PATH_INVALID")

    def test_symlink_source_file_or_directory_is_rejected(self) -> None:
        outside = Path(self.temporary.name) / "outside.cbl"
        outside.write_text(source("OUTSIDE-ENTRY"), encoding="utf-8")
        link = self.sources / "unsafe.cbl"
        link.symlink_to(outside)
        with self.assertRaises(FrameworkDemoError) as raised:
            load_framework_demo()
        self.assertEqual(raised.exception.code, "FRAMEWORK_DEMO_PATH_INVALID")
        link.unlink()
        (self.sources / "unsafe-directory").symlink_to(outside.parent, target_is_directory=True)
        with self.assertRaises(FrameworkDemoError) as raised:
            load_framework_demo()
        self.assertEqual(raised.exception.code, "FRAMEWORK_DEMO_PATH_INVALID")

    def test_manifest_symlink_is_rejected(self) -> None:
        path = self.root / "manifest.json"
        outside = Path(self.temporary.name) / "outside.json"
        path.replace(outside)
        path.symlink_to(outside)
        with self.assertRaises(FrameworkDemoError) as raised:
            load_framework_demo()
        self.assertEqual(raised.exception.code, "FRAMEWORK_DEMO_PATH_INVALID")

    def test_locale_objects_are_preserved_without_coercion(self) -> None:
        title = {"zh-CN": "入口", "zh-HK": "入口", "en": "Entry"}
        self.manifest["title"] = title
        self.manifest["cases"][0]["question"] = title
        self.manifest["cases"][0]["metadata"][0]["value"] = title
        self.save_manifest()
        demo = load_framework_demo()
        self.assertEqual(demo["title"], title)
        self.assertEqual(demo["cases"][0]["question"], title)
        self.assertEqual(demo["cases"][0]["metadata"][0]["value"], title)

    def test_malformed_case_identifiers_fail_safely(self) -> None:
        self.manifest["cases"][0]["id"] = {"unexpected": "object"}
        self.save_manifest()
        with self.assertRaises(FrameworkDemoError) as raised:
            load_framework_demo()
        self.assertEqual(raised.exception.code, "FRAMEWORK_DEMO_MANIFEST_INVALID")


class BundledFrameworkDemoTests(unittest.TestCase):
    def test_bundled_cases_have_actual_source_evidence_and_no_model_trace(self) -> None:
        demo = load_framework_demo()
        self.assertEqual({case["id"] for case in demo["cases"]}, {"online", "client_server", "batch"})
        self.assertFalse(demo["model_called"])
        self.assertTrue(Path(demo["source_path"]).is_dir())
        for case in demo["cases"]:
            self.assertTrue(case["steps"])
            self.assertTrue(case["evidence"])
            self.assertEqual(case["trace"], [])
            references = {item["evidence_id"] for item in case["evidence"]}
            self.assertTrue(all(step["evidence_id"] in references for step in case["steps"]))


if __name__ == "__main__":
    unittest.main()
