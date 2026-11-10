import os
import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from audit_local import main,program,Q,OUT
from answer_markdown import DEFAULT_ANSWER_INSTRUCTIONS
files={'a.cbl':program('ENTRY-A','COMPUTE PREMIUM-AMT = BASIS * FACTOR.\nCALL "STEP-B".'),
 'b.cbl':program('STEP-B','COMPUTE CALC-AMT = 10 + 20.','01 CALC-AMT PIC 9(6) VALUE 0.'),
 'c.cbl':program('ENTRY-C','*> 此项为保费金额。\nCOMPUTE PRM-AMT = BASIS * FACTOR.',
  '01 PRM-AMT PIC 9(6) VALUE 0.\n01 BASIS PIC 9(5) VALUE 100.\n01 FACTOR PIC 9V99 VALUE 1.25.')}
reports=[]
for name,instructions in [('disconnected-chinese-premium', ''),('disconnected-chinese-premium-saved-default',DEFAULT_ANSWER_INSTRUCTIONS)]:
 report=main(name,files,instructions=instructions);reports.append(report)
 truth={'source_truth':{'path':'c.cbl','comment':'此项为保费金额。','expression':'COMPUTE PRM-AMT = BASIS * FACTOR.',
   'business_meaning':'The Chinese source comment explicitly labels this amount as insurance premium; not inferred solely from PRM abbreviation.'},
  'undiscovered': 'c.cbl' not in report['discovery']['selected_paths'],
  'not_supplied_in_any_request': all('c.cbl' not in paths for paths in report['request_pages']),
  'query_expansion_attempted':report['public_scope']['query_expansion_attempted'],
  'stop_reason':report['result']['stop_reason'],'status':report['result']['status'],
  'global_claim_retained':'以上是本系统全部保费计算位置。' in report['result']['answer']}
 (OUT/name/'truth-and-coverage.json').write_text(json.dumps(truth,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps(truth,ensure_ascii=False))
