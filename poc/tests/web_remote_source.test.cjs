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
    showModal(){this.open=true;},close(){this.open=false;},getClientRects(){return [{}];},focus(){this.focused=true;},scrollIntoView(options){this.scrollOptions=options;}});return elements.get(id);};
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
    assert.match(html,/a{40}/);assert.match(html,/data-update-remote/);assert.doesNotMatch(html,/data-source-update/);
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

const localId='1'.repeat(64),remoteId='2'.repeat(64),newId='3'.repeat(64);
const savedLocal={...local,workspace_id:localId};
const savedRemote={...remote,workspace_id:remoteId};
const cloudRemote={...savedRemote,remote_source:{...savedRemote.remote_source,provider:'bitbucket_cloud',
  url:'https://bitbucket.org/demo-workspace/source/src/'+ 'a'.repeat(40)+'/sit',ref:'a'.repeat(40)}};
function savedWorkspace(project,label){return {workspace_id:project.workspace_id,source_label:label,source_origin:project.source_origin,
  remote_source:project.remote_source,available:true};}
const savedRows=[savedWorkspace(savedLocal,'Local billing'),savedWorkspace(cloudRemote,'Remote billing')];
function currentRemote(h,project=cloudRemote){setup(h);h.run(`applyProject(${JSON.stringify(project)});state.conversation={id:'current',title:'Current business',messages:[{id:'answer',role:'assistant',content:'Keep this answer',status:'completed'}]};state.question='A draft about this source';applySourceWorkspaces({source_workspaces:${JSON.stringify(savedRows)}});`);}

test('restart restores the remote library and saved empty libraries without any download request',async()=>{
  const h=harness();h.respond(url=>{
    assert.equal(url,'/api/state');return {session_token:'test',project:cloudRemote,conversations:[],source_workspaces:savedRows,source_preferences:preferences};
  });
  await h.run('initialize()');
  assert.equal(h.requests.length,1);assert.equal(h.run('state.project.workspace_id'),remoteId);
  const html=h.run('savedSourceWorkspacesView()');assert.match(html,/Local billing/);assert.match(html,/Remote billing/);
  assert.match(html,/Open local copy/);assert.match(html,/Currently open/);
  assert.equal(h.run('state.sourceWorkspaces.length'),2);
});

test('a saved library with no conversation can be opened locally and failed opens preserve current scope',async()=>{
  const h=harness();setup(h);h.run(`applyProject(${JSON.stringify(savedLocal)});state.question='Unsent local question'`);
  h.respond((url,options)=>{
    assert.equal(url,'/api/source/open');assert.deepEqual(JSON.parse(options.body),{workspace_id:remoteId});
    return {project:cloudRemote,conversation:null,conversations:[],source_workspaces:savedRows,source_preferences:preferences};
  });
  await h.run(`openSourceWorkspace('${remoteId}')`);
  assert.equal(h.run('state.project.workspace_id'),remoteId);assert.equal(h.run('state.question'),'');assert.equal(h.run('state.conversation'),null);
  assert.match(h.run('sourceUpdateNotice()'),/without downloading/);assert.equal(h.requests.length,1);
  h.run("state.question='Keep the remote draft'");h.respond(()=>({project:cloudRemote,source_workspaces:[]}));
  await h.run(`openSourceWorkspace('${localId}')`);
  assert.equal(h.run('state.project.workspace_id'),remoteId);assert.equal(h.run('state.question'),'Keep the remote draft');
  assert.equal(h.run('state.sourceWorkspaces.length'),2);assert.equal(h.run('state.conversationLoading'),false);
});

test('reconnecting an existing source consumes the cached response without polling or clearing the current draft',async()=>{
  const h=harness();currentRemote(h);h.run("state.pendingMessage={role:'user',content:'Retained pending message'};openRemoteSource()");
  h.get('remote-source-username').value='reader@example.com';h.get('remote-source-token').value='ephemeral-token';
  h.respond((url,options)=>{
    assert.equal(url,'/api/source/remote');assert.equal(JSON.parse(options.body).username,'reader@example.com');
    assert.equal(h.get('remote-source-token').value,'');assert.equal(h.get('remote-source-username').value,'');
    return {source_reused:true,project:cloudRemote,conversation:null,conversations:[],source_workspaces:savedRows};
  });
  await h.events['remote-source-form:submit']({preventDefault(){}});
  assert.equal(h.requests.length,1);assert.equal(h.run('state.question'),'A draft about this source');
  assert.equal(h.run('state.conversation.id'),'current');assert.equal(h.run('state.pendingMessage.content'),'Retained pending message');
  assert.equal(h.run('state.busy'),false);assert.equal(h.run('state.jobId'),null);
  assert.match(h.run('sourceUpdateNotice()'),/without downloading/);
  assert.doesNotMatch(h.run('JSON.stringify(state)'),/ephemeral-token|reader@example.com/);
});

test('unchanged repository update posts only update credentials and preserves the current question and conversation',async()=>{
  const h=harness();currentRemote(h);h.run("openRemoteSource('update')");
  assert.equal(h.get('remote-source-url').readOnly,true);assert.equal(h.get('remote-source-subdirectory').readOnly,true);
  assert.equal(h.get('remote-source-reading-options').hidden,true);assert.equal(h.get('remote-source-follow-default').checked,false);
  h.get('remote-source-username').value='reader@example.com';h.get('remote-source-token').value='ephemeral-token';
  h.respond((url,options)=>{
    if(url==='/api/source/remote/update'){
      assert.deepEqual(JSON.parse(options.body),{workspace_id:remoteId,username:'reader@example.com',token:'ephemeral-token',ref:'a'.repeat(40),follow_default_branch:false});
      assert.equal(h.get('remote-source-username').value,'');assert.equal(h.get('remote-source-token').value,'');
      return {job_id:'update',source:cloudRemote.source,output:cloudRemote.output};
    }
    assert.equal(url,'/api/jobs/update');return {status:'COMPLETED',kind:'index',result:{...cloudRemote,
      remote_update:{status:'up_to_date',commit:'a'.repeat(40),previous_commit:'a'.repeat(40)},source_workspaces:savedRows}};
  });
  await h.events['remote-source-form:submit']({preventDefault(){}});
  assert.equal(h.requests.length,2);assert.equal(h.run('state.question'),'A draft about this source');
  assert.equal(h.run('state.conversation.id'),'current');assert.equal(h.run('state.project.workspace_id'),remoteId);
  assert.match(h.run('sourceUpdateNotice()'),/unchanged/);assert.equal(h.run('state.page'),'sources');
  assert.doesNotMatch(h.run('JSON.stringify(state)'),/ephemeral-token|reader@example.com/);
});

test('following the default branch is explicit and a new version does not inherit the previous source draft',async()=>{
  const h=harness();currentRemote(h);h.run("openRemoteSource('update')");
  assert.equal(h.get('remote-source-follow-default').checked,false);assert.match(h.run('remoteSourceView()'),/pinned to a commit/);h.get('remote-source-follow-default').checked=true;
  const changed={...cloudRemote,workspace_id:newId,source:'/cache/next/source',output:'/cache/next/analysis',snapshot_id:'sha256:next',
    remote_source:{...cloudRemote.remote_source,ref:null,commit:'b'.repeat(40)},remote_update:{status:'updated',previous_commit:'a'.repeat(40),commit:'b'.repeat(40)}};
  h.respond((url,options)=>{
    if(url==='/api/source/remote/update'){assert.equal(JSON.parse(options.body).follow_default_branch,true);return {job_id:'update'};}
    return {status:'COMPLETED',kind:'index',result:{...changed,conversations:[],source_workspaces:[...savedRows,savedWorkspace(changed,'New remote version')]}};
  });
  await h.events['remote-source-form:submit']({preventDefault(){}});
  assert.equal(h.run('state.project.workspace_id'),newId);assert.equal(h.run('state.question'),'');assert.equal(h.run('state.conversation'),null);
  assert.equal(h.run('state.sourceWorkspaces.length'),3);assert.match(h.run('sourceUpdateNotice()'),/Earlier conversations retain their original version/);
  h.run('openRemoteSource()');assert.equal(h.get('remote-source-url').readOnly,false);assert.equal(h.get('remote-source-reading-options').hidden,false);
});

test('saved source controls and new update messages remain translated and escape repository details',()=>{
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale);currentRemote(h);h.run(`applySourceWorkspaces({source_workspaces:${JSON.stringify([
      {...savedWorkspace(cloudRemote,'<img src=x>'),remote_source:{...cloudRemote.remote_source,ref:'<script>ref</script>'}},
      {...savedWorkspace(savedLocal,'Unavailable local'),available:false}])}})`);
    const html=h.run('savedSourceWorkspacesView()');assert.match(html,/&lt;img src=x&gt;/);assert.match(html,/&lt;script&gt;ref&lt;\/script&gt;/);assert.doesNotMatch(html,/<script>|<img src/);
    assert.match(html,new RegExp('data-open-source-workspace="'+localId+'" disabled'));
    if(locale==='en')assert.doesNotMatch(html,/[\u3400-\u9fff]/);
  }
});

test('closing a remote update clears both temporary credentials without making a request',()=>{
  const h=harness();currentRemote(h);h.run("openRemoteSource('update')");
  h.get('remote-source-username').value='reader@example.com';h.get('remote-source-token').value='ephemeral-token';
  h.events.cancel({target:{id:'remote-source-dialog'}});
  assert.equal(h.get('remote-source-username').value,'');assert.equal(h.get('remote-source-token').value,'');assert.equal(h.requests.length,0);
});

test('cached source reindex jobs take precedence over the synchronous cache response',async()=>{
  const h=harness();currentRemote(h);
  h.respond(url=>url==='/api/source/remote'?{source_reused:true,project:cloudRemote,job_id:'reindex'}:
    {status:'COMPLETED',kind:'index',result:{...cloudRemote,diagnosis:{...cloudRemote.diagnosis,source_options:{source_format:'free'}}}});
  await h.run(`startRemoteSource({url:${JSON.stringify(cloudRemote.remote_source.url)},source_format:'free'})`);
  assert.equal(h.requests.length,2);assert.equal(h.requests[1].url,'/api/jobs/reindex');
  assert.equal(h.run('state.project.diagnosis.source_options.source_format'),'free');
  assert.equal(h.run('state.question'),'A draft about this source');assert.equal(h.run('state.conversation.id'),'current');
  assert.match(h.run('sourceUpdateNotice()'),/index was updated without downloading/);
});

test('an unchanged remote commit reports a repaired index rather than falsely saying the old index was reused',async()=>{
  const h=harness();currentRemote(h);
  h.respond(url=>url==='/api/source/remote/update'?{job_id:'repair'}:
    {status:'COMPLETED',kind:'index',result:{...cloudRemote,remote_update:{status:'up_to_date',index_rebuilt:true}}});
  await h.run(`startRemoteSource({workspace_id:'${remoteId}'},'/api/source/remote/update')`);
  const message=h.run('sourceUpdateNotice()');assert.match(message,/local index was rebuilt/);assert.doesNotMatch(message,/indexes were reused/);
  assert.equal(h.run('state.question'),'A draft about this source');assert.equal(h.run('state.conversation.id'),'current');
});

test('an unchanged commit with repaired source reports the repair and translates local verification',async()=>{
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale);currentRemote(h);
    h.respond(url=>url==='/api/source/remote/update'?{job_id:'repair-source'}:
      {status:'COMPLETED',kind:'index',result:{...cloudRemote,remote_update:{status:'repaired',source_repaired:true,index_rebuilt:true}}});
    await h.run(`startRemoteSource({workspace_id:'${remoteId}'},'/api/source/remote/update')`);
    const message=h.run('sourceUpdateNotice()');
    if(locale==='en'){
      assert.match(message,/source copy was repaired/);
      assert.doesNotMatch(message,/without downloading|indexes were reused|[\u3400-\u9fff]/);
      assert.equal(h.run("phaseLabel('remote_verifying')"),'Checking the local source copy');
    }
  }
});

test('remote terminal failures stay above old conversations on every restored page',async()=>{
  for(const page of ['workbench','sources','history'])for(const outcome of ['FAILED','CANCELLED','REJECTED']){
    const h=harness();setup(h);fill(h);
    h.run(`state.page='${page}';state.conversation.messages=Array.from({length:24},(_,i)=>({id:'prior-'+i,role:'assistant',status:'completed',content:'Earlier business answer '+i}));`);
    h.respond(url=>{
      if(url==='/api/source/remote'){
        assert.match(h.get('page-content').innerHTML,/id="job-progress"/,'a request shows progress immediately');
        return outcome==='REJECTED'?{__status:401,error:{code:'REMOTE_SOURCE_AUTH_FAILED'}}:{job_id:'fetch'};
      }
      return {job_id:'fetch',status:outcome,kind:'index',error:{code:'REMOTE_SOURCE_AUTH_FAILED'}};
    });
    await h.events['remote-source-form:submit']({preventDefault(){}});
    assert.equal(h.run('state.page'),page,`${page}/${outcome}`);
    assert.match(h.get('page-content').innerHTML,/^<section id="source-job-feedback"/);
    assert.match(h.get('page-content').innerHTML,new RegExp(outcome==='CANCELLED'?'Source connection stopped':'Source connection did not complete'));
    assert.match(h.get('page-content').innerHTML,/previous source library remains in use/);
    assert.equal(h.run('state.conversation.messages.length'),24);
    assert.equal(h.run('state.question'),'Unsent question');
    assert.equal(h.get('source-job-feedback').focused,true);
    assert.equal(h.get('source-job-feedback').scrollOptions.block,'start');
    assert.doesNotMatch(h.get('page-content').innerHTML,/class="conversation-failure"/,'no duplicate source error buried in old business messages');
    assert.doesNotMatch(h.run('JSON.stringify(state)'),/transient-secret/);
    h.run('retryRemoteSource()');
    assert.equal(h.get('remote-source-dialog').open,true);
    assert.equal(h.get('remote-source-url').value,remote.remote_source.url);
    assert.equal(h.get('remote-source-token').value,'');assert.equal(h.get('remote-source-username').value,'');
    assert.equal(h.requests.length,outcome==='REJECTED'?1:2,'reviewing settings never resubmits automatically');
  }
});

test('first download completion and cached reuse show durable distinct outcomes',async()=>{
  for(const cached of [false,true]){
    const h=harness();setup(h);fill(h);
    h.respond(url=>url==='/api/source/remote'?(cached?{source_reused:true,project:remote}:{job_id:'fetch'}):{job_id:'fetch',status:'COMPLETED',kind:'index',result:remote});
    await h.events['remote-source-form:submit']({preventDefault(){}});
    assert.equal(h.run('state.page'),'sources');assert.equal(h.run('state.busy'),false);
    assert.match(h.get('page-content').innerHTML,/Source connection complete/);
    assert.match(h.get('page-content').innerHTML,new RegExp(cached?'without downloading':'Source was downloaded and locally indexed'));
    assert.equal(h.get('source-job-feedback').focused,true);
    assert.equal(h.requests.length,cached?1:2);
    assert.equal(h.run('state.sourceJobFeedback.status'),'COMPLETED');
    h.run('render()');assert.match(h.get('page-content').innerHTML,/id="source-job-feedback"/,'a later render does not erase the result');
  }
});

test('reload restores the last failed or cancelled remote intake without downloading again',async()=>{
  for(const status of ['FAILED','CANCELLED']){
    const h=harness();h.respond(url=>{
      assert.equal(url,'/api/state');return {session_token:'test',project:local,conversations:[],
        job:{job_id:'prior-import',kind:'index',source_origin:'remote_repository',status,error:{code:'REMOTE_SOURCE_AUTH_FAILED'}}};
    });
    await h.run('initialize()');
    assert.equal(h.run('state.sourceJobFeedback.status'),status);
    assert.match(h.get('page-content').innerHTML,/^<section id="source-job-feedback"/);
    assert.equal(h.get('source-job-feedback').focused,true);
    assert.equal(h.requests.length,1);
    assert.equal(h.run('state.project.source'),local.source);
    assert.doesNotMatch(h.run('sourceJobFeedbackView()'),/data-retry-remote-source/);
    h.run('retryRemoteSource()');assert.equal(h.get('remote-source-dialog').open,false);
    assert.equal(h.requests.length,1,'reload must not guess the previous connection or update intent');
  }
});

test('uncertain request or status connection loss never claims import completion or resubmits',async()=>{
  for(const outcome of ['connection','lost-job']){
    const h=harness();setup(h);fill(h);
    h.respond(url=>{
      if(outcome==='connection')throw new Error('Simulated lost connection');
      return url==='/api/source/remote'?{job_id:'fetch'}:{__status:404,error:{code:'JOB_NOT_FOUND'}};
    });
    await h.events['remote-source-form:submit']({preventDefault(){}});
    assert.equal(h.run('state.sourceJobFeedback.status'),'UNCONFIRMED');
    assert.match(h.get('page-content').innerHTML,/Source connection outcome is not confirmed/);
    assert.doesNotMatch(h.run('sourceJobFeedbackView()'),/data-retry-remote-source|Source connection complete/);
    assert.equal(h.requests.length,outcome==='connection'?1:2);
    assert.equal(h.get('source-job-feedback').focused,true);
    assert.equal(h.run('state.busy'),false);
  }
});

test('source results and private-repository guidance are translated without making credentials required',()=>{
  const html=fs.readFileSync(path.join(web,'index.html'),'utf8');
  assert.match(html,/remote-source-auth-help/);
  for(const id of ['remote-source-token','remote-source-username'])assert.doesNotMatch(html.match(new RegExp('<input id="'+id+'"[^>]*>'))[0],/\brequired\b/);
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale);setup(h);
    for(const status of ['COMPLETED','FAILED','CANCELLED','UNCONFIRMED']){
      h.run(`state.sourceNotice='已下載源碼並完成本機索引，可以開始提出業務問題。';state.error=apiErrorText({code:'REMOTE_SOURCE_AUTH_FAILED'});finishRemoteSourceFeedback('${status}')`);
      const output=h.run('sourceJobFeedbackView()');assert.match(output,/id="source-job-feedback"/);
      if(locale==='en')assert.doesNotMatch(output,/[\u3400-\u9fff]/);
    }
  }
});

test('repository rate limiting remains a clear safe failure without resubmitting in every language',async()=>{
  for(const locale of ['en','zh-CN','zh-HK'])for(const failureAt of ['submit','job']){
    const h=harness(locale);setup(h);fill(h);
    const error={code:'REMOTE_SOURCE_RATE_LIMITED',message:'Untrusted remote body transient-secret'};
    h.respond(url=>url==='/api/source/remote'?
      (failureAt==='submit'?{__status:429,error}:{job_id:'limited'}):
      {status:'FAILED',kind:'index',error});
    await h.events['remote-source-form:submit']({preventDefault(){}});
    const feedback=h.run('sourceJobFeedbackView()');
    assert.match(feedback,locale==='en'?/rate limiting requests/:/限流/);
    assert.match(feedback,locale==='en'?/Retry manually later/:/手[动動]重[试試]/);
    assert.doesNotMatch(feedback,/REQUEST_FAILED|Untrusted remote body|transient-secret/);
    if(locale==='en')assert.doesNotMatch(feedback,/[\u3400-\u9fff]/);
    assert.equal(h.run('state.project.source'),local.source);
    assert.equal(h.run('state.conversation.id'),'old');
    assert.equal(h.run('state.sourceJobFeedback.status'),'FAILED');
    assert.equal(h.requests.length,failureAt==='submit'?1:2);
  }
});

test('Cloud initial download limits never promise smaller transfers from selecting a subfolder',()=>{
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale);
    const timeout=h.run("apiErrorText({code:'REMOTE_SOURCE_TIMEOUT'})");
    const size=h.run("apiErrorText({code:'REMOTE_SOURCE_TOO_LARGE'})");
    for(const message of [timeout,size]){
      assert.match(message,/Cloud/);
      assert.match(message,locale==='en'?/entire repository snapshot/:/整[个個][仓倉][库庫]快照/);
      assert.match(message,locale==='en'?/existing local source copy/:/已有的本[地機][源碼码]{2}/);
      if(locale==='en')assert.doesNotMatch(message,/[\u3400-\u9fff]/);
    }
    assert.match(timeout,locale==='en'?/subfolder does not reduce the download/:/子目[录錄]不[会會][减減]少下[载載]量/);
    assert.match(size,/Server \/ Data Center/);
  }
});


test('retry before reload preserves the remote update endpoint instead of reopening cached source',async()=>{
  const h=harness();currentRemote(h);h.run("openRemoteSource('update')");let attempt=0;
  h.respond(url=>{
    if(url==='/api/source/remote/update'){attempt++;return {job_id:'update-'+attempt};}
    assert.equal(url,'/api/jobs/update-'+attempt);return attempt===1?{status:'FAILED',kind:'index',error:{code:'REMOTE_SOURCE_AUTH_FAILED'}}:{status:'COMPLETED',kind:'index',result:{...cloudRemote,remote_update:{status:'up_to_date'}}};
  });
  await h.events['remote-source-form:submit']({preventDefault(){}});
  h.run('retryRemoteSource()');assert.equal(h.get('remote-source-dialog').open,true);
  await h.events['remote-source-form:submit']({preventDefault(){}});
  assert.equal(attempt,2);assert.equal(h.requests.filter(r=>r.url==='/api/source/remote').length,0);
  assert.match(h.run('sourceJobFeedbackView()'),/unchanged/);
});

test('incremental sync shows actual transfer counts after reload in every language',()=>{
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale);setup(h);
    const project={...remote,remote_source:{...remote.remote_source,provider:'bitbucket_cloud',sync_mode:'incremental',
      downloaded_files:1,reused_files:12201,downloaded_bytes:320}};
    h.run(`applyProject(${JSON.stringify(project)})`);
    const html=h.run('remoteSourceView()');
    assert.match(html,/remote-sync-summary/);assert.match(html,/12,201/);assert.match(html,/320 B/);
    if(locale==='en'){assert.match(html,/Last sync: downloaded/);assert.doesNotMatch(html,/[\u3400-\u9fff]/);}
    h.run("state.busy=true;state.jobKind='index';rememberProgress({phase:'remote_syncing',completed:1,total:12202,unit:'files'})");
    const progress=h.run('progressPanel()');assert.doesNotMatch(progress,/remote_syncing/);
    if(locale==='en')assert.doesNotMatch(progress,/[\u3400-\u9fff]/);
    h.run('state.project.remote_source.downloaded_files=-1');assert.doesNotMatch(h.run('remoteSourceView()'),/remote-sync-summary/);
  }
});
