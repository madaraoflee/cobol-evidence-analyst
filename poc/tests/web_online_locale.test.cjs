'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const web=path.join(__dirname,'../web');
const script=fs.readFileSync(path.join(web,'online.js'),'utf8');
const html=fs.readFileSync(path.join(web,'online.html'),'utf8');
const flush=async()=>{await new Promise(r=>setImmediate(r));await new Promise(r=>setImmediate(r));};
function harness(locale='zh-CN',initial={},responses={}){
  const events=new Map(),elements=new Map(),requests=[],timers=new Map(),storage=new Map([['cobol-lens.locale',locale]]);
  let timer=0;
  const element=id=>{if(!elements.has(id))elements.set(id,{id,value:'',checked:false,disabled:false,hidden:false,innerHTML:'',textContent:'',dataset:{},isConnected:true,
    addEventListener:(name,fn)=>events.set(id+':'+name,fn),reset(){},showModal(){this.open=true;},close(){this.open=false;},click(){},
    getAttribute(name){return this[name]??null;},setAttribute(name,value){this[name]=value;}});return elements.get(id);};
  const staticText=[...html.matchAll(/>([^<>]+)</g)].filter(m=>/[\u3400-\u9fff]/.test(m[1])).map(m=>({data:m[1],original:m[1],isConnected:true,parentElement:{closest:()=>false}}));
  const attrs=[];
  for(const match of html.matchAll(/<[a-z][^>]*>/g)){
    const id=/\bid="([^"]+)"/.exec(match[0])?.[1];const node=element(id||'static-'+attrs.length);
    let has=false;for(const name of ['aria-label','placeholder','title']){const value=new RegExp(name+'="([^"]*)"').exec(match[0])?.[1];if(value){node[name]=value;has=true;}}
    if(has)attrs.push(node);
  }
  const workspace={id:'a'.repeat(64),label:'源码依据',repository:{identity:'example/source',commit:'b'.repeat(40)},revision:'c'.repeat(64),connected:true,coverage:{indexed_files:2,source_bytes:1024}};
  let current={workspaces:[workspace],conversations:[],current_workspace_id:workspace.id,current_conversation_id:null,conversation:null,active_job:null,...initial};
  element('same-credentials').checked=true;element('location-kind').value='downloads';
  const document={documentElement:{lang:'zh-CN'},title:'在线源码业务分析',getElementById:element,
    querySelectorAll:selector=>selector.includes('placeholder')?attrs:[],createElement:()=>({click(){}}),
    createTreeWalker(){let i=-1;return{currentNode:null,nextNode(){if(++i>=staticText.length)return false;this.currentNode=staticText[i];return true;}};}};
  const fetch=async(url,options)=>{
    const request={url,options,body:options.body?JSON.parse(options.body):undefined};requests.push(request);
    let body=url==='/api/state'?{session_token:'synthetic-session',api_configured:true}:url==='/api/online/state'?current:{};
    if(url==='/api/online/conversations'){current={...current,current_conversation_id:'d'.repeat(32),conversation:{id:'d'.repeat(32),status:'idle',messages:[]}};body={conversation:current.conversation};}
    if(Object.hasOwn(responses,url))body=typeof responses[url]==='function'?await responses[url](request):responses[url];
    return {ok:true,json:async()=>body};
  };
  const context=vm.createContext({document,NodeFilter:{SHOW_TEXT:4},localStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)},
    fetch,URL,Blob,confirm:()=>true,setTimeout:(fn,delay)=>{timers.set(++timer,{fn,delay});return timer;},clearTimeout:id=>timers.delete(id),addEventListener:(name,fn)=>events.set('window:'+name,fn)});
  vm.runInContext(script,context);
  return{events,element,requests,storage,document,staticText,context,timers,workspace,ready:flush,setState:value=>current=value,
    async select(value){element('online-language').value=value;assert.equal(typeof events.get('online-language:change'),'function','online language selector is wired');events.get('online-language:change')();await flush();}};
}

test('the Bitbucket entry label follows all three existing workbench locales',()=>{
  for(const [locale,expected] of [['zh-CN','Bitbucket 在线分析'],['zh-HK','Bitbucket 在線分析'],['en','Bitbucket online analysis']]){
    const ctx=vm.createContext({localStorage:{getItem:()=>locale},document:{documentElement:{},createTreeWalker:()=>({nextNode:()=>false}),querySelectorAll:()=>[],getElementById:()=>({})},NodeFilter:{SHOW_TEXT:4}});
    vm.runInContext(fs.readFileSync(path.join(web,'i18n.js'),'utf8'),ctx);
    assert.equal(vm.runInContext("t('Bitbucket 在線分析')",ctx),expected);
  }
});

test('online inherits the saved locale for authored static and dynamic interface text',async()=>{
  for(const [locale,title,back,coverage] of [['zh-CN','在线源码业务分析','返回本地工作台','资料覆盖范围'],['zh-HK','在線源碼業務分析','返回本地工作台','資料覆蓋範圍'],['en','Online source business analysis','Back to local workbench','Material coverage']]){
    const h=harness(locale);await h.ready();
    assert.equal(h.document.title,title);assert.equal(h.element('online-language').value,locale);
    assert.equal(h.staticText.find(n=>n.original==='返回本地工作台').data,back);
    assert.ok(h.element('coverage-title').textContent.includes(coverage));
    assert.equal(h.element('source-title').textContent,'源码依据');
    assert.equal(h.requests.some(r=>r.url==='/api/online/question'),false);
  }
});

test('changing online language preserves model prose titles filenames and evidence bytes',async()=>{
  const raw='源码依据 已完成 本次调查还没有形成回答。';
  const c={id:'d'.repeat(32),title:raw,status:'answered',messages:[{role:'assistant',content:raw,status:'answered',citations:[{key:'one',path:'源码依据.cbl',start:1,end:3}]}]};
  const h=harness('zh-CN',{conversation:c,current_conversation_id:c.id,conversations:[{...c,workspace_id:'a'.repeat(64)}]}, {['/api/online/evidence?conversation_id='+c.id+'&citation_id=one']:{citation:{path:'源码依据.cbl',start:1,end:3,commit:'b'.repeat(40),revision:'c'.repeat(64)},text:raw}});
  await h.ready();await h.select('en');
  assert.equal(h.storage.get('cobol-lens.locale'),'en');assert.match(h.element('messages').innerHTML,/Business analysis/);
  assert.ok(h.element('messages').innerHTML.includes(raw));assert.ok(h.element('messages').innerHTML.includes('源码依据.cbl'));
  assert.ok(h.element('conversation-list').innerHTML.includes(raw));
  h.events.get('messages:click')({target:{closest:()=>({dataset:{citation:'one'}})}});await h.ready();
  assert.equal(h.element('evidence-text').textContent,raw);await h.select('zh-HK');assert.equal(h.element('evidence-text').textContent,raw);
  assert.ok(h.element('evidence-title').textContent.includes('源码依据.cbl'));
  assert.equal(h.requests.filter(r=>r.url==='/api/online/question'||r.url==='/api/online/resume').length,0);
});

test('question and explicit continuation send the selected locale with consent preserved',async()=>{
  const c={id:'d'.repeat(32),status:'needs_continuation',messages:[]};const h=harness('en',{conversation:c,current_conversation_id:c.id});await h.ready();
  h.element('allow-source-transfer').checked=true;h.element('question').value='Keep 原文 unchanged';
  await h.events.get('question-form:submit')({preventDefault(){}});
  const q=h.requests.find(r=>r.url==='/api/online/question');assert.equal(q.body.locale,'en');assert.equal(q.body.question,'Keep 原文 unchanged');assert.equal(q.body.allow_source_transfer,true);
  await h.select('zh-HK');h.events.get('resume:click')();await h.ready();
  const r=h.requests.find(r=>r.url==='/api/online/resume');assert.equal(r.body.locale,'zh-HK');assert.equal(r.body.approve_retry,false);assert.equal(r.body.allow_source_transfer,true);
});

test('visible errors progress and later refreshes use the newly selected locale',async()=>{
  const c={id:'d'.repeat(32),status:'answered',messages:[]};const h=harness('zh-CN',{conversation:c,current_conversation_id:c.id},{'/api/online/question':{error:'ONLINE_BUSY'}});await h.ready();
  h.element('allow-source-transfer').checked=true;h.element('question').value='Question';await h.events.get('question-form:submit')({preventDefault(){}});
  assert.match(h.element('error').textContent,/当前任务/);await h.select('en');assert.match(h.element('error').textContent,/current task/i);
  h.setState({workspaces:[h.workspace],conversation:c,current_conversation_id:c.id,current_workspace_id:h.workspace.id,active_job:{status:'running',progress:{phase:'model_request_started',completed:2}}});
  h.events.get('disconnect:click')();await h.ready();assert.match(h.element('progress-detail').textContent,/Requesting the model/);
  await h.select('zh-HK');assert.match(h.element('progress-detail').textContent,/請求模型/);
});

function safeError(category='rate_limit',extra={}) {
  return {schema_version:'safe-api-error/v1',category,request_id:'local-'+'a'.repeat(32),evidence_source:'provider_code',
    http_status:500,provider_code:'rate_limit_exceeded',reason:'PRIVATE provider payload <script>',next_step:'PRIVATE company source',...extra};
}

test('online safe diagnostic is visible for synchronous failures and retranslated without exposing provider text',async()=>{
  const c={id:'d'.repeat(32),status:'failed',messages:[]};
  for(const [locale,expected] of [['zh-CN','请求速率受限'],['zh-HK','請求速率受限'],['en','request rate limit']]){
    const h=harness(locale,{conversation:c,current_conversation_id:c.id},{'/api/online/question':{error:'HTTP_ERROR',diagnostic:safeError()}});await h.ready();
    h.element('allow-source-transfer').checked=true;h.element('question').value='Question';await h.events.get('question-form:submit')({preventDefault(){}});
    const text=h.element('error').textContent;assert.ok(text.includes(expected),text);assert.match(text,/HTTP 500/);assert.match(text,/local-a{32}/);assert.ok(!text.includes('PRIVATE'));
    await h.select('en');assert.match(h.element('error').textContent,/request rate limit/);
    assert.equal(h.requests.filter(r=>r.url==='/api/online/question').length,1);
  }
});

test('online jobs and reopened pending investigations retain distinct timeout rate limit and unknown 500 explanations',async()=>{
  const cases=[['timeout','timed out'],['rate_limit','request rate limit'],['http_5xx_unknown','specific cause is unconfirmed']];
  for(const [category,expected] of cases){
    for(const location of ['job','pending']){
      const diagnostic=safeError(category,{provider_code:category==='rate_limit'?'rate_limit_exceeded':undefined});
      const c={id:'d'.repeat(32),status:'retry_confirmation_required',messages:[],pending:{retry_confirmation_required:true,...(location==='pending'?{diagnostic}:{})}};
      const h=harness('en',{conversation:c,current_conversation_id:c.id,active_job:location==='job'?{status:'completed',conversation_id:c.id,error:'HTTP_ERROR',diagnostic}:null});await h.ready();
      const text=h.element('model-diagnostic').textContent;assert.ok(text.includes(expected),text);assert.match(text,/HTTP 500/);assert.ok(!text.includes('PRIVATE'));
      assert.equal(h.element('retry').hidden,false);assert.equal(h.element('resume').hidden,true);
      assert.equal(h.requests.some(r=>r.url==='/api/online/question'||r.url==='/api/online/resume'),false);
      if(category==='http_5xx_unknown')assert.ok(!text.includes('rate limit')&&!text.includes('insufficient quota'));
    }
  }
});

test('online rejects malformed diagnostic fields and never renders unsafe upstream identifiers or instructions',async()=>{
  const c={id:'d'.repeat(32),status:'failed',messages:[]};
  const invalid=[safeError('PRIVATE'),safeError('rate_limit',{schema_version:'untrusted'}),safeError('rate_limit',{request_id:'PRIVATE'}),safeError(['rate_limit'])];
  for(const diagnostic of invalid){
    const h=harness('en',{conversation:c,current_conversation_id:c.id},{'/api/online/question':{error:'HTTP_ERROR',diagnostic}});await h.ready();
    h.element('allow-source-transfer').checked=true;h.element('question').value='Question';await h.events.get('question-form:submit')({preventDefault(){}});
    assert.match(h.element('error').textContent,/HTTP_ERROR/);assert.ok(!h.element('error').textContent.includes('PRIVATE'));assert.ok(!h.element('error').textContent.includes('request rate limit'));
  }
  const h=harness('en',{conversation:{...c,pending:{diagnostic:safeError('http_5xx_unknown',{http_status:'PRIVATE',provider_code:'PRIVATE',upstream_request_id:'PRIVATE',upstream_request_id_source:'body'})}}});await h.ready();
  assert.match(h.element('model-diagnostic').textContent,/unconfirmed/);assert.ok(!h.element('model-diagnostic').textContent.includes('PRIVATE'));
});

test('online release selection and accessible labels change language without changing version labels or credentials',async()=>{
  const h=harness('zh-CN',{}, {'/api/online/releases':{release_items:[{name:'release-name',label:'源码依据 已完成'}]}});await h.ready();
  h.events.get('connect:click')();h.element('source-repository').value='example/source';h.element('material-token').value='synthetic-token';
  await h.events.get('load-releases:click')();h.element('release-name').value='release-name';
  await h.select('en');
  assert.match(h.element('release-name').innerHTML,/Select a published version/);assert.ok(h.element('release-name').innerHTML.includes('源码依据 已完成'));
  assert.equal(h.element('release-name').value,'release-name');assert.equal(h.element('material-token').value,'synthetic-token');
  assert.match(h.element('release-status').textContent,/Found 1 versions/);assert.match(h.element('question').placeholder,/For example/);
  const count=h.requests.length;h.events.get('window:storage')({key:'cobol-lens.locale',newValue:'zh-HK'});await h.ready();
  assert.equal(h.document.documentElement.lang,'zh-HK');assert.match(h.element('question').placeholder,/例如/);assert.equal(h.requests.length,count);
});

test('online failures belong to the selected conversation and workspace while unrelated running jobs keep polling',async()=>{
  const c={id:'d'.repeat(32),status:'failed',messages:[]};
  const cases=[
    ['current question',{conversation_id:c.id,workspace_id:'a'.repeat(64)},true],
    ['another conversation',{conversation_id:'e'.repeat(32),workspace_id:'a'.repeat(64)},false],
    ['another workspace',{conversation_id:c.id,workspace_id:'f'.repeat(64)},false],
    ['current connection',{kind:'connect',conversation_id:null,workspace_id:'a'.repeat(64)},true],
    ['other connection',{kind:'connect',conversation_id:null,workspace_id:'f'.repeat(64)},false],
    ['new connection',{kind:'connect',conversation_id:null,workspace_id:null},true],
  ];
  for(const [name,identity,expected] of cases){
    const h=harness('en',{conversation:c,current_conversation_id:c.id,active_job:{kind:'question',status:'failed',error:'HTTP_ERROR',diagnostic:safeError(),...identity}});await h.ready();
    const visible=(h.element('error').hidden?'':h.element('error').textContent)+(h.element('model-diagnostic').hidden?'':h.element('model-diagnostic').textContent);
    assert.equal(visible.includes('request rate limit'),expected,name);
    assert.equal(visible.includes('HTTP_ERROR'),false,name+' must not substitute a stale generic job error');
    const legacy=harness('en',{conversation:c,current_conversation_id:c.id,active_job:{kind:'question',status:'failed',error:'HTTP_ERROR',...identity}});await legacy.ready();
    assert.equal(!legacy.element('error').hidden && legacy.element('error').textContent.includes('HTTP_ERROR'),expected,name+' without a diagnostic');
  }
  const h=harness('en',{conversation:c,current_conversation_id:c.id,active_job:{kind:'question',status:'running',conversation_id:'e'.repeat(32),workspace_id:'a'.repeat(64),diagnostic:safeError()}});await h.ready();
  assert.equal(h.element('model-diagnostic').hidden,true);assert.equal(h.timers.size,1);assert.equal(h.element('send').disabled,true);
  const pending=harness('en',{conversation:{...c,pending:{diagnostic:safeError('timeout')}},current_conversation_id:c.id,active_job:{kind:'question',status:'failed',conversation_id:'e'.repeat(32),workspace_id:'a'.repeat(64),diagnostic:safeError()}});await pending.ready();
  assert.match(pending.element('model-diagnostic').textContent,/timed out/);assert.ok(!pending.element('model-diagnostic').textContent.includes('request rate limit'));
});
