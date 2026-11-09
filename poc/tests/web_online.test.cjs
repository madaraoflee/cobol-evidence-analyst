'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const script=fs.readFileSync(path.join(__dirname,'../web/online.js'),'utf8');

function harness(initial={},responses={}){
  const events=new Map(), elements=new Map(), requests=[], timers=new Map();
  let timerId=0;
  const workspace={id:'a'.repeat(64),label:'Example source',repository:{identity:'example/source',commit:'b'.repeat(40)},revision:'c'.repeat(64),connected:true};
  let current={workspaces:[workspace],conversations:[],current_workspace_id:workspace.id,current_conversation_id:null,conversation:null,active_job:null,...initial};
  const element=id=>{
    if(!elements.has(id))elements.set(id,{id,value:'',checked:false,disabled:false,hidden:false,innerHTML:'',textContent:'',dataset:{},
      addEventListener:(name,fn)=>events.set(id+':'+name,fn),reset(){},showModal(){this.open=true;},close(){this.open=false;events.get(id+':close')?.();},click(){}});
    return elements.get(id);
  };
  element('same-credentials').checked=true;
  element('location-kind').value='downloads';
  const fetch=async(url,options)=>{
    requests.push({url,options,body:options.body?JSON.parse(options.body):undefined});
    let body=url==='/api/state'?{session_token:'session-test',api_configured:true}:url==='/api/online/state'?current:{};
    if(url==='/api/online/open'){current={...current,conversation:null,current_conversation_id:null,current_workspace_id:JSON.parse(options.body).workspace_id};body=current;}
    if(url==='/api/online/conversations'){current={...current,current_conversation_id:'d'.repeat(32),conversation:{id:'d'.repeat(32),status:'idle',messages:[]}};body={conversation:current.conversation};}
    if(Object.hasOwn(responses,url))body=typeof responses[url]==='function'?await responses[url]({url,options,body,requests}):responses[url];
    return {ok:true,json:async()=>body};
  };
  const context=vm.createContext({document:{getElementById:element,querySelectorAll:()=>[],createElement:()=>({click(){}})},
    fetch,URL,Blob,confirm:()=>true,setTimeout:(callback,delay)=>{timers.set(++timerId,{callback,delay});return timerId;},clearTimeout:id=>timers.delete(id),console});
  vm.runInContext(script,context);
  return {events,elements,element,requests,context,workspace,timers,setState(value){current=value;},
    fireTimer(){assert.equal(timers.size,1);const [id,timer]=timers.entries().next().value;timers.delete(id);timer.callback();return timer.delay;},
    async ready(){await new Promise(resolve=>setImmediate(resolve));await new Promise(resolve=>setImmediate(resolve));}};
}

test('repository identity accepts exact Cloud repository and rejects credentials, alternate hosts and paths',()=>{
  const context=vm.createContext({URL});vm.runInContext(script,context);
  const parse=context.OnlineUI.repositoryIdentity;
  assert.equal(parse('https://bitbucket.org/example/source.git'),'example/source');
  assert.equal(parse('example/source'),'example/source');
  for(const value of ['https://user:secret@bitbucket.org/example/source','https://other.invalid/example/source','https://bitbucket.org/example/source/src/main','https://bitbucket.org:9443/example/source','../source','example/source?token=secret'])assert.throws(()=>parse(value));
  assert.ok(!context.OnlineUI.errorText(new Error('https://secret@outside.invalid')).includes('secret'));
});

test('page uses guarded online endpoints and no model request before explicit source consent',async()=>{
  const h=harness();await h.ready();
  assert.equal(h.element('send').disabled,true);
  h.element('question').value='Describe the business flow';
  await h.events.get('question-form:submit')({preventDefault(){}});
  assert.equal(h.requests.some(r=>r.url==='/api/online/question'),false);
  h.element('allow-source-transfer').checked=true;h.events.get('allow-source-transfer:change')();
  await h.events.get('question-form:submit')({preventDefault(){}});
  const request=h.requests.find(r=>r.url==='/api/online/question');
  assert.equal(request.body.allow_source_transfer,true);
  assert.equal(request.options.headers['X-Session-Token'],'session-test');
  assert.equal(request.body.question,'Describe the business flow');
  assert.equal(h.requests.some(r=>r.url==='/api/analyze'),false);
});

test('source switching clears prior answers and fixed citation keys are escaped',async()=>{
  const conversation={id:'d'.repeat(32),status:'answered',messages:[{role:'assistant',content:'OLD ANSWER',status:'answered',citations:[{key:'message:0',id:'evidence',path:'<script>.cbl',start:1,end:4}]}]};
  const h=harness({conversation,current_conversation_id:conversation.id});await h.ready();
  assert.match(h.element('messages').innerHTML,/OLD ANSWER/);
  assert.match(h.element('messages').innerHTML,/data-citation="message:0"/);
  assert.ok(!h.element('messages').innerHTML.includes('<script>'));
  h.events.get('workspace-list:click')({target:{closest:()=>({dataset:{workspace:'e'.repeat(64)}})}});
  await h.ready();
  assert.ok(!h.element('messages').innerHTML.includes('OLD ANSWER'));
});

test('unknown outcome is an explicit retry action and uses saved investigation',async()=>{
  const conversation={id:'d'.repeat(32),status:'interrupted',pending:{retry_confirmation_required:true},messages:[{role:'user',content:'Question',status:'interrupted'}]};
  const h=harness({conversation,current_conversation_id:conversation.id});await h.ready();
  assert.equal(h.element('retry').hidden,false);assert.equal(h.element('resume').hidden,true);
  assert.equal(h.element('retry').disabled,true);
  h.element('allow-source-transfer').checked=true;h.events.get('allow-source-transfer:change')();
  h.events.get('retry:click')();await h.ready();
  const request=h.requests.find(r=>r.url==='/api/online/resume');
  assert.equal(request.body.approve_retry,true);assert.equal(request.body.conversation_id,conversation.id);
});

test('connection payload keeps separate credentials in request only and clears form',async()=>{
  const h=harness();await h.ready();
  h.element('source-repository').value='example/source';h.element('release-name').value='analysis-release-'+'f'.repeat(64)+'.json';
  h.element('material-token').value='test-material-token';h.element('material-username').value='reader@example.invalid';
  await h.events.get('connect-form:submit')({preventDefault(){}});
  const request=h.requests.find(r=>r.url==='/api/online/connect');
  assert.equal(request.body.credentials.source_token,'test-material-token');
  assert.equal(request.body.location.kind,'downloads');
  assert.equal(h.element('material-token').value,'');assert.equal(h.element('material-username').value,'');
  assert.equal(Object.keys(h.context.OnlineUI).join(','),'repositoryIdentity,errorText,escape');
});

test('oversized UTF-8 questions fail before posting and retain the editable question',async()=>{
  const conversation={id:'d'.repeat(32),status:'idle',messages:[]};
  const h=harness({conversation,current_conversation_id:conversation.id});await h.ready();
  h.element('allow-source-transfer').checked=true;
  const text='问题说明'.repeat(3000);h.element('question').value=text;
  await h.events.get('question-form:submit')({preventDefault(){}});
  assert.equal(h.requests.some(r=>r.url==='/api/online/question'),false);
  assert.equal(h.element('question').value,text);
  assert.match(h.element('error').textContent,/大小上限/);
});

test('partial input coverage remains visible even when details were omitted',async()=>{
  const item={id:'a'.repeat(64),repository:{identity:'example/source',commit:'b'.repeat(40)},revision:'c'.repeat(64),connected:true,
    coverage:{indexed_files:120,input_skip_count:2,input_skips:[],input_skips_truncated:true,input_coverage_complete:false}};
  const h=harness({workspaces:[item]});await h.ready();
  assert.match(h.element('coverage-title').textContent,/跳过 2/);
  assert.match(h.element('coverage-summary').textContent,/未成功读取/);
  assert.match(h.element('coverage-skips').innerHTML,/部分跳过记录/);
});

test('snapshot, HTTPS and old workspace views keep weekly sync completion unknown',async()=>{
  for(const source_basis of [undefined,'unknown','repository_snapshot']){
    const item={id:'a'.repeat(64),repository:{identity:'example/source',commit:'b'.repeat(40)},revision:'c'.repeat(64),connected:true,source_basis};
    const h=harness({workspaces:[item]});await h.ready();
    assert.match(h.element('source-basis').textContent,/所选源码提交/);
    assert.match(h.element('source-basis').textContent,/完成情况未确认/);
    assert.equal(h.element('source-basis').hidden,false);
  }
  const h=harness({workspaces:[],current_workspace_id:null});await h.ready();
  assert.equal(h.element('source-basis').hidden,true);
});

test('release preview and selected view distinguish synchronizer attestation from snapshot',async()=>{
  for(const source_basis of ['repository_snapshot','sync_attested']){
    const item={id:'a'.repeat(64),repository:{identity:'example/source',commit:'b'.repeat(40)},revision:'c'.repeat(64),connected:true,source_basis};
    const h=harness({workspaces:[item]},{'/api/online/release':{release:item}});await h.ready();
    h.element('source-repository').value='example/source';
    h.element('release-name').value='analysis-release-'+'f'.repeat(64)+'.json';
    await h.events.get('release-name:change')();
    const expected=source_basis==='sync_attested'?/同步方已声明完成/:/完成情况未确认/;
    assert.match(h.element('source-basis').textContent,expected);
    assert.match(h.element('release-status').textContent,expected);
    assert.ok(!h.element('release-status').textContent.includes('已验证全部同步'));
  }
});


test('online status polling recovers from a transient failure without repeating a question',async()=>{
  let reads=0;
  const job={job_id:'e'.repeat(32),kind:'question',status:'running'};
  const h=harness({active_job:job},{'/api/online/state':({body})=>{
    reads++;
    if(reads===2)throw new Error('temporary network failure');
    return reads<3?body:{...body,active_job:{...job,status:'answered'},conversation:{id:'d'.repeat(32),status:'answered',messages:[{role:'assistant',content:'Recovered answer',status:'answered'}]}};
  }});
  await h.ready();assert.equal(h.timers.size,1);
  h.fireTimer();await h.ready();
  assert.equal(h.element('error').hidden,false);
  assert.equal(h.timers.size,1,'a failed read must leave a recovery poll scheduled');
  h.fireTimer();await h.ready();
  assert.match(h.element('messages').innerHTML,/Recovered answer/);
  assert.equal(h.element('error').hidden,true);
  assert.equal(h.element('progress').hidden,true);
  assert.equal(h.element('new-conversation').disabled,false);
  assert.equal(h.timers.size,0);
  assert.equal(h.requests.filter(item=>item.options.method!=='GET').length,0);
});

test('online status retry delay is bounded and invalid sessions stop polling',async()=>{
  let fail=false,invalid=false;
  const h=harness({active_job:{job_id:'e'.repeat(32),kind:'question',status:'running'}},{'/api/online/state':({body})=>{
    if(invalid)return {error:'INVALID_SESSION'};
    if(fail)throw new Error('temporary network failure');
    return body;
  }});
  await h.ready();fail=true;
  const delays=[];
  for(let index=0;index<8;index++){delays.push(h.fireTimer());await h.ready();assert.equal(h.timers.size,1);}
  assert.ok(delays.every(delay=>delay>=800&&delay<=8000));
  assert.ok(delays.at(-1)>delays[0]);
  invalid=true;h.fireTimer();await h.ready();
  assert.equal(h.timers.size,0);
  assert.match(h.element('error').textContent,/刷新页面/);
  assert.equal(h.requests.filter(item=>item.options.method!=='GET').length,0);
});

test('late online status responses cannot overwrite cancellation or workspace selection',async()=>{
  let reads=0,completeOld;
  const job={job_id:'e'.repeat(32),kind:'question',status:'running'};
  const first={id:'a'.repeat(64),label:'First source',repository:{identity:'example/source',commit:'b'.repeat(40)},revision:'c'.repeat(64),connected:true};
  const second={...first,id:'f'.repeat(64),label:'Second source',repository:{identity:'example/next',commit:'c'.repeat(40)}};
  const h=harness({active_job:job,workspaces:[first,second]},{'/api/online/state':({body})=>{
    reads++;
    if(reads===2)return new Promise(resolve=>{completeOld=()=>resolve(body);});
    return reads<3?body:{...body,active_job:{...job,status:'cancelled'},conversation:{id:'d'.repeat(32),status:'cancelled',messages:[]}};
  }});
  await h.ready();h.fireTimer();await h.ready();
  h.events.get('cancel:click')();await h.ready();
  assert.equal(h.element('progress').hidden,true);
  h.events.get('workspace-list:click')({target:{closest:()=>({dataset:{workspace:second.id}})}});await h.ready();
  assert.equal(h.element('source-title').textContent,'Second source');
  completeOld();await h.ready();
  assert.equal(h.element('progress').hidden,true);
  assert.equal(h.element('source-title').textContent,'Second source');
  assert.equal(h.timers.size,0);
  assert.equal(h.requests.filter(item=>item.url==='/api/online/cancel').length,1);
  assert.equal(h.requests.filter(item=>item.url==='/api/online/question'||item.url==='/api/online/resume').length,0);
});

test('online failed actions refresh status without retrying the mutation',async()=>{
  const job={job_id:'e'.repeat(32),kind:'question',status:'running'};
  const h=harness({active_job:job},{'/api/online/cancel':()=>{throw new Error('reply lost');}});
  await h.ready();
  h.events.get('cancel:click')();await h.ready();
  assert.equal(h.requests.filter(item=>item.url==='/api/online/cancel').length,1);
  assert.equal(h.requests.filter(item=>item.url==='/api/online/state').length,2);
  assert.equal(h.timers.size,1);
  assert.equal(h.element('error').hidden,false);
});
