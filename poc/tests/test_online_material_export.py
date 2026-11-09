from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
import online_material_export as exporter
from online_material_format import MaterialError, bucket, canonical_json, digest, reference_key, validate_manifest


COMMIT = "a" * 40
SOURCE = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. ORDER-ENTRY.\nDATA DIVISION.\n"
          "WORKING-STORAGE SECTION.\nCOPY RECORDS.\n01 RESULT-AMOUNT PIC 9(5).\n"
          "PROCEDURE DIVISION.\nMAIN-PARA.\nIF RESULT-AMOUNT > 0\n"
          "CALL 'PAYMENT-ENTRY'\nEND-IF.\nMOVE 7 TO RESULT-AMOUNT.\nGOBACK.\n")


class MaterialExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "entry.cbl").write_text(SOURCE, encoding="utf-8")
        (self.source / "RECORDS.cpy").write_text("01 INPUT-AMOUNT PIC 9(5).\n", encoding="utf-8")
        self.database = self.root / "index.sqlite"
        self.report = self.build(self.source, self.database)

    @staticmethod
    def build(source, database):
        return build_business_index(source, database, source_format="free", verify_content=True,
                                    framework_reference_path="", quiet=True)

    def export(self, name="material", **kwargs):
        destination = self.root / name
        manifest = exporter.export_material(self.database, self.source, destination,
            repository_id="sample-workspace/business-source", commit=COMMIT, **kwargs)
        return destination, manifest

    def read_object(self, destination, reference):
        data = (destination / reference["path"]).read_bytes()
        self.assertEqual(digest(data), reference["sha256"])
        self.assertEqual(len(data), reference["size"])
        return json.loads(data)

    def lookup(self, destination, manifest, kind, key):
        ref = manifest["routes"][kind].get(bucket(key))
        if ref is None:
            return None
        return self.read_object(destination, ref)["entries"].get(key)

    def paths(self, destination, manifest, kind, key):
        entry = self.lookup(destination, manifest, kind, key)
        if entry is None:
            return []
        if "paths" in entry:
            self.assertEqual(entry["count"], len(entry["paths"]))
            return entry["paths"]
        result = []
        for ref in entry["pages"]:
            page = self.read_object(destination, ref)
            self.assertEqual((page["kind"], page["key"]), ("postings", key))
            result.extend(page["paths"])
        self.assertEqual(len(result), entry["count"])
        return result

    def test_real_index_exports_search_facts_reverse_relations_without_source_or_input_changes(self):
        before_database = self.database.read_bytes()
        before_files = {p.name: p.read_bytes() for p in self.source.iterdir()}
        destination, manifest = self.export(build_report=self.report)
        self.assertEqual(validate_manifest(manifest), manifest)
        self.assertEqual(manifest["coverage"]["indexed_files"], 2)
        self.assertEqual(manifest["coverage"]["source_bytes"], sum(map(len, before_files.values())))
        self.assertEqual(manifest["analysis"]["commit_verification"], "caller_declared")
        self.assertEqual(manifest["coverage"]["scope"], "indexed_snapshot_only")
        self.assertIn("REPOSITORY_COMPLETENESS_NOT_VERIFIED", [x["code"] for x in manifest["coverage"]["boundaries"]])
        self.assertEqual(self.paths(destination, manifest, "terms", "result-amount"), ["entry.cbl"])
        self.assertEqual(self.paths(destination, manifest, "references", reference_key("CALLS", "PAYMENT-ENTRY")), ["entry.cbl"])
        self.assertEqual(self.paths(destination, manifest, "references", reference_key("INCLUDES_COPY", "RECORDS")), ["entry.cbl"])
        entry = self.lookup(destination, manifest, "files", "entry.cbl")
        facts = [self.read_object(destination, ref) for ref in entry["facts"]]
        self.assertTrue(any(symbol["name"] == "ORDER-ENTRY" for page in facts for symbol in page["symbols"]))
        self.assertTrue(any(rule["rule_kind"] == "MOVE" for page in facts for rule in page["rules"]))
        self.assertEqual(entry["sha256"], hashlib.sha256(SOURCE.encode()).hexdigest())
        for file in destination.rglob("*.json"):
            data = file.read_text()
            self.assertNotIn(str(self.source), data)
            self.assertNotIn(str(self.database), data)
            self.assertNotIn("normalized_text", data)
            self.assertNotIn("IDENTIFICATION DIVISION.", data)
            self.assertNotIn("indexed_at_utc", data)
        self.assertEqual(self.database.read_bytes(), before_database)
        self.assertEqual({p.name: p.read_bytes() for p in self.source.iterdir()}, before_files)
        self.assertEqual({p.name for p in destination.iterdir()}, {"material.json", "objects"})

    def test_all_objects_remain_readable_after_original_source_and_database_are_removed(self):
        destination, manifest = self.export()
        shutil.rmtree(self.source)
        self.database.unlink()
        self.assertEqual(self.paths(destination, manifest, "terms", "order-entry"), ["entry.cbl"])
        file = self.lookup(destination, manifest, "files", "entry.cbl")
        self.assertTrue(all(self.read_object(destination, ref)["kind"] == "facts" for ref in file["facts"]))

    def test_cross_directory_reproducibility_and_cross_commit_object_reuse(self):
        first, manifest = self.export()
        other = self.root / "moved-source"
        shutil.copytree(self.source, other)
        database = self.root / "moved-index.sqlite"
        self.build(other, database)
        second = self.root / "second"
        repeated = exporter.export_material(database, other, second,
            repository_id="sample-workspace/business-source", commit=COMMIT)
        self.assertEqual(canonical_json(manifest), canonical_json(repeated))
        third = self.root / "new-commit"
        newer = exporter.export_material(database, other, third,
            repository_id="sample-workspace/business-source", commit="b" * 40)
        self.assertNotEqual(manifest["revision"], newer["revision"])
        self.assertEqual(manifest["routes"], newer["routes"])
        self.assertEqual({p.name for p in (first / "objects").iterdir()}, {p.name for p in (third / "objects").iterdir()})

    def test_changed_source_hash_rejects_and_cleans_own_output(self):
        (self.source / "entry.cbl").write_text(SOURCE.replace("MOVE 7", "MOVE 8"))
        with self.assertRaisesRegex(MaterialError, "MATERIAL_SOURCE_HASH_MISMATCH"):
            self.export()
        self.assertFalse((self.root / "material").exists())

    def test_cancel_after_output_creation_removes_only_new_directory(self):
        destination = self.root / "material"
        retained = self.root / "previous"
        retained.mkdir()
        (retained / "sentinel").write_text("keep")
        def cancel():
            if destination.exists():
                raise InterruptedError("cancelled")
        with self.assertRaises(InterruptedError):
            self.export(check_cancel=cancel)
        self.assertFalse(destination.exists())
        self.assertEqual((retained / "sentinel").read_text(), "keep")

    def test_existing_destination_and_source_subdirectory_are_rejected(self):
        destination = self.root / "material"
        destination.mkdir()
        sentinel = destination / "sentinel"
        sentinel.write_text("keep")
        with self.assertRaisesRegex(MaterialError, "MATERIAL_DESTINATION_EXISTS"):
            self.export()
        self.assertEqual(sentinel.read_text(), "keep")
        with self.assertRaisesRegex(MaterialError, "MATERIAL_DESTINATION_INSIDE_SOURCE"):
            exporter.export_material(self.database, self.source, self.source / "material",
                repository_id="sample/repository", commit=COMMIT)

    def test_portability_options_and_index_identity_are_checked(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE metadata SET value=? WHERE key='source_root_hash'", ("0" * 64,))
        with self.assertRaisesRegex(MaterialError, "MATERIAL_SOURCE_ROOT_MISMATCH"):
            self.export()
        self.build(self.source, self.database)
        with closing(sqlite3.connect(self.database)) as db, db:
            options = json.loads(db.execute("SELECT value FROM metadata WHERE key='source_options'").fetchone()[0])
            options["compilation_context"]["framework_version"] = str(self.root / "private-reference")
            db.execute("UPDATE metadata SET value=? WHERE key='source_options'", (json.dumps(options),))
        with self.assertRaisesRegex(MaterialError, "MATERIAL_NONPORTABLE_OPTIONS"):
            self.export()
        self.assertFalse((self.root / "material").exists())

    def test_big_file_and_long_line_terms_are_not_lost_to_sparse_fact_limits(self):
        large = ("*> context filler\n" * 34000) + ("*> " + " ".join(["padding"] * 10000) + " late-needle\n")
        path = self.source / "large.cbl"
        path.write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. LARGE-ENTRY.\n" + large)
        self.assertGreater(path.stat().st_size, 512 * 1024)
        self.build(self.source, self.database)
        destination, manifest = self.export()
        self.assertEqual(self.paths(destination, manifest, "terms", "late-needle"), ["large.cbl"])
        self.assertEqual(self.paths(destination, manifest, "terms", "large-entry"), ["large.cbl"])

    def test_posting_and_fact_pages_are_bounded_and_deterministic(self):
        for index in range(260):
            (self.source / f"shared-{index:03}.cpy").write_text(f"01 SHARED-FIELD-{index} PIC X.\n")
        fields = "\n".join(f"01 MANY-FIELD-{index} PIC X." for index in range(600))
        (self.source / "many.cpy").write_text(fields + "\n")
        self.build(self.source, self.database)
        destination, manifest = self.export()
        posting = self.lookup(destination, manifest, "terms", "shared")
        self.assertEqual(posting["count"], 260)
        self.assertEqual([len(self.read_object(destination, ref)["paths"]) for ref in posting["pages"]], [256, 4])
        file = self.lookup(destination, manifest, "files", "many.cpy")
        self.assertGreater(len(file["facts"]), 1)
        for ref in file["facts"]:
            page = self.read_object(destination, ref)
            self.assertLessEqual(sum(len(page[key]) for key in ("symbols", "relations", "rules")), 256)

    def test_read_transaction_does_not_mix_concurrent_index_updates(self):
        original = exporter._export_files
        def after_snapshot(db, *args):
            with closing(sqlite3.connect(self.database)) as writer, writer:
                writer.execute("UPDATE source_files SET sha256=? WHERE relative_path='entry.cbl'", ("f" * 64,))
            return original(db, *args)
        with mock.patch.object(exporter, "_export_files", side_effect=after_snapshot):
            destination, manifest = self.export()
        self.assertEqual(self.lookup(destination, manifest, "files", "entry.cbl")["sha256"], hashlib.sha256(SOURCE.encode()).hexdigest())

    def test_report_coverage_keeps_skips_but_rejects_absolute_paths(self):
        report = {**self.report, "input_skips": [{"relative_path": "unreadable.cbl", "reason_code": "SOURCE_READ_FAILED"}]}
        _, manifest = self.export(build_report=report, commit_verification="git_checked")
        self.assertEqual(manifest["coverage"]["input_skips"], report["input_skips"])
        self.assertEqual(manifest["analysis"]["commit_verification"], "git_checked")
        report["input_skips"] = [{"relative_path": str(self.source / "unreadable.cbl"), "reason_code": "SOURCE_READ_FAILED"}]
        with self.assertRaises(MaterialError):
            self.export("bad-report", build_report=report)
        self.assertFalse((self.root / "bad-report").exists())

    def test_dangling_or_stale_fact_evidence_is_not_silently_exported(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            evidence = db.execute("SELECT evidence_id FROM relations WHERE relation_type='CALLS'").fetchone()[0]
            db.execute("UPDATE evidence_spans SET source_sha256=? WHERE evidence_id=?", ("e" * 64, evidence))
        with self.assertRaisesRegex(MaterialError, "MATERIAL_FACT_SOURCE_MISMATCH"):
            self.export()
        self.assertFalse((self.root / "material").exists())
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DELETE FROM evidence_spans WHERE evidence_id=?", (evidence,))
        with self.assertRaisesRegex(MaterialError, "MATERIAL_INVALID_FACT_LOCATION|MATERIAL_FACT_SOURCE_MISMATCH"):
            self.export()
        self.assertFalse((self.root / "material").exists())


if __name__ == "__main__":
    unittest.main()
