"""Offline checks that question-local reuse never crosses evidence versions."""

from copy import deepcopy
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor
import asyncio
import hashlib
import inspect
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_explanation_cache import (business_explanation_cache,
    cache_business_explanation, with_business_explanation_cache)
import business_orientation
import business_synthesis


SOURCE = """*> Purpose: prepare settlement output for approved orders.
IDENTIFICATION DIVISION.
PROGRAM-ID. SETTLEMENT.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 APPROVAL-STATUS PIC X.
01 ORDER-AMOUNT PIC 9(5).
01 SETTLEMENT-AMOUNT PIC 9(6).
PROCEDURE DIVISION.
IF APPROVAL-STATUS = 'Y'
 COMPUTE SETTLEMENT-AMOUNT = ORDER-AMOUNT * 2
END-IF.
DISPLAY SETTLEMENT-AMOUNT.
GOBACK.
"""


def source_page():
    return {"relative_path": "settlement.cbl", "source_text": SOURCE,
        "source_sha256": hashlib.sha256(SOURCE.encode()).hexdigest(),
        "evidence_id": "ev-settlement", "start_line": 1,
        "end_line": len(SOURCE.splitlines()), "format_hint": "free"}


class BusinessExplanationCacheTests(unittest.TestCase):
    def test_three_answer_reviews_reuse_source_projection_but_assess_each_answer(self):
        original = inspect.unwrap(business_orientation.build_business_orientation)
        calls = []

        @cache_business_explanation
        def project(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        requirement_calls = []
        original_requirements = inspect.unwrap(business_synthesis.answer_requirements)

        @cache_business_explanation
        def requirements(*args, **kwargs):
            requirement_calls.append(1)
            return original_requirements(*args, **kwargs)

        question = "请说明 SETTLEMENT 的业务目的与整体流程。"
        investigation = {"state": "located", "can_answer": True, "required_items": []}
        pages = [source_page()]
        answers = ["", "审批通过后按订单金额的两倍形成结算金额。[ev-settlement]",
                   "程序先判断审批状态，审批通过才按订单金额的两倍形成结算金额。[ev-settlement]"]
        with patch.object(business_orientation, "build_business_orientation", project), \
                patch.object(business_synthesis, "answer_requirements", requirements):
            expected = [business_synthesis.assess_business_answer(question, answer, investigation, pages)
                        for answer in answers]
            calls.clear()
            requirement_calls.clear()
            with business_explanation_cache() as cache:
                actual = [business_synthesis.assess_business_answer(question, answer, investigation, pages)
                          for answer in answers]
                self.assertEqual(actual, expected)
                self.assertEqual(len(calls), 1)
                self.assertEqual(len(requirement_calls), 1)
                self.assertGreaterEqual(cache.info()["hits"], 2)
            self.assertNotEqual(actual[0], actual[1])
            self.assertEqual(cache.info()["entries"], 0)

    def test_every_source_identity_scope_and_framework_input_invalidates(self):
        calls = []

        @cache_business_explanation
        def project(question, investigation, pages, *, business_map=None,
                    framework_references=(), framework_facts=()):
            calls.append(1)
            return {"pages": pages, "scope": investigation, "map": business_map,
                    "references": framework_references, "facts": framework_facts}

        original = {"question": "解释业务流程", "investigation": {"source_scope": {"ranges": []}},
            "pages": [source_page()], "business_map": {"source_identity": {"status": "resolved"}},
            "framework_references": [{"reference_id": "manual-1", "text": "read request"}],
            "framework_facts": [{"operation": "READ", "source_ranges": [{"start_line": 8, "end_line": 8}]}]}
        changes = [
            lambda item: item["pages"][0].update(source_text=SOURCE.replace("* 2", "* 3")),
            lambda item: item["pages"][0].update(source_sha256="f" * 64),
            lambda item: item["pages"][0].update(start_line=2),
            lambda item: item["pages"][0].update(end_line=12),
            lambda item: item["pages"][0].update(relative_path="other.cbl"),
            lambda item: item["pages"][0].update(evidence_id="ev-other"),
            lambda item: item["pages"][0].update(include_chain=["copy.cpy"]),
            lambda item: item["pages"][0].update(replacement_trace=["OLD BY NEW"]),
            lambda item: item["pages"][0].update(span_truncated=True),
            lambda item: item["investigation"]["source_scope"].update(ranges=[{"start_line": 9, "end_line": 12}]),
            lambda item: item["business_map"]["source_identity"].update(exclusive_source_scope=True),
            lambda item: item["framework_references"][0].update(text="write request"),
            lambda item: item["framework_facts"][0].update(operation="WRITE"),
            lambda item: item.update(question="解释异常处理"),
        ]
        with business_explanation_cache():
            project(**original)
            self.assertEqual(project(**deepcopy(original)), project(**original))
            for change in changes:
                changed = deepcopy(original)
                change(changed)
                project(**changed)
            self.assertEqual(len(calls), 1 + len(changes))

    def test_in_place_changes_and_returned_mutations_cannot_reuse_stale_values(self):
        calls = []

        @cache_business_explanation
        def project(pages):
            calls.append(1)
            return {"text": [pages[0]["source_text"]]}

        pages = [source_page()]
        with business_explanation_cache():
            project(pages)["text"].append("caller mutation")
            hit = project(pages)
            self.assertEqual(hit, {"text": [SOURCE]})
            hit["text"].append("second mutation")
            self.assertEqual(project(pages), {"text": [SOURCE]})
            pages[0]["source_text"] = "changed"
            self.assertEqual(project(pages), {"text": ["changed"]})
        self.assertEqual(len(calls), 2)

    def test_nested_scopes_exception_cleanup_and_question_decorator(self):
        calls = []

        @cache_business_explanation
        def project(value="source"):
            calls.append(value)
            return {"value": value}

        @with_business_explanation_cache
        def question(fail=False):
            project()
            project(value="source")
            if fail:
                raise RuntimeError("failed question")

        with business_explanation_cache() as outer:
            project()
            with self.assertRaises(RuntimeError):
                with business_explanation_cache() as inner:
                    project()
                    raise RuntimeError("inner failure")
            self.assertEqual(inner.info()["entries"], 0)
            project()
            self.assertEqual(outer.info()["hits"], 1)
            question()
            with self.assertRaises(RuntimeError):
                question(fail=True)
            project()
            self.assertEqual(outer.info()["hits"], 2)
        self.assertEqual(outer.info()["entries"], 0)
        project()
        project()
        self.assertEqual(len(calls), 6)

    def test_bounded_cache_evicts_and_large_inputs_or_outputs_do_not_reside(self):
        calls = []

        @cache_business_explanation
        def project(value):
            calls.append(value)
            return value * 10

        with business_explanation_cache(max_entries=2, max_bytes=160, max_input_bytes=70) as cache:
            project("a")
            project("b")
            project("c")
            self.assertLessEqual(cache.info()["entries"], 2)
            project("a")
            self.assertEqual(calls.count("a"), 2)
            project("x" * 100)
            project("x" * 100)
            project("y" * 30)
            project("y" * 30)
            self.assertEqual(calls.count("x" * 100), 2)
            self.assertEqual(calls.count("y" * 30), 2)
            self.assertLessEqual(cache.info()["bytes"], 160)
            self.assertEqual(cache.info()["bypasses"], 4)

    def test_unsupported_and_cyclic_data_bypass_without_changing_function(self):
        calls = []

        @cache_business_explanation
        def project(value):
            calls.append(1)
            return "available"

        cyclic = []
        cyclic.append(cyclic)
        for value in (cyclic, object(), {1: "non-string key"}, float("nan")):
            with business_explanation_cache() as cache:
                self.assertEqual(project(value), "available")
                self.assertEqual(project(value), "available")
                self.assertEqual(cache.info()["entries"], 0)
        self.assertEqual(len(calls), 8)

    def test_function_names_types_and_failed_results_do_not_alias(self):
        calls = []

        @cache_business_explanation
        def project(value):
            calls.append(1)
            if value == "fail":
                raise RuntimeError("projection failed")
            return type(value).__name__

        @cache_business_explanation
        def other(value):
            return "other"

        with business_explanation_cache():
            for value in (True, 1, 1.0, [1], (1,)):
                self.assertEqual(project(value), type(value).__name__)
            self.assertEqual(other(1), "other")
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    project("fail")
        self.assertEqual(len(calls), 7)

    def test_mapping_insertion_order_is_part_of_projection_identity(self):
        @cache_business_explanation
        def project(value):
            return list(value)

        with business_explanation_cache():
            self.assertEqual(project({"first": 1, "second": 2}), ["first", "second"])
            self.assertEqual(project({"second": 2, "first": 1}), ["second", "first"])

    def test_inherited_thread_context_cannot_read_or_populate_parent_cache(self):
        calls = []

        @cache_business_explanation
        def project(value):
            calls.append(value)
            return value

        def worker():
            project("source")
            project("source")

        with business_explanation_cache() as cache, ThreadPoolExecutor(max_workers=1) as executor:
            project("source")
            executor.submit(copy_context().run, worker).result()
            project("source")
            self.assertEqual(cache.info()["hits"], 1)
            self.assertEqual(cache.info()["misses"], 1)
        self.assertEqual(calls, ["source"] * 3)

    def test_inherited_child_task_does_not_share_parent_cache(self):
        calls = []

        @cache_business_explanation
        def project(value):
            calls.append(value)
            return value

        async def child():
            project("source")
            project("source")
            with business_explanation_cache() as own:
                project("source")
                project("source")
                self.assertEqual(own.info()["hits"], 1)

        async def question():
            with business_explanation_cache() as parent:
                project("source")
                await asyncio.create_task(child())
                project("source")
                self.assertEqual(parent.info()["hits"], 1)
                self.assertEqual(parent.info()["misses"], 1)

        asyncio.run(question())
        self.assertEqual(calls, ["source"] * 4)

    def test_copied_context_after_question_exit_cannot_resurrect_cache(self):
        calls = []

        @cache_business_explanation
        def project(value):
            calls.append(value)
            return value

        with business_explanation_cache() as cache:
            project("source")
            inherited = copy_context()
        inherited.run(project, "source")
        inherited.run(project, "source")
        self.assertEqual(cache.info()["entries"], 0)
        self.assertEqual(cache.info()["bytes"], 0)
        self.assertEqual(calls, ["source"] * 3)

    def test_oversized_integer_bypasses_before_decimal_conversion(self):
        @cache_business_explanation
        def project(value):
            return value.bit_length()

        with business_explanation_cache(max_input_bytes=64) as cache:
            self.assertEqual(project(1 << 100000), 100001)
            self.assertEqual(cache.info()["entries"], 0)
            self.assertEqual(cache.info()["bypasses"], 1)


if __name__ == "__main__":
    unittest.main()
