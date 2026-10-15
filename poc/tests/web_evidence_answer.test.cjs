'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.join(__dirname,'../web');

function harness(locale='zh-CN'){
  const elements=new Map();
  const get=id=>{if(!elements.has(id))elements.set(id,{value:'',addEventListener:()=>{}});return elements.get(id);};
  const context=vm.createContext({document:{documentElement:{},createTreeWalker:()=>({nextNode:()=>false}),
    querySelectorAll:()=>[],querySelector:()=>null,getElementById:get,addEventListener:()=>{}},
    NodeFilter:{SHOW_TEXT:4},localStorage:{getItem:()=>locale,setItem:()=>{}},setTimeout,clearTimeout});
  for(const file of ['i18n.js','marked.umd.js','markdown.js','app.js'])vm.runInContext(
    fs.readFileSync(path.join(root,file),'utf8').replace(/render\(\);initialize\(\);\s*$/,''),context);
  return code=>vm.runInContext(code,context);
}
const diagnostic={schema_version:'safe-api-error/v1',category:'question_time_budget',evidence_source:'local',
  request_id:'local-'+'a'.repeat(32),request_id_source:'local'};
const legacy='本次调查已达到时间预算，已停止继续补查。 查看已保存的证据和待查事项；远程请求结果未知时，先确认请求状态，再决定是否重试。 本地关联编号：'+diagnostic.request_id+'。\n\n对话和源码索引已保留。\n\n诊断：QUESTION_TIME_BUDGET_EXHAUSTED；阶段：provider_request。';
test('source answer progress does not advertise a countdown to discarding an active request',()=>{
  for(const locale of ['zh-CN','zh-HK','en']){
    const run=harness(locale);
    const label=run(`questionBudgetLabel({p:{phase:'waiting_for_model'},questionRemaining:0,questionBudget:120})`);
    assert.ok(label.length>0);
    assert.doesNotMatch(label,/剩余|剩餘|上限|時限|时限|time left|time limit/i);
  }
});
function message(content,extra={}){return {id:'answer-1',role:'assistant',content,status:'completed',analysis_status:'PARTIAL',
  diagnostics:[{stage:'provider_request',diagnostic}],...extra};}

test('retained source facts remain visible while the model failure is collapsed',()=>{
  for(const locale of ['zh-CN','zh-HK','en']){
    const run=harness(locale);const item=message('OUT-AMOUNT = IN-AMOUNT * 2。[ev_local]',{
      answer_origin:'local_source_facts',answer_available:true,model_answer_recorded:false,
      evidence_refs:[{evidence_id:'ev_local',relative_path:'rule.cbl',start_line:4,end_line:4}]});
    const html=run(`conversationMessage(${JSON.stringify(item)})`);
    assert.match(html,/<details class="narrative-notes request-failure retained-answer">/);
    assert.doesNotMatch(html,/<details class="narrative-notes request-failure retained-answer" open/);
    assert.match(html,/OUT-AMOUNT = IN-AMOUNT \* 2/);assert.match(html,/data-turn-evidence="ev_local"/);
    assert.equal(run(`diagnosticOnlyAnswer(${JSON.stringify(item)})`),false);
  }
});

test('old diagnostic-only history renders one error explanation and retains original content',()=>{
  const run=harness();const item=message(legacy,{analysis_status:'ABSTAINED'});
  const html=run(`conversationMessage(${JSON.stringify(item)})`);
  assert.equal(run(`diagnosticOnlyAnswer(${JSON.stringify(item)})`),true);
  assert.equal((html.match(/本次调查已达到时间预算，已停止继续补查。/g)||[]).length,2,
    'one visible reason plus the intentionally collapsed safe JSON; no repeated answer body');
  assert.match(html,/<div class="message-content narrative-body"><\/div>/);
  assert.doesNotMatch(html,/诊断：QUESTION_TIME_BUDGET_EXHAUSTED；阶段：provider_request。/);
  assert.equal(item.content,legacy);
});

test('an additional business sentence is never hidden by old diagnostic recognition',()=>{
  const run=harness();const text=legacy+'\n\nOUT-AMOUNT 来源是直接赋值。';
  const item=message(text,{model_answer_recorded:false});
  assert.equal(run(`diagnosticOnlyAnswer(${JSON.stringify(item)})`),false);
  assert.match(run(`conversationMessage(${JSON.stringify(item)})`),/OUT-AMOUNT 来源是直接赋值。/);
});

test('an explicit diagnostic origin does not duplicate error text as an answer',()=>{
  const run=harness();const item=message('Safe request status only.',{answer_origin:'diagnostic',model_answer_recorded:false});
  const html=run(`conversationMessage(${JSON.stringify(item)})`);
  assert.doesNotMatch(html,/Safe request status only/);assert.match(html,/request-failure/);
});

test('a received model draft always remains intact even when it contains a diagnostic',()=>{
  const run=harness();const item=message(legacy,{answer_origin:'model',model_answer_recorded:true});
  const html=run(`conversationMessage(${JSON.stringify(item)})`);
  assert.equal(run(`diagnosticOnlyAnswer(${JSON.stringify(item)})`),false);
  assert.match(html,/诊断：QUESTION_TIME_BUDGET_EXHAUSTED；阶段：provider_request。/);
  assert.match(html,/retained-answer/);
});

test('diagnostic origin without safe diagnostic metadata still shows its fallback text',()=>{
  const run=harness();const item=message('Request status has been retained.',{answer_origin:'diagnostic',diagnostics:[]});
  assert.equal(run(`diagnosticOnlyAnswer(${JSON.stringify(item)})`),false);
  assert.match(run(`conversationMessage(${JSON.stringify(item)})`),/Request status has been retained/);
});
