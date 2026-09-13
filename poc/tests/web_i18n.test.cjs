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
