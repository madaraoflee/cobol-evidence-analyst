"""Exercise module isolation without changing the existing regression contract."""
from collections import Counter
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import run_python_regressions as runner


class PythonRegressionIsolationTests(unittest.TestCase):
    @staticmethod
    def module(name, count):
        case = type("Cases", (unittest.TestCase,), {"__module__": name})
        return [case() for _ in range(count)]

    def test_long_module_is_isolated_without_dropping_or_reordering_tests(self):
        long = self.module("test_large_repository_inventory_acceptance", 7)
        other_modules = [self.module(f"test_regular_{index}", count)
                         for index, count in enumerate((9, 8, 7, 6, 5, 4, 3, 2, 1))]
        discovered = [*other_modules[0], *long, *(case for group in other_modules[1:] for case in group)]
        shards = runner.partition_tests(discovered, 4, isolated_module="test_large_repository_inventory_acceptance")
        self.assertEqual(shards[0], long)
        self.assertTrue(all(shards))
        self.assertEqual(Counter(id(case) for group in shards for case in group),
                         Counter(id(case) for case in discovered))
        self.assertTrue(all(sum(all(case in shard for case in group) for shard in shards) == 1
                            for group in other_modules))
        for shard in shards:
            order = [id(case) for case in shard]
            self.assertEqual(order, [id(case) for case in discovered if id(case) in order])
        self.assertEqual(sorted(map(len, shards[1:])), [14, 15, 16])

    def test_isolation_refuses_missing_module_or_single_shard(self):
        discovered = self.module("test_large", 2)
        with self.assertRaisesRegex(ValueError, "at least two shards and a discovered module"):
            runner.partition_tests(discovered, 4, isolated_module="test_missing")
        with self.assertRaisesRegex(ValueError, "at least two shards and a discovered module"):
            runner.partition_tests(discovered, 1, isolated_module="test_large")

    def test_windows_workflow_uses_isolation_with_the_original_shard_contract(self):
        workflow = (ROOT / ".github/workflows/windows-desktop.yml").read_text(encoding="utf-8")
        _, step = workflow.split("name: Run all assigned offline Python regressions", 1)
        command, _ = step.split("name: Preserve the complete regression shard report", 1)
        self.assertIn("timeout-minutes: 45", command)
        self.assertIn("--shard-count 4", command)
        self.assertIn("--isolate-module test_large_repository_inventory_acceptance", command)
        with patch.object(runner, "run_regressions", return_value=0) as run:
            code = runner.main(["--shard-index", "0", "--shard-count", "4", "--output", "result.json",
                                "--isolate-module", "test_large_repository_inventory_acceptance"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.kwargs["isolated_module"], "test_large_repository_inventory_acceptance")
