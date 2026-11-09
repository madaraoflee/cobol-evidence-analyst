"""Read-only fixture cleanup retries only the Windows Git object condition."""

import os
from pathlib import Path
import shutil
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import online_fixture_cleanup as cleanup


class OnlineFixtureCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.checkout = self.root / "checkout"
        self.object = self.checkout / ".git/objects/ab/object"
        self.object.parent.mkdir(parents=True)
        self.object.write_bytes(b"synthetic Git object")
        self.outside = self.root / "unrelated.txt"
        self.outside.write_bytes(b"keep")

    def test_readonly_git_object_tree_is_removed_and_sibling_is_preserved(self):
        self.object.chmod(stat.S_IREAD)
        cleanup.remove_fixture_tree(self.checkout)
        self.assertFalse(self.checkout.exists())
        self.assertEqual(self.outside.read_bytes(), b"keep")

    def test_windows_permission_callback_makes_only_failed_readonly_file_writable(self):
        self.object.chmod(stat.S_IREAD)
        original_rmtree = shutil.rmtree
        observed = []
        def windows_readonly_failure(root, *, onerror):
            def retry(path):
                observed.append(Path(path).stat().st_mode)
                os.unlink(path)
            error = PermissionError("read-only Git object")
            onerror(retry, str(self.object), (type(error), error, None))
            original_rmtree(root)
        with mock.patch.object(cleanup.shutil, "rmtree", side_effect=windows_readonly_failure):
            cleanup.remove_fixture_tree(self.checkout)
        self.assertEqual(len(observed), 1)
        self.assertTrue(observed[0] & stat.S_IWRITE)
        self.assertFalse(self.checkout.exists())
        self.assertEqual(self.outside.read_bytes(), b"keep")

    def test_permission_error_for_already_writable_file_remains_a_failure(self):
        error = PermissionError("not the read-only fixture condition")
        retry = mock.Mock()
        def fail(root, *, onerror):
            onerror(retry, str(self.object), (type(error), error, None))
        with mock.patch.object(cleanup.shutil, "rmtree", side_effect=fail), \
                mock.patch.object(cleanup.os, "chmod") as chmod:
            with self.assertRaises(PermissionError) as caught:
                cleanup.remove_fixture_tree(self.checkout)
        self.assertIs(caught.exception, error)
        retry.assert_not_called()
        chmod.assert_not_called()
        self.assertTrue(self.object.is_file())

    def test_non_permission_error_is_not_ignored(self):
        error = OSError("unrelated cleanup failure")
        def fail(root, *, onerror):
            onerror(os.unlink, str(self.object), (type(error), error, None))
        with mock.patch.object(cleanup.shutil, "rmtree", side_effect=fail), \
                mock.patch.object(cleanup.os, "chmod") as chmod:
            with self.assertRaises(OSError) as caught:
                cleanup.remove_fixture_tree(self.checkout)
        self.assertIs(caught.exception, error)
        chmod.assert_not_called()
        self.assertTrue(self.object.is_file())

    def test_readonly_nonregular_path_never_changes_the_target_permissions(self):
        error = PermissionError("not a regular Git object")
        retry = mock.Mock()
        def fail(root, *, onerror):
            onerror(retry, str(self.object), (type(error), error, None))
        with mock.patch.object(cleanup.shutil, "rmtree", side_effect=fail), \
                mock.patch.object(cleanup.Path, "lstat", return_value=SimpleNamespace(st_mode=stat.S_IFLNK | stat.S_IREAD)), \
                mock.patch.object(cleanup.os, "chmod") as chmod:
            with self.assertRaises(PermissionError) as caught:
                cleanup.remove_fixture_tree(self.checkout)
        self.assertIs(caught.exception, error)
        retry.assert_not_called()
        chmod.assert_not_called()
        self.assertTrue(self.object.is_file())


if __name__ == "__main__":
    unittest.main()
