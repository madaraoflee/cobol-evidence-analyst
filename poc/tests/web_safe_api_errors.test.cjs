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
  const context=vm.createContext({
    document:{documentElement:{},createTreeWalker:()=>({nextNode:()=>false}),querySelectorAll:()=>[],querySelector:()=>null,getElementById:get,addEventListener:()=>{}},
    NodeFilter:{SHOW_TEXT:4},localStorage:{getItem:()=>locale,setItem:()=>{}},setTimeout,clearTimeout,
  });
  for(const file of ['i18n.js','marked.umd.js','markdown.js','app.js']){
    vm.runInContext(fs.readFileSync(path.join(root,file),'utf8').replace(/render\(\);initialize\(\);\s*$/,''),context);
  }
  return code=>vm.runInContext(code,context);
}
function diagnostic(overrides={}){
  return {schema_version:'safe-api-error/v1',category:'quota_exhausted',evidence_source:'provider_code',http_status:429,
    request_id:'local-'+'a'.repeat(32),request_id_source:'local',upstream_request_id:'req-'+'b'.repeat(16),upstream_request_id_source:'header',...overrides};
}

test('safe diagnostics rebuild fixed text, validate metadata and exclude private values from export',()=>{
  const run=harness();const privateValue='PRIVATE-CREDENTIAL-SOURCE-MARKER';
  const failure=diagnostic({reason:privateValue,next_step:privateValue,provider_code:privateValue,retry_after_seconds:privateValue,
    raw_body:privateValue,url:privateValue,request_id_source:'upstream'});
  run(`state.mode='real';state.project={agent:{agent_result:{diagnostic_summary:{schema_version:'business-answer-diagnostics/v1',api_failures:[{stage:'continuation',...${JSON.stringify(failure)}}]}}}};`);
  const safe=run('diagnosticSummaryData()');
  assert.equal(safe.api_failures[0].category,'quota_exhausted');assert.equal(safe.api_failures[0].request_id_source,'local');
  assert.equal(safe.api_failures[0].stage,'continuation');assert.equal(safe.api_failures[0].upstream_request_id,failure.upstream_request_id);
  assert.doesNotMatch(JSON.stringify(safe),new RegExp(privateValue));
  for(const key of ['raw_body','provider_code','retry_after_seconds','url'])assert.equal(Object.hasOwn(safe.api_failures[0],key),false);
  assert.match(run('failureDiagnosticsView()'),/账号配额或累计使用额度不足/);
  assert.doesNotMatch(run('failureDiagnosticsView()'),new RegExp(privateValue));
  assert.equal(run(`safeApiDiagnostic(${JSON.stringify(diagnostic({category:'constructor'}))})`),null);
  assert.equal(run(`safeApiDiagnostic(${JSON.stringify(diagnostic({category:['quota_exhausted']}))})`),null);
  assert.equal(run(`safeApiDiagnostic(${JSON.stringify(diagnostic({category:{toString:'quota_exhausted'}}))})`),null);
  assert.equal(run(`safeApiDiagnostic(${JSON.stringify(diagnostic({request_id:privateValue}))})`),null);
});

test('partial conversation answers show cause, next step and correlation IDs without altering received content',()=>{
  const run=harness();const text='Retained business explanation <script>literal</script>.';
  const message={id:'message-one',role:'assistant',status:'completed',analysis_status:'PARTIAL',content:text,
    diagnostics:[{stage:'continuation',diagnostic:diagnostic()}]};
  const view=run(`conversationMessage(${JSON.stringify(message)})`);
  assert.match(view,/request-failure/);assert.match(view,/下一步/);assert.match(view,/本地关联编号/);assert.match(view,/上游关联编号/);
  assert.match(view,/Retained business explanation &lt;script&gt;literal&lt;\/script&gt;/);assert.doesNotMatch(view,/<script>/);
  assert.ok(view.indexOf('request-failure')<view.indexOf('message-content'),'failure explanation stays outside the answer body');
  assert.match(view,/查看安全错误详情/);
  run(`state.mode='real';state.project={agent:{runner_status:'NOT_READY',diagnostic:${JSON.stringify(diagnostic({category:'timeout',evidence_source:'local'}))}}};`);
  assert.match(run('actualAnswer()'),/请求超过等待时间/);assert.match(run('actualAnswer()'),/本机处理/);
});

test('HTML or malformed local responses retain HTTP status and never reflect response text',async()=>{
  const run=harness();const privateValue='PRIVATE-HTML-ERROR-BODY';
  for(const status of [502,200]){
    run(`fetch=async()=>({ok:${status===200},status:${status},json:async()=>{throw Error(${JSON.stringify(privateValue)});}});`);
    const result=await run("api('/api/model-check').then(()=>({ok:true}),error=>({message:error.message,status:error.status,diagnostic:error.diagnostic}))");
    assert.equal(result.status,status);assert.equal(result.diagnostic.category,status===502?'system_error':'invalid_response');
    assert.equal(result.diagnostic.evidence_source,'local');assert.match(result.message,/本机处理/);assert.match(result.message,new RegExp('HTTP '+status));
    assert.match(result.diagnostic.request_id,/^local-[a-f0-9]{32}$/);assert.match(result.message,/由浏览器产生/);assert.doesNotMatch(JSON.stringify(result),new RegExp(privateValue));
  }
  run(`fetch=async()=>{throw Error(${JSON.stringify(privateValue)});};`);
  const failure=await run("api('/api/state').catch(error=>({message:error.message,diagnostic:error.diagnostic}))");
  assert.equal(failure.diagnostic.category,'connection');assert.doesNotMatch(JSON.stringify(failure),new RegExp(privateValue));
});

test('model connection feedback and local errors ignore arbitrary JSON messages and preserve safe categories',async()=>{
  const run=harness();const privateValue='PRIVATE-PROVIDER-MESSAGE';const safe=diagnostic({category:'context_too_large',reason:privateValue,next_step:privateValue});
  run(`fetch=async()=>({ok:false,status:413,json:async()=>({error:{code:${JSON.stringify(privateValue)},message:${JSON.stringify(privateValue)},diagnostic:${JSON.stringify(safe)}}})});`);
  const error=await run("api('/api/model-check').catch(error=>({message:error.message,status:error.status,diagnostic:error.diagnostic}))");
  assert.equal(error.status,413);assert.match(error.message,/模型上下文限制/);assert.doesNotMatch(JSON.stringify(error),new RegExp(privateValue));
  assert.match(run(`modelCheckMessage({usable:false,code:'HTTP_ERROR',diagnostic:${JSON.stringify(safe)}})`),/模型上下文限制/);
  assert.doesNotMatch(run(`apiErrorText({code:${JSON.stringify(privateValue)},message:${JSON.stringify(privateValue)}})`),new RegExp(privateValue));
  assert.match(run("apiBodyOmission('ERROR_RESPONSE_BODY_OMITTED')"),/错误正文已省略/);
});

test('safe failure templates translate and preserve classified IDs in every locale',()=>{
  for(const locale of ['en','zh-CN','zh-HK']){
    const run=harness(locale);
    for(const category of ['request_too_large','context_too_large','model_unavailable','unsupported_feature','output_limit','quota_exhausted','rate_limit','authentication','permission','timeout','connection','http_429_unknown','http_5xx_unknown','request_rejected','invalid_response','system_error']){
      const view=run(`failureDiagnosticsView({diagnostic:${JSON.stringify(diagnostic({category}))}})`);
      assert.match(view,/local-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/);assert.match(view,/req-bbbbbbbbbbbbbbbb/);
      if(locale==='en')assert.doesNotMatch(view,/[\u3400-\u9fff]/,category);
    }
  }
});

test('bare model-check HTTP statuses do not invent quota, rate-limit or request-format causes',()=>{
  for(const locale of ['en','zh-CN','zh-HK']){
    const run=harness(locale);
    const unknown429=run("modelCheckMessage({usable:false,http_status:429,code:'HTTP_ERROR'})");
    assert.match(unknown429,locale==='en'?/without a confirmed cause/:/未提供可確認的原因|未提供可确认的原因/);
    assert.doesNotMatch(unknown429,/is rate limiting|明確報告請求速率受限|明确报告请求速率受限/);
    for(const status of [400,404]){
      const rejected=run(`modelCheckMessage({usable:false,http_status:${status},code:'HTTP_ERROR'})`);
      assert.match(rejected,locale==='en'?/cause is unconfirmed/:/具體原因尚未確認|具体原因尚未确认/);
      assert.doesNotMatch(rejected,/request or endpoint path is invalid|請求格式或接口路徑不正確|请求格式或接口路径不正确/);
    }
  }
});


test('typed transport reasons are rebuilt and correlation IDs stay in the single folded panel',()=>{
  const reasons={dns_resolution_failed:/找不到接口地址/,tls_certificate_invalid:/证书/,tls_handshake_failed:/安全连接/,connection_refused:/拒绝连接/,connection_reset:/连接中断/,network_unreachable:/接口网络/,timeout:/超时/};
  const run=harness();
  for(const [reason,pattern] of Object.entries(reasons)){
    const value=diagnostic({category:reason==='timeout'?'timeout':'connection',evidence_source:'local',http_status:undefined,transport_reason:reason,reason:'PRIVATE-REFLECTION'});
    const safe=run(`safeApiDiagnostic(${JSON.stringify(value)})`);
    assert.equal(safe.transport_reason,reason);assert.match(safe.reason,pattern);
    assert.doesNotMatch(JSON.stringify(safe),/PRIVATE-REFLECTION/);
    const view=run(`failureDiagnosticsView({diagnostic:${JSON.stringify(value)}})`);
    assert.equal((view.match(/<details>/g)||[]).length,1);
    assert.ok(view.indexOf('<details>')<view.indexOf(value.request_id));
  }
  for(const transport_reason of ['PRIVATE-REFLECTION','constructor',['timeout'],{timeout:true}]){
    const safe=run(`safeApiDiagnostic(${JSON.stringify(diagnostic({category:'connection',evidence_source:'local',transport_reason}))})`);
    assert.equal(Object.hasOwn(safe,'transport_reason'),false);
  }
  const remote=run(`safeApiDiagnostic(${JSON.stringify(diagnostic({transport_reason:'dns_resolution_failed'}))})`);
  assert.equal(Object.hasOwn(remote,'transport_reason'),false);assert.equal(remote.category,'quota_exhausted');
});
