from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from online_acceptance import verify
from online_http_fixture import OnlineFixture, SyntheticModel
from online_material_format import MaterialError


class OnlineAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = OnlineFixture(Path(self.temporary.name))
        payload = self.fixture.connect_payload()
        self.reader = self.fixture.material_factory(payload["location"], {},
                                                     {"repository": payload["repository"], "commit": payload["commit"]}, None)
        self.source = self.fixture.source_factory(self.reader.manifest["repository"], {}, lambda: None)

    def test_default_check_uses_no_model_and_retains_no_source_text(self):
        result = verify(self.reader, self.source, "main.cbl", 1, 3)
        self.assertEqual(result["status"], "source_verified")
        self.assertFalse(result["model"]["called"])
        self.assertEqual(result["checks"]["business_answer_quality"], "not_assessed")
        self.assertNotIn("IDENTIFICATION DIVISION", json.dumps(result))

    def test_model_mechanism_is_distinct_from_business_acceptance(self):
        result = verify(self.reader, self.source, "main.cbl", 1, 3, question="解释处理条件",
                         allow_source_transfer=True, client=SyntheticModel())
        self.assertEqual(result["status"], "mechanism_verified")
        self.assertTrue(result["model"]["called"])
        self.assertEqual(result["model"]["citation_count"], 3)
        self.assertEqual(result["checks"]["business_answer_quality"], "not_assessed")

    def test_no_model_transfer_without_explicit_authorization(self):
        client = SyntheticModel()
        with self.assertRaisesRegex(MaterialError, "SOURCE_TRANSFER_REQUIRED"):
            verify(self.reader, self.source, "main.cbl", 1, 3, question="解释", client=client)
        self.assertFalse(client.requests)


if __name__ == "__main__":
    unittest.main()
