'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const web=path.join(__dirname,'../web');

function harness(locale='en',preference=null){
  const elements=new Map(),events={},requests=[],storage=new Map();
  if(preference)storage.set('cobol-workbench-analysis-display',preference);
  const get=id=>{
    if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',hidden:false,value:'',disabled:false,
      querySelector:()=>null,setAttribute:()=>{},removeAttribute:()=>{},classList:{toggle:()=>{}},
      addEventListener:(event,handler)=>events[id+':'+event]=handler});
    return elements.get(id);
  };
  const context=vm.createContext({document:{getElementById:get,querySelectorAll:()=>[],querySelector:()=>null,
    addEventListener:(event,handler)=>events[event]=handler,createTreeWalker:()=>({nextNode:()=>false}),
    documentElement:{classList:{toggle:()=>{}}}},NodeFilter:{SHOW_TEXT:4},
    localStorage:{getItem:key=>key==='cobol-workbench-analysis-display'?storage.get(key):locale,setItem:(key,value)=>storage.set(key,value)},
    fetch:async(...args)=>{requests.push(args);throw Error('Unexpected network request');},
    setTimeout:()=>1,clearTimeout:()=>{},setInterval:()=>1,clearInterval:()=>{}});
  for(const name of ['i18n.js','marked.umd.js','markdown.js','app.js'])vm.runInContext(
    fs.readFileSync(path.join(web,name),'utf8').replace(/render\(\);initialize\(\);\s*$/,''),context);
  return {run:code=>vm.runInContext(code,context),get,events,requests,storage};
}
function snapshot(revision=3){
  return {schema_version:'business-investigation-progress/v1',revision,round:4,phase:'investigate',
    summary:'Checking the remaining balance condition.',counts:{candidate:2,open:1,conflicts:0,total_sources:8},
    findings:[{id:'entry-1',kind:'conditions',text:'A candidate condition needs review.',evidence_count:2},
      {id:'entry-2',kind:'open_questions',text:'Check the later override.',evidence_count:0}],omitted_findings:0,
    compaction:{status:'completed',cycle:1,before_bytes:8000,after_bytes:5000,retained_entries:3,
      deferred_sources:2,structural_preservation_verified:true}};
}
const publish=(h,value,extra={})=>h.run(`rememberProgress(${JSON.stringify({phase:'answering',analysis_state:value,...extra})});updateLiveAnalysis();`);

test('snapshot renders candidate records and structural preservation boundaries in every locale',()=>{
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale),value=snapshot();
    value.findings.push({id:'entry-3',kind:'conflicts',text:'Compare <img src=x onerror=alert(1)>.',evidence_count:1});
    value.omitted_findings=7;
    publish(h,value,{analysis_update:{summary:'Stale legacy conclusion',completed_items:3,total_items:3}});
    const html=h.get('conversation-analysis-live').innerHTML;
    assert.match(html,/A candidate condition needs review/);
    assert.match(html,/Check the later override/);
    assert.match(html,/&lt;img src=x onerror=alert\(1\)&gt;/);
    assert.doesNotMatch(html,/<img|Stale legacy conclusion/);
    assert.match(html,locale==='en'?/Stage findings \(provisional\)/:locale==='zh-CN'?/阶段发现（待核）/:/階段發現（待核）/);
    assert.match(html,locale==='en'?/does not confirm complete semantic preservation/:locale==='zh-CN'?/不代表完整语义已确认/:/不代表完整語義已確認/);
    assert.match(html,locale==='en'?/Records not displayed here.*7/:locale==='zh-CN'?/此处未显示的记录.*7/:/此處未顯示的記錄.*7/);
    assert.match(html,/<details class="analysis-state-details">/);
    assert.doesNotMatch(html,/<details class="analysis-state-details" open/);
  }
});

test('progress only is a local persistent preference and does not stop or resubmit investigation',()=>{
  const h=harness();publish(h,snapshot(),{answer_preview:{text:'Draft business answer',round:4,stage:'draft'}});
  assert.match(h.get('conversation-analysis-live').innerHTML,/Draft business answer/);
  h.events.change({target:{id:'analysis-display-mode',value:'progress'}});
  assert.equal(h.get('conversation-analysis-live').innerHTML,'');
  assert.equal(h.run('state.progress.analysis_state.revision'),3);
  assert.equal(h.storage.get('cobol-workbench-analysis-display'),'progress');
  assert.match(h.run('conversationProgress()'),/value="progress" selected/);
  h.events.change({target:{id:'analysis-display-mode',value:'summary'}});
  assert.match(h.get('conversation-analysis-live').innerHTML,/Draft business answer/);
  assert.match(h.get('conversation-analysis-live').innerHTML,/A candidate condition needs review/);
  assert.deepEqual(h.requests,[]);
  assert.equal(harness('en','progress').run('state.analysisDisplay'),'progress');
  assert.equal(harness('en','invalid').run('state.analysisDisplay'),'summary');
});

test('latest self-contained state survives phases and rejects stale or malformed snapshots',()=>{
  const h=harness();publish(h,snapshot(5));
  h.run("rememberProgress({phase:'waiting_for_model'});");
  publish(h,{...snapshot(4),summary:'Older state'});
  publish(h,{...snapshot(6),counts:{candidate:true}});
  assert.equal(h.run('state.progress.analysis_state.revision'),5);
  assert.doesNotMatch(h.run('liveAnalysisView()'),/Older state/);
  const updated=snapshot(6);updated.phase='compacting';updated.compaction.status='running';
  updated.compaction.structural_preservation_verified=false;publish(h,updated);
  assert.match(h.run('liveAnalysisView()'),/Structural links are not yet verified/);
  const fresh=harness();publish(fresh,updated);
  assert.equal(fresh.run('liveAnalysisView()'),h.run('liveAnalysisView()'));
  h.run('state.progress=null;');publish(h,snapshot(1));
  assert.equal(h.run('state.progress.analysis_state.revision'),1);
  h.run("state.jobId='next-job';");
  h.run("rememberProgress({phase:'preparing'});");
  assert.equal(h.run('state.progress.analysis_state'),undefined);
  publish(h,snapshot(0));
  assert.equal(h.run('state.progress.analysis_state.revision'),0);
});

test('projection rejects private protocol text and drops arbitrary raw fields',()=>{
  const h=harness(),value=snapshot();
  value.reasoning_content='private-reasoning';value.findings[0].reasoning_content='private-reasoning';
  value.findings[0].source_text='private-source';value.findings[0].status='confirmed';
  value.compaction.semantic_lossless=true;value.provider_response={text:'private-envelope'};
  publish(h,value);
  assert.doesNotMatch(h.run('JSON.stringify(state.progress.analysis_state)'),/private-|confirmed|semantic_lossless/);
  for(const text of ['{"reasoning_content":"private-text"}','<think>private-text</think>','{"read":[{"relative_path":"rule.cbl"}]}','{"backward_slice":[]}']){
    const bad=snapshot();bad.summary=text;
    assert.equal(h.run(`analysisState(${JSON.stringify(bad)})`),null);
    const badFinding=snapshot();badFinding.findings[0].text=text;
    assert.equal(h.run(`analysisState(${JSON.stringify(badFinding)})`),null);
  }
  for(const bad of [{...snapshot(),phase:[]},{...snapshot(),round:0},{...snapshot(),revision:true},
    {...snapshot(),revision:2**53},{...snapshot(),compaction:{...snapshot().compaction,structural_preservation_verified:'true'}}]){
    assert.equal(h.run(`analysisState(${JSON.stringify(bad)})`),null);
  }
});

test('display limits preserve total omission count and do not invent compaction',()=>{
  const h=harness(),value=snapshot();delete value.compaction;
  value.findings=Array.from({length:75},(_,index)=>({id:'entry-'+index,kind:'rules',text:'t'.repeat(2500),evidence_count:1}));
  value.summary='s'.repeat(3000);value.omitted_findings=7;publish(h,value);
  assert.equal(h.run('state.progress.analysis_state.findings.length'),64);
  assert.equal(h.run('state.progress.analysis_state.omitted_findings'),18);
  assert.equal(h.run('state.progress.analysis_state.summary.length'),2000);
  assert.equal(h.run('state.progress.analysis_state.summary.endsWith("…")'),true);
  assert.equal(h.run('state.progress.analysis_state.findings[0].text.length'),2000);
  assert.equal(h.run('state.progress.analysis_state.compaction'),undefined);
  assert.doesNotMatch(h.run('liveAnalysisView()'),/Structural links|Context update completed/);
});

test('actual context compaction failure stage survives safe error projection',()=>{
  const h=harness();
  const failure={stage:'context_compact',request_context:{stage:'context_compact',round:4,request_outcome:'response_received'},
    diagnostic:{schema_version:'safe-api-error/v1',category:'http_5xx_unknown',evidence_source:'http_status',
      request_id:'local-'+'a'.repeat(32),request_id_source:'local',http_status:500}};
  const answer={diagnostics:[failure]};
  assert.equal(h.run(`safeFailureDiagnostics(${JSON.stringify(answer)})[0].stage`),'context_compact');
  assert.match(h.run(`failureDiagnosticsView(${JSON.stringify(answer)})`),/Compacting investigation context/);
});

test('global pending work remains visible when the summary has no open entries',()=>{
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale),value=snapshot();
    value.counts.open=0;value.counts.global_pending=17;value.has_pending_work=true;
    publish(h,value);h.run("rememberProgress({phase:'compacting'});");
    const fresh=harness(locale);publish(fresh,value);
    assert.equal(h.run('liveAnalysisView()'),fresh.run('liveAnalysisView()'));
    const html=h.run('liveAnalysisView()');
    assert.match(html,locale==='en'?/Global pending records 17/:locale==='zh-CN'?/全局待查记录 17/:/全局待查記錄 17/);
    assert.match(html,locale==='en'?/Open questions in summary 0/:locale==='zh-CN'?/摘要中的待查事项 0/:/摘要中的待查事項 0/);
  }
  const h=harness(),unknown=snapshot();
  unknown.counts.global_pending=0;unknown.has_pending_work=true;publish(h,unknown);
  assert.match(h.run('liveAnalysisView()'),/count is not yet known/);
  for(const [count,flag] of [[true,true],[-1,true],[2**53,true],[17,false],[17,'true']]){
    const bad=snapshot();bad.counts.global_pending=count;bad.has_pending_work=flag;
    assert.equal(h.run(`analysisState(${JSON.stringify(bad)})`),null);
  }
  const missing=snapshot();missing.has_pending_work=true;
  assert.equal(h.run(`analysisState(${JSON.stringify(missing)})`),null);
});
