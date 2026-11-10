from pathlib import Path
import argparse, json, sys
parser=argparse.ArgumentParser(description="Reproduce the open first-failed-turn handoff boundary offline.")
parser.add_argument("--output", required=True, type=Path)
args=parser.parse_args()
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'poc/tests'), str(ROOT/'poc')]
import test_conversation_analysis_handoff as fixtures
import test_completion_driven_chat as transport_fixture
from company_api import TransportResponse
from web_app import WorkbenchState
case=fixtures.ConversationAnalysisHandoffTests(); case.setUp()
try:
 f=case.fixture
 f.question='root.cbl 新的第五及第六项抵扣条件如何影响前面规则？'
 question=f.question; job='first-failed-progress'
 case.store.append(case.conversation['id'], 'user', question, run_id=job, status='pending')
 ordinary=[]
 def respond(payload, number):
  if payload.get('schema')=='business-analysis-compaction/v1': return case.case.compact_reply(payload)
  ordinary.append(payload)
  if len(ordinary)<=4: return f.read_helper(len(ordinary))
  return 'SYNTHETIC_HTTP_FAILURE'
 def response(status, body):
  if status==200 and json.loads(body)['choices'][0]['message']['content']=='SYNTHETIC_HTTP_FAILURE':
   return TransportResponse(500, json.dumps({'error':{'code':'internal_error'}}))
  return TransportResponse(status, body)
 with patch.object(transport_fixture, 'TransportResponse', side_effect=response):
  failed=f.ask(respond, policy=case.case.policy, request_guard=7, conversation_id=case.conversation['id'])
 saved=case.case.saved(failed)
 app=WorkbenchState(state_path=f.root/'settings.json', config_provider=lambda:f.config)
 app.conversation_store=case.store; app.conversation=case.store.get(case.conversation['id'])
 app._record_answer({'conversation_id':case.conversation['id'], 'question':question, 'conversation_history':[]}, job,
  {'agent':{'runner_status':'COMPLETED', 'agent_result':failed}})
 continuation_payloads=[]
 def continue_response(payload, number):
  continuation_payloads.append(payload); return f.cited_answer(payload)
 continued=f.ask(continue_response, policy=replace(case.case.policy, max_model_requests=1),
  request_guard=len(f.requests)+1, conversation_id=case.conversation['id'], history=[],
  continue_investigation=True, resume_run_id=failed['run_id'])
 f.question='root.cbl 前面的抵扣条件还需要什么前提？'
 history=case.store.model_history(case.conversation['id'], max_characters=80, question=f.question, include_analysis_handoff=True)
 case.store.append(case.conversation['id'], 'user', f.question, run_id='second-after-first-failure', status='pending')
 payloads=[]
 def next_response(payload, number):
  payloads.append(payload); return f.cited_answer(payload)
 following=f.ask(next_response, policy=replace(case.case.policy, max_model_requests=1), request_guard=len(f.requests)+1,
  conversation_id=case.conversation['id'], history=history)
 newer=case.case.saved(following)
 report={'external_model_calls':0, 'socket_network_blocked':True,
  'failed_answer_available':failed['answer_available'], 'failed_answer_origin':failed['answer_origin'],
  'failed_stop_reason':failed['stop_reason'], 'failed_status':case.store.get(case.conversation['id'])['messages'][-2]['status'],
  'failed_uncertain_requests':saved['uncertain_requests'],
  'failed_generations':len(saved['checkpoint']['prior_source_memory']['analysis_ledger']['generations']),
  'history_assistant_count':sum(row['role']=='assistant' for row in history),
  'next_initial_generation':payloads[0].get('analysis_ledger',{}).get('generation',0),
  'next_handoff':newer['checkpoint'].get('conversation_analysis_handoff'),
  'failed_checkpoint_unchanged':case.case.saved(failed)==saved,
  'explicit_continue_answer_available':continued['answer_available'],
  'explicit_continue_generation':continuation_payloads[0].get('analysis_ledger',{}).get('generation',0),
  'explicit_continue_early_condition_preserved':'尚有应收余额' in json.dumps(continuation_payloads[0], ensure_ascii=False)}
 print(json.dumps(report, ensure_ascii=False, indent=2))
 assert report['failed_answer_available'] is False and report['failed_generations']>0
 assert report['explicit_continue_answer_available'] and report['explicit_continue_generation']==2 and report['explicit_continue_early_condition_preserved']
 assert report['failed_uncertain_requests']==[] and report['next_initial_generation']==0 and report['next_handoff'] is None
 args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
 print(json.dumps(report, ensure_ascii=False, indent=2))
finally: case.doCleanups()
