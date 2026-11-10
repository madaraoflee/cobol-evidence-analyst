'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.join(__dirname,'../web');

function harness(locale){
  const elements=new Map();
  const get=id=>{if(!elements.has(id))elements.set(id,{value:'',addEventListener:()=>{}});return elements.get(id);};
  const context=vm.createContext({document:{documentElement:{},createTreeWalker:()=>({nextNode:()=>false}),
    querySelectorAll:()=>[],querySelector:()=>null,getElementById:get,addEventListener:()=>{}},
    NodeFilter:{SHOW_TEXT:4},localStorage:{getItem:()=>locale,setItem:()=>{}},setTimeout,clearTimeout});
  for(const file of ['i18n.js','marked.umd.js','markdown.js','app.js'])
    vm.runInContext(fs.readFileSync(path.join(root,file),'utf8').replace(/render\(\);initialize\(\);\s*$/,''),context);
  return code=>vm.runInContext(code,context);
}
const current={schema_version:'safe-api-error/v1',category:'system_error',evidence_source:'local',request_id:'local-'+'a'.repeat(32)};
const prior={schema_version:'safe-api-error/v1',category:'rate_limit',evidence_source:'provider_code',
  provider_code:'rate_limit_exceeded',http_status:500,request_id:'local-'+'b'.repeat(32),
  reason:'PRIVATE-REASON',raw_response:'PRIVATE-RESPONSE',api_key:'PRIVATE-KEY'};

test('current and reopened failures show the earlier API cause separately in all languages',()=>{
  const labels={'zh-CN':'此前失败','zh-HK':'此前失敗',en:'Earlier failure'};
  for(const locale of Object.keys(labels)){
    const run=harness(locale),subject={diagnostic:current,prior_diagnostic:prior};
    const safe=run(`safeFailureDiagnostics(${JSON.stringify(subject)})`);
    assert.deepEqual(Array.from(safe,item=>item.category),['rate_limit','system_error']);
    assert.equal(safe[0].prior_failure,true);assert.equal(safe[0].http_status,500);
    assert.equal(safe[1].prior_failure,undefined);
    const view=run(`failureDiagnosticsView(${JSON.stringify(subject)})`);
    assert.ok(view.includes(labels[locale]));assert.match(view,/HTTP 500/);
    assert.doesNotMatch(view,/PRIVATE-/);
    run(`state.mode='real';applyProject(${JSON.stringify({diagnosis:{question:'Explain flow'},agent:subject})});`);
    assert.deepEqual(Array.from(run('safeFailureDiagnostics()'),item=>item.category),['rate_limit','system_error']);
    assert.ok(run('failureDiagnosticsView()').includes(labels[locale]));
  }
});

test('prior failure projection remains bounded deduplicated and rejects invalid diagnostics',()=>{
  const run=harness('en');
  const duplicated={diagnostic:current,prior_diagnostic:prior,diagnostics:[{diagnostic:prior}]};
  const safe=run(`safeFailureDiagnostics(${JSON.stringify(duplicated)})`);
  assert.equal(safe.length,2);assert.equal(new Set(Array.from(safe,item=>item.request_id)).size,2);
  assert.doesNotMatch(JSON.stringify(safe),/PRIVATE-/);
  const invalid=run(`safeFailureDiagnostics(${JSON.stringify({diagnostic:current,prior_diagnostic:{reason:'PRIVATE',category:'invented'}})})`);
  assert.equal(invalid.length,1);assert.equal(invalid[0].category,'system_error');
});
