import os
from dataclasses import replace
import json,sys,hashlib,subprocess
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[4]
OUT=Path(os.environ["FORMULA_AUDIT_OUTPUT"]) / "local"
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0,str(ROOT/'poc'))
from business_index import build_business_index
from repository_discovery import ensure_repository_search,discover_repository
from business_map import build_business_map
from business_chat import run_business_chat
from business_synthesis import wants_business_overview
from question_investigation import build_question_investigation
from agent_policy import AgentPolicy
from company_api import CompanyAPIConfig,TransportResponse
from run_store import RunStore
Q='列出系统所有计算所有premium formula 的地方'
def program(name,body,data='01 PREMIUM-AMT PIC 9(6)V99 VALUE 0.\n01 BASIS PIC 9(5) VALUE 100.\n01 FACTOR PIC 9V99 VALUE 1.25.'):
 return f'IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\nWORKING-STORAGE SECTION.\n{data}\nPROCEDURE DIVISION.\nMAIN.\n{body}\nGOBACK.\n'
def page_summary(payload):
 return [p for ctx in payload.get('source_context',[]) for p in ctx.get('pages',[])]
def main(name,files,question=Q,instructions=''):
 path=OUT/name;path.mkdir(exist_ok=True);source=path/'source';source.mkdir(exist_ok=True)
 for relative,text in files.items():(source/relative).write_text(text)
 db=path/'index.sqlite';build_business_index(source,db,source_format='free',framework_reference_path='',quiet=True,verify_content=True)
 ensure_repository_search(db,source)
 discovery=discover_repository(db,question,fallback_to_repository=False)
 mapping=build_business_map(db,source,question)
 pages=[{'relative_path':relative,'source_text':text,'start_line':1,'end_line':len(text.splitlines()),
  'evidence_id':'ev_'+hashlib.sha256(relative.encode()).hexdigest()[:24],
  'source_sha256':hashlib.sha256(text.encode()).hexdigest(),'span_truncated':False} for relative,text in files.items()]
 planned=build_question_investigation(question,mapping,database_path=db,source_pages=pages,answer_instructions=instructions,advance_frontier=True)
 requests=[];systems=[]
 def transport(request):
  wire=json.loads(request.body);payload=json.loads(wire['messages'][-1]['content']);requests.append(payload);systems.append(wire['messages'][0]['content'])
  assert len(requests)<17,'synthetic request guard'
  if payload.get('schema')=='business-analysis-compaction/v1':
   task=payload['analysis_compaction']
   entries=[{'kind':r['kind'],'text':r['text'],'evidence_ids':r['evidence_ids'],'covers_entry_ids':[r['entry_id']]} for r in task['previous_entries']]
   entries.append({'kind':'conditions','text':'已读金额计算需结合源文件中的条件和后续调整。','evidence_ids':task['source_ids'],'covers_entry_ids':[]})
   answer=json.dumps({'schema':'analysis-ledger-result/v1','task_id':task['task_id'],'entries':entries,'deferred_source_ids':[]})
  else:
   visible=page_summary(payload)
   phrases=[]
   for p in visible:
    if 'COMPUTE' in p['source_text'] or 'ADD ' in p['source_text']:
     phrases.append(f"{p['relative_path']} 包含保费金额计算：按所示基础金额与系数计算，或继续累加调整，结果保留在对应金额字段中。[{p['evidence_id']}]")
   answer='\n\n'.join(phrases)+ '\n\n以上是本系统全部保费计算位置。'
   if not visible:answer='需要继续搜索保费计算。'
  return TransportResponse(200,json.dumps({'choices':[{'message':{'role':'assistant','content':answer},'finish_reason':'stop'}]},ensure_ascii=False))
 config=CompanyAPIConfig('https://offline.example.invalid/v1','synthetic-model',api_key='synthetic-local-key')
 with patch('socket.create_connection',side_effect=AssertionError('network forbidden')),patch('socket.socket.connect',side_effect=AssertionError('network forbidden')):
  wrapped=run_business_chat(question,db,source,config,transport=transport,allow_network=False,framework_reference_path='',
   policy=AgentPolicy(max_model_requests=8,max_answer_revisions=0),answer_instructions=instructions)
 result=wrapped['agent_result'];saved=RunStore(path/'investigation-runs.sqlite').load(result['run_id'])
 def summarized(inv):
  return {k:inv.get(k) for k in ('scope','state','can_answer','can_answer_locally','candidate_paths','analysis_plan','required_items','open_gaps','pending_work','investigation_complete','investigation_frontier_state','global_coverage')}
 report={'name':name,'question':question,'instructions':instructions,'business_overview':wants_business_overview(question,answer_instructions=instructions),
  'files':sorted(files),'discovery':{k:discovery.get(k) for k in ('selected_paths','matched_file_count','repository_file_count','fallback_all','source_identity','boundaries','search_terms')},
  'mapping':{k:mapping.get(k) for k in ('direct_paths','selected_paths','relations','source_identity','rule_lead_coverage')},
  'all_pages_supplied_planner':summarized(planned),
  'request_count':len(requests),'request_pages':[sorted(set(p['relative_path'] for p in page_summary(r))) for r in requests],
  'request_investigations':[summarized(r.get('question_investigation',{})) for r in requests],
  'request_ledger_generations':[r.get('analysis_ledger',{}).get('generation') for r in requests],
  'result':{k:result.get(k) for k in ('status','stop_reason','answer_available','answer_origin','answer','answer_completion','boundaries')},
  'final_investigation':summarized(result.get('investigation_state',{}).get('question_investigation',{})),
  'public_scope':{k:result.get('investigation',{}).get(k) for k in ('scope_kind','full_repository_semantics_verified','selected_paths','query_expansion_attempted')},
  'saved_frontier':{k:saved['checkpoint'].get(k) for k in ('investigation_frontier_state','source_frontier_state','analysis_compaction_pending_ids')},
  'saved_ledger_generations':len(saved['checkpoint'].get('prior_source_memory',{}).get('analysis_ledger',{}).get('generations',[]))}
 (path/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
 (path/'requests.json').write_text(json.dumps(requests,ensure_ascii=False,indent=2)+'\n')
 (path/'system-messages.json').write_text(json.dumps(systems,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({'name':name,'files':len(files),'selected':discovery['selected_paths'],'overview':report['business_overview'],
  'planner_paths':planned.get('candidate_paths'),'planner_pending':planned.get('pending_work'),'planner_gaps':planned.get('open_gaps'),
  'status':result['status'],'stop_reason':result['stop_reason'],'requests':len(requests),'request_pages':report['request_pages'],
  'final_pending':report['final_investigation'].get('pending_work'),'final_gaps':report['final_investigation'].get('open_gaps')},ensure_ascii=False))
 return report
if __name__=='__main__':
 reports=[]
 files={'a.cbl':program('ENTRY-A','COMPUTE PREMIUM-AMT = BASIS * FACTOR.\nCALL "STEP-B".'),
        'b.cbl':program('STEP-B','COMPUTE CALC-AMT = 10 + 20.','01 CALC-AMT PIC 9(6) VALUE 0.'),
        'c.cbl':program('ENTRY-C','COMPUTE PRM-AMT = BASIS * FACTOR.','01 PRM-AMT PIC 9(6) VALUE 0.\n01 BASIS PIC 9(5) VALUE 100.\n01 FACTOR PIC 9V99 VALUE 1.25.')}
 reports.append(main('disconnected-abbreviation',files))
 reports.append(main('twelve-matched-paths',{f'calc-{n:02}.cbl':program(f'ENTRY-{n:02}',f'COMPUTE PREMIUM-AMT = BASIS * FACTOR + {n}.') for n in range(12)}))
 reports.append(main('thirty-seven-formulas',{'calc.cbl':program('ENTRY-A','\n'.join(f'STEP-{n:02}.\nADD {n+1} TO PREMIUM-AMT.' for n in range(37)))}))
 (OUT/'summary.json').write_text(json.dumps({'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
  'external_model_calls':0,'network_blocked':True,'reports':[str(OUT/r['name']/'report.json') for r in reports]},ensure_ascii=False,indent=2)+'\n')
