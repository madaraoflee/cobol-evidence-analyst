"""Exercise two retrieval gaps through the ordinary local workbench and its saved conversation."""

from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_grep_acceptance as workbench
from local_grep_acceptance import choose_reply, originals


SCENARIOS = ("noisy_definitions", "distant_guard")


def sources(scenario):
    files = originals("explicit_grep")
    if scenario == "noisy_definitions":
        for i in range(9):
            # These are executable displays of a string, not nine alternative program entries.
            files[f"a-notes/advice-{i}.cbl"] = (
                f'IDENTIFICATION DIVISION.\nPROGRAM-ID. ADVICE{i}.\n'
                'PROCEDURE DIVISION.\nDISPLAY "PROGRAM-ID. ADJ731".\nGOBACK.').encode()
    elif scenario == "distant_guard":
        text = workbench.WORKER.replace("IF POOL-AMOUNT", "*> opening comments\n" * 36 + "IF POOL-AMOUNT")
        files["archive/settlement.cbl"] = text.replace(" SUBTRACT", "*> inside comments\n" * 40 + " SUBTRACT").encode()
    else:
        raise ValueError(scenario)
    return files


def _program_page(pages, name):
    header = re.compile(r"^\s*PROGRAM-ID\.\s+" + re.escape(name) + r"\.", re.M | re.I)
    return next((p for p in pages if header.search(p["source_text"])), None)


def replay_case(scenario):
    files = sources(scenario)
    attempts = {}

    def ask_again(term, fallback):
        # Allow the assistant to request successive pages of a common literal.
        # The existing workbench's 12-request ceiling catches a missing termination condition.
        attempts[term] = attempts.get(term, 0) + 1
        return {"grep": [term]} if attempts[term] <= 5 else fallback

    def model(pages, actions, *, initial_deferral):
        # Prefer the longest genuine view when several reads of a program are supplied.
        pages = sorted(pages, key=lambda p: -len(p["source_text"]))
        entry = next((p for p in pages if "TO REFUND-ROUTINE" in p["source_text"]), None)
        if not entry:
            return workbench.INSUFFICIENT
        partial = ("主程序在类型符合时扣准备金，应收为正才尝试退款。尚未核实退款的扣减条件和最终应收。"
                   f'[{entry["evidence_id"]}]')
        caller = entry
        chain = [entry]
        for variable in ("REFUND-ROUTINE", "NEXT-ROUTINE"):
            assigned = re.search(r'MOVE "([A-Z0-9-]+)" TO ' + variable, caller["source_text"])
            if not assigned or f"CALL {variable} USING " not in caller["source_text"]:
                return partial
            next_program = _program_page(pages, assigned[1])
            if not next_program:
                return ask_again("PROGRAM-ID. " + assigned[1], partial)
            chain.append(next_program)
            caller = next_program
        local_pages = [p for p in pages if p["relative_path"] == caller["relative_path"]]
        if not ("IF POOL-AMOUNT" in caller["source_text"] and "SUBTRACT POOL-AMOUNT" in caller["source_text"]):
            # The main program itself contains the SUBTRACT verb. Its second use can locate the
            # downstream calculation even before we know the downstream deduction field name.
            verb = re.search(r"\b(SUBTRACT)\s+\S+\s+FROM\s+\S+", entry["source_text"])
            if not verb:
                return partial
            debit = next((p for p in local_pages if re.search(r"^\s*SUBTRACT\s+", p["source_text"], re.M)), None)
            if not debit:
                return ask_again(verb[1], partial)
            control = next((p for p in local_pages if re.search(r"^\s*IF\s+", p["source_text"], re.M)), None)
            if not control:
                return partial
            # Once the actual branch is visible, read from the observed header to the observed
            # return. Only genuine supplied source can establish the execution order.
            terminal = next((p for p in local_pages if re.search(r"^\s*GOBACK\.", p["source_text"], re.M)), None)
            if terminal:
                return {"read": [{"relative_path": caller["relative_path"], "start_line": caller["start_line"],
                                  "end_line": terminal["end_line"]}]}
            return partial
        return choose_reply(chain, actions, initial_deferral=initial_deferral)

    # These hooks vary only the input files and the model's replies in the established
    # network-blocked workbench fixture. All indexing, tools, reading and delivery are real.
    with patch.object(workbench, "originals", return_value=files), patch.object(workbench, "choose_reply", model):
        return workbench.replay("explicit_grep")


class LocalGrepFollowupDeliveryTests(unittest.TestCase):
    def assert_complete_deductions(self, result):
        self.assertEqual(result["status"], "completed", (result["stop_reason"], result["answer"]))
        self.assertEqual(result["network_connections"], 0)
        self.assertTrue(result["while_waiting"]["checked_findings"])
        body = re.sub(r"\[ev_page_[a-f0-9]+\]|[\s`]", "", result["answer"])
        for field in ("1-RESERVECREDIT", "CREDIT-AMT", "NET-DUE", "POOL-AMOUNT", "REMAINING-DUE"):
            self.assertIn(field, body)
        self.assertRegex(body, r"1-RESERVECREDIT.{1,100}跳过.{1,150}(?:大于[0零]|为正|>0)")
        self.assertRegex(body, r"(?:否则|小于(?:或)?等于[0零]).{1,75}(?:跳过|保留)")
        self.assertRegex(body, r"POOL-AMOUNT.{0,20}(?:严格小于|小于(?!或?等于)|<).{0,20}REMAINING-DUE")
        self.assertRegex(body, r"(?:相等或更大|大于(?:或)?等于|>=).{0,55}跳过")
        self.assertRegex(body, r"最终.{0,16}应收.{0,50}(?:减去|扣除)")
        self.assertRegex(body, r"100.{1,80}20.{1,80}30.{1,60}(?<!\d)50(?!\d)")
        self.assertRegex(body, r"80.{1,70}(?<!\d)80(?!\d)")
        return [ref for ref in result["citations"] if ref["evidence_id"] in result["inline_citations"]]

    def test_workbench_opens_later_matches_of_same_literal_until_actual_program_is_read(self):
        result = replay_case("noisy_definitions")
        refs = self.assert_complete_deductions(result)
        steps = [row for row in result["requests"] if row["reply"] == {"grep": ["PROGRAM-ID. ADJ731"]}]
        self.assertGreaterEqual(len(steps), 2)
        searches = [a for row in result["requests"] for a in row["grep_actions"]
                    if a.get("target") == "PROGRAM-ID. ADJ731"]
        first = searches[0]
        self.assertEqual(len(first["matches"]), 10)
        self.assertEqual(first["matches"][-1], {"relative_path": "legacy/route.cbl", "line": 2})
        self.assertTrue(all(m["relative_path"].startswith("a-notes/") for m in first["matches"][:-1]))
        self.assertTrue(any(ref["relative_path"] == "legacy/route.cbl" for ref in refs))
        self.assertTrue(any(ref["relative_path"] == "archive/settlement.cbl" for ref in refs))

    def test_workbench_supplies_remote_refund_guard_after_searching_the_debit(self):
        result = replay_case("distant_guard")
        refs = self.assert_complete_deductions(result)
        path = "archive/settlement.cbl"
        requests = result["requests"]
        step = next(i for i, r in enumerate(requests) if r["reply"] == {"grep": ["SUBTRACT"]})
        for line in (40, 81):
            self.assertFalse(any(p["path"] == path and p["lines"][0] <= line <= p["lines"][1]
                                 for r in requests[:step + 1] for p in r["supplied_pages"]))
            self.assertTrue(any(p["path"] == path and p["lines"][0] <= line <= p["lines"][1]
                                for p in requests[step + 1]["supplied_pages"]), line)
            self.assertTrue(any(ref["relative_path"] == path and ref["start_line"] <= line <= ref["end_line"]
                                for ref in refs), line)


if __name__ == "__main__":
    unittest.main()
