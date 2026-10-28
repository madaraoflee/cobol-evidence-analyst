'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const web=path.join(__dirname,'../web');

function harness(locale='en'){
  const events={},elements=new Map(),requests=[],stored=[];let responder=()=>assert.fail('Unexpected request');
  const get=id=>{if(!elements.has(id))elements.set(id,{id,value:'',checked:false,open:false,hidden:false,innerHTML:'',textContent:'',disabled:false,
    classList:{toggle(){},contains(){return false;}},setAttribute(){},removeAttribute(){},addEventListener:(name,handler)=>events[id+':'+name]=handler,
    showModal(){this.open=true;},close(){this.open=false;},getClientRects(){return [{}];},focus(){}});return elements.get(id);};
  const document={documentElement:{classList:{toggle(){}}},createTreeWalker:()=>({nextNode:()=>false}),querySelectorAll:()=>[],querySelector:()=>null,
    getElementById:get,addEventListener:(name,handler)=>events[name]=handler};
  const context=vm.createContext({document,NodeFilter:{SHOW_TEXT:4},localStorage:{getItem:()=>locale,setItem:(...args)=>stored.push(args)},
    fetch:async(url,options)=>{requests.push({url,options});const data=await responder(url,options);const status=data.__status || 200;
      return {ok:status>=200 && status<300,status,json:async()=>data};},setTimeout:()=>1,clearTimeout(){},setInterval:()=>1,clearInterval(){}});
  for(const file of ['i18n.js','marked.umd.js','markdown.js'])vm.runInContext(fs.readFileSync(path.join(web,file),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(web,'app.js'),'utf8').replace(/render\(\);initialize\(\);\s*$/,''),context);
  return {run:code=>vm.runInContext(code,context),get,events,requests,stored,respond:fn=>responder=fn};
}
const custom='Explain this to a new analyst.\nUse my own headings: Why / Who / What happens next.\n保留原文 <details> & markdown | fields.';
const defaults='Business explanation\nBatch job\nOnline function / Programs\nTables\nFiles';
async function load(h,value=custom){h.respond(()=>({session_token:'local-session',conversations:[],answer_instructions:value,default_answer_instructions:defaults}));await h.run('initialize()');h.run('showSettings()');}

test('saved answer instructions restore from the service without browser storage and preserve editable drafts on locale changes',async()=>{
  const h=harness();await load(h);
  assert.equal(h.get('answer-instructions').value,custom);assert.equal(h.run('state.answerInstructions'),custom);
  assert.equal(h.get('answer-instructions-template').hidden,false);assert.equal(h.stored.length,0);
  const draft=custom+'\nNew unsaved requirement';h.get('answer-instructions').value=draft;
  h.events['language-select:change']({target:{value:'zh-CN'}});
  assert.equal(h.get('answer-instructions').value,draft);assert.equal(h.run('state.answerInstructions'),custom);
  assert.equal(h.requests.length,1);
});

test('saving accepts arbitrary multiline requirements without heading rules and becomes effective only after a successful save',async()=>{
  const h=harness();await load(h,'');h.get('answer-instructions').value=custom;
  h.respond((url,opts)=>{
    assert.equal(url,'/api/answer-instructions');assert.equal(opts.method,'POST');
    assert.equal(opts.headers['X-Session-Token'],'local-session');assert.deepEqual(JSON.parse(opts.body),{answer_instructions:custom});
    assert.equal(h.run('state.answerInstructions'),'');assert.equal(h.get('answer-instructions-save').disabled,true);
    return {answer_instructions:custom};
  });
  h.run('state.busy=true');await h.events['answer-instructions-form:submit']({preventDefault(){}});
  assert.equal(h.run('state.answerInstructions'),custom);assert.equal(h.get('answer-instructions-save').disabled,false);
  assert.match(h.get('answer-instructions-feedback').textContent,/Future analyses and follow-up questions/);
  assert.equal(h.requests.length,2);assert.equal(h.stored.length,0);
});

test('inserting the shared business template only edits the draft; reset persists an empty value and restores the default',async()=>{
  const h=harness();await load(h);h.events['answer-instructions-template:click']();
  assert.equal(h.get('answer-instructions').value,defaults);assert.equal(h.run('state.answerInstructions'),custom);assert.equal(h.requests.length,1);
  h.respond((url,opts)=>{assert.equal(url,'/api/answer-instructions');assert.deepEqual(JSON.parse(opts.body),{answer_instructions:''});return {answer_instructions:''};});
  await h.events['answer-instructions-reset:click']();
  assert.equal(h.get('answer-instructions').value,'');assert.equal(h.run('state.answerInstructions'),'');
  assert.match(h.get('answer-instructions-feedback').textContent,/built-in business explanation format/);
  h.get('settings-dialog').close();h.run('showSettings()');assert.equal(h.get('answer-instructions').value,'');
});

test('a failed save retains the previous setting and the editable draft; disconnected controls cannot write',async()=>{
  const h=harness();await load(h);const draft='Use paragraphs only.';h.get('answer-instructions').value=draft;
  h.respond(()=>({__status:500,error:{code:'REQUEST_FAILED'}}));await h.run('saveAnswerInstructions()');
  assert.equal(h.run('state.answerInstructions'),custom);assert.equal(h.get('answer-instructions').value,draft);
  assert.equal(h.get('answer-instructions').disabled,false);assert.doesNotMatch(h.get('answer-instructions-feedback').textContent,/Saved\./);
  h.run('state.connected=false;answerInstructionsControls()');assert.equal(h.get('answer-instructions-save').disabled,true);
  await h.run('saveAnswerInstructions()');assert.equal(h.requests.length,2);assert.match(h.get('answer-instructions-feedback').textContent,/Connect to the local service/);
});

test('a pending save cannot send duplicate requests or overwrite the template while saving',async()=>{
  const h=harness();await load(h);let finish;h.get('answer-instructions').value='First saved draft';
  h.respond(()=>new Promise(resolve=>{finish=resolve;}));const first=h.run('saveAnswerInstructions()');
  await h.run('saveAnswerInstructions()');h.events['answer-instructions-template:click']();
  assert.equal(h.get('answer-instructions').value,'First saved draft');assert.equal(h.requests.length,2);
  finish({answer_instructions:'First saved draft'});await first;assert.equal(h.run('state.answerInstructionsSaving'),false);
});

test('the settings form has an accessible optional multiline field and all authored labels translate',()=>{
  const html=fs.readFileSync(path.join(web,'index.html'),'utf8');
  const textarea=html.match(/<textarea id="answer-instructions"[^>]*>/)?.[0];assert.ok(textarea);
  assert.match(textarea,/aria-describedby="answer-instructions-help"/);assert.doesNotMatch(textarea,/required|maxlength|minlength|pattern=/);
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale);
    for(const text of ['系統設定','查看系統設定','AI 回答要求（System prompt）','儲存回答要求','填入業務模板','清空並使用預設']){
      const translated=h.run(`t(${JSON.stringify(text)})`);assert.ok(translated.length);
      if(locale==='en')assert.doesNotMatch(translated,/[\u3400-\u9fff]/);
    }
  }
});

test('a failed reset keeps both the saved requirement and the current unsaved draft',async()=>{
  const h=harness();await load(h);const draft='An unsaved replacement\nKeep this text if reset cannot be saved.';
  h.get('answer-instructions').value=draft;
  h.respond((url,opts)=>{assert.deepEqual(JSON.parse(opts.body),{answer_instructions:''});return {__status:500,error:{code:'REQUEST_FAILED'}};});
  await h.events['answer-instructions-reset:click']();
  assert.equal(h.run('state.answerInstructions'),custom);assert.equal(h.get('answer-instructions').value,draft);
  assert.equal(h.get('answer-instructions-reset').disabled,false);
  assert.doesNotMatch(h.get('answer-instructions-feedback').textContent,/built-in business explanation format/);
});

test('running and completed question polls leave the settings draft intact, including long multiline requirements',async()=>{
  const h=harness();await load(h);const draft=('Use the business vocabulary first.\nThen explain the role and flow of each object.\n').repeat(1000);
  h.get('answer-instructions').value=draft;h.run("state.busy=true;state.jobKind='question';state.jobId='active-question';");
  h.respond(()=>({status:'RUNNING',progress:{phase:'investigating',completed:1,total:null,unit:'requests'}}));
  await h.run("pollJob('active-question')");assert.equal(h.get('answer-instructions').value,draft);
  h.respond(()=>({status:'COMPLETED'}));await h.run("pollJob('active-question')");
  assert.equal(h.get('answer-instructions').value,draft);assert.equal(h.run('state.answerInstructions'),custom);
  h.respond((url,opts)=>{assert.equal(url,'/api/answer-instructions');assert.equal(JSON.parse(opts.body).answer_instructions,draft);return {answer_instructions:draft};});
  await h.run('saveAnswerInstructions()');assert.equal(h.run('state.answerInstructions'),draft);
  assert.equal(h.get('answer-instructions').value,draft);
});
