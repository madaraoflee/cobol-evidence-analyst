from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from numeric_semantics import DECIMAL_RECEIVER_PROFILE, numeric_layout, receiving_contract, store_decimal


class NumericSemanticsTests(unittest.TestCase):
    def store(self, value, declaration="01 AMOUNT PIC S9(3)V99 COMP-3.", **options):
        return store_decimal(value, declaration, profile=DECIMAL_RECEIVER_PROFILE, **options)

    def test_picture_usage_and_scale_are_explicit(self):
        layout = numeric_layout("05 AMOUNT PIC S9(7)V99 USAGE IS COMP-3.")
        self.assertEqual((layout.usage, layout.signed, layout.integer_digits, layout.scale), ("PACKED-DECIMAL", True, 7, 2))
        self.assertTrue(layout.supported)
        contract = receiving_contract("05 AMOUNT PIC S9(7)V99.", rounded=True, on_size_error=True)
        self.assertFalse(contract["profile_confirmed"])
        self.assertFalse(contract["intermediate_precision_verified"])
        self.assertEqual(contract["size_error_check"], "after_rounding")

    def test_decimal_by_itself_does_not_establish_cobol_arithmetic(self):
        result = store_decimal(Decimal("1.25"), "01 AMOUNT PIC 9(3)V99.")
        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["reason"], "receiver_profile_not_confirmed")
        self.assertEqual(self.store(1.25)["reason"], "exact_decimal_input_required")

    def test_rounded_and_truncation_are_distinct_for_both_signs(self):
        for value, truncated, rounded in (("1.235", "1.23", "1.24"), ("-1.235", "-1.23", "-1.24")):
            self.assertEqual(self.store(value)["value"], truncated)
            self.assertEqual(self.store(value, rounded=True)["value"], rounded)

    def test_rounding_can_trigger_size_error_and_preserves_receiver_with_handler(self):
        declaration = "01 AMOUNT PIC S9(2)V99."
        self.assertFalse(self.store("99.995", declaration)["size_error"])
        result = self.store("99.995", declaration, rounded=True, on_size_error=True)
        self.assertEqual(result["status"], "size_error")
        self.assertIsNone(result["value"])
        self.assertTrue(result["receiver_preserved"])

    def test_move_truncates_high_and_low_digits_without_arithmetic_size_error(self):
        result = self.store("-123.456", "01 AMOUNT PIC 9(2)V99.", operation="MOVE")
        self.assertEqual(result["value"], "23.45")
        self.assertTrue(result["integer_truncated"])
        self.assertFalse(result["size_error"])

    def test_binary_edited_and_scaled_pictures_require_more_semantics(self):
        for declaration in ("01 AMOUNT PIC S9(5) COMP.", "01 AMOUNT PIC 9(5) COMP-5.",
                            "01 AMOUNT PIC ZZ9.99.", "01 AMOUNT PIC PPP999.", "01 AMOUNT PIC X(8)."):
            with self.subTest(declaration=declaration):
                self.assertFalse(numeric_layout(declaration).supported)
                self.assertEqual(self.store("1", declaration)["status"], "unsupported")
