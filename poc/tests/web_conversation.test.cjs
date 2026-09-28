'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {frameworkDemoFixture}=require('./framework_demo_fixture.cjs');
const web=path.join(__dirname,'../web');

function harness(locale='en'){
  const events={},elements=new Map(),requests=[];let responder=()=>{throw Error('Unexpected request');};
  const get=id=>{if(!elements.has(id))elements.set(id,{value:'',open:false,hidden:false,innerHTML:'',textContent:'',disabled:false,classList:{toggle:()=>{},contains:()=>false},setAttribute:()=>{},addEventListener:(event,handler)=>events[id+':'+event]=handler,showModal(){this.open=true;},close(){this.open=false;},focus(){}});return elements.get(id);};
  const context=vm.createContext({document:{documentElement:{classList:{toggle:()=>{}}},createTreeWalker:()=>({nextNode:()=>false}),querySelectorAll:()=>[],querySelector:()=>null,getElementById:get,addEventListener:(event,handler)=>events[event]=handler},NodeFilter:{SHOW_TEXT:4},localStorage:{getItem:()=>locale,setItem:()=>{}},fetch:async(url,options)=>{requests.push({url,options});const data=await responder(url,options);const status=data?.__status || (data?.error && !data?.status?409:200);return {ok:status>=200 && status<300,status,json:async()=>data};},setTimeout:()=>1,clearTimeout:()=>{},setInterval:()=>1,clearInterval:()=>{}});
  vm.runInContext(fs.readFileSync(path.join(web,'i18n.js'),'utf8'),context);vm.runInContext(fs.readFileSync(path.join(web,'marked.umd.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(web,'markdown.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(web,'app.js'),'utf8').replace(/render\(\);initialize\(\);\s*$/,''),context);
  return {run:code=>vm.runInContext(code,context),get,events,requests,respond:fn=>responder=fn};
}
function project(){return {source:'local-source',output:'local-output',snapshot_id:'sha256:source',run_id:'run-1',programs:[{program_name:'REQUEST',relative_path:'request.cbl'}],diagnosis:{catalog_ready:true,source_manifest_verified:true,runner_status:'INDEX_READY',scope:{mode:'repository_index'},repository_search:{full_text_complete:true},build_report:{files:{candidate:1}},source_options:{reading_strategy:'retrieval'}},agent:null,relations:{edges:[]}};}
function conversation(messages=[]){return {id:'conversation-1',title:'Request processing',snapshot_id:'sha256:source',messages};}
const firstMessages=[{id:'user-1',role:'user',content:'How is a request accepted?',status:'COMPLETED'},{id:'assistant-1',role:'assistant',content:'A valid request is accepted. [ev-1]',status:'COMPLETED',run_id:'run-1',evidence_refs:[{evidence_id:'ev-1',relative_path:'request.cbl',start_line:3,end_line:8}],framework_references:[]}];
function setup(h,messages=[]){h.run(`state.connected=true;state.apiConfigured=true;state.mode='real';state.conversationSupported=true;applyProject(${JSON.stringify(project())});state.readingStrategy='retrieval';state.question='';applyConversation(${JSON.stringify(conversation(messages))});`);}
async function submit(h,question){h.events.input({target:{id:'question-input',value:question}});await h.events.submit({target:{id:'question-form'},preventDefault(){}});}

test('the supported service opens a real conversation workspace with retrieval as default',async()=>{
  const h=harness();h.respond(url=>url==='/api/state'?{session_token:'test',api_configured:true,project:project(),conversations:[],conversation:null}:url==='/api/framework-demo'?frameworkDemoFixture():assert.fail(url));
  await h.run('initialize()');assert.equal(h.run('state.mode'),'real');assert.equal(h.run('state.readingStrategy'),'retrieval');assert.equal(h.run('state.question'),'');
  const html=h.run('renderWorkbench()');assert.match(html,/conversation-composer/);assert.match(html,/What would you like to understand/);assert.doesNotMatch(html,/id="reading-depth"|id="job-progress"|evidence-panel/);assert.match(html,/<details class="conversation-options">/);assert.equal(h.requests.length,2);
});

test('server default conversation title follows the selected interface language without rewriting user titles',()=>{
  const h=harness('en');setup(h);h.run("state.conversation.title='新对话'");
  assert.match(h.run('renderWorkbench()'),/New conversation/);assert.doesNotMatch(h.run('renderWorkbench()'),/新对话/);
  h.run("state.conversation.title='Business title from user'");assert.match(h.run('renderWorkbench()'),/Business title from user/);
});

test('two real API turns retain history and submit the server conversation id without browser history',async()=>{
  const h=harness();setup(h);let turn=0;
  h.respond((url,options)=>{
    if(url==='/api/analyze'){turn++;assert.equal(h.run('state.project.programs.length'),1);assert.match(h.run('renderWorkbench()'),turn===2?/A valid request is accepted/:/How is a request accepted/);return {job_id:'job-'+turn,progress:{phase:'retrieving'}};}
    if(url.startsWith('/api/jobs/')){const messages=turn===1?firstMessages:[...firstMessages,{id:'user-2',role:'user',content:'What happens if it is invalid?',status:'COMPLETED'},{id:'assistant-2',role:'assistant',content:'It is returned for correction.',status:'COMPLETED'}];return {status:'COMPLETED',result:{...project(),conversation:conversation(messages)}};}
    assert.fail(url);
  });
  await submit(h,'How is a request accepted?');assert.equal(h.run('state.question'),'');
  h.events.input({target:{id:'question-input',value:'What happens if it is invalid?'}});assert.match(h.run('renderWorkbench()'),/A valid request is accepted/);assert.equal(h.run('draftChanged()'),false);
  await h.events.submit({target:{id:'question-form'},preventDefault(){}});
  const sent=h.requests.filter(item=>item.url==='/api/analyze').map(item=>JSON.parse(item.options.body));assert.equal(sent.length,2);for(const body of sent){assert.equal(body.conversation_id,'conversation-1');assert.equal(body.reading_strategy,'retrieval');assert.equal(body.entry,null);assert.equal(body.messages,undefined);assert.equal(body.history,undefined);}
  assert.equal(h.run('state.conversation.messages.length'),4);const html=h.run('renderWorkbench()');assert.match(html,/How is a request accepted/);assert.match(html,/What happens if it is invalid/);assert.match(html,/returned for correction/);assert.equal(h.run('state.pendingMessage'),null);assert.equal(h.run('state.page'),'workbench');
});

test('new conversations and restored conversations preserve the local index and make no analysis request',async()=>{
  const h=harness();setup(h,firstMessages);h.respond(url=>url==='/api/conversations'?{conversation:{id:'conversation-2',title:'',messages:[]}}:{conversation:conversation(firstMessages)});
  await h.run('newConversation()');assert.equal(h.run('state.conversation.id'),'conversation-2');assert.equal(h.run('state.project.snapshot_id'),'sha256:source');assert.equal(h.run('state.question'),'');
  await h.run("openConversation('conversation-1')");assert.equal(h.run('state.conversation.messages.length'),2);assert.match(h.run('renderWorkbench()'),/A valid request is accepted/);assert.deepEqual(h.requests.map(item=>item.url),['/api/conversations','/api/conversations/conversation-1']);
});

test('raw response capture is off by default and one explicit selection applies to one submission',async()=>{
  const h=harness();setup(h);
  h.respond(url=>url==='/api/analyze'?{job_id:'local-job'}:{status:'COMPLETED',result:{...project(),conversation:conversation(firstMessages)}});
  await submit(h,'Explain the rule.');
  h.events['capture-api-response:change']({target:{checked:true}});
  await submit(h,'Explain the exception.');
  assert.equal(h.run('state.captureApiResponses'),false);
  assert.equal(h.get('capture-api-response').checked,false);
  await submit(h,'Which condition decides?');
  const submissions=h.requests.filter(item=>item.url==='/api/analyze').map(item=>JSON.parse(item.options.body));
  assert.deepEqual(submissions.map(item=>item.capture_api_responses),[false,true,false]);
  for(const locale of ['en','zh-CN','zh-HK']){
    h.run(`selectInterfaceLocale('${locale}')`);
    assert.ok(h.run("t('下一次回答保存接口原文').length")>5);
  }
});

test('failed turns remain visible and retry restores the original question within the same conversation',async()=>{
  const h=harness();setup(h,firstMessages);const failed=[...firstMessages,{id:'user-2',role:'user',content:'Which conditions reject it?',status:'failed'}];
  h.respond(url=>url==='/api/analyze'?{job_id:'job-failed'}:{status:'FAILED',error:{code:'REQUEST_FAILED'},conversation:conversation(failed)});
  await submit(h,'Which conditions reject it?');assert.match(h.run('renderWorkbench()'),/A valid request is accepted/);assert.match(h.run('renderWorkbench()'),/Retry this question/);
  h.run("retryConversationMessage('user-2')");assert.equal(h.run('state.question'),'Which conditions reject it?');assert.equal(h.run('state.conversation.id'),'conversation-1');assert.equal(h.run('state.project.programs.length'),1);
});

test('historical source links use the message and conversation identity, and stale responses keep only the location',async()=>{
  const h=harness();setup(h,firstMessages);h.respond(()=>({spans:[{evidence_id:'ev-1',relative_path:'request.cbl',start_line:3,end_line:8,integrity:'VALID',source_text:'MOVE REQUEST TO ACCEPTED'}]}));
  const html=h.run('renderWorkbench()');assert.match(html,/data-message-id="assistant-1" data-turn-evidence="ev-1"/);
  await h.run("loadConversationEvidence('assistant-1','ev-1')");assert.match(h.requests[0].url,/conversation_id=conversation-1&message_id=assistant-1/);assert.match(h.run('conversationEvidenceView()'),/MOVE<\/span> REQUEST/);
  h.respond(()=>({error:{code:'INDEX_CHANGED'}}));await h.run("loadConversationEvidence('assistant-1','ev-1')");assert.match(h.run('conversationEvidenceView()'),/request.cbl/);assert.doesNotMatch(h.run('conversationEvidenceView()'),/MOVE<\/span> REQUEST/);
});

test('an answer keeps the relevant source map available without protocol status in the conversation',()=>{
  const h=harness();setup(h,[{id:'assistant-related',role:'assistant',content:'The request is reviewed after the threshold.',status:'PARTIAL',
    related_sources:[{relative_path:'request.cbl',program_names:['REQUEST']},{relative_path:'review.cbl',program_names:['REVIEW']}]}]);
  const html=h.run('conversationWorkbench()');
  assert.match(html,/The request is reviewed after the threshold/);
  assert.match(html,/review.cbl/);
  assert.match(html,/Related source · 2/);
  assert.doesNotMatch(html,/部分解讀/);
  assert.doesNotMatch(html,/value="full_chain"/);
});

test('server phase names render compactly without invented percentages and in all three languages',()=>{
  const h=harness();setup(h,firstMessages);h.run("state.busy=true;state.jobId='job';state.jobKind='question'");
  for(const phase of ['using_index','retrieving','answering']){h.run(`rememberProgress({phase:'${phase}',completed:1,total:5,unit:'requests'})`);const html=h.run('conversationProgress()');assert.doesNotMatch(html,/\d+%|Estimating|[\u3400-\u9fff]/);assert.match(h.run('renderWorkbench()'),/A valid request is accepted/);}
  for(const locale of ['zh-CN','zh-HK','en']){h.run(`selectInterfaceLocale('${locale}')`);const html=h.run('conversationWorkbench()');assert.match(html,/A valid request is accepted/);if(locale==='en')assert.doesNotMatch(html,/[\u3400-\u9fff]/);}
});

test('framework folder settings reload through the local API and display the detected document',async()=>{
  const h=harness();setup(h);h.get('framework-path').value='"D:\\references\\framework"';h.respond(url=>{assert.equal(url,'/api/framework');return {framework_knowledge:{status:'LOADED',configured_path:'D:\\references\\framework',document:{title:'Reference guide'}}};});
  await h.events['framework-form:submit']({preventDefault(){}});assert.equal(JSON.parse(h.requests[0].options.body).path,'D:\\references\\framework');assert.match(h.get('framework-feedback').textContent,/Reference guide/);assert.equal(h.run('frameworkConfiguredPath()'),'D:\\references\\framework');
});

test('model connection test reports only a verified reply or safe fixed errors without source or secrets',async()=>{
  const h=harness();setup(h);h.respond((url,options)=>{assert.equal(url,'/api/model-check');assert.deepEqual(JSON.parse(options.body),{});return {usable:true,code:'OK',model_returned:true,private_reply:'DO-NOT-SHOW',api_key:'DO-NOT-SHOW'};});
  await h.events['model-check-button:click']();
  assert.match(h.get('model-check-feedback').textContent,/model returned a usable reply/);assert.equal(h.get('settings-api').textContent,'Verified working');
  assert.doesNotMatch(h.get('model-check-feedback').textContent,/DO-NOT-SHOW|local-source|https?:\/\//);
  h.respond(()=>({usable:false,code:'HTTP_ERROR',http_status:401,model_returned:false}));await h.events['model-check-button:click']();
  assert.match(h.get('model-check-feedback').textContent,/Authentication failed/);
  h.respond(()=>({usable:false,code:'MODEL_TEXT_EMPTY',model_returned:false}));await h.events['model-check-button:click']();
  assert.match(h.get('model-check-feedback').textContent,/no usable text/);
  h.respond(()=>({usable:false,code:'REQUEST_TIMEOUT',model_returned:false}));await h.events['model-check-button:click']();
  assert.match(h.get('model-check-feedback').textContent,/timed out/);
  h.respond(()=>({usable:false,code:'INVALID_RESPONSE_SHAPE',model_returned:false}));await h.events['model-check-button:click']();
  assert.match(h.get('model-check-feedback').textContent,/incompatible format/);assert.doesNotMatch(h.get('model-check-feedback').textContent,/\.env/);
  assert.equal(h.requests.filter(item=>item.url==='/api/model-check').length,5);
  assert.equal(h.requests.filter(item=>item.url==='/api/analyze').length,0);
});

test('folder picker fills each path without indexing; cancel preserves input and unavailable picker leaves manual entry',async()=>{
  const h=harness();setup(h);h.respond((url,options)=>{assert.equal(url,'/api/pick-folder');assert.deepEqual(JSON.parse(options.body),{});return {cancelled:false,path:'C:\\source\\programs'};});
  await h.run("pickFolder('source-path')");assert.equal(h.get('source-path').value,'C:\\source\\programs');
  h.respond((url,options)=>{assert.deepEqual(JSON.parse(options.body),{initial_path:'C:\\source\\programs'});return {cancelled:true,path:null};});await h.run("pickFolder('source-path')");assert.equal(h.get('source-path').value,'C:\\source\\programs');assert.match(h.get('source-picker-feedback').textContent,/cancelled/);
  h.respond(()=>({cancelled:false,path:'C:\\references'}));await h.run("pickFolder('source-framework-path')");assert.equal(h.get('source-framework-path').value,'C:\\references');
  h.respond(()=>({__status:503,error:{code:'FOLDER_PICKER_UNAVAILABLE'}}));await h.run("pickFolder('output-path')");assert.equal(h.get('output-path').value,'');assert.match(h.get('source-picker-feedback').textContent,/unavailable/);
  assert.equal(h.requests.filter(item=>item.url==='/api/pick-folder').length,4);assert.equal(h.requests.filter(item=>item.url==='/api/analyze').length,0);
});

test('conversation content is never translated or treated as HTML and framework references stay within the turn',()=>{
  const h=harness();setup(h,[{id:'a-unsafe',role:'assistant',content:'業務原文 <script>alert(1)</script> [ref-local]',status:'COMPLETED',framework_references:[{reference_id:'ref-local',heading:'Reference <img>',text:'<script>source text</script>'}]}]);
  const html=h.run('conversationWorkbench()');assert.match(html,/業務原文/);assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>|<img>/);assert.match(html,/data-message-id="a-unsafe" data-turn-framework="ref-local"/);assert.match(html,/id="turn-a-unsafe-framework-ref-local"/);
});

test('restored assistant turns render Markdown with turn-local citations and keep user questions literal',()=>{
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale);setup(h,[{id:'user-md',role:'user',content:'**原始问题** <script>text</script>'},{id:'assistant-md',role:'assistant',content:'## 业务回答\n\n**先读保单**，然后计算 `AMOUNT`。 [ev-md]\n\n- 判断生效条件\n- 计算结果',status:'COMPLETED',evidence_refs:[{evidence_id:'ev-md',relative_path:'calculation.cbl',start_line:1,end_line:5}]}]);
    const html=h.run('conversationWorkbench()');assert.match(html,/<h3>业务回答<\/h3>/);assert.match(html,/<strong>先读保单<\/strong>/);assert.match(html,/<code>AMOUNT<\/code>/);assert.match(html,/<ul>/);
    assert.match(html,/data-message-id="assistant-md" data-turn-evidence="ev-md"/);
    assert.match(html,/\*\*原始问题\*\* &lt;script&gt;text&lt;\/script&gt;/);assert.doesNotMatch(html,/<script>/);
  }
});

test('Markdown citation tokens cannot turn source identifiers or reference labels into HTML',()=>{
  const h=harness();const sourceId='"><img src=x onerror=alert(1)>';const frameworkId='"><script>alert(2)</script>';
  setup(h,[{id:'safe-turn',role:'assistant',content:`**业务说明** [${sourceId}] [${frameworkId}]`,status:'COMPLETED',evidence_refs:[{evidence_id:sourceId,relative_path:'<img src=x>.cbl',start_line:1,end_line:2}],framework_references:[{reference_id:frameworkId,heading:'<script>unsafe label</script>',text:'<img src=x>'}]}]);
  const html=h.run('conversationWorkbench()');assert.match(html,/<strong>业务说明<\/strong>/);assert.match(html,/data-turn-evidence="&quot;&gt;&lt;img/);assert.match(html,/&lt;script&gt;unsafe label&lt;\/script&gt;/);
  assert.doesNotMatch(html,/<img|<script|onerror="/i);
});
