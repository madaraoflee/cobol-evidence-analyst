import os
import json,sys,sqlite3
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
from audit_local import ROOT,OUT,Q,page_summary
from business_chat import run_business_chat
from company_api import CompanyAPIConfig,TransportResponse
from agent_policy import AgentPolicy
from run_store import RunStore
path=OUT/'twelve-matched-paths';db=path/'index.sqlite';store=RunStore(path/'investigation-runs.sqlite')
with sqlite3.connect(path/'investigation-runs.sqlite') as conn:
 run=conn.execute('SELECT run_id FROM investigation_runs ORDER BY created_at LIMIT 1').fetchone()[0]
original=store.load(run);initial=json.loads((path/'report.json').read_text())
requests=[]
def transport(request):
 payload=json.loads(json.loads(request.body)['messages'][-1]['content']);requests.append(payload)
 assert len(requests)<=8
 visible=page_summary(payload)
 answer='\n'.join(f"{p['relative_path']} 按基础金额乘系数并加上本项固定调整额得到保费金额。[{p['evidence_id']}]" for p in visible)
 return TransportResponse(200,json.dumps({'choices':[{'message':{'role':'assistant','content':answer},'finish_reason':'stop'}]},ensure_ascii=False))
config=CompanyAPIConfig('https://offline.example.invalid/v1','synthetic-model',api_key='synthetic-local-key')
with patch('socket.create_connection',side_effect=AssertionError('network forbidden')),patch('socket.socket.connect',side_effect=AssertionError('network forbidden')):
 result=run_business_chat(Q,db,path/'source',config,transport=transport,allow_network=False,framework_reference_path='',
  policy=AgentPolicy(max_model_requests=8,max_answer_revisions=0),resume_run_id=run,continue_investigation=True)['agent_result']
saved=store.load(result['run_id'])
next_paths=sorted({p['relative_path'] for r in requests for p in page_summary(r)})
prior_paths=sorted({p for row in initial['request_pages'] for p in row})
report={'original_run_id':run,'continued_run_id':result['run_id'],'model_requests':len(requests),
 'initial_request_paths':prior_paths,'continued_request_paths':next_paths,'new_paths':sorted(set(next_paths)-set(prior_paths)),
 'status':result['status'],'stop_reason':result['stop_reason'],'gaps':result['investigation_state']['question_investigation']['open_gaps'],
 'pending_work':result['investigation_state']['question_investigation'].get('pending_work'),
 'saved_frontier':saved['checkpoint'].get('investigation_frontier_state'),'previous_unchanged':store.load(run)==original}
(path/'continuation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
(path/'continuation-requests.json').write_text(json.dumps(requests,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(report,ensure_ascii=False,indent=2))
