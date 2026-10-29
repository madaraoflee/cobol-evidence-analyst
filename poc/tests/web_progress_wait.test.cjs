'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const web=path.join(__dirname,'../web');

function harness(kind='question'){
  const requests=[],timers=[],elements=new Map();
  let responder=()=>{throw Error('Unexpected request');};
  const get=id=>{
    if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',hidden:false,value:'',disabled:false,
      querySelector:()=>null,setAttribute:()=>{},removeAttribute:()=>{},classList:{toggle:()=>{}},addEventListener:()=>{}});
    return elements.get(id);
  };
  const context=vm.createContext({
    document:{getElementById:get,querySelectorAll:()=>[],querySelector:()=>null,addEventListener:()=>{},
      createTreeWalker:()=>({nextNode:()=>false}),
      documentElement:{classList:{toggle:()=>{}}}},
    NodeFilter:{SHOW_TEXT:4},
    localStorage:{getItem:()=> 'en',setItem:()=>{}},
    fetch:async(url,options)=>{requests.push({url,options});const reply=await responder(url);
      const status=typeof reply.status==='number'?reply.status:200;
      return {ok:status===200,status,json:async()=>reply.body || reply};},
    setTimeout:(fn,delay)=>{timers.push({fn,delay});return timers.length;},clearTimeout:()=>{},
    setInterval:()=>1,clearInterval:()=>{},
  });
  for(const name of ['i18n.js','marked.umd.js','markdown.js','app.js']){
    vm.runInContext(fs.readFileSync(path.join(web,name),'utf8').replace(/render\(\);initialize\(\);\s*$/,''),context);
  }
  const run=code=>vm.runInContext(code,context);
  run(`state.token='neutral-token';state.jobId='${'a'.repeat(32)}';state.busy=true;state.jobKind='${kind}';state.conversationSupported=true;render=()=>{};`);
  return {run,get,requests,timers,respond:fn=>responder=fn,
    start:()=>run('pollJob(state.jobId)'),next:async()=>{const timer=timers.shift();assert.ok(timer);await timer.fn();return timer.delay;}};
}
const running=(revision,text='Actual text.',phase='writing_answer')=>({status:'RUNNING',
  ...(revision===null?{}:{progress_revision:revision}),
  progress:{phase,answer_preview:{text,round:1,stage:'draft'}}});

test('question renders changed snapshots immediately and waits using the latest revision',async()=>{
  const h=harness();let calls=0;
  h.respond(()=>running(++calls,calls===1?'First real fragment.':'Second real fragment.'));
  await h.start();
  assert.match(h.get('conversation-analysis-live').innerHTML,/First real fragment/);
  assert.equal(h.timers[0].delay,0);
  await h.next();
  assert.match(h.requests[1].url,/\/progress\?after=1&wait_ms=20000$/);
  assert.equal(h.requests[1].options.headers['X-Session-Token'],'neutral-token');
  assert.match(h.get('conversation-analysis-live').innerHTML,/Second real fragment/);
  assert.equal(h.timers[0].delay,0);
  await h.next();
  assert.match(h.requests[2].url,/after=2&/);
});

test('unchanged wait timeout renews the same cursor without changing the answer',async()=>{
  const h=harness();h.respond(()=>running(4));
  await h.start();await h.next();await h.next();
  assert.equal(h.requests[1].url,h.requests[2].url);
  assert.equal(h.run('state.progress.answer_preview.text'),'Actual text.');
});

test('old servers and source import keep ordinary polling',async()=>{
  for(const [kind,revision] of [['question',null],['index',8]]){
    const h=harness(kind);h.respond(()=>running(revision));
    await h.start();assert.equal(h.timers[0].delay,1000);await h.next();
    assert.doesNotMatch(h.requests[1].url,/\/progress\?/);
  }
});

test('unsupported wait endpoint falls back once to normal job queries',async()=>{
  const h=harness();h.respond(url=>url.includes('/progress?')?{status:404,body:{error:{code:'NOT_FOUND'}}}:running(1));
  await h.start();await h.next();assert.equal(h.timers[0].delay,0);await h.next();
  assert.doesNotMatch(h.requests[2].url,/\/progress\?/);
  assert.equal(h.timers[0].delay,1000);await h.next();
  assert.doesNotMatch(h.requests[3].url,/\/progress\?/);
});

test('disconnection resumes from the last revision without starting a question',async()=>{
  const h=harness();let calls=0;
  h.respond(()=>{calls++;if(calls===2)throw Error('offline');return running(calls===1?7:9);});
  await h.start();await h.next();assert.equal(h.timers[0].delay,2500);await h.next();
  assert.match(h.requests[2].url,/after=7&/);
  assert.equal(h.run('state.pollError'),'');
  assert.ok(h.requests.every(request=>!request.url.includes('/api/analyze')));
});

test('session rejection stops waiting without attempting an unauthenticated fallback',async()=>{
  const h=harness();let calls=0;
  h.respond(()=>++calls===1?running(1):{status:403,body:{error:{code:'INVALID_SESSION'}}});
  await h.start();await h.next();
  assert.equal(h.run('state.jobId'),null);assert.equal(h.timers.length,0);
  assert.equal(h.requests.length,2);
});

test('cancel uses its own request while the progress wait is pending',async()=>{
  const h=harness();let release;
  h.respond(url=>url.endsWith('/cancel')?{cancel_requested:true}:
    url.includes('/progress?')?new Promise(resolve=>{release=resolve;}):running(1));
  await h.start();const pending=h.next();await Promise.resolve();
  await h.run('cancelJob()');assert.ok(h.requests.some(request=>request.url.endsWith('/cancel')));
  release({status:'CANCELLED',progress_revision:2,progress:{phase:'writing_answer'}});
  await pending;assert.equal(h.run('state.busy'),false);assert.equal(h.run('state.jobId'),null);
});

test('completion and failure end waiting; completion clears preview without manufacturing a revision',async()=>{
  for(const status of ['COMPLETED','FAILED']){
    const h=harness();let calls=0;
    h.respond(()=>++calls===1?running(1):{status,progress_revision:2,progress:{phase:'verifying_answer'},
      ...(status==='FAILED'?{error:{code:'ANALYSIS_FAILED'}}:{})});
    await h.start();await h.next();
    assert.equal(h.run('state.busy'),false);assert.equal(h.timers.length,0);
    if(status==='COMPLETED')assert.equal(h.run('state.progress.answer_preview'),undefined);
  }
});

test('response from a replaced job never changes the active preview',async()=>{
  const h=harness();let release;
  h.respond(()=>new Promise(resolve=>{release=resolve;}));
  const pending=h.start();
  h.run("state.jobId='replacement';state.progress={phase:'preparing'};");
  release(running(3,'Old question fragment.'));
  await pending;
  assert.equal(h.run('state.progress.phase'),'preparing');assert.equal(h.timers.length,0);
});

test('phase displays checking while retaining provisional text, then actual revision',()=>{
  const h=harness();
  h.run(`rememberProgress(${JSON.stringify(running(1).progress)});rememberProgress({phase:'reviewing_answer'});updateProgressPanel();`);
  assert.match(h.get('conversation-analysis-live').innerHTML,/answer-preview-heading"><strong>Checking/);
  assert.doesNotMatch(h.get('conversation-analysis-live').innerHTML,/answer-preview-heading"><strong>Writing/);
  assert.match(h.get('conversation-analysis-live').innerHTML,/analysis-activity-history/);
  assert.match(h.get('conversation-analysis-live').innerHTML,/This draft may change/);
  h.run("rememberProgress({phase:'writing_answer',answer_preview:{text:'Actual revision.',round:2,stage:'revision'}});updateProgressPanel();");
  assert.match(h.get('conversation-analysis-live').innerHTML,/Revising/);
});
