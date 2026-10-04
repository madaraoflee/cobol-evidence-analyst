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
const LOCAL_SOURCE_FAILURES={
  storage_full:{'zh-CN':'本机存储空间不足。','zh-HK':'本機儲存空間不足。',en:'Local storage is full.'},
  storage_permission:{'zh-CN':'无法写入本机分析文件。','zh-HK':'無法寫入本機分析文件。',en:'Could not write local analysis files.'},
  storage_locked:{'zh-CN':'本机数据库或文件被占用。','zh-HK':'本機資料庫或文件被佔用。',en:'The local database or a file is in use.'},
  storage_corrupt:{'zh-CN':'本机分析数据库损坏或格式无效。','zh-HK':'本機分析資料庫損壞或格式無效。',en:'The local analysis database is corrupt or has an invalid format.'},
  report_invalid:{'zh-CN':'本机分析报告无法读取或格式无效。','zh-HK':'本機分析報告無法讀取或格式無效。',en:'The local analysis report could not be read or has an invalid format.'},
  source_snapshot_mismatch:{'zh-CN':'本次导入的程序清单与索引快照不一致。','zh-HK':'本次匯入的程式清單與索引快照不一致。',en:'The imported program catalog does not match the index snapshot.'},
  source_encoding_invalid:{'zh-CN':'本次导入遇到无法按所选编码读取的文件。','zh-HK':'本次匯入遇到無法按所選編碼讀取的文件。',en:'An imported file could not be read using the selected encoding.'},
  source_changed:{'zh-CN':'导入期间源码发生了变化，无法建立一致索引。','zh-HK':'匯入期間源碼發生了變化，無法建立一致索引。',en:'The source changed during import, so a consistent index could not be built.'},
  source_empty:{'zh-CN':'所选目录没有符合设置的可用源码。','zh-HK':'所選目錄沒有符合設定的可用源碼。',en:'The selected folder has no usable source matching the settings.'},
  fts_unavailable:{'zh-CN':'当前 Python 的 SQLite 不支持全文索引。','zh-HK':'目前 Python 的 SQLite 不支援全文索引。',en:'SQLite in the current Python installation does not support full-text indexing.'},
};
const LOCAL_SOURCE_NEXT_STEPS={
  storage_full:{'zh-CN':/释放结果目录.*临时目录.*磁盘空间/,'zh-HK':/釋放結果目錄.*暫存目錄.*磁碟空間/,en:/Free disk space.*results.*temporary/},
  storage_permission:{'zh-CN':/结果目录.*配置目录.*写权限/,'zh-HK':/結果目錄.*設定目錄.*寫入權限/,en:/write permissions.*results.*settings/},
  storage_locked:{'zh-CN':/停止其他.*同一结果目录/,'zh-HK':/停止其他.*同一結果目錄/,en:/Stop other.*same results folder/},
  storage_corrupt:{'zh-CN':/保留原结果目录.*新的独立结果目录.*重新建立索引/,'zh-HK':/保留原結果目錄.*新的獨立結果目錄.*重新建立索引/,en:/Keep.*original.*rebuild.*new.*separate/},
  report_invalid:{'zh-CN':/保留原文件.*导入详情.*重新生成/,'zh-HK':/保留原文件.*匯入詳情.*重新生成/,en:/Keep.*original files.*import details.*regenerate/},
  source_snapshot_mismatch:{'zh-CN':/保留原资料库.*重新执行源码更新/,'zh-HK':/保留原資料庫.*重新執行源碼更新/,en:/Keep.*original repository.*source update again/},
  source_encoding_invalid:{'zh-CN':/核对导入进度.*文本源码.*正确编码/,'zh-HK':/核對匯入進度.*文字源碼.*正確編碼/,en:/import progress.*text source.*correct encoding/},
  source_changed:{'zh-CN':/暂停源码编辑或同步.*文件稳定.*重新导入/,'zh-HK':/暫停源碼編輯或同步.*文件穩定.*重新匯入/,en:/Pause source editing or synchronization.*import again.*files are stable/},
  source_empty:{'zh-CN':/源码目录.*扩展名.*无扩展名文件设置/,'zh-HK':/源碼目錄.*副檔名.*無副檔名文件設定/,en:/source folder.*extensions.*files without extensions/},
  fts_unavailable:{'zh-CN':/FTS5.*Python 3\.10.*重新启动工作台/,'zh-HK':/FTS5.*Python 3\.10.*重新啟動工作台/,en:/Python 3\.10 or later.*FTS5 support.*restart the workbench/},
};
const SOURCE_UPDATE_CODES={
  SOURCE_UPDATE_FAILED:{'zh-CN':'源码更新未完成，原资料库和对话已保留。请检查导入详情后重试。','zh-HK':'源碼更新未完成，原資料庫和對話已保留。請檢查匯入詳情後重試。'},
  SOURCE_UPDATE_ROLLBACK_FAILED:{'zh-CN':'源码更新与恢复均未完成。请保留原结果目录，停止使用该目录并检查磁盘空间和写权限。','zh-HK':'源碼更新與恢復均未完成。請保留原結果目錄，停止使用該目錄並檢查磁碟空間和寫入權限。',en:'The source update and recovery did not complete. Keep the original results folder, stop using it and check disk space and write permissions.'},
};

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

test('local source failures rebuild translated fixed text and retain recovery warnings without private fields',()=>{
  const privateValue='PRIVATE-LOCAL-SOURCE-UPDATE';
  for(const locale of ['en','zh-CN','zh-HK']){
    const run=harness(locale);
    for(const [category,messages] of Object.entries(LOCAL_SOURCE_FAILURES)){
      const source=diagnostic({category,evidence_source:'local',http_status:undefined,upstream_request_id:undefined,upstream_request_id_source:undefined});
      const unsafe={...source,reason:privateValue,next_step:privateValue,provider_code:privateValue,retry_after_seconds:privateValue,
        raw:privateValue,raw_body:{message:privateValue},url:privateValue};
      const rebuilt=run(`safeApiDiagnostic(${JSON.stringify(unsafe)})`);
      const canonical=run(`safeApiDiagnostic(${JSON.stringify(source)})`);
      assert.ok(rebuilt,category);assert.equal(rebuilt.category,category);assert.equal(rebuilt.evidence_source,'local');
      assert.equal(rebuilt.reason,messages[locale],category);assert.match(rebuilt.next_step,LOCAL_SOURCE_NEXT_STEPS[category][locale],category);
      assert.equal(JSON.stringify(rebuilt),JSON.stringify(canonical),category);
      assert.doesNotMatch(JSON.stringify(rebuilt),new RegExp(privateValue));
      for(const key of ['raw','raw_body','url','provider_code','retry_after_seconds'])assert.equal(Object.hasOwn(rebuilt,key),false,category);
      const view=run(`failureDiagnosticsView({diagnostic:${JSON.stringify(unsafe)}})`);
      assert.ok(view.includes(messages[locale]),category);assert.ok(view.includes(rebuilt.next_step),category);
      assert.ok(view.includes(locale==='en'?'Local processing':locale==='zh-CN'?'本机处理':'本機處理'),category);
      for(const code of Object.keys(SOURCE_UPDATE_CODES)){
        const text=run(`apiErrorText({code:${JSON.stringify(code)},message:${JSON.stringify(privateValue)},diagnostic:${JSON.stringify(unsafe)}})`);
        const diagnosticText=run(`failureDiagnosticText(${JSON.stringify(source)})`);
        if(code==='SOURCE_UPDATE_ROLLBACK_FAILED'){
          assert.ok(text.startsWith(SOURCE_UPDATE_CODES[code][locale]+' '),`${category}: recovery warning precedes the diagnosis`);
          assert.ok(text.endsWith(diagnosticText),`${category}: safe diagnosis stays after the recovery warning`);
        }else assert.equal(text,diagnosticText,`${category}: ${code}`);
        assert.doesNotMatch(text,new RegExp(privateValue));
        if(locale==='en')assert.doesNotMatch(text,/[\u3400-\u9fff]/,`${category}: ${code}`);
      }
      if(locale==='en')assert.doesNotMatch(JSON.stringify(rebuilt)+view,/[\u3400-\u9fff]/,category);
    }
  }
});

test('safe summary exports retain all local source classifications and local evidence without private response values',()=>{
  const privateValue='PRIVATE-LOCAL-SOURCE-SUMMARY';
  for(const locale of ['en','zh-CN','zh-HK']){
    const run=harness(locale);
    const failures=Object.keys(LOCAL_SOURCE_FAILURES).map((category,index)=>({stage:'repository_search',...diagnostic({category,evidence_source:'local',
      request_id:'local-'+(index+1).toString(16).repeat(32),http_status:undefined,upstream_request_id:undefined,upstream_request_id_source:undefined,
      reason:privateValue,next_step:privateValue,raw:privateValue,raw_body:privateValue,url:privateValue,provider_code:privateValue})}));
    run(`state.mode='real';state.project={agent:{agent_result:{diagnostic_summary:{schema_version:'business-answer-diagnostics/v1',api_failures:${JSON.stringify(failures)}}}}};`);
    const summary=run('diagnosticSummaryData()');
    assert.equal(summary.api_failures.length,failures.length);
    assert.equal(JSON.stringify(summary.api_failures.map(item=>item.category)),JSON.stringify(Object.keys(LOCAL_SOURCE_FAILURES)));
    for(const [index,item] of summary.api_failures.entries()){
      assert.equal(item.evidence_source,'local');assert.equal(item.request_id,failures[index].request_id);assert.equal(item.request_id_source,'local');
      assert.equal(item.stage,'repository_search');assert.equal(item.reason,LOCAL_SOURCE_FAILURES[item.category][locale]);
      for(const key of ['raw','raw_body','url','provider_code'])assert.equal(Object.hasOwn(item,key),false,item.category);
    }
    assert.doesNotMatch(JSON.stringify(summary),new RegExp(privateValue));
    if(locale==='en')assert.doesNotMatch(JSON.stringify(summary),/[\u3400-\u9fff]/);
  }
});

test('source-update fallback codes translate and unknown errors never reflect server codes or messages',()=>{
  const privateValue='PRIVATE-UNCLASSIFIED-SOURCE-ERROR';
  for(const locale of ['en','zh-CN','zh-HK']){
    const run=harness(locale);
    const fallback=run(`apiErrorText({code:${JSON.stringify(privateValue)},message:${JSON.stringify(privateValue)}})`);
    assert.equal(fallback,run("apiErrorText({code:'REQUEST_FAILED'})"));assert.doesNotMatch(fallback,new RegExp(privateValue));
    const unknown=diagnostic({category:privateValue,evidence_source:'local',reason:privateValue,next_step:privateValue,raw:privateValue});
    assert.equal(run(`safeApiDiagnostic(${JSON.stringify(unknown)})`),null);
    assert.equal(run(`apiErrorText({code:${JSON.stringify(privateValue)},message:${JSON.stringify(privateValue)},diagnostic:${JSON.stringify(unknown)}})`),fallback);
    for(const [code,messages] of Object.entries(SOURCE_UPDATE_CODES)){
      const text=run(`apiErrorText({code:${JSON.stringify(code)},message:${JSON.stringify(privateValue)},diagnostic:${JSON.stringify(unknown)}})`);
      if(locale==='en'){
        assert.doesNotMatch(text,/[\u3400-\u9fff]/);assert.match(text,/source update/i);
        assert.doesNotMatch(text,/REQUEST_FAILED/);
      }else assert.equal(text,messages[locale],code);
      assert.doesNotMatch(text,new RegExp(privateValue));
    }
  }
});

test('source-update API failures preserve safe codes and diagnostics without reflecting service error text',async()=>{
  const privateValue='PRIVATE-SOURCE-UPDATE-RESPONSE';
  for(const locale of ['en','zh-CN','zh-HK']){
    const run=harness(locale);
    for(const code of Object.keys(SOURCE_UPDATE_CODES)){
      for(const category of [null,...Object.keys(LOCAL_SOURCE_FAILURES)]){
        const safe=category?diagnostic({category,evidence_source:'local',reason:privateValue,next_step:privateValue,raw:privateValue,
          raw_body:privateValue,provider_code:privateValue,http_status:undefined,upstream_request_id:undefined,upstream_request_id_source:undefined}):null;
        const response={error:{code,message:privateValue,diagnostic:safe,raw:privateValue}};
        run(`fetch=async()=>({ok:false,status:500,json:async()=>(${JSON.stringify(response)})});`);
        const error=await run("api('/api/source-update').catch(error=>({code:error.code,message:error.message,status:error.status,diagnostic:error.diagnostic}))");
        assert.equal(error.code,code);assert.equal(error.status,500);
        assert.equal(error.message,run(`apiErrorText(${JSON.stringify(response.error)})`));
        if(category){
          assert.equal(error.diagnostic.category,category);assert.equal(error.diagnostic.evidence_source,'local');
          assert.equal(error.diagnostic.reason,LOCAL_SOURCE_FAILURES[category][locale]);
        }else assert.equal(error.diagnostic,null);
        assert.doesNotMatch(JSON.stringify(error),new RegExp(privateValue));
        if(locale==='en')assert.doesNotMatch(JSON.stringify(error),/[\u3400-\u9fff]/);
      }
    }
    run(`fetch=async()=>({ok:false,status:500,json:async()=>({error:{code:${JSON.stringify(privateValue)},message:${JSON.stringify(privateValue)},diagnostic:${JSON.stringify(diagnostic({category:privateValue,evidence_source:'local',reason:privateValue,next_step:privateValue,raw:privateValue}))}}})});`);
    const unknown=await run("api('/api/source-update').catch(error=>({code:error.code,message:error.message,diagnostic:error.diagnostic}))");
    assert.equal(unknown.code,'REQUEST_FAILED');assert.equal(unknown.diagnostic,null);
    assert.equal(unknown.message,run("apiErrorText({code:'REQUEST_FAILED'})"));assert.doesNotMatch(JSON.stringify(unknown),new RegExp(privateValue));
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
