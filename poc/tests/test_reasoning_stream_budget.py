"""Hidden generation has a finite content allowance without a time cutoff."""

from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]
from api_diagnostics import APIResponseDiagnostics
from company_api import APIClientError, CompanyAPIConfig, OpenAICompatibleChatClient, TransportResponse, UrllibTransport
import test_company_api_streaming as stream_fixtures


class ReasoningStreamBudgetTests(unittest.TestCase):
    def setUp(self):
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model",
            api_key="neutral-stream-credential", max_output_tokens=16)

    def exchange(self, chunks, *, native, config=None):
        config = config or self.config
        diagnostics = APIResponseDiagnostics(protected_values=(config.api_key, config.base_url, config.chat_model))
        if native:
            response = stream_fixtures.ChunkResponse(chunks)
            transport = UrllibTransport()
            transport._opener = mock.Mock()
            transport._opener.open.return_value = response
            calls = transport._opener.open
        else:
            response = None
            transport = mock.Mock(return_value=TransportResponse(200, b"".join(chunks), stream_fixtures.STREAM_HEADERS))
            calls = transport
        client = OpenAICompatibleChatClient(config, transport=transport, diagnostics=diagnostics)
        return client, calls, response, diagnostics

    def ask(self, client, texts):
        return client.complete(messages=[{"role": "user", "content": "Explain the neutral source"}],
                               on_text_delta=texts.append)

    def test_exact_character_allowance_completes_in_native_and_injected_modes(self):
        for native in (True, False):
            with self.subTest(native=native):
                client, calls, response, _ = self.exchange([
                    stream_fixtures.event({"reasoning_content": "h" * 256}),
                    stream_fixtures.completion("Answer")], native=native)
                result = self.ask(client, [])
                self.assertEqual(result["choices"][0]["message"]["content"], "Answer")
                self.assertEqual(calls.call_count, 1)
                if response is not None:
                    self.assertTrue(response.closed.wait(1))

    def test_exceeding_by_one_character_stops_once_and_marks_unknown(self):
        for native in (True, False):
            with self.subTest(native=native):
                client, calls, response, diagnostics = self.exchange([
                    stream_fixtures.event({"reasoning_content": "h" * 257}),
                    stream_fixtures.completion("Unreceived answer")], native=native)
                texts = []
                with self.assertRaisesRegex(APIClientError, "^MODEL_REASONING_LIMIT_EXCEEDED$") as caught:
                    self.ask(client, texts)
                error = caught.exception
                self.assertEqual((error.reasoning_characters, error.reasoning_character_limit), (257, 256))
                self.assertTrue(error.remote_outcome_unknown)
                self.assertIsNone(error.partial_response)
                self.assertEqual(texts, [])
                self.assertEqual(calls.call_count, 1)
                self.assertNotIn("h" * 257, json.dumps(diagnostics.to_dict()) + str(error))
                if response is not None:
                    self.assertTrue(response.closed.wait(1))

    def test_all_choices_and_both_hidden_fields_share_one_allowance(self):
        first = stream_fixtures.event({"reasoning_content": "a" * 128, "reasoning": "b" * 128})
        second = b'data: {"choices":[{"index":1,"delta":{"reasoning":"c"}}]}\n\n'
        for native in (True, False):
            with self.subTest(native=native):
                client, _, response, _ = self.exchange([first, second], native=native)
                with self.assertRaises(APIClientError) as caught:
                    self.ask(client, [])
                self.assertEqual(caught.exception.code, "MODEL_REASONING_LIMIT_EXCEEDED")
                self.assertEqual(caught.exception.reasoning_characters, 257)
                if response is not None:
                    self.assertTrue(response.closed.wait(1))

    def test_visible_text_in_the_crossing_event_is_retained_and_sanitized(self):
        private = "hidden-generation-canary-" * 20
        visible = "Received answer. " + " ".join((self.config.api_key, self.config.base_url, self.config.chat_model))
        for native in (True, False):
            with self.subTest(native=native):
                client, calls, response, diagnostics = self.exchange([
                    stream_fixtures.event({"content": visible}),
                    stream_fixtures.event({"reasoning_content": private, "content": " Visible final fragment."})], native=native)
                with self.assertRaises(APIClientError) as caught:
                    self.ask(client, [])
                error = caught.exception
                partial = error.partial_response
                self.assertEqual(set(partial), {"object", "choices"})
                self.assertEqual(partial["choices"][0]["finish_reason"], None)
                self.assertEqual(set(partial["choices"][0]["message"]), {"role", "content"})
                self.assertIn("Received answer.", partial["choices"][0]["message"]["content"])
                self.assertIn("Visible final fragment.", partial["choices"][0]["message"]["content"])
                safe = json.dumps({"partial": partial, "error": error.to_safe_dict(), "diagnostics": diagnostics.to_dict()})
                for value in (private, self.config.api_key, self.config.base_url, self.config.chat_model):
                    self.assertNotIn(value, safe)
                self.assertEqual(calls.call_count, 1)
                if response is not None:
                    self.assertTrue(response.closed.wait(1))

    def test_partial_response_withholds_a_trailing_credential_prefix(self):
        prefix = self.config.api_key[:12]
        client, _, _, _ = self.exchange([
            stream_fixtures.event({"content": "Business text. " + prefix}),
            stream_fixtures.event({"reasoning": "x" * 257})], native=False)
        with self.assertRaises(APIClientError) as caught:
            self.ask(client, [])
        content = caught.exception.partial_response["choices"][0]["message"]["content"]
        self.assertEqual(content, "Business text. ")
        self.assertNotIn(prefix, content)

    def test_visible_text_does_not_reset_the_hidden_allowance(self):
        client, _, _, _ = self.exchange([
            stream_fixtures.event({"reasoning_content": "h" * 200}),
            stream_fixtures.event({"content": "Visible progress."}),
            stream_fixtures.event({"reasoning": "h" * 57})], native=False)
        with self.assertRaises(APIClientError) as caught:
            self.ask(client, [])
        self.assertEqual(caught.exception.reasoning_characters, 257)
        self.assertIn("Visible progress.", caught.exception.partial_response["choices"][0]["message"]["content"])

    def test_provider_terminal_event_is_not_reclassified_as_unknown(self):
        for native in (True, False):
            for chunks in ([stream_fixtures.event({"content": "Done.", "reasoning": "h" * 257}, finish="stop"),
                            b"data: [DONE]\n\n"],
                           [stream_fixtures.event({"content": "Done."}, finish="stop"),
                            stream_fixtures.event({"reasoning": "h" * 257}), b"data: [DONE]\n\n"]):
                with self.subTest(native=native, chunks=len(chunks)):
                    client, _, response, _ = self.exchange(chunks, native=native)
                    result = self.ask(client, [])
                    self.assertEqual(result["choices"][0]["message"]["content"], "Done.")
                    self.assertEqual(result["choices"][0]["finish_reason"], "stop")
                    if response is not None:
                        self.assertTrue(response.closed.wait(1))

    def test_request_output_allowance_controls_guard_even_with_default_effort(self):
        config = replace(self.config, max_output_tokens=32, reasoning_effort=None)
        client, _, _, _ = self.exchange([stream_fixtures.event({"reasoning": "h" * 300}),
                                        stream_fixtures.completion("Answer")], native=False, config=config)
        self.assertEqual(self.ask(client, [])["choices"][0]["message"]["content"], "Answer")

    def test_completed_native_stream_closes_at_allowance_without_waiting_for_more_hidden_content(self):
        unread = mock.Mock(side_effect=AssertionError("No further read after completed allowance"))
        client, calls, response, _ = self.exchange([
            stream_fixtures.event({"content": "Done.", "reasoning": "h" * 257}, finish="stop"),
            unread], native=True)
        self.assertEqual(self.ask(client, [])["choices"][0]["message"]["content"], "Done.")
        self.assertTrue(response.closed.wait(1))
        unread.assert_not_called()
        self.assertEqual(calls.call_count, 1)


if __name__ == "__main__":
    unittest.main()
