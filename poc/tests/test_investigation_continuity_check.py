"""The supplemental desktop checker binds actual HTTP source and boundaries."""

import hashlib
from copy import deepcopy
import http.client
import json
from pathlib import Path
import ssl
import sys
import tempfile
import unittest
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.check_investigation_continuity import (BUSINESS_CALLER, BUSINESS_SOURCE,
    CompletionProvider, IndependentReviewFixture, REVIEW_COPY, REVIEW_PROMPT, REVIEW_QUESTION,
    REVIEW_SOURCE, SOURCE, continuation_turn, run_independent_review_check, verify_pages)
from tools.desktop_acceptance_runtime import FIXTURES


class InvestigationContinuityCheckTests(unittest.TestCase):
    def independent_fixture(self, source):
        pages = []
        for name, body in (("root.cbl", REVIEW_SOURCE), ("amount-input.cpy", REVIEW_COPY)):
            (source / name).write_text(body, encoding="utf-8")
            pages.append({"evidence_id": "ev_page_" + str(len(pages) + 1) * 24,
                "relative_path": name, "source_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "start_line": 1, "end_line": len(body.splitlines()), "source_text": body.rstrip("\n")})
        pages[-1].update(copy_instance="a" * 24, replacement_trace=[], include_chain=[{
            "relative_path": "root.cbl", "line": 5, "source_hash": pages[0]["source_sha256"], "copy_name": "AMOUNT-INPUT"}])
        fixture = IndependentReviewFixture(source)
        envelope = {"messages": [{"role": "system", "content": REVIEW_PROMPT}, {"role": "user", "content": "{}"}]}
        payload = {"question": REVIEW_QUESTION, "answer_detail": "detailed", "source_context": [{"pages": pages}]}
        answer, reason = fixture.respond(envelope, payload, 0)
        self.assertEqual(reason, "stop")
        self.assertGreater(len(answer), 600)
        evidence = [{"reference_id": page["evidence_id"], "kind": "source_page", "path": page["relative_path"],
            "sha256": page["source_sha256"], "start_line": page["start_line"], "end_line": page["end_line"],
            "text": page["source_text"], "text_sha256": hashlib.sha256(page["source_text"].encode()).hexdigest(),
            "text_truncated": False, "text_offset_chars": 0,
            "source_context": {key: page[key] for key in ("include_chain", "replacement_trace", "copy_instance") if key in page}}
            for page in pages]
        wire = {"schema": "semantic-answer-review/v1", "status": "reviewable", "question": REVIEW_QUESTION,
            "answer_sha256": hashlib.sha256(answer.encode()).hexdigest(), "answer_characters": len(answer),
            "units": [{"unit_id": "unit-001", "text": answer}], "evidence": evidence,
            "evidence_sha256": hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False).encode()).hexdigest()}
        return fixture, envelope, payload, wire

    def test_independent_review_fixture_requires_exact_answer_source_and_copy_context(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, _, _, wire = self.independent_fixture(Path(directory))
            response = json.loads(fixture.review_response(wire, changes=True))
            self.assertEqual(set(response), {"answer_sha256", "evidence_sha256", "reviews", "omissions"})
            self.assertEqual(response["reviews"][0]["verdict"], "insufficient")
            variants = []
            for key, value in (("answer_sha256", "0" * 64), ("evidence_sha256", "0" * 64),
                               ("source_context", []), ("evidence", wire["evidence"][:1])):
                variants.append({**deepcopy(wire), key: value})
            for key, value in (("sha256", "0" * 64), ("text", REVIEW_COPY.replace("10", "99")),
                               ("source_context", {}), ("text_truncated", True)):
                changed = deepcopy(wire)
                changed["evidence"][-1][key] = value
                variants.append(changed)
            for changed in variants:
                with self.subTest(changed=changed), self.assertRaises(AssertionError):
                    fixture.review_response(changed, changes=False)

    def test_independent_revision_rejects_missing_review_stale_instructions_and_extra_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, envelope, payload, wire = self.independent_fixture(Path(directory))
            fixture.review_response(wire, changes=True)
            with self.assertRaises(AssertionError):
                fixture.respond(envelope, {**payload, "draft_answer": fixture.answers[0]}, 2)
            with self.assertRaises(AssertionError):
                fixture.respond({"messages": [{"content": "Earlier saved instruction"}]}, payload, 0)
            with self.assertRaises(AssertionError):
                fixture.respond(envelope, payload, 4)
            changed = deepcopy(payload)
            changed["source_context"][0]["pages"][-1]["include_chain"][0]["source_hash"] = "0" * 64
            with self.assertRaises(AssertionError):
                fixture.respond(envelope, changed, 0)

    def test_independent_review_source_http_preview_runs_four_requests_and_keeps_json_private(self):
        # This checks the harness through the real ordinary HTTP route. Release
        # verification still has to pass the same command with the final EXE.
        with tempfile.TemporaryDirectory() as directory:
            checks, observations = {}, []
            result = run_independent_review_check([sys.executable,
                str(Path(__file__).resolve().parents[1] / "desktop_app.py")], Path(directory), checks, observations)
            self.assertEqual(len(checks), 4)
            self.assertTrue(all(checks.values()), checks)
            self.assertEqual(result["synthetic_model_requests"], 4)
            self.assertEqual(observations[0]["stages"][1:], ["semantic_review", "semantic_revise", "semantic_review"])
            self.assertEqual(len(set(observations[0]["reviewed_answer_sha256"])), 2)
            self.assertEqual(set(result["fixture_sha256"]), {"independent_review/root.cbl", "independent_review/amount-input.cpy"})

    def test_cross_language_fixture_cannot_continue_a_completed_or_unsourced_turn(self):
        user = {"role": "user", "status": "partial"}
        assistant = {"role": "assistant", "status": "partial", "evidence_refs": [{"evidence_id": "ev:source"}],
                     "investigation_run_id": "a" * 24}
        self.assertEqual(continuation_turn({"messages": [user, assistant]}), (user, assistant))
        for changed in ({**assistant, "status": "completed"}, {**assistant, "evidence_refs": []},
                        {**assistant, "investigation_run_id": None}):
            with self.subTest(changed=changed), self.assertRaises(AssertionError):
                continuation_turn({"messages": [user, changed]})
        with self.assertRaises(AssertionError):
            continuation_turn({"messages": [{**user, "status": "completed"}, assistant]})

    def test_supply_check_rejects_wrong_version_and_invented_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            for newline in ("\n", "\r\n"):
                with self.subTest(newline=repr(newline)):
                    raw = SOURCE.replace("\n", newline).encode("utf-8")
                    (source / "root.cbl").write_bytes(raw)
                    page = {"relative_path": "root.cbl", "source_sha256": hashlib.sha256(raw).hexdigest(),
                        "start_line": 1, "end_line": len(SOURCE.splitlines()), "source_text": SOURCE}
                    payload = {"source_context": [{"pages": [page]}]}
                    self.assertEqual(verify_pages(source, payload), page)
                    for changed in ({"source_sha256": "0" * 64}, {"source_text": SOURCE.replace("* 2", "* 3")}):
                        with self.subTest(changed=list(changed)), self.assertRaises(AssertionError):
                            verify_pages(source, {"source_context": [{"pages": [{**page, **changed}]}]})

    def test_loopback_provider_emits_real_length_boundary_and_records_request(self):
        with tempfile.TemporaryDirectory() as directory:
            with CompletionProvider(directory, lambda envelope, payload, index: ("Retained partial.", "length")) as provider:
                address = urlsplit(provider.base_url)
                context = ssl.create_default_context(cafile=str(FIXTURES / "loopback-server.crt"))
                connection = http.client.HTTPSConnection(address.hostname, address.port, context=context, timeout=5)
                try:
                    body = json.dumps({"messages": [{"role": "user", "content": "{}"}]}).encode()
                    connection.request("POST", "/v1/chat/completions", body, {"Content-Type": "application/json"})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    data = json.loads(response.read())
                finally:
                    connection.close()
                self.assertEqual(data["choices"][0]["finish_reason"], "length")
                self.assertEqual(data["choices"][0]["message"]["content"], "Retained partial.")
                self.assertEqual(provider.requests[0]["request_sha256"], hashlib.sha256(body).hexdigest())
                self.assertEqual(provider.errors, [])

    def test_business_handoff_check_requires_both_original_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            pages = []
            for name, text in (("decision.cbl", BUSINESS_SOURCE), ("caller.cbl", BUSINESS_CALLER)):
                path = source / name
                path.write_text(text, encoding="utf-8")
                pages.append({"relative_path": name, "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "start_line": 1, "end_line": len(text.splitlines()), "source_text": text})
            payload = {"source_context": [{"pages": pages}]}
            self.assertEqual(verify_pages(source, payload, "MOVE 12 TO RESULT-CODE")["relative_path"], "decision.cbl")
            self.assertEqual(verify_pages(source, payload, "MOVE 'N' TO ALLOW-PROCESS")["relative_path"], "caller.cbl")
            with self.assertRaises(StopIteration):
                verify_pages(source, {"source_context": [{"pages": pages[:1]}]}, "MOVE 'N' TO ALLOW-PROCESS")
            invented = {**pages[1], "source_text": BUSINESS_CALLER.replace("RESULT-CODE = 12", "RESULT-CODE = 13")}
            with self.assertRaises(AssertionError):
                verify_pages(source, {"source_context": [{"pages": [pages[0], invented]}]}, "MOVE 'N' TO ALLOW-PROCESS")


if __name__ == "__main__":
    unittest.main()
