"""A source update must reject artifact paths it cannot safely restore."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from source_update import SourceUpdateBackup


class SourceUpdateBackupTests(unittest.TestCase):
    def test_backup_rejects_file_and_directory_symlinks_before_updating(self):
        with tempfile.TemporaryDirectory(prefix="source-update-") as folder:
            root = Path(folder)
            output = root / "output"
            output.mkdir()
            for target_is_directory in (False, True):
                with self.subTest(target_is_directory=target_is_directory):
                    target = root / ("directory-target" if target_is_directory else "file-target")
                    if target_is_directory:
                        target.mkdir()
                    else:
                        target.write_text("owned elsewhere")
                    artifact = output / "source-versions.sqlite"
                    artifact.symlink_to(target, target_is_directory=target_is_directory)
                    try:
                        with self.assertRaisesRegex(ValueError, "SOURCE_UPDATE_PATH_INVALID"):
                            backup = SourceUpdateBackup(output, [artifact.name])
                            backup.close()
                        self.assertTrue(artifact.is_symlink())
                        self.assertTrue(target.exists())
                    finally:
                        artifact.unlink()


if __name__ == "__main__":
    unittest.main()
