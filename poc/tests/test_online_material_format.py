from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from online_material_format import (MaterialError, SCHEMA_VERSION, canonical_json, decode_json,
                                    object_reference, relative_path, seal_manifest, tokens,
                                    validate_manifest, validate_reference)


class MaterialFormatTests(unittest.TestCase):
    def manifest(self):
        return seal_manifest({"schema_version": SCHEMA_VERSION,
                              "repository": {"identity": "example/source", "commit": "a" * 40,
                                             "subdirectory": "src"},
                              "analysis": {}, "coverage": {},
                              "routes": {"files": {}, "terms": {}, "references": {}}})

    def test_identity_and_content_changes_invalidate_revision(self):
        original = self.manifest()
        for field, value in (("identity", "example/other"), ("commit", "b" * 40), ("subdirectory", "other")):
            changed = copy.deepcopy(original)
            changed["repository"][field] = value
            with self.assertRaisesRegex(MaterialError, "REVISION_MISMATCH"):
                validate_manifest(changed)
        self.assertEqual(validate_manifest(decode_json(canonical_json(original))), original)

    def test_untrusted_paths_and_object_references_are_rejected(self):
        for path in ("../source", "/tmp/source", "a//b", "a/./b", "a\\b", "C:source", "a\nfile"):
            with self.subTest(path=path), self.assertRaises(MaterialError):
                relative_path(path)
        ref = object_reference(b"{}")
        self.assertEqual(validate_reference(ref), ref)
        for key, value in (("path", "../object.json"), ("size", True), ("size", 9 * 1024 * 1024)):
            with self.assertRaises(MaterialError):
                validate_reference({**ref, key: value})

    def test_duplicate_json_keys_invalid_numbers_and_size_limit_are_rejected(self):
        for data in (b'{"revision":"a","revision":"b"}', b'{"value":NaN}', b'\xff', b'[] garbage',
                     b'{"value":"\\ud800"}', b'{"\\udfff":[]}'):
            with self.subTest(data=data), self.assertRaises(MaterialError):
                decode_json(data)
        with self.assertRaises(MaterialError):
            decode_json(b"{}", maximum=1)
        with self.assertRaises(MaterialError):
            canonical_json({"value": float("inf")})

    def test_lexical_tokens_preserve_hyphenated_names_and_case_normalize(self):
        self.assertEqual(tokens("ＰＡＹＭＥＮＴ-STATUS"), {"payment-status", "payment", "status"})
        self.assertEqual(tokens("X" * 257), set())


if __name__ == "__main__":
    unittest.main()
