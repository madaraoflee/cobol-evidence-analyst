import json,sys,hashlib
from pathlib import Path
from unittest.mock import patch
ROOT=Path('/private/tmp/cobol-global-formula-audit-tree-20261110')
sys.path[:0]=[str(ROOT/'poc'),str(ROOT/'poc/tests')]
from test_repository_inventory_store import PagedInventoryOrchestrationTests
from business_chat import run_business_chat
from company_api import TransportResponse
from agent_policy import AgentPolicy
case=PagedInventoryOrchestrationTests();case.setUp()
try:
 for value,label in [({'query':''},'invalid'),({'query':'PREMIUM','cursor':0},'repeat-empty')]:
  calls=[]
  def transport(request):
   payload=json.loads(json.loads(request.body)['messages'][-1]['content']);calls.append({'items':[i['candidate_id'] for i in payload.get('repository_inventory',{}).get('items',[])], 'lookup_result':payload.get('repository_inventory',{}).get('lookup_result'), 'stage_schema':payload.get('schema')})
   if len(calls)==12:return TransportResponse(500,json.dumps({'error':{'message':'Synthetic safety stop after twelve repeated lookups'}}))
   return TransportResponse(200,json.dumps({'choices':[{'message':{'role':'assistant','content':json.dumps({'inventory_lookup':value})},'finish_reason':'stop'}]}))
  result=run_business_chat('列出系统所有计算所有premium formula 的地方',case.fixture.database,case.fixture.source,case.fixture.config,transport=transport,allow_network=False,framework_reference_path='',conversation_id='probe-'+label,policy=AgentPolicy())['agent_result']
  out={'probe':label,'calls':len(calls),'last_result':result.get('status'),'stop_reason':result.get('stop_reason'),'unique_item_batches':len({json.dumps(x['items']) for x in calls}),'pending':result.get('repository_inventory',{}).get('counts',{}).get('pending'),'safety_stop_injected_at_call':12,'calls_details':calls,'runtime_sha256':hashlib.sha256((ROOT/'poc/business_chat.py').read_bytes()).hexdigest()}
  (Path('/private/tmp/cobol-termination-audit-20261111')/('lookup-'+label+'.json')).write_text(json.dumps(out,indent=2)); print(json.dumps({k:v for k,v in out.items() if k!='calls_details'}))
finally:case.doCleanups()
