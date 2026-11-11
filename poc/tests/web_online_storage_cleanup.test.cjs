'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const script=fs.readFileSync(path.join(__dirname,'../web/online.js'),'utf8');
const flush=async()=>{await new Promise(resolve=>setImmediate(resolve));await new Promise(resolve=>setImmediate(resolve));};

function harness(locale,initial){
  const events=new Map(),elements=new Map(),requests=[],timers=new Map();
  let timerId=0;
  const element=id=>{
    if(!elements.has(id))elements.set(id,{id,value:'',checked:false,disabled:false,hidden:false,innerHTML:'',textContent:'',dataset:{},
      addEventListener:(name,listener)=>events.set(id+':'+name,listener),reset(){},showModal(){this.open=true;},close(){this.open=false;},click(){}});
    return elements.get(id);
  };
  const workspace={id:'a'.repeat(64),label:'Example source',repository:{identity:'example/source',commit:'b'.repeat(40)},revision:'c'.repeat(64),connected:true};
  let current={workspaces:[workspace],conversations:[],current_workspace_id:workspace.id,...initial};
  const fetch=async(url,options)=>{
    requests.push({url,options});
    return {ok:true,json:async()=>url==='/api/state'?{session_token:'synthetic-session',api_configured:true}:url==='/api/online/state'?current:{}};
  };
  const context=vm.createContext({document:{getElementById:element,querySelectorAll:()=>[],createElement:()=>({click(){}})},
    localStorage:{getItem:()=>locale,setItem(){}},fetch,URL,Blob,confirm:()=>true,
    setTimeout:(callback,delay)=>{timers.set(++timerId,{callback,delay});return timerId;},clearTimeout:id=>timers.delete(id)});
  vm.runInContext(script,context);
  return {element,events,requests,ready:flush,setState:value=>{current={...current,...value};},
    async poll(){assert.equal(timers.size,1);const [id,timer]=timers.entries().next().value;timers.delete(id);timer.callback();await flush();}};
}
function diagnostic(){
  return {schema_version:'safe-api-error/v1',category:'http_5xx_unknown',request_id:'local-'+'a'.repeat(32),
    evidence_source:'http_status',http_status:500,reason:'PRIVATE provider response',next_step:'PRIVATE source excerpt'};
}
function conversation(extra={}){
  return {id:'d'.repeat(32),status:'failed',messages:[{role:'assistant',status:'failed',content:'Verified partial business answer',
    citations:[{key:'saved:0',path:'main.cbl',start:4,end:5}]}],pending:{status:'failed'},...extra};
}

test('online scoped cleanup displays the original API 500 and separate safe storage error in each locale',async()=>{
  for(const [locale,primary,secondary] of [
    ['zh-CN','具体原因尚未确认','存储收尾出现错误'],
    ['zh-HK','具體原因尚未確認','儲存收尾出現錯誤'],
    ['en','specific cause is unconfirmed','storage cleanup failed']]){
    for(const location of ['job','pending']){
      const c=conversation({pending:{status:'failed',...(location==='pending'?{diagnostic:diagnostic(),storage_error:'ONLINE_STATE_UNAVAILABLE'}:{})}});
      const h=harness(locale,{conversation:c,current_conversation_id:c.id,active_job:location==='job'?{
        job_id:'e'.repeat(32),conversation_id:c.id,status:'failed',error:'HTTP_ERROR',diagnostic:diagnostic(),storage_error:'ONLINE_STATE_UNAVAILABLE'}:null});
      await h.ready();
      const text=h.element('model-diagnostic').textContent;
      assert.ok(text.includes(primary),text);assert.ok(text.includes(secondary),text);assert.match(text,/HTTP 500/);
      assert.match(text,/local-a{32}/);assert.equal(h.element('model-diagnostic').hidden,false);
      assert.ok(!text.includes('PRIVATE'));assert.match(h.element('messages').innerHTML,/Verified partial business answer/);
      assert.match(h.element('messages').innerHTML,/data-citation="saved:0"/);
      assert.equal(h.requests.some(request=>request.options.method!=='GET'),false);
    }
  }
});

test('online scoped cleanup ignores arbitrary secondary values without exposing raw exception text',async()=>{
  for(const storage_error of ['PRIVATE raw exception <script>','UNREVIEWED_CODE',{code:'ONLINE_STATE_UNAVAILABLE'},null]){
    const c=conversation({pending:{status:'failed',diagnostic:diagnostic(),storage_error}});
    const h=harness('en',{conversation:c,current_conversation_id:c.id,active_job:null});await h.ready();
    const text=h.element('model-diagnostic').textContent;
    assert.match(text,/HTTP 500/);assert.ok(!text.includes('storage cleanup failed'));
    assert.ok(!text.includes('PRIVATE'));assert.ok(!text.includes('UNREVIEWED_CODE'));assert.ok(!text.includes('<script>'));
  }
});

test('a later normal completion removes the prior storage cleanup message and diagnostic',async()=>{
  const c=conversation({pending:{status:'running',diagnostic:diagnostic(),storage_error:'ONLINE_STATE_UNAVAILABLE'}});
  const h=harness('en',{conversation:c,current_conversation_id:c.id,active_job:{job_id:'e'.repeat(32),conversation_id:c.id,status:'running'}});
  await h.ready();assert.match(h.element('model-diagnostic').textContent,/storage cleanup failed/);
  h.setState({conversation:{...c,status:'answered',pending:null},active_job:{job_id:'e'.repeat(32),conversation_id:c.id,status:'answered'}});
  await h.poll();
  assert.equal(h.element('model-diagnostic').textContent,'');assert.equal(h.element('model-diagnostic').hidden,true);
  assert.equal(h.requests.some(request=>request.url==='/api/online/question'||request.url==='/api/online/resume'),false);
});

test('storage cleanup warning preserves explicit retry confirmation without resending an unknown model request',async()=>{
  const c=conversation({status:'retry_confirmation_required',pending:{status:'retry_confirmation_required',retry_confirmation_required:true,
    diagnostic:diagnostic(),storage_error:'ONLINE_STATE_UNAVAILABLE'}});
  const h=harness('en',{conversation:c,current_conversation_id:c.id,active_job:null});await h.ready();
  assert.equal(h.element('retry').hidden,false);assert.equal(h.element('resume').hidden,true);
  assert.match(h.element('model-diagnostic').textContent,/HTTP 500/);assert.match(h.element('model-diagnostic').textContent,/storage cleanup failed/);
  assert.equal(h.requests.some(request=>request.url==='/api/online/question'||request.url==='/api/online/resume'),false);
});
