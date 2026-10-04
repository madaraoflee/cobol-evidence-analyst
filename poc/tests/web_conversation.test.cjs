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
  const move=(parent,child,prepend)=>{if(child.parentElement)child.parentElement.children=child.parentElement.children.filter(item=>item!==child);child.parentElement=parent;parent.children[prepend?'unshift':'push'](child);};
  const get=id=>{if(!elements.has(id))elements.set(id,{id,value:'',open:false,hidden:false,innerHTML:'',textContent:'',disabled:false,attributes:new Map(),children:[],parentElement:null,classList:{toggle:()=>{},contains:()=>false},setAttribute(name,value){this.attributes.set(name,String(value));},getAttribute(name){return this.attributes.get(name)??null;},addEventListener:(event,handler)=>events[id+':'+event]=handler,append(child){move(this,child,false);},prepend(child){move(this,child,true);},showModal(){this.open=true;},close(){if(!this.open)return;this.open=false;events[id+':close']?.();},getClientRects(){return this.hidden?[]:[{}];},focus(){document.activeElement=this;}});return elements.get(id);};
  const document={documentElement:{classList:{toggle:()=>{}}},activeElement:null,createTreeWalker:()=>({nextNode:()=>false}),querySelectorAll:()=>[],querySelector:selector=>selector==='.app-shell'?get('app-shell'):null,getElementById:get,addEventListener:(event,handler)=>events[event]=handler};
  get('app-shell').append(get('workspace-sidebar'));get('app-shell').append(get('app-body'));get('app-shell').append(get('activity-sidebar'));
  const context=vm.createContext({document,NodeFilter:{SHOW_TEXT:4},localStorage:{getItem:()=>locale,setItem:()=>{}},fetch:async(url,options)=>{requests.push({url,options});const data=await responder(url,options);const status=data?.__status || (data?.error && !data?.status?409:200);return {ok:status>=200 && status<300,status,json:async()=>data};},setTimeout:()=>1,clearTimeout:()=>{},setInterval:()=>1,clearInterval:()=>{}});
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

test('detailed answers are default and a brief preference reaches the actual request',async()=>{
  const h=harness();setup(h);assert.equal(h.run('state.answerDetail'),'detailed');
  assert.match(h.run('conversationComposer()'),/value="detailed" selected/);
  h.events.change({target:{id:'answer-detail',value:'brief'}});
  h.respond(url=>url==='/api/analyze'?{job_id:'neutral-job'}:{status:'COMPLETED',result:{...project(),conversation:conversation(firstMessages)}});
  await submit(h,'Explain the rule.');
  const body=JSON.parse(h.requests.find(item=>item.url==='/api/analyze').options.body);
  assert.equal(body.answer_detail,'brief');assert.equal(h.run('state.answerDetail'),'brief');
});

test('long normal and length-limited answers retain their final text with explicit partial notices',()=>{
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale);const answer='## Eligibility\n\n'+'A valid request requires capacity.\n\n'.repeat(3000)+'TAIL-RETAINED';
    setup(h,[{id:'assistant-long',role:'assistant',content:answer,status:'completed',analysis_status:'ANALYZED',finish_reason:'stop'}]);
    const normal=h.run('renderWorkbench()');assert.match(normal,/TAIL-RETAINED/);assert.equal((normal.match(/A valid request requires capacity/g)||[]).length,3000);assert.doesNotMatch(normal,/answer-incomplete/);
    h.run("state.conversation.messages[0].analysis_status='PARTIAL';state.conversation.messages[0].finish_reason='length';state.conversation.messages[0].answer_truncated=true");
    const partial=h.run('renderWorkbench()');assert.match(partial,/answer-incomplete/);assert.match(partial,/finish_reason=length/);assert.match(partial,/TAIL-RETAINED/);
    assert.equal(h.run('state.conversation.messages[0].content'),answer);
    if(locale==='en')assert.match(partial,/Incomplete answer: the model reached its output length limit/);
  }
});

test('page projection limits have a visible notice distinct from the model output limit',()=>{
  const h=harness();setup(h,[{id:'projected-answer',role:'assistant',content:'Retained answer',status:'completed',analysis_status:'ANALYZED',finish_reason:'stop',answer_display_truncated:true,answer_complete_characters:1000015}]);
  const html=h.run('renderWorkbench()');assert.match(html,/page display character limit/);assert.match(html,/complete model answer remains in the local result file/);assert.doesNotMatch(html,/finish_reason=length/);assert.match(html,/Retained answer/);
});

test('an unknown completion reason never invents a provider length stop',()=>{
  const h=harness();setup(h,[{id:'partial-answer',role:'assistant',content:'Available answer',status:'completed',analysis_status:'PARTIAL',finish_reason:'unknown',answer_truncated:true}]);
  const html=h.run('renderWorkbench()');assert.match(html,/answer-incomplete/);assert.doesNotMatch(html,/finish_reason=length/);assert.match(html,/Available answer/);
});

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

test('reload restores the latest safe failure only for the matching conversation and failed run',async()=>{
  const diagnostic={schema_version:'safe-api-error/v1',category:'connection',transport_reason:'connection_reset',evidence_source:'local',request_id:'local-'+'b'.repeat(32),reason:'UNTRUSTED PROVIDER TEXT'};
  for(const match of ['current','other-conversation','other-run','completed-turn']){
    const h=harness('zh-CN');
    const current=conversation([...firstMessages,{id:'failed-question',role:'user',content:'Keep this question',run_id:match==='other-run'?'older-run':'failed-run',status:match==='completed-turn'?'completed':'failed'}]);
    const job={job_id:'failed-run',status:'FAILED',conversation:{id:match==='other-conversation'?'unrelated':current.id},error:{diagnostic}};
    h.respond(url=>url==='/api/state'?{session_token:'test',api_configured:true,project:project(),conversations:[current],conversation:current,job}:url==='/api/framework-demo'?frameworkDemoFixture():assert.fail(url));
    await h.run('initialize()');
    const html=h.run('conversationWorkbench()');
    assert.match(html,/A valid request is accepted/);assert.match(html,/Keep this question/);
    assert.doesNotMatch(html,/UNTRUSTED PROVIDER TEXT/);
    if(match==='current'){assert.match(html,/接口连接中断/);assert.match(html,/<details><summary>查看安全错误详情/);}
    else assert.doesNotMatch(html,/conversation-failure/);
  }
});

test('reload restores a cancelled turn without claiming a provider failure',()=>{
  const h=harness('zh-CN');setup(h,[{id:'cancelled-question',role:'user',content:'Stop this question',run_id:'cancelled-run',status:'cancelled'}]);
  h.run("restoreConversationFailure({job_id:'cancelled-run',status:'CANCELLED',conversation:{id:'conversation-1'}})");
  const html=h.run('conversationWorkbench()');assert.match(html,/已停止本次工作/);assert.doesNotMatch(html,/查看安全错误详情/);
});

test('switching conversations blocks both Enter and form submission until the active scope is ready',async()=>{
  const h=harness();setup(h,firstMessages);
  h.run("state.question='Keep this draft';state.conversationLoading=true");
  await h.events.submit({target:{id:'question-form'},preventDefault(){}});
  let submitted=0;h.get('question-form').requestSubmit=()=>submitted++;
  h.events.keydown({target:{id:'question-input'},key:'Enter',preventDefault(){}});
  assert.equal(submitted,0);assert.equal(h.requests.length,0);
  assert.equal(h.run('state.question'),'Keep this draft');
  h.run('state.conversationLoading=false');
  h.events.keydown({target:{id:'question-input'},key:'Enter',isComposing:true,preventDefault(){}});
  h.events.keydown({target:{id:'question-input'},key:'Enter',shiftKey:true,preventDefault(){}});
  assert.equal(submitted,0,'IME confirmation and newline must never send');
});

test('a failed turn has one concise panel with correlation details folded outside the answer',()=>{
  const h=harness('zh-CN');setup(h,firstMessages);
  const id='local-'+'a'.repeat(32);
  h.run(`state.error='Technical text ${id}';state.errorDiagnostic={schema_version:'safe-api-error/v1',category:'http_429_unknown',evidence_source:'http_status',http_status:429,request_id:'${id}',request_id_source:'local'};render()`);
  assert.equal(h.get('notice').hidden,true);
  const html=h.run('conversationWorkbench()');
  assert.equal((html.match(/class="conversation-failure"/g)||[]).length,1);
  const paragraph=html.match(/class="conversation-failure" role="status">[\s\S]*?<p>(.*?)<\/p>/)[1];
  assert.match(html, /class="failure-heading">[\s\S]*?本次请求未完成<\/div>/);
  assert.match(paragraph,/HTTP 429/);assert.equal((paragraph.match(/HTTP 429/g)||[]).length,1);
  assert.doesNotMatch(paragraph,/local-|Technical text/);
  assert.match(html,/<details><summary>查看安全错误详情/);
  assert.match(html,new RegExp(id));assert.match(html,/A valid request is accepted/);
  h.run("state.page='sources';render()");
  assert.equal(h.get('notice').hidden,false,'failure remains visible outside the conversation');
});

test('closing evidence invalidates a pending response without changing the answer or draft',async()=>{
  const h=harness();setup(h,firstMessages);let resolve;
  h.run("state.question='Draft while reading'");h.respond(()=>new Promise(done=>{resolve=done;}));
  const pending=h.run("loadConversationEvidence('assistant-1','ev-1')");
  h.run('closeConversationEvidence()');
  resolve({spans:[{evidence_id:'ev-1',integrity:'VALID',source_text:'LATE SOURCE'}]});
  await pending;
  assert.equal(h.run('state.conversationEvidence'),null);
  assert.equal(h.run('state.question'),'Draft while reading');
  assert.match(h.run('conversationWorkbench()'),/A valid request is accepted/);
  assert.doesNotMatch(h.run('conversationWorkbench()'),/LATE SOURCE/);
});

test('starter questions populate a draft without making an analysis request',()=>{
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale);setup(h);
    const view=h.run('conversationWorkbench()');
    assert.match(view,/starter-questions/);assert.match(view,/maxlength="8000"/);
    const question=view.match(/data-question="([^"]+)"/)[1];
    h.events.click({target:{closest:()=>({dataset:{question},hasAttribute:()=>false,classList:{contains:()=>false}})}});
    assert.equal(h.run('state.question'),question);assert.equal(h.requests.length,0);
  }
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

test('recent conversations retain every populated conversation beyond the first twelve',()=>{
  const h=harness();setup(h,firstMessages);
  const conversations=Array.from({length:18},(_,index)=>({id:'conversation-'+(index+1),title:'Request question '+(index+1),message_count:2}));
  conversations.splice(3,0,{id:'empty-conversation',title:'Empty conversation',message_count:0});
  h.run(`state.conversations=${JSON.stringify(conversations)};renderChrome()`);
  const html=h.get('conversation-list').innerHTML;
  assert.equal((html.match(/data-conversation=/g)||[]).length,18);
  for(const item of conversations.filter(item=>item.message_count)){
    assert.ok(html.includes(`data-conversation="${item.id}"`),item.id+' must remain available');
    assert.ok(html.includes(`data-delete-conversation="${item.id}"`));
  }
  assert.match(html,/aria-current="true"/);assert.doesNotMatch(html,/empty-conversation/);
  assert.equal(h.requests.length,0);
});

test('opening an older conversation and receiving a follow-up keep its original list position',async()=>{
  const h=harness();setup(h,firstMessages);
  const older={...conversation(firstMessages),id:'older-request',created_at:'2026-09-01T08:00:00Z',first_question_at:'2026-09-01T08:01:00Z'};
  const newer={id:'newer-request',title:'Newer question',created_at:'2026-09-02T08:00:00Z',first_question_at:'2026-09-02T08:01:00Z',message_count:2};
  h.run(`state.conversations=${JSON.stringify([newer,{...older,messages:undefined,message_count:2}])}`);
  h.respond(url=>{assert.equal(url,'/api/conversations/older-request');return {conversation:older};});
  await h.run("openConversation('older-request')");
  assert.equal(h.run('state.conversations.map(item=>item.id).join()'),'newer-request,older-request');
  h.run(`applyConversation(${JSON.stringify({...older,title:'Updated older question',updated_at:'2026-10-04T10:00:00Z',messages:[...firstMessages,{id:'follow-up',role:'user',content:'Explain further.',created_at:'2026-10-04T10:00:00Z'}]})});renderChrome()`);
  assert.equal(h.run('state.conversations.map(item=>item.id).join()'),'newer-request,older-request');
  assert.match(h.get('conversation-list').innerHTML,/Updated older question/);
  assert.equal(h.run("state.conversations[1].first_question_at"),older.first_question_at);
});

test('initial restoration orders conversations by first question rather than creation or updates',async()=>{
  const h=harness();
  const older={...conversation(firstMessages),id:'older-request',created_at:'2026-09-02T08:00:00Z',first_question_at:'2026-09-02T08:01:00Z',updated_at:'2026-10-04T10:00:00Z'};
  const laterQuestion={id:'later-question',title:'Asked later',created_at:'2026-09-01T08:00:00Z',first_question_at:'2026-09-03T08:01:00Z',updated_at:'2026-09-03T08:02:00Z'};
  h.respond(url=>url==='/api/state'?{session_token:'test',project:project(),conversations:[older,laterQuestion],conversation:older}:url==='/api/framework-demo'?frameworkDemoFixture():assert.fail(url));
  await h.run('initialize()');
  assert.equal(h.run('state.conversation.id'),'older-request');
  assert.equal(h.run('state.conversations.map(item=>item.id).join()'),'later-question,older-request');
});

test('conversation responses without dates preserve existing metadata and legacy order',()=>{
  const h=harness();setup(h,firstMessages);
  h.run("state.conversations=[{id:'legacy-newer',title:'Earlier list entry'},{id:'conversation-1',title:'Older list entry'}]");
  h.run(`applyConversation(${JSON.stringify(conversation(firstMessages))})`);
  assert.equal(h.run('state.conversations.map(item=>item.id).join()'),'legacy-newer,conversation-1');
  h.run("state.conversations[1].created_at='2026-09-01T08:00:00Z';state.conversations[1].first_question_at='2026-09-01T08:01:00Z';state.conversations[0].created_at='2026-09-02T08:00:00Z'");
  h.run(`applyConversation(${JSON.stringify(conversation(firstMessages))})`);
  assert.equal(h.run('state.conversations.map(item=>item.id).join()'),'legacy-newer,conversation-1');
  assert.equal(h.run('state.conversations[1].first_question_at'),'2026-09-01T08:01:00Z');
});

test('recent conversations drawer restores its rail, expanded state and visible trigger after closing',()=>{
  const h=harness();setup(h,firstMessages);
  const shell=h.get('app-shell'),rail=h.get('activity-sidebar'),dialog=h.get('activity-dialog'),toggle=h.get('activity-toggle');
  for(const close of [()=>h.events['activity-close:click'](),()=>dialog.close()]){
    h.events['activity-toggle:click']();
    assert.equal(dialog.open,true);assert.equal(rail.parentElement,dialog);
    assert.equal(toggle.getAttribute('aria-expanded'),'true');assert.equal(h.run('document.activeElement.id'),'activity-close');
    h.events['activity-toggle:click']();
    assert.equal(dialog.children.length,1,'opening an open drawer must not duplicate its rail');
    close();
    assert.equal(dialog.open,false);assert.equal(rail.parentElement,shell);assert.equal(shell.children.at(-1),rail);
    assert.equal(toggle.getAttribute('aria-expanded'),'false');assert.equal(h.run('document.activeElement.id'),'activity-toggle');
  }
  h.events['activity-toggle:click']();toggle.hidden=true;dialog.close();
  assert.equal(rail.parentElement,shell);assert.equal(toggle.getAttribute('aria-expanded'),'false');
  assert.notEqual(h.run('document.activeElement.id'),'activity-toggle','a desktop-hidden drawer trigger must not receive focus');
  assert.equal(h.requests.length,0);
});

test('switching between navigation and recent conversations keeps only one drawer open',()=>{
  const h=harness();const shell=h.get('app-shell');
  h.events['navigation-toggle:click']();
  assert.equal(h.get('workspace-sidebar').parentElement,h.get('navigation-dialog'));
  h.events['activity-toggle:click']();
  assert.equal(h.get('navigation-dialog').open,false);assert.equal(h.get('navigation-toggle').getAttribute('aria-expanded'),'false');
  assert.equal(shell.children[0],h.get('workspace-sidebar'));assert.equal(h.get('activity-dialog').open,true);
  h.events['navigation-toggle:click']();
  assert.equal(h.get('activity-dialog').open,false);assert.equal(h.get('activity-toggle').getAttribute('aria-expanded'),'false');
  assert.equal(shell.children.at(-1),h.get('activity-sidebar'));assert.equal(h.get('navigation-dialog').open,true);
  assert.equal(h.requests.length,0);
});

test('selecting a recent conversation closes the drawer before restoring that conversation',async()=>{
  const h=harness();setup(h,firstMessages);
  const restored={...conversation(firstMessages),id:'conversation-18',title:'Older request question'};
  h.respond(url=>{assert.equal(url,'/api/conversations/conversation-18');assert.equal(h.get('activity-dialog').open,false);return {conversation:restored};});
  h.run('const restoreConversation=openConversation;let restoredConversation;openConversation=id=>restoredConversation=restoreConversation(id)');
  h.events['activity-toggle:click']();
  const target={dataset:{conversation:'conversation-18'},hasAttribute:()=>false,classList:{contains:()=>false}};
  h.events.click({target:{closest:()=>target}});
  await h.run('restoredConversation');
  assert.equal(h.get('activity-sidebar').parentElement,h.get('app-shell'));
  assert.equal(h.get('activity-toggle').getAttribute('aria-expanded'),'false');
  assert.equal(h.run('state.conversation.id'),'conversation-18');assert.equal(h.run('state.project.snapshot_id'),'sha256:source');
  assert.equal(h.requests.length,1);assert.match(h.run('renderWorkbench()'),/A valid request is accepted/);
});

test('deleting from recent conversations closes the drawer and still requires explicit confirmation',()=>{
  const h=harness();setup(h,firstMessages);h.events['activity-toggle:click']();
  const target={dataset:{deleteConversation:'conversation-1'},hasAttribute:()=>false,classList:{contains:()=>false}};
  h.events.click({target:{closest:()=>target}});
  assert.equal(h.get('activity-dialog').open,false);assert.equal(h.get('activity-sidebar').parentElement,h.get('app-shell'));
  assert.equal(h.get('delete-conversation-dialog').open,true);assert.equal(h.run('state.deletingConversationId'),'conversation-1');
  assert.equal(h.requests.length,0);
  h.events['delete-conversation-cancel:click']();
  assert.equal(h.run('state.conversation.id'),'conversation-1');assert.equal(h.run('state.deletingConversationId'),null);
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

test('retry completes a failed turn in its original conversation without adding the question again',async()=>{
  const h=harness();setup(h,firstMessages);let turn=0;
  const failed=[...firstMessages,{id:'user-2',role:'user',content:'Which conditions reject it?',status:'failed'}];
  const completed=[...firstMessages,{...failed[2],status:'completed'},{id:'assistant-2',role:'assistant',content:'Missing approval rejects it.',status:'completed'}];
  h.respond(url=>{
    if(url==='/api/analyze')return {job_id:'job-'+ ++turn};
    return turn===1?{status:'FAILED',error:{code:'REQUEST_FAILED'},conversation:conversation(failed)}:{status:'COMPLETED',result:{...project(),conversation:conversation(completed)}};
  });
  await submit(h,'Which conditions reject it?');
  assert.match(h.run('renderWorkbench()'),/Retry this question/);
  h.run("state.question='A separate draft.'");
  const firstRetry=h.run("retryConversationMessage('user-2')");
  await h.run("retryConversationMessage('user-2')");
  await firstRetry;
  const submissions=h.requests.filter(item=>item.url==='/api/analyze').map(item=>JSON.parse(item.options.body));
  assert.equal(submissions.length,2);
  assert.equal(submissions[1].retry_message_id,'user-2');
  assert.equal(submissions[1].conversation_id,'conversation-1');
  assert.equal(submissions[1].question,'Which conditions reject it?');
  assert.equal(h.run('state.question'),'A separate draft.');
  assert.equal(h.run('state.pendingMessage'),null);
  assert.equal(h.run('state.conversation.messages.length'),4);
  assert.equal(h.run("state.conversation.messages.filter(message=>message.content==='Which conditions reject it?').length"),1);
  assert.doesNotMatch(h.run('renderWorkbench()'),/Retry this question/);
});

test('delete confirmation removes only the chosen conversation and keeps the source index',async()=>{
  const h=harness();setup(h,firstMessages);
  h.run("state.conversations=[{id:'conversation-2',title:'Other question',message_count:2},...state.conversations]");
  assert.match(h.run('renderChrome()') || h.get('conversation-list').innerHTML,/data-delete-conversation="conversation-2"/);
  h.run("promptDeleteConversation('conversation-2')");
  assert.equal(h.get('delete-conversation-dialog').open,true);
  assert.equal(h.requests.length,0);
  h.events['delete-conversation-cancel:click']();
  assert.equal(h.get('delete-conversation-dialog').open,false);
  h.run("promptDeleteConversation('conversation-2')");
  h.respond((url,options)=>{assert.equal(options.method,'DELETE');assert.equal(options.body,undefined);return {conversations:[{id:'conversation-1',title:'Request processing',message_count:2}]};});
  await h.events['delete-conversation-confirm:click']();
  assert.equal(h.requests[0].url,'/api/conversations/conversation-2');
  assert.equal(h.run('state.conversation.id'),'conversation-1');
  assert.equal(h.run('state.project.snapshot_id'),'sha256:source');
  assert.equal(h.run('state.conversations.length'),1);
  h.run("promptDeleteConversation('conversation-1')");
  h.respond(()=>({conversations:[]}));
  await h.events['delete-conversation-confirm:click']();
  assert.equal(h.run('state.conversation'),null);
  assert.equal(h.run('state.project.snapshot_id'),'sha256:source');
  assert.equal(h.run('state.conversations.length'),0);
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

test('complete related-object result paginates without putting all rows in the answer',async()=>{
  const h=harness('en');
  const impact={handle:'a'.repeat(32),total:651,counts:{program:650,copybook:1},
    rows:[{id:'one',name:'PROGRAM-ONE',relative_path:'one.cbl',type:'program',match_type:'text_match'}],next_cursor:1};
  setup(h,[{id:'assistant-impact',role:'assistant',content:'The indexed result contains 651 related objects.',
    status:'COMPLETED',impact_result:impact}]);
  let html=h.run('conversationWorkbench()');
  assert.match(html,/Related objects · 651/);assert.match(html,/PROGRAM-ONE/);
  h.respond(url=>{assert.match(url,/\/api\/impact\?/);assert.match(url,/cursor=1/);
    return {...impact,rows:[{id:'two',name:'PROGRAM-TWO',relative_path:'two.cbl',type:'program',match_type:'text_match'}],next_cursor:null};});
  await h.run("loadImpactPage('assistant-impact')");
  html=h.run('conversationWorkbench()');assert.match(html,/PROGRAM-ONE/);assert.match(html,/PROGRAM-TWO/);
  assert.doesNotMatch(html,/Load more/);assert.match(html,/Export complete JSONL/);
  assert.equal(h.requests.length,1);
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
    const html=h.run('conversationWorkbench()');assert.match(html,/<h3 id="answer-assistant-md-section-0" tabindex="-1">业务回答<\/h3>/);assert.match(html,/<strong>先读保单<\/strong>/);assert.match(html,/<code>AMOUNT<\/code>/);assert.match(html,/<ul>/);
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

test('reading navigation stays within its answer and cannot promote heading text to HTML',()=>{
  const h=harness();
  const content='## Same heading\n\nKeep this answer.\n\n### Same heading\n\n<script>unsafe</script>\n\n### `FIELD` & <img src=x onerror=alert(1)>\n\nTAIL';
  setup(h,[{id:'answer-one',role:'assistant',content},{id:'answer-two',role:'assistant',content}]);
  const html=h.run('conversationWorkbench()');
  assert.match(html,/data-answer-section="answer-answer-one-section-1"/);
  assert.match(html,/data-answer-section="answer-answer-two-section-1"/);
  assert.equal((html.match(/>TAIL<\/p>/g)||[]).length,2);
  assert.doesNotMatch(html,/<script>|<img|<button[^>]*onerror=/);
  let focused=false,scrolled=false;
  h.get('answer-answer-two-section-1').focus=()=>focused=true;
  h.get('answer-answer-two-section-1').scrollIntoView=()=>scrolled=true;
  const target={dataset:{answerSection:'answer-answer-two-section-1'},hasAttribute:()=>false,classList:{contains:()=>false}};
  h.events.click({target:{closest:()=>target},preventDefault(){}});
  assert.equal(focused,true);assert.equal(scrolled,true);assert.equal(h.requests.length,0);
});

test('the welcome puts the composer before optional prompts and keeps source onboarding available',()=>{
  for(const locale of ['zh-CN','zh-HK','en']){
    const h=harness(locale);setup(h);
    const html=h.run('conversationWorkbench()');
    assert.ok(html.indexOf('id="question-input"')<html.indexOf('class="starter-questions"'));
    assert.doesNotMatch(html,/conversation-toolbar|data-new-conversation/);
    assert.equal((html.match(/data-question=/g)||[]).length,3);
    h.run('state.project=null');
    assert.match(h.run('conversationWorkbench()'),/data-connect/);
    assert.match(h.run('conversationComposer()'),/ disabled/);
  }
});
