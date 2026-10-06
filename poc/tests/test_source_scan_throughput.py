"""Faster source scans preserve physical lines, hashes and search tokens."""

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from repository_discovery import _identifier_tokens, _tokens, _WORDS, _PARTS
from source_reading import _verified_lines


class SourceScanThroughputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="source-scan-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def item(self, text, encoding="utf-8"):
        raw = text.encode(encoding)
        path = self.root / "flow.cbl"
        path.write_bytes(raw)
        return {"relative_path": path.name, "encoding": encoding,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "line_count": len(text.lstrip("\ufeff").splitlines())}

    def test_full_and_verification_only_scans_agree_across_chunk_boundaries(self):
        endings = ("\r\n", "\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")
        texts = ["", "\ufeff", "\r\n", "final line", "\n\nlast\r", "X" * 65535 + "\r\nEND\n",
                 "X" * 200000 + "\r\n" + "\n" * 50 + "TAIL",
                 "\ufeff\ufeff" + "".join("中性 FLOW\t" + end + end for end in endings) + "ENDING"]
        for encoding in ("utf-8", "utf-16", "utf-32"):
            for text in texts:
                with self.subTest(encoding=encoding, length=len(text)):
                    item = self.item(text, encoding)
                    expected = [(line[:37], len(line) > 37) for line in text.lstrip("\ufeff").splitlines()]
                    self.assertEqual(list(_verified_lines(self.root, item, None, 37)), expected)
                    self.assertEqual(list(_verified_lines(self.root, item, None, 1, verify_only=True)), [])

    def test_verification_only_retains_hash_line_count_and_decode_checks(self):
        item = self.item("MOVE INPUT-AMOUNT TO RESULT-AMOUNT.\n" * 3000)
        with self.assertRaisesRegex(ValueError, "SOURCE_LINE_COUNT_MISMATCH"):
            list(_verified_lines(self.root, dict(item, line_count=item["line_count"] + 1), None, 1, verify_only=True))
        path = self.root / item["relative_path"]
        path.write_bytes(path.read_bytes().replace(b"INPUT", b"OTHER"))
        with self.assertRaisesRegex(ValueError, "SOURCE_HASH_MISMATCH"):
            list(_verified_lines(self.root, item, None, 1, verify_only=True))
        path.write_bytes(b"\xff")
        with self.assertRaisesRegex(ValueError, "SOURCE_ENCODING_INVALID"):
            list(_verified_lines(self.root, item, None, 1, verify_only=True))

    def test_verification_only_remains_cancellable_between_chunks(self):
        item = self.item("X" * 200000)
        calls = 0
        def cancel():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("cancelled")
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            list(_verified_lines(self.root, item, cancel, 1, verify_only=True))
        self.assertEqual(calls, 2)

    def test_identifier_cache_preserves_token_order_and_excludes_large_inputs(self):
        def original(text):
            result = []
            for word in dict.fromkeys(_WORDS.findall(text)):
                if "\u3400" <= word[0] <= "\u9fff":
                    if len(word) <= 64:
                        result.append(word)
                    for size in (4, 3, 2):
                        result.extend(word[start:start + size] for start in range(len(word) - size + 1))
                else:
                    result.append(word.casefold())
                    result.extend(part.casefold() for part in _PARTS.split(word) if part)
            return list(dict.fromkeys(result))
        _identifier_tokens.cache_clear()
        self.addCleanup(_identifier_tokens.cache_clear)
        texts = ["BILL-AMOUNT billAmount XMLRequest PAY_STATUS X.$#@-19 中文说明中文说明",
                 "FIELD-" + "X" * 20000, "".join(f"FIELD-{index} " for index in range(10000))]
        for text in texts * 2:
            self.assertEqual(_tokens(text), original(text))
        self.assertLessEqual(_identifier_tokens.cache_info().currsize, 8192)
        before = _identifier_tokens.cache_info()
        _tokens("FIELD-" + "X" * 20000)
        self.assertEqual(_identifier_tokens.cache_info(), before)


if __name__ == "__main__":
    unittest.main()
