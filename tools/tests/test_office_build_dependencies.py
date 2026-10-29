"""Helper staging must include offline scenario imports without repository help."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import build_office_acceptance as builder


class OfficeHelperDependenciesTests(unittest.TestCase):
    def test_staged_tools_have_their_complete_local_import_closure(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary) / "isolated-stage"
            builder.stage_sources(stage)
            # A fresh isolated interpreter cannot borrow the repository's
            # unstaged tools or this test runner's already imported modules.
            script = textwrap.dedent('''
                import ast
                import importlib
                import json
                from pathlib import Path
                import sys
                stage = Path(sys.argv[1]).resolve()
                sys.path[:0] = [str(stage), str(stage / 'tools'), str(stage / 'poc')]
                importlib.import_module('office_acceptance_app')
                dependencies = set()
                for path in sorted((stage / 'tools').glob('*.py')):
                    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                        if isinstance(node, ast.ImportFrom) and node.module:
                            module = node.module
                            if module.startswith('tools.') or (stage / 'tools' / (module + '.py')).is_file():
                                dependencies.add(module)
                        elif isinstance(node, ast.Import):
                            dependencies.update(item.name for item in node.names
                                if item.name.startswith('tools.'))
                for name in sorted(dependencies):
                    module = importlib.import_module(name)
                    assert Path(module.__file__).resolve().is_relative_to(stage), name
                runtime = importlib.import_module('tools.desktop_acceptance_runtime')
                runtime.FIXTURES = stage / 'kit-fixtures'
                for name in dependencies:
                    if name.rsplit('.', 1)[-1] == 'desktop_acceptance_runtime':
                        continue
                    module = sys.modules[name]
                    if hasattr(module, 'write_profile'):
                        assert module.write_profile is runtime.write_profile, name
                        assert module.write_profile.__globals__['FIXTURES'] == runtime.FIXTURES, name
                    if hasattr(module, 'LoopbackProvider'):
                        assert module.LoopbackProvider is runtime.LoopbackProvider, name
                print(json.dumps(sorted(dependencies)))
            ''')
            result = subprocess.run([sys.executable, "-I", "-c", script, str(stage)],
                cwd=temporary, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
