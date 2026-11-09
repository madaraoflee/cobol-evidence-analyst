"""Byte identity is stable across fixture hosts and preserves source newlines."""

import hashlib
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_online_material_export as fixtures
import test_online_analysis as cli_fixtures
from online_material_format import MaterialError
from online_material_query import MaterialQuery
from online_material_reader import DirectoryObjectStore, MaterialReader


class OnlineMaterialExportNewlineTests(unittest.TestCase):
    @staticmethod
    def windows_text_defaults():
        original = Path.write_text
        def windows_default(path, data, *args, **kwargs):
            if kwargs.get("newline") is None:
                kwargs["newline"] = "\r\n"
            return original(path, data, *args, **kwargs)
        return mock.patch.object(Path, "write_text", windows_default)

    def make_fixture(self):
        fixture = fixtures.MaterialExportTests(
            "test_real_index_exports_search_facts_reverse_relations_without_source_or_input_changes")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        return fixture

    def test_digest_fixture_is_identical_under_windows_text_defaults(self):
        with self.windows_text_defaults():
            fixture = self.make_fixture()
            destination, manifest = fixture.export()
        expected = fixtures.SOURCE.encode("utf-8")
        self.assertEqual((fixture.source / "entry.cbl").read_bytes(), expected)
        self.assertEqual(fixture.lookup(destination, manifest, "files", "entry.cbl")["sha256"],
                         hashlib.sha256(expected).hexdigest())

    def test_cli_evidence_preserves_windows_crlf_bytes_and_pinned_subdirectory(self):
        fixture = cli_fixtures.OnlineAnalysisIntegrationTests(
            "test_evidence_cli_reads_pinned_subdirectory_and_keeps_credentials_out_of_output")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        source = cli_fixtures.program("MAIN-ENTRY")
        with self.windows_text_defaults():
            (fixture.source / "main.cbl").write_text(source, encoding="utf-8")
            raw = (fixture.source / "main.cbl").read_bytes()
            status, result = fixture.build("--subdirectory", "src")
        self.assertEqual(status, 0, result)
        self.assertEqual(raw, source.replace("\n", "\r\n").encode("utf-8"))
        with mock.patch("online_source_client.PinnedSourceClient") as client:
            client.return_value.read_file.return_value = raw
            status, result = fixture.invoke("evidence", "--material", str(fixture.output), "main.cbl", "1", "2")
        self.assertEqual(status, 0, result)
        self.assertEqual(client.return_value.read_file.call_args.args, ("src/main.cbl",))
        self.assertEqual(result["transfer"]["source_bytes"], len(raw))
        self.assertEqual(result["citation"]["hash"], hashlib.sha256(raw).hexdigest())

    def test_export_and_evidence_preserve_exact_lf_crlf_and_mixed_source_bytes(self):
        fixture = self.make_fixture()
        source = fixture.source / "entry.cbl"
        hashes = set()
        for name, endings in (("lf", ("\n",)), ("crlf", ("\r\n",)),
                              ("mixed", ("\n", "\r\n", "\r"))):
            with self.subTest(newlines=name):
                raw = "".join(line + endings[index % len(endings)]
                              for index, line in enumerate(fixtures.SOURCE.splitlines())).encode("utf-8")
                source.write_bytes(raw)
                fixture.build(fixture.source, fixture.database)
                destination, manifest = fixture.export(name)
                metadata = fixture.lookup(destination, manifest, "files", "entry.cbl")
                expected_hash = hashlib.sha256(raw).hexdigest()
                self.assertEqual(metadata["sha256"], expected_hash)
                self.assertEqual(metadata["size"], len(raw))
                self.assertEqual(metadata["line_count"], len(fixtures.SOURCE.splitlines()))
                query = MaterialQuery(MaterialReader(DirectoryObjectStore(destination)))
                evidence = query.evidence("entry.cbl", 1, metadata["line_count"],
                                          lambda path, **kwargs: source.read_bytes())
                self.assertEqual(evidence["citation"]["hash"], expected_hash)
                self.assertEqual(evidence["text"], fixtures.SOURCE.rstrip("\n"))
                self.assertEqual(source.read_bytes(), raw)
                hashes.add(expected_hash)
        self.assertEqual(len(hashes), 3)

    def test_newline_only_change_invalidates_the_original_index_hash(self):
        fixture = self.make_fixture()
        source = fixture.source / "entry.cbl"
        source.write_bytes(fixtures.SOURCE.replace("\n", "\r\n").encode("utf-8"))
        fixture.build(fixture.source, fixture.database)
        source.write_bytes(fixtures.SOURCE.encode("utf-8"))
        with self.assertRaisesRegex(MaterialError, "MATERIAL_SOURCE_HASH_MISMATCH"):
            fixture.export()
        self.assertFalse((fixture.root / "material").exists())


if __name__ == "__main__":
    unittest.main()
