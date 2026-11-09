"""Remove owned synthetic trees with read-only Git objects on Windows."""

import os
from pathlib import Path
import shutil
import stat


def remove_fixture_tree(root):
    def retry_readonly_file(function, path, exception_info):
        error = exception_info[1]
        if not isinstance(error, PermissionError):
            raise error
        mode = Path(path).lstat().st_mode
        if not stat.S_ISREG(mode) or mode & stat.S_IWRITE:
            raise error
        os.chmod(path, mode | stat.S_IWRITE)
        function(path)

    shutil.rmtree(root, onerror=retry_readonly_file)
