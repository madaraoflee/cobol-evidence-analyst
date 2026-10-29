"""Cloud update wiring preserves the prior copy and exposes actual sync counts."""

from pathlib import Path
import os
import unittest
from unittest import mock

import test_web_remote_source as fixtures
import test_remote_incremental as remote_fixtures
from remote_source import fetch_remote_source as actual_fetch, resolve_remote_source as actual_resolve
from web_app import _remote_metadata


class WebIncrementalSyncTests(unittest.TestCase):
    def real_cloud_fixture(self):
        fixture = fixtures.RemoteSourceIntegrationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        remote = remote_fixtures.RemoteIncrementalTests()
        remote.setUp()
        self.addCleanup(remote.doCleanups)
        fixture.write_source(remote.root, "REMOTE-REQUEST")
        original = (remote.root / "request.cbl").read_bytes()
        remote.files[remote_fixtures.OLD] = {"stage/request.cbl": original}

        def fetch(spec, destination, **kwargs):
            return actual_fetch(spec, destination, opener=remote.transport, **kwargs)

        with mock.patch("web_app.fetch_remote_source", side_effect=fetch):
            result = fixture.finish(fixture.app.start_remote(fixture.payload(url=remote.spec.public_metadata()["url"]))["job_id"])
        self.assertEqual(result["status"], "COMPLETED", result)
        remote.requests.clear(); remote.raw_paths.clear()
        return fixture, remote, result["result"], original, fetch

    def check_same_commit_repair(self, damage):
        fixture, remote, previous, original, fetch = self.real_cloud_fixture()
        old_source = Path(previous["source"])
        path = old_source / "request.cbl"
        conversation = fixture.app.new_conversation({})["conversation"]
        store = fixture.app.conversation_store
        store.append(conversation["id"], "user", "Saved question before cache damage.", status="completed")
        history = store.get(conversation["id"])
        if damage == "missing":
            path.unlink()
        else:
            before = path.stat()
            changed = original.replace(b"REMOTE-REQUEST", b"BROKEN-REQUEST")
            self.assertEqual(len(changed), len(original))
            path.write_bytes(changed)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
        damaged_bytes = path.read_bytes() if path.exists() else None
        with mock.patch("web_app.resolve_remote_source", side_effect=lambda spec, **kwargs:
                        actual_resolve(spec, opener=remote.transport, **kwargs)), \
                mock.patch("web_app.fetch_remote_source", side_effect=fetch) as fetched:
            result = fixture.finish(fixture.app.update_remote({"workspace_id": previous["workspace_id"],
                "token": fixtures.ACCESS_TOKEN})["job_id"])
        self.assertEqual(result["status"], "COMPLETED", result)
        current = result["result"]
        self.assertEqual(current["remote_update"]["status"], "repaired")
        self.assertTrue(current["remote_update"]["source_repaired"])
        self.assertEqual(current["remote_source"]["commit"], previous["remote_source"]["commit"])
        self.assertEqual(current["remote_source"]["downloaded_files"], 1)
        self.assertEqual(current["remote_source"]["sync_mode"], "incremental")
        self.assertNotEqual(current["source"], previous["source"])
        self.assertEqual((Path(current["source"]) / "request.cbl").read_bytes(), original)
        self.assertEqual(path.read_bytes() if path.exists() else None, damaged_bytes)
        self.assertEqual(store.get(conversation["id"]), history)
        self.assertEqual(remote.raw_paths, [(remote_fixtures.OLD, "stage/request.cbl")])
        self.assertFalse(any("/get/" in url for url in remote.requests))
        self.assertEqual(fetched.call_count, 1)
        fixture.assert_no_token_saved()

    def test_same_commit_missing_source_is_repaired_in_a_new_snapshot(self):
        self.check_same_commit_repair("missing")

    def test_same_commit_same_size_same_mtime_modified_source_is_repaired(self):
        self.check_same_commit_repair("changed")

    def test_same_commit_intact_source_needs_only_revision_metadata_no_download(self):
        fixture, remote, previous, original, _ = self.real_cloud_fixture()
        with mock.patch("web_app.resolve_remote_source", side_effect=lambda spec, **kwargs:
                        actual_resolve(spec, opener=remote.transport, **kwargs)), \
                mock.patch("web_app.fetch_remote_source") as fetched:
            result = fixture.finish(fixture.app.update_remote({"workspace_id": previous["workspace_id"],
                "token": fixtures.ACCESS_TOKEN})["job_id"])
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual(result["result"]["remote_update"]["status"], "up_to_date")
        self.assertEqual(result["result"]["source"], previous["source"])
        self.assertEqual(len(remote.requests), 1)
        self.assertEqual(remote.raw_paths, [])
        fetched.assert_not_called()

    def test_ordinary_restart_and_open_do_not_scan_all_cached_bytes_or_use_network(self):
        fixture, remote, previous, original, _ = self.real_cloud_fixture()
        with mock.patch("web_app.cached_snapshot_matches", side_effect=AssertionError("Unexpected whole-source check")) as verify, \
                mock.patch("web_app.resolve_remote_source") as resolve, mock.patch("web_app.fetch_remote_source") as fetched:
            restored = fixture.new_app()
            result = restored.open_source_workspace({"workspace_id": previous["workspace_id"]})
        self.assertEqual(result["project"]["source"], previous["source"])
        verify.assert_not_called(); resolve.assert_not_called(); fetched.assert_not_called()
        self.assertEqual(remote.requests, [])

    def test_cloud_update_receives_prior_snapshot_and_persists_transfer_counts(self):
        fixture = fixtures.RemoteSourceIntegrationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        url = "https://bitbucket.org/example-team/business-source/src/main/stage/"
        metadata = {"provider": "bitbucket_cloud", "url": url, "ref": "main",
                    "subdirectory": "stage", "commit": "a" * 40}

        def initial(spec, destination, **kwargs):
            self.assertNotIn("previous_source", kwargs)
            destination.mkdir(parents=True)
            fixture.write_source(destination, "REMOTE-REQUEST")
            return dict(metadata)

        with mock.patch("web_app.fetch_remote_source", side_effect=initial):
            started = fixture.app.start_remote(fixture.payload(url=url))
            previous = fixture.finish(started["job_id"])["result"]
        newer = {**metadata, "commit": "b" * 40}
        before = fixture.file_contents(Path(previous["source"]), Path(previous["output"]))

        def updated(spec, destination, **kwargs):
            self.assertEqual(kwargs["previous_source"], Path(previous["source"]))
            self.assertEqual(kwargs["previous_output"], Path(previous["output"]))
            self.assertEqual(kwargs["previous_metadata"]["commit"], metadata["commit"])
            self.assertEqual(kwargs["resolved"], newer)
            destination.mkdir(parents=True)
            fixture.write_source(destination, "UPDATED-REQUEST")
            return {**newer, "sync_mode": "incremental", "downloaded_files": 1,
                    "reused_files": 12001, "removed_files": 2, "downloaded_bytes": 320, "file_count": 12002}

        with mock.patch("web_app.resolve_remote_source", return_value=newer), \
                mock.patch("web_app.fetch_remote_source", side_effect=updated) as fetch:
            result = fixture.finish(fixture.app.update_remote({"workspace_id": previous["workspace_id"],
                "token": fixtures.ACCESS_TOKEN})["job_id"])
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual(fetch.call_count, 1)
        current = result["result"]
        self.assertEqual(current["remote_source"]["downloaded_files"], 1)
        self.assertEqual(current["remote_source"]["reused_files"], 12001)
        self.assertEqual(current["remote_source"]["sync_mode"], "incremental")
        self.assertEqual(fixture.file_contents(Path(previous["source"]), Path(previous["output"])), before)
        restored = fixture.new_app()
        self.assertEqual(restored.project["remote_source"], current["remote_source"])
        with mock.patch("web_app.resolve_remote_source", return_value=newer), \
                mock.patch("web_app.fetch_remote_source") as fetch:
            unchanged = fixture.finish(restored.update_remote({"workspace_id": current["workspace_id"],
                "token": fixtures.ACCESS_TOKEN})["job_id"], app=restored)
        self.assertEqual(unchanged["status"], "COMPLETED")
        self.assertEqual(unchanged["result"]["remote_update"]["status"], "up_to_date")
        self.assertEqual(unchanged["result"]["remote_source"]["downloaded_files"], 1)
        fetch.assert_not_called()
        fixture.assert_no_token_saved()

    def test_transfer_metadata_ignores_non_counts_and_private_fields(self):
        metadata = {**fixtures.REMOTE_METADATA, "sync_mode": "incremental",
                    "downloaded_files": True, "reused_files": -1, "removed_files": "2",
                    "downloaded_bytes": 2**53, "file_count": 20,
                    "token": "do-not-store", "previous_source": "/private-location"}
        result = _remote_metadata(metadata)
        self.assertEqual(result["file_count"], 20)
        for key in ("downloaded_files", "reused_files", "removed_files", "downloaded_bytes", "token", "previous_source"):
            self.assertNotIn(key, result)


if __name__ == "__main__":
    unittest.main()
