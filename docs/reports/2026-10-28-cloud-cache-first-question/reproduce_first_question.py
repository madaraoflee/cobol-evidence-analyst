import importlib.util,json,pathlib,subprocess,sys,tempfile,time,types,sqlite3
ROOT=pathlib.Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT/'poc'))
from business_index import build_business_index
from repository_discovery import ensure_repository_search
from company_api import CompanyAPIConfig,TransportResponse
from agent_policy import AgentPolicy
import business_chat
baseline=types.ModuleType('first_question_baseline');baseline.__file__=str(ROOT/'poc/business_chat.py');sys.modules[baseline.__name__]=baseline
exec(compile(subprocess.check_output(['git','show','4cdbab4665df4d2b6fcbf70b9dccf3f7a286474c:poc/business_chat.py'],cwd=ROOT),baseline.__file__,'exec'),baseline.__dict__)
root=pathlib.Path(tempfile.mkdtemp(prefix='first-question-large-'));source=root/'source';source.mkdir();db=root/'index.sqlite'
for n in range(12200):
 (source/f'unit{n:05}.cbl').write_text(f'IDENTIFICATION DIVISION.\nPROGRAM-ID. NEUTRAL{n:05}.\nPROCEDURE DIVISION.\nGOBACK.\n')
(source/'a-policy.cbl').write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. CHECKORDER.\n*> Order eligibility check\nDATA DIVISION.\nWORKING-STORAGE SECTION.\n01 ORDER-AMOUNT PIC 9(5).\n01 RESULT-CODE PIC XX.\nPROCEDURE DIVISION.\nIF ORDER-AMOUNT > ZERO MOVE '00' TO RESULT-CODE ELSE MOVE '90' TO RESULT-CODE END-IF.\nGOBACK.\n")
(source/'z-policy.cbl').write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. SETTLEMENTCASE.\nDATA DIVISION.\nWORKING-STORAGE SECTION.\n01 BASE-AMOUNT PIC 9(5) VALUE 2.\n01 FINAL-AMOUNT PIC 9(5).\nPROCEDURE DIVISION.\nCOMPUTE FINAL-AMOUNT = BASE-AMOUNT * 3.\nGOBACK.\n")
t0=time.monotonic();build_business_index(source,db,source_format='free',verify_content=True,quiet=True);overview=ensure_repository_search(db,source)
config=CompanyAPIConfig('https://offline.example.invalid/v1','offline-source-probe',api_key='unused')
report={'root':str(root),'source_files':12202,'indexed_files':overview['indexed_files'],'index_seconds':round(time.monotonic()-t0,3),'transport':'local_synthetic_no_network','runs':[]}
for name,question,target,action in [('known_path_read','请解释这份源码实现的业务用途。','a-policy.cbl','read'),('formula_read_gap','请说明业务金额的计算规则。','a-policy.cbl','read'),('cross_language_search','结算金额怎么计算？','z-policy.cbl','search'),('exact_last_file','z-policy.cbl 的业务流程是什么？','z-policy.cbl',None)]:
 for version,module in [('baseline',baseline),('fixed',business_chat)]:
  requests=[]
  def transport(request):
   envelope=json.loads(request.body);p=json.loads(envelope['messages'][-1]['content']);pages=[x for b in p['source_context'] for x in b['pages']]
   requests.append({'retrieval':p['retrieval_status'],'paths':[x['relative_path'] for x in pages],'action_read_completed':any(x.get('range_complete') for x in p['completed_actions']),'source_chars':sum(len(x['source_text']) for x in pages),'request_bytes':len(request.body),'task':p['task']})
   found=next((x for x in pages if x['relative_path']==target),None)
   if action and len(requests)==1:
    answer=json.dumps({'read':[{'relative_path':target,'start_line':1,'end_line':16}]}) if action=='read' else json.dumps({'search':['SETTLEMENTCASE']})
   elif found:
    answer=('金额大于零时设置结果码00，否则设置90。' if target=='a-policy.cbl' else '最终金额按基础金额的三倍计算。')+f"[{found['evidence_id']}]"
   else:answer='当前没有提供相关源码。'
   return TransportResponse(200,json.dumps({'choices':[{'message':{'role':'assistant','content':answer},'finish_reason':'stop'}]},ensure_ascii=False))
  start=time.monotonic();r=module.run_business_chat(question,db,source,config,history=[],transport=transport,framework_reference_path='',policy=AgentPolicy(max_model_requests=2 if action else 1,max_answer_revisions=0,max_investigation_rounds=5))['agent_result']
  row={'case':name,'version':version,'seconds':round(time.monotonic()-start,3),'requests':requests,'status':r['status'],'stop_reason':r['stop_reason'],'model_answer_recorded':r['model_answer_recorded'],'answer':r['answer']};report['runs'].append(row)
  (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');print(json.dumps({k:v for k,v in row.items() if k not in {'requests','answer'}},ensure_ascii=False),flush=True)
print(str(root/'report.json'),flush=True)
