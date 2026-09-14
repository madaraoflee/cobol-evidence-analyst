'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
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
  vm.runInContext(fs.readFileSync(path.join(root,'app.js'),'utf8').replace(/render\(\);initialize\(\);\s*$/,''),context);
  return {run:code=>vm.runInContext(code,context),change:locale=>events['language-select:change']({target:{value:locale}}),storage,get,fetches:()=>fetches};
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
test('sample pages contain no untranslated Chinese in English',()=>{
  const h=harness('en');
  for(const fn of ['renderWorkbench','renderSources','renderHistory','relationsView','traceView','markdownSummary']){
    assert.doesNotMatch(h.run(`${fn}()`),/[\u3400-\u9fff]/,fn);
  }
});
test('switching language retains local drafts, source scope, answers and pending jobs without network calls',()=>{
  const h=harness();
  h.run("render=()=>{}; state.mode='real';state.question='請解釋年繳保費';state.entry='ENTRY-PROGRAM';state.project={source:'D:/源碼',agent:{agent_result:{answer:'保留原文'}}};state.evidence={source_text:'來源：分析工作台',relative_path:'D:/源碼/程式.cbl'};state.selectedEvidence='actual-evidence';state.busy=true;state.jobId='pending-job';");
  const before=h.run('JSON.stringify(state)');h.change('en');
  assert.equal(h.run('JSON.stringify(state)'),before);assert.equal(h.fetches(),0);
});
test('default sample question follows locale, but edited sample drafts are preserved',()=>{
  const h=harness();h.run('render=()=>{}');h.change('en');
  assert.equal(h.run('state.question'),'How is the instalment premium calculated?');
  h.run("state.question='自訂問題';");h.change('zh-CN');assert.equal(h.run('state.question'),'自訂問題');
});
test('progress uses real counters, handles unknown totals, and translates all stages',()=>{
  const h=harness('en');h.run("state.mode='real';state.busy=true;state.jobId='job';rememberProgress({phase:'catalog',completed:250,total:1000,unit:'files',elapsed_seconds:12,eta_seconds:36,bytes_completed:1024,eta_scope:'current_phase'});");
  assert.match(h.run('progressPanel()'),/25\.0%/);assert.match(h.run('progressPanel()'),/250 \/ 1,000 files/);
  assert.match(h.run('progressPanel()'),/750 files/);assert.match(h.run('progressPanel()'),/00:36/);
  for(const phase of ['preparing','discovery','catalog','scope','discovering','reading','parsing','writing','indexing','expanding_copy','resolving_relations','binding_calls','removing','finalizing','verifying','investigating']){
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
 assert.doesNotMatch(h.run('scopeNotice()'),/[\u3400-\u9fff]/);assert.match(h.run('scopeNotice()'),/2 unresolved dependencies/);assert.match(h.run('scopeNotice()'),/16.0 MiB/);
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
