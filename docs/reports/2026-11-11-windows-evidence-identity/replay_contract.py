import sys,unittest,json,importlib.util,hashlib
from pathlib import Path
from unittest.mock import patch
ROOT=Path('/private/tmp/cobol-global-formula-audit-tree-20261110');OUT=Path('/private/tmp/cobol-crlf-identity-audit-20261111');sys.path[:0]=[str(ROOT/'poc'),str(ROOT/'poc/tests')]
import test_repository_formula_inventory as current
import test_repository_fixture_source_identity as regression
spec=importlib.util.spec_from_file_location('frozen_fixture',OUT/'before/test_repository_formula_inventory.py');old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
original=current.RepositoryFormulaInventoryTests.prepare
results={}
try:
 for label,prepare in [('before',old.RepositoryFormulaInventoryTests.prepare),('after',original)]:
  def simulated_windows(self,files,_prepare=prepare):
   write=Path.write_text
   def source_write(path,text,*args,**kwargs):
    if path.parent==self.source:kwargs['newline']='\r\n'
    return write(path,text,*args,**kwargs)
   with patch.object(Path,'write_text',source_write):return _prepare(self,files)
  current.RepositoryFormulaInventoryTests.prepare=simulated_windows
  with (OUT/(label+'-all16-forced-crlf.log')).open('w') as stream:
   result=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.TestLoader().loadTestsFromTestCase(current.RepositoryFormulaInventoryTests))
  results[label]={'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'runtime_changed':False}
finally:current.RepositoryFormulaInventoryTests.prepare=original
with (OUT/'after-new-identity-regressions.log').open('w') as stream:
 result=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.TestLoader().loadTestsFromTestCase(regression.RepositoryFixtureSourceIdentityTests))
results['new_tests']={'tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped)}
results['files_sha256']={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in ['poc/tests/test_repository_formula_inventory.py','poc/tests/test_repository_fixture_source_identity.py']}
results['before_file_sha256']=hashlib.sha256((OUT/'before/test_repository_formula_inventory.py').read_bytes()).hexdigest()
results['new_test_ids']=[t.id() for t in unittest.TestLoader().loadTestsFromTestCase(regression.RepositoryFixtureSourceIdentityTests)]
results['boundary']='Mac forced-CRLF fixture creation reproduces the Windows default; no runtime validator or historical test assertion modified.'
(OUT/'helper-fix-handoff.json').write_text(json.dumps(results,indent=2)+'\n');print(json.dumps(results))
