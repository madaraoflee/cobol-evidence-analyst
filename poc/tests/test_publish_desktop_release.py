from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import publish_desktop_release as publisher
import test_release_acceptance as fixtures


class PublishDesktopReleaseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ReleaseAcceptanceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.arguments = {**self.fixture.paths, "repo": "example/workbench", "tag": "v1-candidate",
                          "output": self.fixture.root / "publication.json"}
        compiled = self.fixture.read("compiled_report")
        self.release = {"id": 10, "tag_name": "v1-candidate", "draft": True,
            "target_commitish": self.fixture.commit, "assets": [{"id": 20,
                "name": self.fixture.paths["package"].name,
                "size": self.fixture.paths["package"].stat().st_size,
                "digest": "sha256:" + compiled["archive_sha256"]}]}
        self.commands = []

    def fake_gh(self, arguments, **options):
        self.commands.append(arguments)
        if arguments[0] == "api":
            path = arguments[1]
            if "/git/ref/tags/" in path:
                return None
            if "/releases/assets/" in path:
                return self.fixture.paths["package"].read_bytes()
            if "/releases/tags/" in path:
                document = copy.deepcopy(self.release)
                if any(command[:2] == ["release", "edit"] for command in self.commands):
                    document["draft"] = False
                return json.dumps(document).encode()
        return b""

    def test_failed_gate_never_calls_github_even_with_publish_flag(self):
        compiled = self.fixture.read("compiled_report")
        compiled["cases"][0]["checks"]["quality_formula"] = False
        self.fixture.write("compiled_report", compiled)
        with mock.patch.object(publisher, "_gh", side_effect=AssertionError("must not call GitHub")):
            result = publisher.publish_release(**self.arguments, publish=True)
        self.assertEqual(result["status"], "gate_rejected")
        self.assertFalse(result["published"])

    def test_default_dry_run_is_local_and_receipt_contains_allowlisted_hashes_only(self):
        with mock.patch.object(publisher, "_gh", side_effect=AssertionError("must not call GitHub")):
            result = publisher.publish_release(**self.arguments)
        self.assertEqual(result["status"], "local_acceptance_passed_dry_run")
        receipt = json.loads((self.fixture.root / "release-acceptance-receipt.json").read_text())
        self.assertEqual(set(receipt), {"schema_version", "candidate_allowed", "publish_allowed", "artifact", "input_sha256"})
        serialized = json.dumps(receipt)
        for private in ("entry.cbl", "flow", "local-reviewer", "The result is twice", str(self.fixture.root)):
            self.assertNotIn(private, serialized)
        self.assertNotIn("binary_path", receipt["artifact"])

    def test_publish_rechecks_draft_and_uploads_only_receipt_before_confirmation(self):
        with mock.patch.object(publisher, "_gh", side_effect=self.fake_gh):
            result = publisher.publish_release(**self.arguments, publish=True)
        self.assertTrue(result["published"], result)
        mutations = [command for command in self.commands if command[0] == "release"]
        self.assertEqual([command[1] for command in mutations], ["upload", "edit"])
        self.assertEqual(Path(mutations[0][3]).name, "release-acceptance-receipt.json")
        self.assertIn("--draft=false", mutations[1])
        self.assertEqual(sum(command[0] == "api" and "/releases/tags/" in command[1]
                             for command in self.commands), 3)

    def test_wrong_remote_commit_hash_state_or_extra_binary_blocks_writes(self):
        original = copy.deepcopy(self.release)
        mutations = [lambda doc: doc.update(target_commitish="main"),
                     lambda doc: doc.update(draft=False),
                     lambda doc: doc["assets"][0].update(digest="sha256:" + "0" * 64),
                     lambda doc: doc["assets"].append({"id": 21, "name": "other-platform.zip"})]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                self.release = copy.deepcopy(original)
                mutate(self.release)
                self.commands = []
                with mock.patch.object(publisher, "_gh", side_effect=self.fake_gh):
                    result = publisher.publish_release(**self.arguments, publish=True)
                self.assertFalse(result["published"])
                self.assertFalse(any(command[0] == "release" for command in self.commands))

    def test_existing_tag_must_resolve_to_accepted_commit(self):
        def different_tag(arguments, **options):
            if arguments[0] == "api" and "/git/ref/tags/" in arguments[1]:
                return json.dumps({"object": {"type": "commit", "sha": "f" * 40}}).encode()
            return self.fake_gh(arguments, **options)
        with mock.patch.object(publisher, "_gh", side_effect=different_tag):
            result = publisher.publish_release(**self.arguments, publish=True)
        self.assertEqual(result["error"], "RELEASE_TAG_COMMIT_MISMATCH")
        self.assertFalse(any(command[0] == "release" for command in self.commands))

    def test_missing_asset_digest_downloads_and_hashes_bytes(self):
        self.release["assets"][0].pop("digest")
        with mock.patch.object(publisher, "_gh", side_effect=self.fake_gh):
            result = publisher.publish_release(**self.arguments, publish=True)
        self.assertTrue(result["published"])
        self.assertEqual(sum("--header" in command for command in self.commands), 2)

    def test_local_inputs_changed_after_gate_cannot_be_published(self):
        def change_input(*args):
            self.fixture.paths["policy"].write_text("{}")
            return 10, 20
        with mock.patch.object(publisher, "_verified_draft", side_effect=change_input), \
                mock.patch.object(publisher, "_gh", side_effect=AssertionError("no write")):
            result = publisher.publish_release(**self.arguments, publish=True)
        self.assertEqual(result["error"], "INPUT_CHANGED_AFTER_GATE")

    def test_remote_change_after_receipt_upload_never_promotes(self):
        with mock.patch.object(publisher, "_verified_draft", side_effect=[(10, 20), (10, 21)]), \
                mock.patch.object(publisher, "_gh", side_effect=self.fake_gh):
            result = publisher.publish_release(**self.arguments, publish=True)
        self.assertEqual(result["error"], "DRAFT_CHANGED_DURING_PUBLICATION")
        self.assertEqual([command[1] for command in self.commands if command[0] == "release"], ["upload"])

    def test_uncertain_publish_is_not_retried_or_claimed_successful(self):
        def uncertain(arguments, **options):
            if arguments[:2] == ["release", "edit"]:
                self.commands.append(arguments)
                raise publisher.PublicationError("GITHUB_COMMAND_UNCONFIRMED")
            return self.fake_gh(arguments, **options)
        with mock.patch.object(publisher, "_gh", side_effect=uncertain):
            result = publisher.publish_release(**self.arguments, publish=True)
        self.assertFalse(result["published"])
        self.assertEqual(result["status"], "publication_not_confirmed")
        self.assertEqual(sum(command[:2] == ["release", "edit"] for command in self.commands), 1)

    def test_output_cannot_replace_any_gate_input(self):
        with self.assertRaisesRegex(publisher.PublicationError, "OVERLAPS_INPUT"), \
                mock.patch.object(publisher, "_gh", side_effect=AssertionError("no network")):
            publisher.publish_release(**{**self.arguments, "output": self.fixture.paths["policy"]})

    def test_receipt_cannot_use_upload_label_syntax_or_relative_option_path(self):
        with self.assertRaisesRegex(publisher.PublicationError, "RECEIPT_PATH_INVALID"), \
                mock.patch.object(publisher, "_gh", side_effect=AssertionError("no network")):
            publisher.publish_release(**self.arguments, receipt=self.fixture.root / "old#receipt.json", publish=True)
        with mock.patch.object(publisher, "_gh", side_effect=self.fake_gh):
            result = publisher.publish_release(**self.arguments, publish=True)
        self.assertTrue(result["published"])
        upload = next(command for command in self.commands if command[:2] == ["release", "upload"])
        self.assertTrue(Path(upload[3]).is_absolute())

    def test_cli_defaults_to_dry_run_and_missing_office_review_never_calls_github(self):
        arguments = [value for name, argument in self.arguments.items()
                     for value in ("--" + name.replace("_", "-"), str(argument))]
        with mock.patch.object(publisher, "_gh", side_effect=AssertionError("no network")), \
                mock.patch("builtins.print"):
            self.assertEqual(publisher.main(arguments), 0)
            reviews = self.fixture.read("reviews")
            reviews.pop("office_acceptance")
            self.fixture.write("reviews", reviews)
            self.assertEqual(publisher.main([*arguments, "--publish"]), 1)


if __name__ == "__main__":
    unittest.main()
