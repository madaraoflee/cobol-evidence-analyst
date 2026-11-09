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
  const context=vm.createContext({
    document:{documentElement:{},createTreeWalker:()=>({nextNode:()=>false}),querySelectorAll:()=>[],querySelector:()=>null,getElementById:get,addEventListener:()=>{}},
    NodeFilter:{SHOW_TEXT:4},localStorage:{getItem:()=>locale,setItem:()=>{}},setTimeout,clearTimeout,
  });
  for(const file of ['i18n.js','marked.umd.js','markdown.js','app.js'])
    vm.runInContext(fs.readFileSync(path.join(root,file),'utf8').replace(/render\(\);initialize\(\);\s*$/,''),context);
  return code=>vm.runInContext(code,context);
}

test('legacy TLS classification states an unconfirmed phase and preserves unknown request outcome in every language',()=>{
  const expected={'zh-CN':/具体阶段尚未确认/,'zh-HK':/具體階段尚未確認/,en:/failing phase is unconfirmed/};
  const failure={stage:'provider_request',diagnostic:{schema_version:'safe-api-error/v1',
    category:'connection',evidence_source:'local',request_id:'local-'+'a'.repeat(32),
    transport_reason:'tls_handshake_failed'},request_context:{request_outcome:'unknown'}};
  for(const locale of Object.keys(expected)){
    const run=harness(locale);
    const safe=run(`safeFailureDiagnostics({diagnostics:[${JSON.stringify(failure)}]})`)[0];
    assert.equal(safe.transport_reason,'tls_handshake_failed');
    assert.equal(safe.request_context.request_outcome,'unknown');
    assert.match(safe.reason,expected[locale]);
    assert.doesNotMatch(safe.reason,/无法建立安全连接|無法建立安全連線|could not be established|Could not establish/);
    const html=run(`failureDiagnosticsView({diagnostics:[${JSON.stringify(failure)}]})`);
    assert.match(html,expected[locale]);
  }
});
