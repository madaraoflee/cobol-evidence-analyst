'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {frameworkDemoFixture}=require('./framework_demo_fixture.cjs');
const web=path.join(__dirname,'../web');

function harness(locale='en'){
  const events={};const elements=new Map();const requests=[];
  let responder=()=>{throw Error('Unexpected request');};
  const get=id=>{
    if(!elements.has(id))elements.set(id,{value:'',open:false,hidden:false,innerHTML:'',textContent:'',disabled:false,classList:{toggle:()=>{},contains:()=>false},setAttribute:()=>{},addEventListener:(event,handler)=>events[id+':'+event]=handler,showModal(){this.open=true;},close(){this.open=false;}});
    return elements.get(id);
  };
  const context=vm.createContext({
    document:{documentElement:{},createTreeWalker:()=>({nextNode:()=>false}),querySelectorAll:()=>[],querySelector:()=>null,getElementById:get,addEventListener:(event,handler)=>events[event]=handler},
    NodeFilter:{SHOW_TEXT:4},localStorage:{getItem:()=>locale,setItem:()=>{}},
    fetch:async(url,options)=>{requests.push({url,options});const data=await responder(url,options);return {ok:true,status:200,json:async()=>data};},
    setTimeout:()=>1,clearTimeout:()=>{},setInterval:()=>1,clearInterval:()=>{},
  });
  vm.runInContext(fs.readFileSync(path.join(web,'i18n.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(web,'marked.umd.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(web,'markdown.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(web,'app.js'),'utf8').replace(/render\(\);initialize\(\);\s*$/,''),context);
  return {run:code=>vm.runInContext(code,context),get,events,requests,respond:fn=>responder=fn,
    fixture(value=frameworkDemoFixture()){vm.runInContext(`state.mode='demo';preview.catalog=${JSON.stringify(value)};state.demoLoading=false;selectDemoCase('online',false);`,context);},
    click(data){return events.click({preventDefault:()=>{},target:{closest:()=>({dataset:data,hasAttribute:()=>false,classList:{contains:()=>false}})}});},
  };
}

function preparedProject(locale='en'){
  const demo=frameworkDemoFixture();const selected=demo.cases[1];
  return {source:demo.source_path,source_origin:'synthetic_framework',output:'local-output',snapshot_id:demo.snapshot_id,programs:demo.programs,
    diagnosis:{runner_status:'INDEX_READY',catalog_ready:true,question_status:'NETWORK_DISABLED',question:selected.question[locale],entry_requested:selected.entry_path,source_options:{max_source_pages:12,reading_strategy:'full_chain',index_mode:'catalog'},catalog_report:{files:{candidate:7}}},agent:{runner_status:'NETWORK_DISABLED',agent_result:null},relations:{edges:[]}};
}

test('startup opens an empty real source workspace and only reads local state',async()=>{
  const h=harness();
  assert.equal(h.run('state.mode'),'real');assert.equal(h.run('state.demoLoading'),false);assert.equal(h.run('preview.catalog'),null);
  h.respond(url=>url==='/api/state'?{session_token:'test-session',api_configured:false}:assert.fail(url));
  await h.run('initialize()');
  assert.deepEqual(h.requests.map(item=>item.url),['/api/state']);
  assert.equal(h.run('state.mode'),'real');assert.equal(h.run('state.demoLoading'),false);assert.equal(h.run('state.project'),null);
  assert.equal(h.run('state.question'),'');assert.equal(h.run('state.entry'),'');assert.equal(h.run('sourceUsable()'),false);
  assert.equal(h.run('currentPrograms().length'),0);assert.equal(h.run('preview.catalog'),null);
  assert.match(h.get('page-content').innerHTML,/Start with your own source/);assert.match(h.get('page-content').innerHTML,/data-connect/);
  assert.doesNotMatch(h.get('page-content').innerHTML,/data-demo-case|data-prepare-demo|Synthetic business case/);
  assert.equal(h.get('project-title').textContent,'No local source connected');
  h.run('openSource()');assert.equal(h.get('source-path').value,'');assert.equal(h.get('output-path').value,'');
});

test('startup never restores a saved synthetic project as the real source scope',async()=>{
  for(const withConversation of [false,true]){
    const h=harness();const project=preparedProject();
    const conversation={id:'saved-example',title:'Example discussion',source_origin:'synthetic_framework',messages:[{id:'example-answer',role:'assistant',content:'SAVED-EXAMPLE-ANSWER'}]};
    h.respond(url=>url==='/api/state'?{session_token:'test-session',project,...(withConversation?{conversations:[conversation],conversation}: {})}:assert.fail(url));
    await h.run('initialize()');
    assert.deepEqual(h.requests.map(item=>item.url),['/api/state']);
    assert.equal(h.run('state.mode'),'real');assert.equal(h.run('state.project'),null);assert.equal(h.run('state.conversation'),null);
    assert.equal(h.run('state.question'),'');assert.equal(h.run('state.entry'),'');assert.equal(h.run('sourceUsable()'),false);
    assert.equal(h.run('currentPrograms().length'),0);assert.equal(h.run('preview.catalog'),null);
    assert.doesNotMatch(h.get('page-content').innerHTML,/SAVED-EXAMPLE-ANSWER|data-demo-case|data-prepare-demo/);
    h.run('openSource()');assert.equal(h.get('source-path').value,'');assert.equal(h.get('output-path').value,'');
  }
});

test('explicit case selection loads the authenticated guide once and only an explicit preparation requests its index',async()=>{
  const h=harness();h.respond(url=>url==='/api/state'?{session_token:'test-session',api_configured:false}:url==='/api/framework-demo'?frameworkDemoFixture():assert.fail(url));
  await h.run('initialize()');await h.run("setMode('demo')");
  assert.deepEqual(h.requests.map(item=>item.url),['/api/state','/api/framework-demo']);
  assert.equal(h.requests[1].options.headers['X-Session-Token'],'test-session');
  assert.equal(h.run('state.mode'),'demo');assert.equal(h.run('state.question'),'Explain Service request intake.');
  assert.match(h.get('page-content').innerHTML,/Synthetic business case · No model call/);
  assert.match(h.get('project-description').textContent,/7 files · 3 program definitions/);assert.equal(h.get('nav-count').textContent,'7');
  assert.equal(h.run('state.project'),null);
  await h.run("setMode('real')");await h.run("setMode('demo')");
  assert.deepEqual(h.requests.map(item=>item.url),['/api/state','/api/framework-demo']);
  h.respond(url=>url==='/api/framework-demo/prepare'?{status:'READY',project:preparedProject()}:assert.fail(url));
  await h.run('prepareDemo()');
  assert.deepEqual(h.requests.map(item=>item.url),['/api/state','/api/framework-demo','/api/framework-demo/prepare']);
  assert.equal(h.run('state.mode'),'real');assert.equal(h.run('syntheticProject()'),true);
});

test('returning to real mode after preparing a case restores the real project, conversation, draft and paths',async()=>{
  const h=harness();const project={...preparedProject(),source:'local-source',source_origin:'local_source',output:'local-results',snapshot_id:'sha256:local'};
  project.agent=null;project.diagnosis={...project.diagnosis,question:'Original business question',entry_requested:'',source_manifest_verified:true,scope:{mode:'repository_index'},repository_search:{full_text_complete:true}};
  const conversation={id:'real-conversation',title:'Current real discussion',messages:[{id:'real-answer',role:'assistant',content:'REAL-ANSWER-RETAINED'}]};
  h.run(`state.connected=true;state.conversationSupported=true;applyProject(${JSON.stringify(project)});applyConversation(${JSON.stringify(conversation)});state.question='Keep my unsent real question';state.readingStrategy='retrieval';`);
  const before=h.run('JSON.stringify({project:state.project,conversation:state.conversation,question:state.question,entry:state.entry,readingStrategy:state.readingStrategy})');
  h.respond(url=>url==='/api/framework-demo'?frameworkDemoFixture():url==='/api/framework-demo/prepare'?{status:'READY',project:preparedProject(),conversation:{id:'prepared-example',messages:[]}}:assert.fail(url));
  await h.run("setMode('demo')");await h.run('prepareDemo()');
  assert.equal(h.run('syntheticProject()'),true);assert.equal(h.run('state.conversation.id'),'prepared-example');
  await h.run("setMode('real')");
  assert.equal(h.run('JSON.stringify({project:state.project,conversation:state.conversation,question:state.question,entry:state.entry,readingStrategy:state.readingStrategy})'),before);
  assert.match(h.run('renderWorkbench()'),/REAL-ANSWER-RETAINED/);assert.doesNotMatch(h.run('renderWorkbench()'),/Synthetic business case|prepared-example/);
  h.run('openSource()');assert.equal(h.get('source-path').value,'local-source');assert.equal(h.get('output-path').value,'local-results');
  assert.deepEqual(h.requests.map(item=>item.url),['/api/framework-demo','/api/framework-demo/prepare']);
});

test('unavailable or invalid case data stays empty and clears stale guide evidence',async()=>{
  const h=harness();h.respond(()=>{throw Error('offline');});await h.run('initialize()');
  assert.equal(h.run('state.mode'),'real');await h.run("setMode('demo')");
  assert.match(h.run('renderWorkbench()'),/Start the local service/);assert.doesNotMatch(h.run('renderWorkbench()'),/data-demo-case|data-evidence/);
  h.fixture();h.run('state.connected=true');h.respond(()=>({cases:[]}));await h.run('loadFrameworkDemo()');
  assert.equal(h.run('demoCase()'),null);assert.equal(h.run('state.evidence'),null);assert.match(h.run('renderWorkbench()'),/could not be loaded/);
  assert.equal(h.run('currentPrograms().length'),0);assert.equal(h.get('export-button').disabled,true);
});

test('three case cards switch source, stages, question and relationships without network calls',async()=>{
  const h=harness();h.fixture();h.run('render=()=>{}');
  assert.equal((h.run('frameworkCaseSelector()').match(/data-demo-case=/g)||[]).length,3);
  h.click({demoCase:'client_server'});
  assert.equal(h.run('state.entry'),'programs/screen-control.cbl');assert.equal(h.run('state.question'),'Explain Request review decision.');
  assert.match(h.run('previewAnswer()'),/Controller-based online/);assert.match(h.run('relationsView()'),/SCREENCONTROL/);assert.match(h.run('evidencePanel()'),/screen-control.cbl/);
  await h.run("loadEvidence('ev_guide_client_server')");assert.match(h.run('state.evidence.source_text'),/SCREENCONTROL/);
  h.click({demoCase:'batch'});assert.equal(h.run('state.selectedEvidence'),'ev_guide_batch');assert.match(h.run('previewAnswer()'),/Batch processing/);
  assert.equal(h.requests.length,0);assert.doesNotMatch(h.run('questionCard()'),/textarea|question-form/);assert.match(h.run('questionCard()'),/Load this case/);
  assert.doesNotMatch(h.run('answerTabs()'),/data-tab="trace"|data-tab="api"/);assert.match(h.run('traceView()'),/No model analysis has started/);
});

test('all case views translate authored guide objects while program names and source stay literal',()=>{
  const h=harness();h.fixture();h.run('render=()=>{}');
  for(const locale of ['en','zh-CN','zh-HK']){
    h.events['language-select:change']({target:{value:locale}});
    for(const id of ['online','client_server','batch']){
      h.run(`selectDemoCase('${id}',false)`);
      const content=h.run('renderWorkbench()+renderSources()+renderHistory()+relationsView()+markdownSummary()');
      if(locale==='en')assert.doesNotMatch(content,/[\u3400-\u9fff]/);
      assert.match(content,/REQUESTIO/);assert.doesNotMatch(content,/\[object Object\]|screen_controlled_online/);
    }
  }
  assert.equal(h.requests.length,0);
});

test('case text and source are escaped and forged citations do not create links',()=>{
  const fixture=frameworkDemoFixture();const selected=fixture.cases[0];
  selected.title={en:'<script>case</script>'};selected.purpose={en:'<img src=x onerror=alert(1)>'};selected.steps[0].description={en:'<iframe src=unsafe>'};
  selected.steps.push({title:'Unknown reference',description:'No source span',evidence_id:'ev_forged'});
  selected.relations.push({source_program:'<script>source</script>',target_name:'<img src=x>',relation_type:'CALLS',status:'source_observed',evidence_id:'ev_forged'});
  selected.evidence[0].source_text="DISPLAY '來源：分析工作台 <script>source</script>'.";
  selected.metadata[0].value={en:'<img src=x>'};fixture.snapshot_id='<script>bad';
  const h=harness();h.fixture(fixture);const content=h.run('renderWorkbench()+relationsView()+evidencePanel()');
  assert.match(content,/&lt;script&gt;/);assert.match(content,/&lt;img/);assert.match(content,/來源：分析工作台/);assert.doesNotMatch(content,/<script|<img|<iframe/);
  assert.match(content,/data-evidence="ev_guide_online"/);assert.doesNotMatch(content,/data-evidence="ev_forged"/);
});

test('load case only prepares a local job, restores its question and permits an explicit ordinary analysis',async()=>{
  const h=harness('zh-CN');h.fixture();h.run("state.connected=true;state.token='session';state.apiConfigured=true;selectDemoCase('client_server',false)");
  const project=preparedProject('zh-CN');
  h.respond((url,options)=>{
    if(url==='/api/framework-demo/prepare'){assert.deepEqual(JSON.parse(options.body),{case_id:'client_server',locale:'zh-CN'});return {job_id:'prepared',status:'RUNNING'};}
    if(url==='/api/jobs/prepared')return {status:'COMPLETED',result:project};
    assert.fail(url);
  });
  await h.run('prepareDemo()');
  assert.deepEqual(h.requests.map(item=>item.url),['/api/framework-demo/prepare','/api/jobs/prepared']);
  assert.equal(h.run('state.mode'),'real');assert.equal(h.run('state.page'),'workbench');assert.equal(h.run('state.busy'),false);
  assert.equal(h.run('state.question'),'请解释申请复核决定。');assert.equal(h.run('sourceUsable()'),true);assert.match(h.run('questionCard()'),/id="question-input"/);
  assert.match(h.get('project-title').textContent,/合成源码/);assert.match(h.get('footer-scope').textContent,/合成源码/);
  const question='Explain a new free question without a predefined answer';h.run(`state.question=${JSON.stringify(question)}`);
  h.respond((url,options)=>{
    if(url==='/api/analyze'){const body=JSON.parse(options.body);assert.equal(body.question,question);assert.equal(body.allow_network,true);assert.equal(body.max_source_pages,12);assert.equal(body.reading_strategy,'full_chain');return {job_id:'analysis',status:'RUNNING'};}
    if(url==='/api/jobs/analysis')return {status:'COMPLETED',result:{...project,diagnosis:{...project.diagnosis,question}}};
    assert.fail(url);
  });
  await h.events.submit({target:{id:'question-form'},preventDefault:()=>{}});
  assert.equal(h.requests.filter(item=>item.url==='/api/analyze').length,1);assert.equal(h.run('state.question'),question);assert.equal(h.run('syntheticProject()'),true);
});

test('editable example enters chat, a second example reuses the synthetic index, and follow-ups keep the same conversation',async()=>{
  const h=harness('en');h.fixture();h.run("state.connected=true;state.token='session';state.apiConfigured=true;state.conversationSupported=true");
  const project=preparedProject('en');project.diagnosis.source_manifest_verified=true;project.diagnosis.scope={mode:'repository_index'};project.diagnosis.repository_search={full_text_complete:true};
  const thread={id:'example-conversation',title:'',messages:[]};
  h.events.input({target:{id:'demo-question-input',value:'How is any request routed?'}});
  assert.equal(h.run('state.question'),'How is any request routed?');
  assert.match(h.run('renderWorkbench()'),/How is any request routed/);
  h.respond(url=>url==='/api/framework-demo/prepare'?{job_id:'first-index',status:'RUNNING'}:url==='/api/jobs/first-index'?{status:'COMPLETED',result:{...project,conversation:thread}}:assert.fail(url));
  await h.events.submit({target:{id:'demo-question-form'},preventDefault(){}});
  assert.equal(h.run('state.mode'),'real');assert.equal(h.run('state.entry'),'');assert.equal(h.run('state.question'),'How is any request routed?');
  assert.equal(h.run('state.project.output'),'local-output');assert.match(h.run('renderWorkbench()'),/conversation-workspace/);
  await h.run("setMode('demo')");h.run("selectDemoCase('batch',false)");
  h.events.input({target:{id:'demo-question-input',value:'What else happens overnight?'}});
  h.respond(url=>url==='/api/framework-demo/prepare'?{status:'READY',project,conversation:thread,suggested_question:'Suggested question'}:assert.fail(url));
  await h.events.submit({target:{id:'demo-question-form'},preventDefault(){}});
  assert.equal(h.run('state.question'),'What else happens overnight?');assert.equal(h.run('state.project.output'),'local-output');
  assert.equal(h.requests.filter(item=>item.url==='/api/framework-demo/prepare').length,2);
  assert.equal(h.requests.filter(item=>item.url.startsWith('/api/jobs/')).length,1);
  h.respond((url,options)=>{
    if(url==='/api/analyze'){const body=JSON.parse(options.body);assert.equal(body.conversation_id,'example-conversation');assert.equal(body.question,'What else happens overnight?');assert.equal(body.entry,null);assert.equal(body.reading_strategy,'retrieval');return {job_id:'answer'};}
    if(url==='/api/jobs/answer')return {status:'COMPLETED',result:{...project,conversation:{...thread,messages:[{id:'user-1',role:'user',content:'What else happens overnight?',status:'COMPLETED'},{id:'assistant-1',role:'assistant',content:'Requests are processed overnight.',status:'COMPLETED'}]}}};
    assert.fail(url);
  });
  h.events.input({target:{id:'question-input',value:'What else happens overnight?'}});
  await h.events.submit({target:{id:'question-form'},preventDefault(){}});
  assert.match(h.run('renderWorkbench()'),/Requests are processed overnight/);
});

test('a failed example preparation keeps the prior synthetic project and conversation',async()=>{
  const h=harness('en');h.fixture();const project=preparedProject('en');
  h.run(`state.connected=true;state.conversationSupported=true;state.project=${JSON.stringify(project)};state.conversation={id:'prior',messages:[{id:'before',role:'user',content:'Earlier question'}]};state.question='My edited question'`);
  h.respond(()=>{throw Error('offline');});await h.run('prepareDemo()');
  assert.equal(h.run('state.mode'),'demo');assert.equal(h.run('state.project.output'),'local-output');assert.equal(h.run('state.conversation.id'),'prior');assert.equal(h.run('state.question'),'My edited question');
  h.respond(url=>url==='/api/framework-demo/prepare'?{job_id:'failed-job',status:'RUNNING'}:{status:'FAILED',error:{code:'INDEX_UNAVAILABLE'}});
  await h.run('prepareDemo()');assert.equal(h.run('state.mode'),'demo');assert.equal(h.run('state.project.output'),'local-output');assert.equal(h.run('state.conversation.id'),'prior');
});

test('guide and later real-mode exports preserve synthetic provenance and source hashes',()=>{
  const h=harness();h.fixture();const guide=JSON.parse(h.run('JSON.stringify(exportData())'));
  assert.equal(guide.source_origin,'synthetic_framework');assert.equal(guide.model_called,false);assert.equal(guide.case.evidence[0].source_sha256,'sha256:online-source');
  assert.match(h.run('markdownSummary()'),/Synthetic business case · No model call/);assert.match(h.run('markdownSummary()'),/sha256:online-source/);
  h.run(`state.mode='real';applyProject(${JSON.stringify(preparedProject())});renderChrome()`);
  assert.equal(h.run('exportData().mode'),'synthetic_source_analysis');assert.equal(h.run('exportData().source_origin'),'synthetic_framework');
  assert.match(h.run('markdownSummary()'),/Local synthetic source/);assert.match(h.get('project-title').textContent,/Synthetic source/);
  assert.match(h.get('footer-scope').textContent,/Synthetic source · Business interpretation needs review/);
});

test('an offline prepared case is ready to analyse, with missing implementations inside an optional detail',()=>{
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale);const project=preparedProject(locale);project.diagnosis.unresolved_dependencies=[{target_name:'GENERATEDIO',relation_type:'CALLS',relative_path:'programs/screen-control.cbl'}];
    project.diagnosis.messages=['Previous indexing boundary'];
    h.run(`state.mode='real';applyProject(${JSON.stringify(project)});renderChrome()`);
    const answer=h.run('actualAnswer()');assert.match(answer,/class="prepared-case"/);assert.match(answer,/<details[^>]*><summary>/);assert.match(answer,/GENERATEDIO/);
    assert.doesNotMatch(answer,/Previous indexing boundary|Model analysis not enabled|Resolve the gaps/);
    if(locale==='en'){assert.match(answer,/Case loaded; no model call yet/);assert.match(answer,/7 files · 3 catalog entries/);assert.doesNotMatch(answer,/[\u3400-\u9fff]/);assert.match(h.get('snapshot-info').innerHTML,/Snapshot fixture/);}
    h.run("state.question='A new question'");assert.match(h.run('actualAnswer()'),/class="prepared-case"/);
    h.run("state.project.agent.runner_status='SAFE_STOP';state.project.agent.stop_detail={reason:'model_client_error'}");assert.doesNotMatch(h.run('actualAnswer()'),/class="prepared-case"/);
  }
});

test('business scenario titles and process explanations precede optional technical detail',()=>{
  const h=harness();h.fixture();const cards=h.run('frameworkCaseSelector()');const answer=h.run('previewAnswer()');
  assert.ok(cards.indexOf('Service request intake')<cards.indexOf('Traditional online'));
  assert.doesNotMatch(cards,/SERVICEENTRY|SCREENCONTROL|NIGHTBATCH|<code/);
  assert.match(answer,/Business process/);assert.match(answer,/Business rules and impact/);
  assert.ok(answer.indexOf('Business rules and impact')<answer.indexOf('class="guide-technical"'));
  assert.ok(answer.indexOf('SERVICEENTRY')>answer.indexOf('class="guide-technical"'));
  assert.match(answer,/<details class="guide-technical"><summary>Technical and framework references<\/summary>/);
  assert.match(answer,/data-evidence="ev_guide_online"/);assert.match(h.run('evidencePanel()'),/class="code-body"/);
  const summary=h.run('markdownSummary()');assert.match(summary,/# Xin Xian · Business explanation/);
  assert.ok(summary.indexOf('Business rules and impact')<summary.indexOf('Framework type'));
  assert.equal(h.run('exportData().model_called'),false);assert.equal(h.run('exportData().source_origin'),'synthetic_framework');
});

test('real analysis suggestions cover general business questions without fixed topic categories',()=>{
  const h=harness();h.run(`state.mode='real';applyProject(${JSON.stringify(preparedProject())})`);
  const card=h.run('questionCard()');assert.match(card,/Rules and calculations/);assert.match(card,/Exceptions and impact/);
  assert.match(card,/business rules and calculations, including inputs, conditions and exceptions/);assert.match(card,/which business outcomes are affected/);
  assert.doesNotMatch(card,/Which programs are called|main processing steps, inputs, outputs|status returned to the caller/);
  h.click({program:'SERVICEENTRY',programLabel:'SERVICEENTRY'});
  assert.equal(h.run('state.question'),'Explain SERVICEENTRY in terms of its business purpose, eligibility rules, status changes and exception impact.');
});

test('the business question and answer precede collapsed framework references, which citations reopen',()=>{
  const h=harness();const project=preparedProject();
  const framework={status:'MATCHED',document:{title:'Business rule reference',sha256:'sha256:rules',section_count:1},references:[{reference_id:'ref-rule',heading:'Eligibility rule',start_line:1,end_line:4,text:'Available background guidance.'}]};
  project.agent={runner_status:'COMPLETED',agent_result:{status:'ANALYZED',analysis_mode:'source_reading',narrative:{text:'Eligible requests move to approval. [ref-rule]',verification:'unverified'},framework_context:framework,claims:[],evidence_refs:[],stop_reason:'completed'}};
  h.run(`state.mode='real';applyProject(${JSON.stringify(project)})`);const workbench=h.run('renderWorkbench()');
  assert.ok(workbench.indexOf('Business question')<workbench.indexOf('Eligible requests move to approval.'));
  assert.ok(workbench.indexOf('Eligible requests move to approval.')<workbench.indexOf('class="supporting-context"'));
  assert.match(workbench,/<details class="supporting-context"><summary>Technical and framework references<\/summary>/);
  assert.match(workbench,/data-tab="api"/);assert.match(workbench,/data-framework-reference="ref-rule"/);
  const ref=h.get('framework-reference-ref-rule');const list={open:false};const support={open:false};let scrolled=false;
  ref.closest=selector=>selector==='.framework-references'?list:selector==='.supporting-context'?support:null;ref.scrollIntoView=()=>{scrolled=true;};
  h.click({frameworkReference:'ref-rule'});
  assert.equal(ref.open,true);assert.equal(list.open,true);assert.equal(support.open,true);assert.equal(scrolled,true);
});
