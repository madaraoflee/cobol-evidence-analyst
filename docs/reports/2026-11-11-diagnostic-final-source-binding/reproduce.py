"""Offline real-driver reproduction; does not edit repository files."""
import sys
sys.dont_write_bytecode = True
from argparse import Namespace
import importlib.util
import json
from pathlib import Path
import tempfile
from unittest import mock

repo = Path('/private/tmp/cobol-global-formula-audit-tree-20261110')
tool = repo / 'tools/diagnose_online_inventory_cost.py'
spec = importlib.util.spec_from_file_location('diagnostic_boundary_reproduction', tool)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
initial = module.binding(repo)
changed = dict(initial)
key = next(iter(changed))
changed[key] = 'f' * 64 if changed[key] != 'f' * 64 else '0' * 64
output = Path(tempfile.mkdtemp(prefix='diagnostic-binding-reproduction-')) / 'output'
args = Namespace(repo_root=repo, output=output, files=3)
with mock.patch.object(module.argparse.ArgumentParser, 'parse_args', return_value=args), \
     mock.patch.object(module, 'binding', side_effect=[initial, initial, changed]) as binding:
    code = module.main()
report = json.loads((output / 'DIAGNOSTIC-REPORT.json').read_text())
print(json.dumps({'return_code': code, 'binding_calls': binding.call_count,
    'status': report['status'], 'source_unchanged': report['source_unchanged_in_declared_scope'],
    'report': str(output / 'DIAGNOSTIC-REPORT.json')}, indent=2))
assert code == 0 and binding.call_count == 3
assert report['status'] == 'completed_diagnostic'
assert report['source_unchanged_in_declared_scope'] is False
