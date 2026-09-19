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
  vm.runInContext(fs.readFileSync(path.join(root,'app.js'),'utf8').replace(/render\(\);initialize\(\);\s*$/,''),context);
  return {run:code=>vm.runInContext(code,context),get,events,fetches:()=>fetches};
}
function project(overrides={}){
  return {source:'local-source',output:'analysis-output',snapshot_id:'sha256:source',programs:[{program_name:'ENTRY',relative_path:'main.cbl',entry_key:'ENTRY'}],diagnosis:{question:'Explain flow',entry_requested:'ENTRY',catalog_ready:true,source_options:{max_source_pages:12},unresolved_dependencies:[]},agent:{runner_status:'COMPLETED',reason_code:'AGENT_RUN_COMPLETED',agent_result:{status:'ANALYZED',analysis_mode:'source_reading',answer:'Fallback explanation',narrative:{text:'## Purpose\n\nThe program reads input and returns a calculated result. [ev_page_a]\n\n## Error handling\n\nThe error status is returned to its caller.',verification:'unverified'},claims:[],evidence_refs:[{evidence_id:'ev_page_a',relative_path:'main.cbl',start_line:1,end_line:120,source_hash:'sha256:page'}],framework_context:{references:[{reference_id:'ref-a',heading:'Reference rules',start_line:1,end_line:4}]},reading_coverage:{total_files:1,total_lines:120,total_pages:1,selected_pages:1,selected_lines:120,complete:true},tool_trace:[],model_turns:1,stop_reason:'completed',...overrides}}};
}
function setup(h,overrides={}){h.run(`state.mode='real';applyProject(${JSON.stringify(project(overrides))});`);}

test('narrative results display a long explanation before coverage and source details even with no claims',()=>{
  const h=harness();const text='## Business flow\n\n'+Array.from({length:120},(_,i)=>`Step ${i}: Read, calculate, validate and return the result.`).join('\n\n')+'\n\nLast available paragraph. [ev_page_a]';
  setup(h,{narrative:{text,verification:'unverified'}});
  const answer=h.run('actualAnswer()');assert.match(answer,/<h3>Business flow<\/h3>/);assert.match(answer,/Step 119/);assert.match(answer,/Last available paragraph/);assert.doesNotMatch(answer,/Insufficient evidence|No business answer/);
  assert.ok(answer.indexOf('Last available paragraph')<answer.indexOf('Reading coverage'));assert.ok(answer.indexOf('Last available paragraph')<answer.indexOf('Source pages'));
  assert.match(answer,/Model interpretation · Business review needed/);assert.equal(h.run('result().claims.length'),0);
});

test('model narrative is escaped, remains untranslated and only real source or framework references become links',()=>{
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale);setup(h,{narrative:{text:'# 來源：分析工作台 <img src=x onerror=alert(1)>\n\n<script>alert(1)</script> [ev_page_a] [ev_missing] [ref-a] [ref-forged] [click](javascript:alert(1)) & raw',verification:'unverified'}});
    const answer=h.run('actualAnswer()');assert.match(answer,/來源：分析工作台/);assert.match(answer,/&lt;img/);assert.match(answer,/&lt;script&gt;/);assert.doesNotMatch(answer,/<img|<script|href="javascript:/);
    assert.match(answer,/data-evidence="ev_page_a"/);assert.match(answer,/data-framework-reference="ref-a"/);assert.match(answer,/\[ev_missing\]/);assert.match(answer,/\[ref-forged\]/);assert.doesNotMatch(answer,/data-evidence="ev_missing"|data-framework-reference="ref-forged"/);
    assert.equal(h.fetches(),0);
  }
});

test('source citations continue to use the existing evidence click handler',()=>{
  const h=harness();setup(h);h.run('let openedEvidence=null;loadEvidence=id=>{openedEvidence=id;};');
  h.events.click({target:{closest:()=>({dataset:{evidence:'ev_page_a'},hasAttribute:()=>false,classList:{contains:()=>false}})}});
  assert.equal(h.run('openedEvidence'),'ev_page_a');assert.match(h.run('evidencePanel()'),/main.cbl/);
});

test('reading coverage distinguishes planned, sent and interpreted pages, and partial results survive failed requests',()=>{
  const h=harness();setup(h,{status:'PARTIAL',stop_reason:'model_client_error',reading_coverage:{total_files:2,total_lines:1000,total_pages:10,selected_pages:8,selected_lines:800,planned_pages:8,sent_pages:5,summarized_pages:3,summarized_lines:300,failed_pages:2,complete:false},analysis_scope:{unresolved_dependency_count:2}});
  const answer=h.run('actualAnswer()');assert.match(answer,/The program reads input/);assert.match(answer,/Interpreted 3 \/ 10 pages \(current source scope\)/);assert.match(answer,/Sent 5 pages/);assert.match(answer,/Selected for this run: 8 pages/);assert.match(answer,/300 \/ 1,000 lines/);assert.match(answer,/There are 5 unread pages/);assert.match(answer,/Some analysis requests did not complete/);assert.match(answer,/2 dependencies are missing implementations/);assert.doesNotMatch(answer,/>Analysis interrupted</);
  assert.match(h.run('apiSummary()'),/Model explanation available; business meaning needs review/);assert.doesNotMatch(h.run('apiSummary()'),/no business conclusion reached/);
});

test('complete reading is scoped and never upgrades model interpretation to verified business conclusions',()=>{
  const h=harness();setup(h);const answer=h.run('actualAnswer()');
  assert.match(answer,/Read 1 \/ 1 pages \(current source scope\)/);assert.match(answer,/Complete reading does not verify the business conclusions/);assert.doesNotMatch(answer,/source statements verified/i);
  assert.match(h.run("statusLabel('ANALYZED')"),/Business explanation generated/);assert.match(h.run('apiSummary()'),/business meaning needs review/);
  h.run("state.project.agent.agent_result.status='PARTIAL';state.project.agent.agent_result.stop_reason='output_truncated';state.project.agent.agent_result.reading_coverage={total_pages:1,selected_pages:1,sent_pages:1,summarized_pages:1,complete:false,truncated_response_pages:['ev_page_a']}");
  assert.match(h.run('actualAnswer()'),/Some model responses ended before completion/);
  assert.doesNotMatch(h.run('actualAnswer()'),/Some analysis requests did not complete/);
});

test('an all-failed response with a fallback narrative cannot masquerade as a completed explanation',()=>{
  const h=harness();setup(h,{status:'ABSTAINED',narrative:{text:'No usable explanation was generated.',verification:'unverified'},stop_reason:'model_client_error',model_answer_recorded:false});
  h.run("state.project.agent.runner_status='SAFE_STOP'");const answer=h.run('actualAnswer()');assert.match(answer,/>Analysis interrupted</);assert.doesNotMatch(answer,/narrative-body/);assert.doesNotMatch(h.run('apiSummary()'),/Model explanation available/);
});

test('new drafts hide previous narratives, citations and coverage while changing depth preserves the question',()=>{
  const h=harness();setup(h);h.run('render=()=>{};');const question=h.run('state.question');
  h.events.change({target:{id:'reading-depth',value:'48'}});assert.equal(h.run('state.question'),question);assert.equal(h.run('state.maxSourcePages'),48);assert.equal(h.run('draftChanged()'),true);assert.doesNotMatch(h.run('actualAnswer()'),/The program reads input|data-evidence|Read 1 \/ 1/);
  h.run("state.maxSourcePages=12;state.question='Different question'");assert.doesNotMatch(h.run('actualAnswer()'),/The program reads input|data-evidence/);
});

test('reading depth is restored and sent numerically in analysis requests with real-mode controls only',async()=>{
  const h=harness();assert.doesNotMatch(h.run('questionCard()'),/id="reading-depth"/);
  const p=project();p.diagnosis.source_options.max_source_pages=48;h.run(`state.mode='real';applyProject(${JSON.stringify(p)});state.apiConfigured=true;let submitted=null;startJob=async options=>{submitted=options;};`);
  assert.equal(h.run('state.maxSourcePages'),48);assert.match(h.run('questionCard()'),/value="48" selected/);
  await h.events.submit({target:{id:'question-form'},preventDefault:()=>{}});assert.equal(h.run('submitted.max_source_pages'),48);assert.equal(h.run('submitted.question'),'Explain flow');assert.equal(h.run('submitted.allow_network'),true);
  h.run('state.busy=true');assert.match(h.run('questionCard()'),/id="reading-depth" disabled/);
  p.diagnosis.source_options.max_source_pages=32;h.run(`state.busy=false;applyProject(${JSON.stringify(p)});`);assert.match(h.run('readingDepthControl()'),/Custom · Up to 32 sections/);assert.equal(h.run('draftChanged()'),false);
});

test('markdown export preserves narrative, real citations and actual reading coverage without translation',()=>{
  const h=harness();setup(h,{narrative:{text:'來源：分析工作台\n\nDetailed business explanation [ev_page_a].',verification:'unverified'},reading_coverage:{total_files:1,total_pages:5,selected_pages:4,sent_pages:3,summarized_pages:2,planned_pages:4,total_lines:500,summarized_lines:200,complete:false}});
  const markdown=h.run('markdownSummary()');assert.match(markdown,/來源：分析工作台/);assert.match(markdown,/Detailed business explanation \[ev_page_a\]/);assert.match(markdown,/Interpreted 2 \/ 5 pages/);assert.match(markdown,/Sent 3 pages/);assert.match(markdown,/main.cbl:1–120 · ev_page_a/);assert.match(markdown,/Model interpretation · Business review needed/);assert.doesNotMatch(markdown,/Fallback explanation/);
});

test('new reading phases, page units and authored narrative controls translate in all supported languages',()=>{
  const h=harness();setup(h);
  for(const phase of ['reading_sources','analyzing_pages','synthesizing']){
    h.run(`state.busy=true;rememberProgress({phase:'${phase}',completed:2,total:8,unit:'pages'})`);const progress=h.run('progressPanel()');assert.match(progress,/2 \/ 8 pages/);assert.doesNotMatch(progress,/[\u3400-\u9fff]/);
  }
  h.run('state.busy=false');assert.doesNotMatch(h.run('actualAnswer()'),/[\u3400-\u9fff]/);assert.doesNotMatch(h.run('questionCard()'),/[\u3400-\u9fff]/);
  h.run("selectInterfaceLocale('zh-CN')");assert.match(h.run('actualAnswer()'),/模型解读 · 需业务复核/);assert.match(h.run('questionCard()'),/深入阅读/);
  h.run("selectInterfaceLocale('zh-HK')");assert.match(h.run('actualAnswer()'),/模型解讀 · 需業務覆核/);assert.match(h.run('questionCard()'),/深入閱讀/);
});

test('verified source availability is independent of business status and keeps partial explanations visible',()=>{
  const h=harness();setup(h,{status:'PARTIAL',stop_reason:'model_client_error'});
  h.run("state.project.diagnosis.catalog_ready=false;state.project.diagnosis.source_manifest_verified=true;state.project.diagnosis.runner_status='BLOCKED';state.project.diagnosis.scope={truncated:true};state.project.diagnosis.unresolved_dependencies=Array.from({length:30},()=>({target_name:'EXTERNAL'}));state.project.agent.runner_status='SAFE_STOP';");
  assert.equal(h.run('sourceUsable()'),true);assert.match(h.run('actualAnswer()'),/The program reads input/);assert.match(h.run('actualAnswer()'),/30 dependencies are missing implementations/);assert.doesNotMatch(h.run('actualAnswer()'),/>Analysis interrupted</);
  assert.doesNotMatch(h.run('questionCard()'),/type="submit" disabled/);
  h.run('state.project.snapshot_id=null');assert.equal(h.run('sourceUsable()'),false);
  h.run('state.project.diagnosis.catalog_ready=true');assert.equal(h.run('sourceUsable()'),true);
});

test('call coverage is explicit selected-source coverage, not completed model reading or whole-chain verification',()=>{
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale);setup(h,{status:'PARTIAL',reading_coverage:{total_pages:12,selected_pages:12,sent_pages:5,summarized_pages:4,complete:false,call_chain:{resolved_calls:8,covered_calls:3,uncovered_calls:5,unresolved_calls:2}}});
    const answer=h.run('actualAnswer()');assert.match(answer,/<details class="narrative-boundaries call-chain-coverage"><summary>/);assert.match(answer,/3 \/ 8/);
    if(locale==='en'){
      assert.match(answer,/Calls with related source selected: 3 \/ 8 resolved calls · 5 not covered · Plus 2 calls with unresolved targets/);
      assert.match(answer,/does not establish completed model reading, full call-chain coverage or verified business outcomes/);
      assert.doesNotMatch(answer,/[\u3400-\u9fff]/);assert.match(h.run('markdownSummary()'),/3 \/ 8 resolved calls/);
    }
  }
  const old=harness();setup(old);assert.doesNotMatch(old.run('actualAnswer()'),/call-chain-coverage/);
});

test('full flow is the new default, uses batch sizing, and restores earlier focused runs honestly',async()=>{
  const h=harness();assert.equal(h.run('state.readingStrategy'),'full_chain');
  h.run("state.mode='real';render=()=>{};");
  let control=h.run('readingDepthControl()');assert.match(control,/value="full_chain" selected/);assert.match(control,/12 sections per batch/);assert.doesNotMatch(control,/Up to 12|Up to 48/);assert.match(control,/Batch size does not cap the total/);
  setup(h);assert.equal(h.run('state.readingStrategy'),'focused');assert.equal(h.run('draftChanged()'),false);assert.match(h.run('readingDepthControl()'),/Up to 12 sections/);
  h.events.change({target:{id:'reading-strategy',value:'full_chain'}});assert.equal(h.run('state.question'),'Explain flow');assert.equal(h.run('draftChanged()'),true);assert.doesNotMatch(h.run('actualAnswer()'),/The program reads input/);
  h.run("state.apiConfigured=true;let submitted=null;startJob=async options=>{submitted=options;};");
  await h.events.submit({target:{id:'question-form'},preventDefault:()=>{}});assert.equal(h.run('submitted.reading_strategy'),'full_chain');assert.equal(h.run('submitted.max_source_pages'),12);
  const p=project();p.diagnosis.source_options={max_source_pages:48,reading_strategy:'full_chain'};h.run(`applyProject(${JSON.stringify(p)})`);assert.equal(h.run('draftChanged()'),false);assert.match(h.run('readingDepthControl()'),/48 sections per batch/);
  h.run("state.busy=true");assert.match(h.run('readingDepthControl()'),/id="reading-strategy" disabled/);
  h.run("state.busy=false;setMode('demo');state.readingStrategy='focused';setMode('real')");assert.equal(h.run('state.readingStrategy'),'full_chain');assert.equal(h.run('state.maxSourcePages'),48);
});

test('batched coverage and per-source business detail remain readable, literal and traceable in every locale',()=>{
  const summaries=[{relative_path:'programs/entry.cbl',program_name:'REQUEST-ENTRY',program_names:['REQUEST-ENTRY','REQUEST-CHECK'],text:'來源：分析工作台 <script>unsafe()</script>\n\nThis request is passed to review. [ev_page_a] [ev_forged]',verification:'unverified',evidence_ids:['ev_page_a','ev_forged'],complete:false}];
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale);setup(h,{program_summaries:summaries,reading_coverage:{reading_strategy:'full_chain',batch_pages:12,total_batches:5,completed_batches:2,total_pages:53,sent_pages:24,summarized_pages:23,complete:false}});
    const html=h.run('actualAnswer()');assert.ok(html.indexOf('The program reads input')<html.indexOf('program-summaries'));assert.match(html,/REQUEST-ENTRY, REQUEST-CHECK/);assert.match(html,/來源：分析工作台 &lt;script&gt;/);assert.doesNotMatch(html,/<script>|data-evidence="ev_forged"/);assert.match(html,/\[ev_forged\]/);
    const details=h.run('programSummariesView(result(),currentRefs(),[])');assert.match(details,/data-evidence="ev_page_a"/);assert.match(details,/program-summary/);
    const exported=h.run('markdownSummary()');assert.match(exported,/This request is passed to review/);assert.match(exported,/來源：分析工作台 <script>/);
    if(locale==='en'){assert.match(html,/Completed batches: 2 \/ 5/);assert.match(html,/Full business flow · Batched reading/);assert.match(html,/Business explanations by source/);assert.match(exported,/## Business explanations by source/);assert.doesNotMatch(h.run('readingDepthControl()'),/[\u3400-\u9fff]/);}
  }
  const h=harness();setup(h,{program_summaries:[{text:'Do not make a citation: [ev_page_a]',evidence_ids:[]}]});assert.doesNotMatch(h.run('programSummariesView(result(),currentRefs(),[])'),/data-evidence/);
});

test('source change messages explain reanalysis rather than a version requirement and offline API tabs are honest',()=>{
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale);setup(h);h.run("state.project.agent={runner_status:'NETWORK_DISABLED',agent_result:null}");const api=h.run('apiResponseView()');
    if(locale==='en'){assert.match(api,/No model analysis has been started/);assert.doesNotMatch(api,/did not save|not captured|version/i);assert.match(h.run("apiErrorText({code:'INDEX_CHANGED'})"),/Source or its index has changed/);assert.doesNotMatch(h.run("apiErrorText({code:'INDEX_CHANGED'})"),/version/i);}
    assert.notEqual(h.run("apiErrorText({code:'INDEX_CHANGED'})"),h.run("apiErrorText({code:'NO_CURRENT_INDEX'})"));
  }
});
