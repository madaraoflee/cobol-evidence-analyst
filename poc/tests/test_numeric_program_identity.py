"""Pure numeric PROGRAM-ID declarations remain indexed source identities."""

from contextlib import closing
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
import business_index
from business_chat import run_business_chat
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search
from repository_identity import resolve_source_identity
from source_catalog import refresh_source_catalog
from statement_facts import program_identifiers
from structural_index import build_structural_index


def program(name, body="DISPLAY 'NUMERIC-OWNER'."):
    return ("IDENTIFICATION DIVISION.\nPROGRAM-ID. " + name + ".\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 AMOUNT PIC 9(5).\n"
            "PROCEDURE DIVISION.\nMAIN.\n" + body + "\nGOBACK.\n")


class NumericProgramIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="numeric-program-identity-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        environment = mock.patch.dict(os.environ, {"FRAMEWORK_REFERENCE_PATH": ""})
        environment.start()
        self.addCleanup(environment.stop)
        for owner, name in ((socket, "create_connection"), (socket.socket, "connect"),
                            (socket.socket, "connect_ex")):
            patcher = mock.patch.object(owner, name, side_effect=AssertionError("Offline test forbids network"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def fixture(self):
        self.source.joinpath("numeric.cbl").write_text(program("710001"), encoding="utf-8")
        self.source.joinpath("leading.cbl").write_text(program("000710001"), encoding="utf-8")
        self.source.joinpath("quoted.cbl").write_text(program("'710002'"), encoding="utf-8")
        self.source.joinpath("caller.cbl").write_text(program("CALLER",
            "*> PROGRAM-ID. 710003.\nCALL '710001'.\nDISPLAY 'PROGRAM-ID. 710004.'."), encoding="utf-8")

    def test_plain_leading_zero_and_quoted_numbers_retain_exact_identifier_text(self):
        for clause, expected in (("PROGRAM-ID. 710001.", "710001"),
                                 ("PROGRAM-ID. 000710001.", "000710001"),
                                 ("PROGRAM-ID. '710002'.", "710002")):
            with self.subTest(clause=clause):
                self.assertEqual([item[0] for item in program_identifiers(clause)], [expected])

    def test_decimal_scientific_comment_call_and_truncated_prefix_are_not_definitions(self):
        for clause in ("PROGRAM-ID. 71.001.", "PROGRAM-ID. 71E3.", "PROGRAM-ID. 71E+3.",
                       "PROGRAM-ID. 71E-3.", "*> PROGRAM-ID. 710001.",
                       "CALL '710001'.", "DISPLAY 'PROGRAM-ID. 710001.'.", "PROGRAM-ID. 710001"):
            with self.subTest(clause=clause):
                self.assertEqual(program_identifiers(clause), ())

    def test_catalog_and_both_index_writers_keep_numeric_definitions_separate_from_mentions(self):
        self.fixture()
        expected = {"710001": "numeric.cbl", "000710001": "leading.cbl",
                    "710002": "quoted.cbl", "CALLER": "caller.cbl"}
        catalog = refresh_source_catalog(self.source, self.root / "catalog.sqlite", source_format="free")
        self.assertEqual({item["program_name"]: item["relative_path"] for item in catalog["programs"]}, expected)
        for builder in (business_index.build_business_index, build_structural_index):
            with self.subTest(builder=builder.__name__):
                database = self.root / (builder.__name__ + ".sqlite")
                builder(self.source, database, source_format="free", quiet=True)
                with closing(sqlite3.connect(database)) as connection:
                    actual = dict(connection.execute("SELECT name,relative_path FROM symbols WHERE symbol_type='Program'"))
                    self.assertEqual(actual, expected)
                    identity = resolve_source_identity(connection, "请说明 710001 的计算公式。")
                    self.assertEqual(identity["status"], "resolved")
                    self.assertEqual(identity["direct_paths"], ["numeric.cbl"])

    def test_default_business_question_receives_numeric_program_original_source(self):
        self.fixture()
        business_index.build_business_index(self.source, self.database, source_format="free",
                                            framework_reference_path="", quiet=True)
        ensure_repository_search(self.database, self.source)
        requests = []
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            numeric = next(page for page in payload["source_context"][0]["pages"]
                           if page["relative_path"] == "numeric.cbl")
            self.assertIn("PROGRAM-ID. 710001.", numeric["source_text"])
            answer = f"所选程序显示固定文本。[{numeric['evidence_id']}]"
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": answer},
                                                                   "finish_reason": "stop"}]}))
        config = CompanyAPIConfig("https://offline.example.invalid/v1", "offline-model", api_key="offline-only")
        run_business_chat("请说明 710001 的完整业务流程。", self.database, self.source, config,
                          framework_reference_path="", allow_network=False, transport=transport,
                          policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0))
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["business_map"]["source_identity"]["direct_paths"], ["numeric.cbl"])
        self.assertIn("numeric.cbl", requests[0]["source_context"][0]["working_set"]["supplied_complete_paths"])

    def test_old_sparse_index_rebuilds_previously_missing_numeric_program_symbol(self):
        self.source.joinpath("numeric.cbl").write_text(program("710001"), encoding="utf-8")
        with mock.patch.object(business_index, "program_identifier", return_value=None), \
                mock.patch.object(business_index, "PARSER_VERSION", "business-sparse-v1.6"):
            business_index.build_business_index(self.source, self.database, source_format="free",
                                                framework_reference_path="", quiet=True)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM symbols WHERE symbol_type='Program'").fetchone()[0], 0)
        business_index.build_business_index(self.source, self.database, source_format="free",
                                            framework_reference_path="", quiet=True)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT name FROM symbols WHERE symbol_type='Program'").fetchone()[0], "710001")
            self.assertEqual(connection.execute("SELECT value FROM metadata WHERE key='parser_version'").fetchone()[0],
                             business_index.PARSER_VERSION)


if __name__ == "__main__":
    unittest.main()
