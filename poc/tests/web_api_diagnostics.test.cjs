'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.join(__dirname,'../web');

function harness(locale='en'){
  const events={};const elements=new Map();let fetches=0;
  const get=id=>{if(!elements.has(id))elements.set(id,{value:'',open:false,addEventListener:(event,handler)=>events[id+':'+event]=handler});return elements.get(id);};
  const context=vm.createContext({
    document:{documentElement:{},createTreeWalker:()=>({nextNode:()=>false}),querySelectorAll:()=>[],querySelector:selector=>elements.get(selector),getElementById:get,addEventListener:(event,handler)=>events[event]=handler},
    NodeFilter:{SHOW_TEXT:4},localStorage:{getItem:()=>locale,setItem:()=>{}},
    fetch:()=>{fetches++;throw Error('unexpected request');},setTimeout,clearTimeout,
  });
  vm.runInContext(fs.readFileSync(path.join(root,'i18n.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(root,'marked.umd.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(root,'markdown.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(root,'app.js'),'utf8').replace(/render\(\);initialize\(\);\s*$/,''),context);
  return {run:code=>vm.runInContext(code,context),get,events,fetches:()=>fetches};
}
function setup(h,agent){
  h.run(`state.mode='real';applyProject(${JSON.stringify({diagnosis:{question:'Explain flow',entry_requested:'ENTRY'},agent})});`);
}
function exchange(overrides={}){
  return {sequence:1,phase:'investigation',endpoint:'chat/completions',http_status:200,outcome_code:'RESPONSE_RECEIVED',body_text:JSON.stringify({choices:[{message:{role:'assistant',content:'Readable response'}}]}),body_truncated:false,body_bytes:96,elapsed_ms:5,...overrides};
}
function agent(overrides={}){
  return {runner_status:'SAFE_STOP',reason_code:'AGENT_SAFETY_STOPPED',agent_result:{status:'ABSTAINED',stop_reason:'model_protocol_error',claims:[]},api_diagnostics:{captured:true,exchanges:[exchange()]},...overrides};
}

test('raw API and extracted model content stay literal, untranslated and escaped in every locale',()=>{
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale);const original='來源：分析工作台 <script>alert(1)</script> <img src=x onerror=alert(1)> & "content"';
    setup(h,agent({api_diagnostics:{captured:true,exchanges:[exchange({body_text:JSON.stringify({choices:[{message:{role:'assistant',content:original}}]})})]}}));
    const view=h.run('apiResponseView()');
    assert.match(view,/來源：分析工作台/);assert.match(view,/&lt;script&gt;alert/);assert.match(view,/&lt;img src=x onerror=alert/);assert.doesNotMatch(view,/<script>|<img/);
    assert.equal((view.match(/來源：分析工作台/g)||[]).length,2,'original text appears in extracted content and raw body');
    assert.equal(h.run('result().claims.length'),0);assert.equal(h.fetches(),0);
  }
});

test('unaccepted model text remains inspectable without HTTP capture and never becomes a current result or citation',()=>{
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale);setup(h,{runner_status:'NOT_READY',reason_code:'SOURCE_ANALYSIS_FAILED',agent_result:null,
      unaccepted_response:{reason_code:'SOURCE_ANALYSIS_FAILED',text:'來源：分析工作台 <script>rejected</script> [ev_old]'}});
    const answer=h.run('actualAnswer()');assert.match(answer,/data-tab="api"/);assert.doesNotMatch(answer,/rejected|data-evidence|narrative-body/);
    const diagnostic=h.run('apiResponseView()');assert.match(diagnostic,/來源：分析工作台/);assert.match(diagnostic,/&lt;script&gt;rejected&lt;\/script&gt;/);assert.match(diagnostic,/\[ev_old\]/);
    assert.doesNotMatch(diagnostic,/<script|data-evidence|data-framework-reference/);assert.equal(h.run('currentRefs().length'),0);
    if(locale==='en'){assert.match(diagnostic,/Unaccepted model response/);assert.match(diagnostic,/cannot support current business findings/);assert.doesNotMatch(diagnostic,/Model explanation available|The Agent workflow completed/);}
    h.run("state.question='Different question'");assert.doesNotMatch(h.run('apiResponseView()'),/rejected|ev_old/);
  }
});

test('a diagnostic-only project retains the API tab when no source diagnosis is available',()=>{
  const h=harness();h.run(`state.mode='real';state.tab='api';state.project={agent:${JSON.stringify({runner_status:'NOT_READY',agent_result:null,unaccepted_response:{reason_code:'SOURCE_SNAPSHOT_MISMATCH',text:'Preserved final explanation.'}})}};`);
  const workbench=h.run('renderWorkbench()');assert.match(workbench,/data-tab="api"/);assert.match(workbench,/Preserved final explanation/);assert.match(workbench,/Unaccepted model response/);assert.doesNotMatch(workbench,/narrative-body/);
});

test('a models response proves HTTP receipt but cannot prove a chat response or a completed investigation',()=>{
  const h=harness();setup(h,agent({runner_status:'NOT_READY',reason_code:'COMPANY_API_NOT_READY',agent_result:null,api_diagnostics:{captured:true,exchanges:[exchange({phase:'capability_probe',endpoint:'models',body_text:'{"data":[]}'})]}}));
  const view=h.run('apiResponseView()');
  assert.match(view,/HTTP response received; chat response not confirmed/);assert.doesNotMatch(view,/Chat response received/);
  assert.match(view,/Capability checks failed/);assert.match(view,/business investigation has not started/);assert.match(view,/HTTP 200/);assert.match(view,/Capability check/);
});

test('successful chat capability checks remain separate from business investigation completion',()=>{
  const h=harness();setup(h,agent({runner_status:'NOT_READY',reason_code:'',agent_result:null,capability_report:{capabilities:{chat:{status:'SUPPORTED'}}},api_diagnostics:{captured:true,exchanges:[exchange({phase:'capability_probe'})]}}));
  const view=h.run('apiResponseView()');assert.match(view,/Chat response received/);assert.match(view,/no business investigation result is available/);assert.match(view,/No business conclusion reached/);assert.doesNotMatch(view,/The Agent workflow completed/);
});

test('a 200 error envelope or invalid JSON is not a confirmed chat response',()=>{
  const h=harness();
  for(const body_text of ['{"error":"unavailable"}','not-json','{"choices":[{"message":{"role":"user","content":"Hello"}}]}','{"choices":[{"message":{"role":"assistant","content":""}}]}']){
    setup(h,agent({api_diagnostics:{captured:true,exchanges:[exchange({body_text})]}}));
    assert.match(h.run('apiSummary()'),/HTTP response received; chat response not confirmed/);
  }
});

test('assistant tool calls confirm chat receipt without inventing extracted text or business evidence',()=>{
  const h=harness();setup(h,agent({api_diagnostics:{captured:true,exchanges:[exchange({body_text:JSON.stringify({choices:[{message:{role:'assistant',content:null,tool_calls:[{id:'call-1',type:'function',function:{name:'search_code',arguments:'{}'}}]}}]})})]}}));
  const view=h.run('apiResponseView()');assert.match(view,/Chat response received/);assert.match(view,/No message.content was extracted/);assert.match(view,/search_code/);assert.match(view,/Workflow interrupted; no business conclusion reached/);
});

test('safe stops outrank ABSTAINED and distinguish protocol, request and limit failures',()=>{
  const h=harness();
  for(const [reason,explanation] of [['model_protocol_error',/Agent action contract/],['invalid_final_answer',/answer contract/],['model_client_error',/request or response processing failed/],['model_turn_budget_exhausted',/model-turn limit/],['tool_execution_error',/local investigation tool failed/]]){
    setup(h,agent({agent_result:{status:'ABSTAINED',stop_reason:reason,claims:[]}}));
    const answer=h.run('actualAnswer()');assert.match(answer,/>Analysis interrupted</);assert.match(answer,explanation);assert.match(answer,new RegExp(reason.toUpperCase()));assert.match(answer,/data-tab="api"/);assert.doesNotMatch(answer,/Insufficient evidence/);
  }
  setup(h,agent({stop_detail:{reason:'tool_budget_exhausted',code:'TOOL_BUDGET_EXHAUSTED'},agent_result:{status:'ABSTAINED',claims:[]}}));
  assert.match(h.run('actualAnswer()'),/tool-call limit/);
  setup(h,agent({agent_result:{status:'ABSTAINED',stop_reason:'unexpected_<script>',claims:[]}}));
  assert.doesNotMatch(h.run('actualAnswer()'),/<script>/);assert.match(h.run('actualAnswer()'),/UNEXPECTED_&lt;SCRIPT&gt;/);
});

test('ordinary abstention remains an evidence outcome and normal completion keeps its verified claims',()=>{
  const h=harness();setup(h,agent({runner_status:'COMPLETED',reason_code:'AGENT_RUN_COMPLETED',agent_result:{status:'ABSTAINED',stop_reason:'model_abstained',claims:[]}}));
  assert.doesNotMatch(h.run('actualAnswer()'),/>Analysis interrupted</);assert.match(h.run('actualAnswer()'),/Insufficient evidence/);
  setup(h,agent({runner_status:'COMPLETED',reason_code:'AGENT_RUN_COMPLETED',agent_result:{status:'PARTIAL',stop_reason:'completed',claims:[{claim:'A source-backed claim',kind:'code_fact',support_status:'supported'}]}}));
  assert.match(h.run('actualAnswer()'),/A source-backed claim/);assert.match(h.run('apiSummary()'),/The Agent workflow completed/);assert.doesNotMatch(h.run('actualAnswer()'),/Readable response/);
});

test('transport errors, truncated payloads and missing diagnostics remain inspectable without false success',()=>{
  const h=harness();setup(h,agent({api_diagnostics:{captured:true,exchanges:[exchange({http_status:null,outcome_code:'NETWORK_ERROR',body_text:'',body_bytes:0}),exchange({sequence:2,http_status:503,outcome_code:'HTTP_ERROR',body_text:'Service <b>unavailable</b>'}),exchange({sequence:3,body_text:'{"choices": [',body_truncated:true})]}}));
  const view=h.run('apiResponseView()');assert.match(view,/No HTTP response received/);assert.match(view,/HTTP 503/);assert.match(view,/NETWORK_ERROR/);assert.match(view,/Service &lt;b&gt;unavailable&lt;\/b&gt;/);assert.match(view,/response was truncated/);assert.doesNotMatch(view,/Chat response received/);
  setup(h,agent({api_diagnostics:undefined}));assert.match(h.run('apiResponseView()'),/did not save raw API responses/);
  setup(h,agent({api_diagnostics:{captured:true,exchanges:[]}}));assert.match(h.run('apiResponseView()'),/No API requests were made/);
  setup(h,undefined);assert.match(h.run('apiResponseView()'),/No model analysis has been started/);
});

test('safe error details stay collapsed and available without raw response capture',()=>{
  for(const [locale,label] of [['en','Error details'],['zh-CN','错误详情'],['zh-HK','錯誤詳情']]){
    const h=harness(locale);
    setup(h,agent({api_diagnostics:undefined,agent_result:{status:'ABSTAINED',diagnostics:[
      {code:'HTTP_ERROR',stage:'provider_request',http_status:500,message:'PRIVATE-MARKER'},
      {code:'TRANSPORT_ERROR',stage:'provider_request',transport_reason:'connection_reset'},
      {code:'MODEL_TEXT_EMPTY',stage:'response_parse',response_shape:{choice_count:1,finish_reason:'length',content_shape:'null',reasoning_present:true,tool_calls_present:false,content:'PRIVATE-MARKER'}},
      {code:'PRIVATE-MARKER',stage:'<script>PRIVATE-MARKER</script>',http_status:'500',transport_reason:'__proto__',response_shape:{choice_count:-1,finish_reason:'PRIVATE-MARKER',content_shape:'PRIVATE-MARKER',reasoning_present:'PRIVATE-MARKER'}},
    ]}}));
    const view=h.run('apiResponseView()');
    assert.match(view,new RegExp(`<details class="api-error-details"><summary>${label}</summary>`));
    assert.match(view,/HTTP_ERROR/);assert.match(view,/provider_request/);assert.match(view,/http_status&quot;: 500/);
    assert.match(view,/connection_reset/);assert.match(view,/MODEL_TEXT_EMPTY/);assert.match(view,/response_parse/);
    assert.match(view,/finish_reason&quot;: &quot;length/);assert.match(view,/reasoning_present&quot;: true/);
    assert.match(view,/UNKNOWN_ERROR/);
    assert.doesNotMatch(view,/<details class="api-error-details"[^>]*\bopen|PRIVATE-MARKER|__proto__|<script/);
    assert.equal(h.fetches(),0);
    h.run("state.question='A different question'");assert.doesNotMatch(h.run('apiResponseView()'),/HTTP_ERROR|connection_reset|api-error-details/);
  }
});

test('new questions and changed entries hide old API data even when the previous run never started its Agent',()=>{
  const h=harness();setup(h,agent({runner_status:'NOT_READY',reason_code:'COMPANY_API_NOT_READY',agent_result:null}));
  assert.match(h.run('apiResponseView()'),/Readable response/);
  h.run("state.question='A new question'");assert.match(h.run('apiResponseView()'),/No API response for the new question/);assert.doesNotMatch(h.run('apiResponseView()'),/Readable response|HTTP 200/);
  h.run("state.question='Explain flow';state.entry='OTHER'");assert.doesNotMatch(h.run('apiResponseView()'),/Readable response|HTTP 200/);
  h.run("state.entry='ENTRY';state.tab='api'");const content=h.get('.answer-content');
  h.events.input({target:{id:'question-input',value:'Another new question'}});assert.match(content.innerHTML,/No API response for the new question/);assert.doesNotMatch(content.innerHTML,/Readable response/);
});

test('API tab is real-mode only and all authored diagnostic messages translate into English',()=>{
  const h=harness();assert.doesNotMatch(h.run('answerTabs()'),/data-tab="api"/);assert.equal(h.run('apiResponseView()'),'');
  setup(h,agent());h.run("state.tab='api'");assert.match(h.run('answerTabs()'),/data-tab="api"[^>]+aria-selected="true"/);assert.match(h.run('renderWorkbench()'),/Raw response body/);assert.doesNotMatch(h.run('apiResponseView()'),/[\u3400-\u9fff]/);
  for(const reason of ['model_protocol_error','invalid_final_answer','model_client_error','tool_budget_exhausted','model_turn_budget_exhausted','no_progress','model_output_budget_exceeded','unauthorized_tool','invalid_tool_arguments','evidence_phase_closed','tool_execution_error','tool_contract_mismatch','tool_status_invalid','snapshot_missing','snapshot_mismatch','snapshot_integrity_error','tool_policy_denied','tool_capability_unavailable','evidence_scope_violation','invalid_evidence_reference','unsupported_claim_content','unsupported_claim','claim_checker_error','unknown']){
    assert.doesNotMatch(h.run(`stopExplanation('${reason}')`),/[\u3400-\u9fff]/,reason);
  }
  h.run("selectInterfaceLocale('zh-CN')");assert.match(h.run('actualAnswer()'),/分析流程中断/);h.run("selectInterfaceLocale('zh-HK')");assert.match(h.run('actualAnswer()'),/分析流程中斷/);
});

test('business investigation responses appear first and each phase keeps newest requests first with original sequence numbers',()=>{
  const h=harness();const exchanges=[exchange({sequence:1,phase:'capability_probe',body_text:'PROBE-FIRST'}),exchange({sequence:2,phase:'capability_probe',body_text:'PROBE-LAST'}),exchange({sequence:3,body_text:'INVESTIGATION-FIRST'}),exchange({sequence:4,body_text:'INVESTIGATION-LAST'})];
  setup(h,agent({api_diagnostics:{captured:true,exchanges}}));
  const view=h.run('apiResponseView()');assert.match(view,/newest requests first/);
  assert.ok(view.indexOf('INVESTIGATION-LAST')<view.indexOf('INVESTIGATION-FIRST'));assert.ok(view.indexOf('INVESTIGATION-FIRST')<view.indexOf('PROBE-LAST'));assert.ok(view.indexOf('PROBE-LAST')<view.indexOf('PROBE-FIRST'));
  assert.match(view,/<h3>4 · Business investigation<\/h3>/);assert.match(view,/<h3>2 · Capability check<\/h3>/);
  assert.equal(h.run('state.project.agent.api_diagnostics.exchanges[0].sequence'),1,'render does not reorder persisted diagnostics');
});

test('omitted response reasons and diagnostic storage limits are explicit in all locales',()=>{
  const h=harness();
  for(const [reason,expected] of [['HTTP_ERROR_BODY_NOT_COLLECTED',/did not collect the error body/],['NO_RESPONSE',/No response was received/],['RESPONSE_BODY_INVALID',/body was invalid/],['TRANSPORT_RESPONSE_INVALID',/transport response was invalid/]]){
    setup(h,agent({api_diagnostics:{captured:true,truncated:true,omitted_exchange_count:3,exchanges:[exchange({body_text:'',body_omitted_reason:reason})]}}));
    const view=h.run('apiResponseView()');assert.match(view,expected);assert.match(view,/diagnostic storage limit was reached/);assert.match(view,/Omitted request records:3/);assert.doesNotMatch(view,/[\u3400-\u9fff]/);
  }
  h.run("selectInterfaceLocale('zh-CN')");assert.match(h.run('apiResponseView()'),/未保存的请求记录：3/);
  h.run("selectInterfaceLocale('zh-HK')");assert.match(h.run('apiResponseView()'),/未保存的請求記錄：3/);
});

test('post-response source validation failures preserve the API response without accepting the business result',()=>{
  const h=harness();setup(h,agent({runner_status:'NOT_READY',reason_code:'SOURCE_ANALYSIS_FAILED',agent_result:null}));
  const view=h.run('apiResponseView()');assert.match(view,/Readable response/);assert.match(view,/business result was not accepted/);assert.match(view,/No business conclusion reached/);assert.doesNotMatch(view,/The Agent workflow completed/);
  h.run("state.question='Changed question'");assert.doesNotMatch(h.run('apiResponseView()'),/Readable response/);
});
