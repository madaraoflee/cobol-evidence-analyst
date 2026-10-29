"""Invalid deployment inputs must not establish source branch certainty."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from compilation_context import CompilationContext, resolve_compilation_context
from statement_facts import CompilationGuardState


class CompilationContextValidationTests(unittest.TestCase):
    def test_string_booleans_cannot_select_a_preprocessing_branch(self):
        for flag in ("false", "true", 0, 1, [], {}):
            with self.subTest(flag=flag):
                with self.assertRaisesRegex(ValueError, "^COMPILATION_CONTEXT_INVALID$"):
                    CompilationGuardState({"conditions_complete": flag})
        state = CompilationGuardState({"conditions_complete": False})
        state.feed(">>IF FEATURE-A", 1)
        self.assertIsNone(state.status)
        state.feed(">>ELSE", 3)
        self.assertIsNone(state.status)
        self.assertEqual(state.diagnostics[0]["reason"], "compile_condition_unknown")

    def test_all_compilation_switches_require_boolean_values(self):
        for name in ("copy_library_order_known", "debug_lines", "nested_copy_replacing"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "^COMPILATION_CONTEXT_INVALID$"):
                    resolve_compilation_context({name: "false"})
        for name in ("debug_lines", "nested_copy_replacing"):
            self.assertIsNone(getattr(resolve_compilation_context({name: None}), name))

    def test_library_and_resource_strings_are_not_silently_split(self):
        for name in ("copy_libraries", "resource_kinds"):
            for value in ("copybooks", None, [12], [""]):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        resolve_compilation_context({name: value})
        for path in ("/outside", "../outside", "C:/outside", "C:outside", r"C:\outside"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "^COMPILATION_COPY_LIBRARY_INVALID$"):
                    CompilationContext(copy_libraries=(path,))

    def test_conditions_reject_malformed_names_values_and_pairs(self):
        for value in (None, "AB", ["AB"], {1: True}, {"": True},
                      {"FEATURE": None}, {"FEATURE": []}, {"FEATURE": 1.5}, [["FEATURE"]]):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "^COMPILATION_CONDITION_INVALID$"):
                    resolve_compilation_context({"compile_conditions": value})
        with self.assertRaisesRegex(ValueError, "^COMPILATION_CONDITION_DUPLICATE$"):
            resolve_compilation_context({"compile_conditions": {"feature": True, "FEATURE": False}})

    def test_context_is_an_object_with_known_fields(self):
        for value in ([], False, "", {"unknown": True}):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "^COMPILATION_CONTEXT_INVALID$"):
                    resolve_compilation_context(value)

    def test_valid_settings_round_trip_and_cannot_be_mutated_by_the_caller(self):
        libraries, conditions = ["copybooks"], [["feature", True]]
        context = CompilationContext(copy_libraries=libraries, compile_conditions=conditions,
                                     copy_library_order_known=True, conditions_complete=True)
        fingerprint = context.fingerprint
        libraries.append("other")
        conditions[0][1] = False
        self.assertEqual(context.copy_libraries, ("copybooks",))
        self.assertTrue(context.condition("FEATURE"))
        self.assertFalse(context.condition("MISSING"))
        self.assertEqual(context.fingerprint, fingerprint)
        self.assertEqual(resolve_compilation_context(context.to_dict()), context)


if __name__ == "__main__":
    unittest.main()
