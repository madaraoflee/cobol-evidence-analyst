'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {frameworkDemoFixture}=require('./framework_demo_fixture.cjs');
const root=path.join(__dirname,'../web');
function harness(saved='zh-HK',storageBlocked=false){
  const events={};let fetches=0;const storage=new Map([['cobol-lens.locale',saved]]);
  const elements=new Map();
  const get=id=>{if(!elements.has(id))elements.set(id,{value:'',open:false,addEventListener:(event,handler)=>events[id+':'+event]=handler});return elements.get(id);};
  const context=vm.createContext({
    document:{documentElement:{},createTreeWalker:()=>({nextNode:()=>false}),querySelectorAll:()=>[],getElementById:get,addEventListener:()=>{}},
    NodeFilter:{SHOW_TEXT:4},localStorage:{getItem:key=>{if(storageBlocked)throw Error('denied');return storage.get(key);},setItem:(key,value)=>{if(storageBlocked)throw Error('denied');storage.set(key,value);}},
    fetch:()=>{fetches++;throw Error('unexpected request');},setTimeout,clearTimeout
  });
  vm.runInContext(fs.readFileSync(path.join(root,'i18n.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(root,'marked.umd.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(root,'markdown.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(root,'app.js'),'utf8').replace(/render\(\);initialize\(\);\s*$/,''),context);
  return {run:code=>vm.runInContext(code,context),change:locale=>events['language-select:change']({target:{value:locale}}),storage,get,fetches:()=>fetches,
    fixture(){vm.runInContext(`state.mode='demo';preview.catalog=${JSON.stringify(frameworkDemoFixture())};selectDemoCase('online',false);state.demoLoading=false;`,context);},
  };
}
test('all three locale choices persist; unsupported preferences and denied storage are safe',()=>{
  const h=harness('unknown');assert.equal(h.run('currentLocale'),'zh-HK');
  for(const locale of ['en','zh-CN','zh-HK']){assert.equal(h.run(`selectInterfaceLocale('${locale}')`),true);assert.equal(h.storage.get('cobol-lens.locale'),locale);}
  assert.equal(h.run("selectInterfaceLocale('invalid')"),false);
  const blocked=harness('en',true);assert.equal(blocked.run("selectInterfaceLocale('en')"),true);
  assert.equal(harness('en').run('currentLocale'),'en');
});
test('authored interface changes language while interpolated source and model text remain byte-for-byte intact',()=>{
  const h=harness('en');
  assert.equal(h.run("t('分析工作台')"),'Workbench');
  assert.equal(h.run("ui`來源： ${'來源：分析工作台 <&> 原始源碼'}`"),'Source: 來源：分析工作台 <&> 原始源碼');
  h.run("selectInterfaceLocale('zh-CN')");assert.equal(h.run("t('讓業務邏輯，清晰可見。')"),'让业务逻辑，清晰可见。');
});
test('framework guide pages contain no untranslated Chinese in English',()=>{
  const h=harness('en');h.fixture();
  for(const fn of ['renderWorkbench','renderSources','renderHistory','relationsView','traceView','markdownSummary']){
    assert.doesNotMatch(h.run(`${fn}()`),/[\u3400-\u9fff]/,fn);
  }
});
test('switching language retains local drafts, source scope, answers and pending jobs without network calls',()=>{
  const h=harness();
  h.run("render=()=>{}; state.mode='real';state.question='請解釋申請狀態轉換';state.entry='ENTRY-PROGRAM';state.project={source:'D:/源碼',agent:{agent_result:{answer:'保留原文'}}};state.evidence={source_text:'來源：分析工作台',relative_path:'D:/源碼/程式.cbl'};state.selectedEvidence='actual-evidence';state.busy=true;state.jobId='pending-job';");
  const before=h.run('JSON.stringify(state)');h.change('en');
  assert.equal(h.run('JSON.stringify(state)'),before);assert.equal(h.fetches(),0);
});
test('case suggestions follow the selected locale and real questions remain editable separately',()=>{
  const h=harness();h.fixture();h.run('render=()=>{}');h.change('en');
  assert.equal(h.run('state.question'),'Explain Service request intake.');
  h.change('zh-CN');assert.equal(h.run('state.question'),'请解释服务申请受理。');
  assert.doesNotMatch(h.run('questionCard()'),/<textarea|id="question-input"/);
});
test('progress uses real counters, handles unknown totals, and translates all stages',()=>{
  const h=harness('en');h.run("state.mode='real';state.busy=true;state.jobId='job';rememberProgress({phase:'catalog',completed:250,total:1000,unit:'files',elapsed_seconds:12,eta_seconds:36,bytes_completed:1024,eta_scope:'current_phase'});");
  assert.match(h.run('progressPanel()'),/25\.0%/);assert.match(h.run('progressPanel()'),/250 \/ 1,000 files/);
  assert.match(h.run('progressPanel()'),/750 files/);assert.match(h.run('progressPanel()'),/00:36/);
  for(const phase of ['preparing','discovery','catalog','scope','discovering','reading','parsing','writing','indexing','expanding_copy','resolving_relations','binding_calls','removing','finalizing','verifying','framework','investigating','repository_search','discovering_business','select_scope','planning_search']){
    h.run(`state.progress.phase='${phase}'`);assert.doesNotMatch(h.run('progressPanel()'),/[\u3400-\u9fff]/,phase);
  }
  h.run("rememberProgress({phase:'parsing',completed:0,total:1,unit:'files',file_completed:512,file_total:80000,eta_scope:'current_file',eta_seconds:30});");
  assert.match(h.run('progressPanel()'),/512 \/ 80,000 lines/);assert.match(h.run('progressPanel()'),/left for this file/);
  h.run("rememberProgress({phase:'discovery',completed:100,total:null,unit:'files'});");
  assert.equal(h.run('progressViewModel().known'),false);assert.doesNotMatch(h.run('progressPanel()'),/<progress[^>]*value=/);assert.match(h.run('progressPanel()'),/Estimating/);
  h.run("state.cancelRequested=true");assert.match(h.run('progressPanel()'),/id="cancel-job"[^>]*disabled/);
});
test('large catalogs page and search without expanding all entries; selected path stays stable',()=>{
  const h=harness('en');h.run("state.mode='real';state.project={programs:Array.from({length:12000},(_,i)=>({program_name:'PROGRAM-'+i,relative_path:'folder/member-'+i+'.cbl',entry_key:'folder/member-'+i+'.cbl',name_origin:'program_id'})),diagnosis:{catalog_ready:true,catalog_report:{files:{candidate:12000,cached:11999}},build_report:{files:{candidate:1}}}};state.entry='folder/member-11999.cbl';");
  assert.equal((h.run('sourceRows()').match(/data-program=/g)||[]).length,100);
  assert.ok((h.run('entryOptions()').match(/<option/g)||[]).length<=102);assert.match(h.run('entryOptions()'),/member-11999.cbl.*selected/);
  assert.equal(h.run('sourceFiles().candidate'),12000);
  h.run("state.filter='member-11999.cbl';state.entryFilter='member-11999.cbl'");
  assert.equal((h.run('sourceRows()').match(/data-program=/g)||[]).length,1);
  assert.match(h.run('sourcePagination()'),/1–1 \/ 1/);assert.match(h.run('entryOptions()'),/PROGRAM-11999/);
});
test('scoped answers keep catalog navigation and canonical entry identity after a failed entry',()=>{
 const h=harness('en');h.run("state.mode='real';state.project={snapshot_id:'sha256:test',programs:[{entry_key:'member.cbl::PROGRAM::2',program_name:'PROGRAM',relative_path:'member.cbl'}],diagnosis:{runner_status:'BLOCKED',catalog_ready:true,entry_requested:'member.cbl',selected_entry:{entry_key:'member.cbl::PROGRAM::2'},scope:{detail_file_count:1,max_files:24,max_scope_bytes:16777216,total_scope_bytes:500,truncated:true},unresolved_dependencies:[{},{}]}};");
 assert.equal(h.run('sourceUsable()'),true);assert.equal(h.run('selectedEntryValue(diagnosis())'),'member.cbl::PROGRAM::2');
 assert.doesNotMatch(h.run('scopeNotice()'),/[\u3400-\u9fff]/);assert.doesNotMatch(h.run('scopeNotice()'),/unresolved dependencies/);assert.match(h.run('scopeNotice()'),/16.0 MiB/);
});
test('configuration errors explain local setup in three languages without echoing service values',()=>{
 const h=harness('en');
 for(const code of ['BASE_URL_MISSING','CHAT_MODEL_MISSING','API_KEY_MISSING','BASE_URL_INVALID','API_STYLE_UNSUPPORTED','ENV_FILE_INVALID','ENV_FILE_UNREADABLE','ENV_FILE_TOO_LARGE','CONFIGURATION_INVALID']){
  h.run(`state.apiConfigurationError='${code}'`);
  assert.doesNotMatch(h.run('apiConfigurationMessage()'),/[\u3400-\u9fff]/,code);
  assert.ok(h.run('apiConfigurationMessage().length')>20);
 }
 h.run("state.apiConfigurationError='ENV_FILE_INVALID';selectInterfaceLocale('zh-CN')");
 assert.match(h.run('apiConfigurationMessage()'),/格式无效/);
 h.run("selectInterfaceLocale('zh-HK')");assert.match(h.run('apiConfigurationMessage()'),/格式無效/);
 h.run("state.apiConfigurationError='secret-response-canary'");
 assert.doesNotMatch(h.run('apiConfigurationMessage()'),/secret-response-canary/);
 h.run('state.apiConfigurationError=null');assert.equal(h.run('apiConfigurationMessage()'),'');
});
test('framework references distinguish matching from runtime verification and escape private content',()=>{
 const h=harness('en');h.run(`state.mode='real';state.frameworkKnowledge={status:'LOADED',document:{title:'Local reference',sha256:'sha256:abc',section_count:5}};
 state.project={diagnosis:{framework_context:{status:'MATCHED',document:{title:'Private <img src=x>',sha256:'sha256:abc',section_count:5},references:[{reference_id:'ref-1',heading:'<script>chapter</script>',page:3,start_line:10,end_line:15,text:'CALL "EXAMPLE" <script>alert(1)</script> 業務原文'}],source_matches:[{evidence_id:'source-1',relative_path:'programs/<main>.cbl',start_line:8,end_line:9,reference_ids:['ref-1']}],coverage:{truncated:true}}}};`);
 const rendered=h.run('frameworkKnowledgeCard()');assert.match(rendered,/Relevant framework references found/);assert.match(rendered,/does not verify runtime/);
 assert.match(rendered,/Page 3 · L10–15/);assert.match(rendered,/data-evidence="source-1"/);assert.match(rendered,/1 source matches/);assert.match(rendered,/reached its limit/);
 assert.match(rendered,/&lt;script&gt;alert/);assert.match(rendered,/&lt;img src=x&gt;/);assert.match(rendered,/programs\/&lt;main&gt;\.cbl/);assert.doesNotMatch(rendered,/<script>|<img/);assert.match(rendered,/業務原文/);
 h.run("selectInterfaceLocale('zh-CN')");assert.match(h.run('frameworkKnowledgeCard()'),/框架知识/);
 h.run("selectInterfaceLocale('zh-HK')");assert.match(h.run('frameworkKnowledgeCard()'),/框架知識/);
 h.run("state.mode='demo'");assert.equal(h.run('frameworkKnowledgeCard()'),'');assert.equal(h.fetches(),0);
});
test('framework cards hide stale source matches after edits, document changes or unavailable configuration',()=>{
 const h=harness('en');h.run(`state.mode='real';state.question='Explain flow';state.entry='ENTRY';state.frameworkKnowledge={status:'LOADED',document:{sha256:'old'}};
 state.project={diagnosis:{question:'Explain flow',entry_requested:'ENTRY',framework_context:{status:'MATCHED',document:{sha256:'old'},references:[{reference_id:'ref-1',heading:'Flow',start_line:1,end_line:5,text:'REFERENCE-CONTENT'}],source_matches:[{evidence_id:'ev-1',reference_ids:['ref-1']}]}}};`);
 assert.match(h.run('frameworkKnowledgeCard()'),/REFERENCE-CONTENT/);
 h.run("state.question='New question'");assert.doesNotMatch(h.run('frameworkKnowledgeCard()'),/REFERENCE-CONTENT|data-evidence/);
 h.run("state.question='Explain flow';state.frameworkKnowledge.document.sha256='new'");assert.match(h.run('frameworkKnowledgeCard()'),/reference document has changed/);assert.doesNotMatch(h.run('frameworkKnowledgeCard()'),/REFERENCE-CONTENT/);
 h.run("state.frameworkKnowledge={status:'UNAVAILABLE',reason_code:'DO-NOT-ECHO'}");const unavailable=h.run('frameworkKnowledgeCard()');assert.match(unavailable,/reference unavailable/);assert.match(unavailable,/FRAMEWORK_REFERENCE_PATH/);assert.doesNotMatch(unavailable,/REFERENCE-CONTENT|DO-NOT-ECHO|Relevant framework references found/);
});
test('framework claim citations and exported provenance retain both source and document references',()=>{
 const h=harness('en');h.run(`state.mode='real';state.readingStrategy='focused';state.question='Explain flow';state.entry='ENTRY';const context={status:'MATCHED',document:{title:'Local reference',sha256:'sha256:example'},references:[{reference_id:'ref-1',heading:'Flow',page:2,start_line:3,end_line:7,text:'BACKGROUND'}]};
 state.project={source:'source',diagnosis:{question:'Explain flow',entry_requested:'ENTRY',framework_context:context},agent:{agent_result:{status:'PARTIAL',answer:'Conditional explanation',framework_context:context,claims:[{kind:'framework_interpretation',claim:'The visible call may follow the documented sequence.',support_status:'citation_verified_only',evidence_ids:['source-1'],framework_reference_ids:['ref-1']}],evidence_refs:[{evidence_id:'source-1',relative_path:'entry.cbl',start_line:3,end_line:4}]}}};`);
 const answer=h.run('actualAnswer()');assert.match(answer,/data-framework-reference="ref-1"/);assert.match(answer,/href="#framework-reference-ref-1"/);assert.match(answer,/Conditional framework interpretation/);assert.match(answer,/data-evidence="source-1"/);assert.doesNotMatch(answer,/[\u3400-\u9fff]/);
 const exported=h.run('markdownSummary()');assert.match(exported,/Framework knowledge/);assert.match(exported,/sha256:example/);assert.match(exported,/ref-1 · Flow · Page 2 · L3–7/);assert.match(exported,/\[source-1\] \[ref-1\]/);assert.match(exported,/does not verify runtime/);
});
