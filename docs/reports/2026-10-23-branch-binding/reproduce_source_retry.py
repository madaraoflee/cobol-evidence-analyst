import json, sys, socket, hashlib
from pathlib import Path
from unittest.mock import patch
sys.path[:0] = [str(Path(__file__).resolve().parents[3] / 'poc/tests'), str(Path(__file__).resolve().parents[3] / 'poc')]
from test_run_store import RunStoreTests
from business_chat import run_business_chat
from business_index import build_business_index
from repository_discovery import ensure_repository_search
from agent_policy import AgentPolicy
from company_api import TransportResponse
from run_store import RunStore
fixture = RunStoreTests()
fixture.setUp()
fixture.setup_chat()
question = 'ENTRYPOINT OUT-AMOUNT 怎么计算？'
calls = []
received = False

def transport(request):
    global received
    payload = json.loads(json.loads(request.body)['messages'][-1]['content'])
    pages = [p for b in payload['source_context'] for p in b.get('pages', [])]
    page = next(p for p in pages if 'COMPUTE OUT-AMOUNT' in p['source_text'])
    factor = 3 if 'IN-AMOUNT * 3' in page['source_text'] else 2
    calls.append(factor)
    received = True
    answer = f"OUT-AMOUNT = IN-AMOUNT * {factor}。[{page['evidence_id']}]"
    return TransportResponse(200, json.dumps({'choices':[{'message':{'content':answer},'finish_reason':'stop'}]}))

def interrupt():
    if received:
        raise RuntimeError('simulated_process_interruption_after_response')

policy = AgentPolicy(max_model_requests=1, max_answer_revisions=0)
opts = dict(policy=policy, framework_reference_path='', transport=transport, conversation_id='same-conversation')
try:
    with patch.object(socket, 'create_connection', side_effect=AssertionError('Network forbidden')), patch.object(socket.socket, 'connect', side_effect=AssertionError('Network forbidden')):
        try:
            run_business_chat(question, fixture.database, fixture.source, fixture.config, check_cancel=interrupt, **opts)
        except RuntimeError as error:
            assert str(error) == 'simulated_process_interruption_after_response'
        store = RunStore(fixture.root / 'investigation-runs.sqlite')
        prior = store.unfinished('sha256:'+hashlib.sha256(question.encode()).hexdigest(), hashlib.sha256(fixture.config.chat_model.encode()).hexdigest())
        print(json.dumps({'before': {'state':prior['state'], 'saved_response_count':len(prior['requests']), 'uncertain_requests':len(prior['uncertain_requests']), 'source_factor':2}},ensure_ascii=False))
        path = fixture.source / 'entry.cbl'
        path.write_text(path.read_text().replace('IN-AMOUNT * 2', 'IN-AMOUNT * 3'))
        build_business_index(fixture.source, fixture.database, source_format='free', verify_content=True)
        ensure_repository_search(fixture.database, fixture.source)
        for attempt in (1,2):
            result = run_business_chat(question, fixture.database, fixture.source, fixture.config, start_new_investigation=True, **opts)
            record = store.load(prior['run_id'])
            print(json.dumps({'retry_attempt':attempt,'reason':result.get('reason_code'),'answer':result['agent_result']['answer'],'same_run':result['agent_result']['run_id']==prior['run_id'],'old_run_state':record['state'],'requests_total':len(calls)},ensure_ascii=False))
        fresh = run_business_chat(question, fixture.database, fixture.source, fixture.config, **{**opts,'conversation_id':'new-conversation'})
        print(json.dumps({'new_conversation':{'answer':fresh['agent_result']['answer'],'new_run':fresh['agent_result']['run_id']!=prior['run_id'],'requests_total':len(calls),'factor_per_request':calls}},ensure_ascii=False))
finally:
    fixture.doCleanups()
