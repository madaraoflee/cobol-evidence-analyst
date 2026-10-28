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
const local={source:'/local/source',output:'/local/output',source_origin:'local_source',snapshot_id:'sha256:original',programs:[],
  diagnosis:{catalog_ready:true,source_manifest_verified:true,source_options:{encoding:'cp950',source_format:'fixed'},build_report:{files:{candidate:2}}},relations:{edges:[]}};
const remote={...local,source:'/cache/source',output:'/cache/analysis',source_origin:'remote_repository',snapshot_id:'sha256:download',
  remote_source:{provider:'bitbucket',url:'https://code.example.com/projects/DEMO/repos/source/browse',ref:'refs/heads/main',subdirectory:'sit',commit:'a'.repeat(40)}};
const preferences={source:local.source,output:local.output,encoding:'cp950',source_format:'fixed',extensions:null};
function setup(h){h.run(`state.connected=true;state.conversationSupported=true;applyProject(${JSON.stringify(local)});rememberSourcePreferences(${JSON.stringify(preferences)});state.conversation={id:'old',messages:[{id:'answer',role:'assistant',content:'Existing answer',status:'completed'}]};state.question='Unsent question';`);}
function fill(h){h.run('openRemoteSource()');for(const [id,value] of Object.entries({'remote-source-url':remote.remote_source.url,'remote-source-ref':'refs/heads/main','remote-source-subdirectory':'sit','remote-source-token':'transient-secret','remote-source-encoding':'cp950','remote-source-format':'fixed','remote-source-extensions':'.cbl,.cpy'}))h.get(id).value=value;}

test('remote form posts only import options and clears the token before the request, preserving local preferences',async()=>{
  const h=harness();setup(h);fill(h);const before=h.run('JSON.stringify(state.sourcePreferences)');
  h.respond((url,opts)=>{
    assert.equal(h.get('remote-source-token').value,'');assert.equal(h.get('remote-source-dialog').open,false);
    if(url==='/api/source/remote'){
      assert.deepEqual(JSON.parse(opts.body),{url:remote.remote_source.url,ref:'refs/heads/main',subdirectory:'sit',token:'transient-secret',encoding:'cp950',source_format:'fixed',extensions:'.cbl,.cpy',reading_strategy:'focused',answer_detail:'detailed',max_source_pages:4});
      return {job_id:'fetch',source:remote.source,output:remote.output,status:'RUNNING',source_origin:'remote_repository'};
    }
    assert.equal(url,'/api/jobs/fetch');return {status:'COMPLETED',kind:'index',result:remote};
  });
  await h.events['remote-source-form:submit']({preventDefault(){}});
  assert.equal(h.run('JSON.stringify(state.sourcePreferences)'),before);
  assert.equal(h.run('state.project.source'),remote.source);assert.equal(h.run('state.conversation'),null);
  assert.equal(h.requests.some(r=>r.url==='/api/analyze'),false);
  assert.doesNotMatch(h.run('JSON.stringify(state)'),/transient-secret/);assert.equal(h.stored.length,0);
  h.run("openSource('switch')");assert.equal(h.get('source-path').value,local.source);assert.equal(h.get('output-path').value,local.output);
});

test('rejected, failed, and cancelled downloads retain local source, dialogue and draft',async()=>{
  for(const outcome of ['rejected','failed','cancelled']){
    const h=harness();setup(h);fill(h);
    h.respond(url=>url==='/api/source/remote'?(outcome==='rejected'?{__status:400,error:{code:'REMOTE_SOURCE_INVALID_URL'}}:{job_id:'fetch'}):
      {status:outcome==='failed'?'FAILED':'CANCELLED',kind:'index',result:remote,error:{code:'REMOTE_SOURCE_AUTH_FAILED'}});
    await h.events['remote-source-form:submit']({preventDefault(){}});
    assert.equal(h.run('state.project.source'),local.source,outcome);assert.equal(h.run('state.conversation.id'),'old');
    assert.equal(h.run('state.question'),'Unsent question');assert.equal(h.run('state.busy'),false);
    assert.equal(h.get('remote-source-token').value,'');assert.doesNotMatch(h.run('JSON.stringify(state)'),/transient-secret/);
  }
});

test('closing or cancelling the remote dialog clears the password without fetching',()=>{
  const h=harness();setup(h);fill(h);
  h.events.cancel({target:{id:'remote-source-dialog'}});assert.equal(h.get('remote-source-token').value,'');
  h.get('remote-source-token').value='another-secret';h.events['remote-source-dialog:close']();assert.equal(h.get('remote-source-token').value,'');
  assert.equal(h.requests.length,0);
});

test('reload of a running remote import keeps saved local preferences and polls the remote scope',async()=>{
  const h=harness();h.respond(url=>url==='/api/state'?{session_token:'test',project:local,source_preferences:preferences,conversations:[],
    active_job_id:'fetch',job:{kind:'index',source_origin:'remote_repository',source:remote.source,output:remote.output,status:'RUNNING'}}:
    {status:'RUNNING',progress:{phase:'remote_downloading',completed:42,total:null,unit:'bytes'}});
  await h.run('initialize()');assert.equal(h.run('state.sourcePreferences.source'),local.source);
  assert.equal(h.run('state.sourceJobTarget.source'),remote.source);assert.equal(h.run('state.project.source'),local.source);
  assert.match(h.get('page-content').innerHTML,/Downloading repository source/);
});

test('restored remote snapshots do not replace the saved local form and can be fetched again',async()=>{
  const h=harness();h.respond(()=>({session_token:'test',project:remote,source_preferences:preferences,conversations:[]}));
  await h.run('initialize()');h.run('openRemoteSource()');
  assert.equal(h.get('remote-source-url').value,remote.remote_source.url);assert.equal(h.get('remote-source-ref').value,'refs/heads/main');
  assert.equal(h.get('remote-source-subdirectory').value,'sit');assert.equal(h.get('remote-source-token').value,'');
  h.run("openSource('switch')");assert.equal(h.get('source-path').value,local.source);
});

test('remote source provenance and progress are translated, escaped and identify a fixed snapshot',()=>{
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale);setup(h);h.run(`applyProject(${JSON.stringify({...remote,remote_source:{...remote.remote_source,subdirectory:'<script>unsafe</script>'}})})`);
    const html=h.run('renderSources()');assert.match(html,/&lt;script&gt;unsafe&lt;\/script&gt;/);assert.doesNotMatch(html,/<script>/);
    assert.match(html,/a{40}/);assert.match(html,/data-connect-remote/);assert.doesNotMatch(html,/data-source-update/);
    if(locale==='en')assert.doesNotMatch(html,/[\u3400-\u9fff]/);
    for(const phase of ['remote_resolving','remote_downloading','remote_extracting']){
      h.run(`state.busy=true;state.jobKind='index';rememberProgress({phase:'${phase}',completed:1,total:null,unit:'bytes'})`);
      const progress=h.run('progressPanel()');assert.doesNotMatch(progress,new RegExp(phase));if(locale==='en')assert.doesNotMatch(progress,/[\u3400-\u9fff]/);
    }
    for(const code of h.run('Object.keys(REMOTE_SOURCE_ERROR_MESSAGES)')){
      const text=h.run(`apiErrorText({code:'${code}',message:'private upstream response'})`);
      assert.doesNotMatch(text,/private upstream response|REMOTE_SOURCE_/);if(locale==='en')assert.doesNotMatch(text,/[\u3400-\u9fff]/);
    }
  }
});

test('remote intake lives below the local source card in the left sidebar and keeps its form intact',()=>{
  const html=fs.readFileSync(path.join(web,'index.html'),'utf8');
  assert.match(html,/id="source-form"/);assert.match(html,/id="source-path" required/);assert.match(html,/id="output-path" required/);
  assert.match(html,/id="remote-source-token" type="password"[^>]*autocomplete="new-password"/);
  const sidebar=html.match(/<aside[^>]*id="workspace-sidebar"[\s\S]*?<\/aside>/)[0];
  assert.ok(sidebar.indexOf('id="connect-remote-button"')>sidebar.indexOf('class="project-strip"'));
  assert.ok(sidebar.indexOf('id="connect-remote-button"')<sidebar.indexOf('class="sidebar-bottom"'));
  assert.doesNotMatch(html.match(/<aside[^>]*id="activity-sidebar"[\s\S]*?<\/aside>/)[0],/data-connect-remote/);
  assert.match(sidebar,/data-connect-remote/);
  const sourceForm=html.match(/<form id="source-form"[\s\S]*?<\/form>/)[0];assert.doesNotMatch(sourceForm,/data-connect-remote/);
  const h=harness();assert.doesNotMatch(h.run('conversationWelcome(false)'),/data-connect-remote/);assert.match(h.run('conversationWelcome(false)'),/data-connect /);
  setup(h);h.run('render()');assert.equal(h.get('connect-remote-button').disabled,false);
  h.run('state.busy=true;render()');assert.equal(h.get('connect-remote-button').disabled,true);
  assert.equal(h.run('projectSourcePreferences('+JSON.stringify(remote)+')'),null);
});


test('remote intake preserves shared analysis preferences and optional local reference settings',async()=>{
  const h=harness();setup(h);h.get('activity-dialog').open=true;fill(h);
  assert.equal(h.get('activity-dialog').open,false,'opening intake closes the narrow-screen right drawer');
  h.run("state.answerDetail='brief';state.maxSourcePages=12");
  h.get('remote-source-compilation-context').value='"/local/compilation.json"';
  h.get('remote-source-framework-path').value='/local/reference';
  h.respond((url,opts)=>{
    if(url==='/api/source/remote'){
      const body=JSON.parse(opts.body);assert.equal(body.answer_detail,'brief');assert.equal(body.max_source_pages,12);
      assert.equal(body.compilation_context_path,'/local/compilation.json');assert.equal(body.framework_reference_path,'/local/reference');
      assert.equal(body.question,undefined);assert.equal(body.allow_network,undefined);return {job_id:'fetch'};
    }
    return {status:'COMPLETED',kind:'index',result:remote};
  });
  await h.events['remote-source-form:submit']({preventDefault(){}});
});

test('downloaded and local source use identical question options and answer rendering',()=>{
  const h=harness();setup(h);
  const diagnosis={...local.diagnosis,source_options:{...local.diagnosis.source_options,answer_detail:'brief',max_source_pages:12}};
  const message={id:'answer-1',role:'assistant',status:'completed',content:'Valid requests continue. [ev:rule]',
    evidence_refs:[{evidence_id:'ev:rule',relative_path:'request.cbl',start_line:4,end_line:6}]};
  h.run(`applyProject(${JSON.stringify({...local,diagnosis})})`);
  const localOptions=JSON.parse(h.run("JSON.stringify(analysisOptions('Explain the rule'))"));
  const localAnswer=h.run(`conversationMessage(${JSON.stringify(message)})`);
  h.run(`applyProject(${JSON.stringify({...remote,diagnosis})})`);
  const remoteOptions=JSON.parse(h.run("JSON.stringify(analysisOptions('Explain the rule'))"));
  assert.deepEqual({...remoteOptions,source:localOptions.source,output:localOptions.output},localOptions);
  assert.equal(h.run(`conversationMessage(${JSON.stringify(message)})`),localAnswer);
  assert.match(localAnswer,/Valid requests continue/);assert.match(localAnswer,/data-turn-evidence="ev:rule"/);
  assert.equal(remoteOptions.allow_network,true);assert.equal(remoteOptions.answer_detail,'brief');assert.equal(remoteOptions.max_source_pages,12);
});

test('failed or cancelled imports restore framework settings without rolling back a newly saved answer prompt',async()=>{
  for(const status of ['FAILED','CANCELLED']){
    const h=harness();setup(h);const original={status:'LOADED',configured_path:'/local/original-reference'};
    h.run(`state.frameworkKnowledge=${JSON.stringify(original)};state.answerInstructions='Old writing requirements';showSettings()`);
    h.get('answer-instructions').value='New writing requirements';let finished=false;
    h.respond((url,opts)=>{
      if(url==='/api/source/remote')return {job_id:'import',source:remote.source,output:remote.output};
      if(url==='/api/answer-instructions')return {answer_instructions:JSON.parse(opts.body).answer_instructions};
      assert.equal(url,'/api/jobs/import');return finished?{status,kind:'index',error:{code:'REMOTE_SOURCE_NETWORK_FAILED'}}:{status:'RUNNING',kind:'index'};
    });
    await h.run("startRemoteSource({url:'https://code.example.invalid/projects/DEMO/repos/source',framework_reference_path:'/remote/new-reference'})");
    assert.equal(h.run('state.frameworkKnowledge.configured_path'),'/remote/new-reference');
    await h.run('saveAnswerInstructions()');finished=true;await h.run("pollJob('import')");
    assert.equal(h.run('JSON.stringify(state.frameworkKnowledge)'),JSON.stringify(original));
    assert.equal(h.run('state.project.source'),local.source);assert.equal(h.run('state.question'),'Unsent question');
    assert.equal(h.run('state.answerInstructions'),'New writing requirements');assert.equal(h.get('answer-instructions').value,'New writing requirements');
    assert.equal(h.run('state.busy'),false);
  }
});
