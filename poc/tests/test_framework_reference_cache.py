from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import framework_knowledge as knowledge


class FrameworkReferenceCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        knowledge._CACHE.clear()
        self.addCleanup(knowledge._CACHE.clear)

    def reference(self, name):
        path = self.root / name
        path.write_text("# Processing manual\n\n## Completion\nFLOW-RESULT identifies completion.\n", encoding="utf-8")
        return path

    def test_byte_budget_evicts_least_recently_used_document(self):
        first, second, third = [self.reference(name) for name in ("first.md", "second.md", "third.md")]
        with patch.object(knowledge, "MAX_REFERENCE_CACHE_BYTES", 120), \
             patch.object(knowledge, "_document_memory_bytes", return_value=60):
            first_doc = knowledge._load_file(first)
            knowledge._load_file(second)
            self.assertIs(knowledge._load_file(first), first_doc)
            knowledge._load_file(third)
        self.assertEqual([Path(key[0]).name for key in knowledge._CACHE], ["first.md", "third.md"])
        self.assertLessEqual(sum(item[0] for item in knowledge._CACHE.values()), 120)

    def test_large_document_is_usable_without_remaining_in_cache(self):
        path = self.reference("reference.md")
        with patch.object(knowledge, "MAX_REFERENCE_CACHE_BYTES", 1):
            document = knowledge._load_file(path)
        self.assertTrue(document.sections)
        self.assertFalse(knowledge._CACHE)

    def test_changed_file_replaces_prior_revision(self):
        path = self.reference("reference.md")
        previous = knowledge._load_file(path)
        path.write_text(path.read_text(encoding="utf-8") + "Additional operation rules.\n", encoding="utf-8")
        current = knowledge._load_file(path)
        self.assertNotEqual(previous.sha256, current.sha256)
        self.assertEqual(len(knowledge._CACHE), 1)
        self.assertIs(next(iter(knowledge._CACHE.values()))[1], current)

    def test_accounting_includes_parsed_sections_and_term_sets(self):
        path = self.reference("reference.md")
        document = knowledge._load_file(path)
        self.assertGreater(knowledge._document_memory_bytes(document), path.stat().st_size)


if __name__ == "__main__":
    unittest.main()
