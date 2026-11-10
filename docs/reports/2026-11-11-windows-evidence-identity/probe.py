import sys,json,hashlib
from pathlib import Path
from unittest.mock import patch
ROOT=Path('/private/tmp/cobol-global-formula-audit-tree-20261110');sys.path[:0]=[str(ROOT/'poc'),str(ROOT/'poc/tests')]
from test_repository_formula_inventory import RepositoryFormulaInventoryTests,program
from repository_inventory import inventory_batch,accept_inventory_response,_cover
case=RepositoryFormulaInventoryTests();case.setUp()
try:
 original=Path.write_text
 def windows_write(path,text,*args,**kwargs):
  if path.parent==case.source:kwargs['newline']='\r\n'
  return original(path,text,*args,**kwargs)
 with patch.object(Path,'write_text',windows_write):
  case.prepare({'entry.cbl':program('ENTRY','COMPUTE PREMIUM-AMT = BASIS * FACTOR.')})
 raw=(case.source/'entry.cbl').read_bytes();items=inventory_batch(case.state);pages=case.pages
 findings=case.findings(items,pages)
 bad=accept_inventory_response(case.state,json.dumps({'inventory_findings':findings}),pages,requested_items=items)
 fixed_pages=[{**page,'source_sha256':hashlib.sha256((case.source/page['relative_path']).read_bytes()).hexdigest()} for page in pages]
 good=accept_inventory_response(case.state,json.dumps({'inventory_findings':findings}),fixed_pages,requested_items=items)
 out={'platform':'Mac with source write newline=CRLF to reproduce Windows default','raw_has_crlf':b'\r\n' in raw,'raw_sha256':hashlib.sha256(raw).hexdigest(),'fixture_page_sha256':pages[0]['source_sha256'],'catalog_source_sha256':items[0]['source_sha256'],'original_page_matches_index':pages[0]['source_sha256']==items[0]['source_sha256'],'accepted_before':bad,'accepted_with_raw_hash':good,'requested_items':len(items),'runtime_source_validation_unchanged':True,'raw_text_decoded_normalized_lf': '\r\n' not in fixed_pages[0]['source_text'],'fixture_sha256':hashlib.sha256((ROOT/'poc/tests/test_repository_formula_inventory.py').read_bytes()).hexdigest()}
 Path('/private/tmp/cobol-crlf-identity-audit-20261111/result.json').write_text(json.dumps(out,indent=2)+'\n');print(json.dumps(out))
finally:case.doCleanups()
