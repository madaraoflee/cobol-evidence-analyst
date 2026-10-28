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

const localWorkspace='a'.repeat(64),remoteWorkspace='b'.repeat(64),sharedId='c'.repeat(32);
function project(workspace=localWorkspace){return {workspace_id:workspace,source:workspace===localWorkspace?'/local/source':'/remote/source',
  output:workspace===localWorkspace?'/local/output':'/remote/output',source_origin:workspace===localWorkspace?'local_source':'remote_repository',
  snapshot_id:'sha256:'+workspace,programs:[],relations:{edges:[]},diagnosis:{catalog_ready:true,source_manifest_verified:true,
    source_options:{encoding:'utf-8',source_format:'fixed'},build_report:{files:{candidate:2}}}};}
function row(workspace,title){return {id:sharedId,workspace_id:workspace,source_origin:workspace===localWorkspace?'local_source':'remote_repository',
  source_label:workspace===localWorkspace?'Local billing':'Remote billing',title,message_count:2,created_at:'2030-01-01T10:00:00Z'};}
function conversation(title='Local processing'){return {id:sharedId,title,messages:[{id:'user',role:'user',content:'Explain the rule.',status:'completed'},
  {id:'answer',role:'assistant',content:'The rule checks the amount.',status:'completed',evidence_refs:[{evidence_id:'ev:rule',relative_path:'entry.cbl',start_line:3,end_line:5}]}]};}
function setup(h){h.run(`state.connected=true;state.apiConfigured=true;state.conversationSupported=true;applyProject(${JSON.stringify(project())});applyConversation(${JSON.stringify(conversation())});applyRecentConversations({recent_conversations:${JSON.stringify([row(localWorkspace,'Local processing'),row(remoteWorkspace,'Remote processing')])}});state.question='Unsent local draft';`);}

test('recent conversations are grouped by source with workspace-specific active and delete controls',()=>{
  for(const locale of ['zh-CN','zh-HK','en']){
    const h=harness(locale);setup(h);
    h.run(`state.recentConversations.push(${JSON.stringify({...row(remoteWorkspace,'<img>Unknown topic'),workspace_id:'d'.repeat(64),source_origin:'unrecognized',source_label:'<script>untrusted</script>'})})`);
    const html=h.run('recentConversationGroups()');
    const local=html.match(/<section[^>]*data-source-origin="local_source"[\s\S]*?<\/section>/)[0];
    const remote=html.match(/<section[^>]*data-source-origin="remote_repository"[\s\S]*?<\/section>/)[0];
    const other=html.match(/<section[^>]*data-source-origin="other"[\s\S]*?<\/section>/)[0];
    assert.match(local,/Local processing/);assert.match(local,/aria-current="true"/);assert.match(local,new RegExp('data-workspace="'+localWorkspace+'"'));
    assert.match(remote,/Remote processing/);assert.doesNotMatch(remote,/aria-current/);assert.match(remote,new RegExp('data-workspace="'+remoteWorkspace+'"'));
    assert.doesNotMatch(remote,/Unknown topic/);assert.match(other,/&lt;img&gt;Unknown topic/);assert.match(other,/&lt;script&gt;untrusted&lt;\/script&gt;/);
    if(locale==='en')assert.doesNotMatch(html,/[\u3400-\u9fff]/);
  }
});

test('empty groups remain visible and unsupported older services keep current conversations accessible',()=>{
  const h=harness();h.run('state.conversationSupported=true');
  const empty=h.run('recentConversationGroups()');assert.match(empty,/Local source/);assert.match(empty,/Bitbucket/);
  assert.equal((empty.match(/class="conversation-empty"/g)||[]).length,2);
  h.run(`applyProject(${JSON.stringify(project())});applyConversation(${JSON.stringify(conversation())})`);
  const fallback=h.run('recentConversationGroups()');assert.match(fallback,/Local processing/);
  assert.match(fallback,new RegExp('data-workspace="'+localWorkspace+'"'));
  h.run('state.project.workspace_id=undefined');assert.doesNotMatch(h.run('recentConversationGroups()'),/data-workspace/);
});

test('opening a grouped conversation restores its source before subsequent question options',async()=>{
  const h=harness();setup(h);const target=project(remoteWorkspace),remoteConversation=conversation('Remote processing');
  h.run("state.selectedEvidence='ev:old';state.evidence={private:true};state.conversationEvidence={private:true};state.impactPages={old:{}};");
  h.respond((url,options)=>{
    assert.equal(url,'/api/conversations/open');assert.equal(options.method,'POST');
    assert.deepEqual(JSON.parse(options.body),{workspace_id:remoteWorkspace,conversation_id:sharedId});
    return {project:target,conversation:remoteConversation,conversations:[row(remoteWorkspace,'Remote processing')],
      recent_conversations:[row(localWorkspace,'Local processing'),row(remoteWorkspace,'Remote processing')],source_preferences:{source:'/local/source',output:'/local/output'},
      framework_knowledge:{status:'LOADED',configured_path:'/reference/current'}};
  });
  await h.run(`openConversation('${sharedId}','${remoteWorkspace}')`);
  assert.equal(h.run('state.project.workspace_id'),remoteWorkspace);assert.equal(h.run('state.conversation.title'),'Remote processing');
  const question=JSON.parse(h.run("JSON.stringify(analysisOptions('Follow up'))"));
  assert.equal(question.source,target.source);assert.equal(question.output,target.output);assert.equal(question.conversation_id,sharedId);
  assert.equal(h.run('state.sourcePreferences.source'),'/local/source');assert.equal(h.run('state.frameworkKnowledge.configured_path'),'/reference/current');
  assert.equal(h.run('state.evidence'),null);assert.equal(h.run('state.conversationEvidence'),null);assert.equal(h.run('Object.keys(state.impactPages).length'),0);
  const html=h.get('conversation-list').innerHTML;assert.match(html,/Local processing/);assert.match(html,/Remote processing/);
});

test('a rejected or mismatched cross-source open retains the active project and draft',async()=>{
  for(const invalid of ['rejected','wrong-scope']){
    const h=harness();setup(h);h.respond(()=>invalid==='rejected'?{__status:404,error:{code:'CONVERSATION_NOT_FOUND'}}:{project:project(),conversation:conversation('Wrong response')});
    await h.run(`openConversation('${sharedId}','${remoteWorkspace}')`);
    assert.equal(h.run('state.project.workspace_id'),localWorkspace);assert.equal(h.run('state.conversation.title'),'Local processing');
    assert.equal(h.run('state.question'),'Unsent local draft');assert.equal(h.run('state.conversationLoading'),false);
    assert.equal(h.run('state.recentConversations.length'),2);
  }
});

test('deleting an identically named conversation from another workspace does not clear the current source',async()=>{
  const h=harness();setup(h);
  h.respond((url,options)=>{
    assert.equal(url,`/api/recent-conversations/${remoteWorkspace}/${sharedId}`);assert.equal(options.method,'DELETE');
    return {conversation:conversation(),conversations:[row(localWorkspace,'Local processing')],recent_conversations:[row(localWorkspace,'Local processing')]};
  });
  h.run(`promptDeleteConversation('${sharedId}','${remoteWorkspace}')`);
  assert.equal(h.get('delete-conversation-name').textContent,'Remote processing');
  await h.run('deleteConversation()');assert.equal(h.run('state.conversation.title'),'Local processing');
  assert.equal(h.run('state.project.workspace_id'),localWorkspace);assert.equal(h.run('state.question'),'Unsent local draft');
  assert.equal(h.run('state.recentConversations.length'),1);assert.equal(h.run('state.deletingConversationWorkspaceId'),null);
});

test('completed imports update both source groups and failed imports retain their earlier groups',async()=>{
  for(const outcome of ['COMPLETED','FAILED']){
    const h=harness();setup(h);const target=project(remoteWorkspace);
    const groups=[row(localWorkspace,'Local processing'),row(remoteWorkspace,outcome==='COMPLETED'?'Remote refreshed':'Remote processing')];
    h.respond(url=>url==='/api/source/remote'?{job_id:'source-job',source:target.source,output:target.output}:{status:outcome,kind:'index',
      result:outcome==='COMPLETED'?{...target,conversations:[groups[1]],recent_conversations:groups}:null,
      error:outcome==='FAILED'?{code:'REMOTE_SOURCE_NETWORK_FAILED'}:null});
    await h.run("startRemoteSource({url:'https://code.example.invalid/projects/DEMO/repos/source/browse'})");
    assert.equal(h.run('state.recentConversations.length'),2);
    const html=h.get('conversation-list').innerHTML;assert.match(html,/Local processing/);
    assert.match(html,outcome==='COMPLETED'?/Remote refreshed/:/Remote processing/);
    assert.equal(h.run('state.project.workspace_id'),outcome==='COMPLETED'?remoteWorkspace:localWorkspace);
  }
});

test('new conversations refresh grouped summaries while empty titles stay out of recent lists',async()=>{
  const h=harness();setup(h);const empty={id:'e'.repeat(32),title:'新对话',messages:[]};
  h.respond(()=>({conversation:empty,conversations:[row(localWorkspace,'Local processing')],recent_conversations:[row(localWorkspace,'Local processing'),row(remoteWorkspace,'Remote processing'),
    {...row(localWorkspace,'新对话'),id:empty.id,message_count:0}]}));
  await h.run('newConversation()');assert.equal(h.run('state.conversation.id'),empty.id);
  const html=h.get('conversation-list').innerHTML;assert.match(html,/Remote processing/);assert.doesNotMatch(html,new RegExp(empty.id));
  assert.equal(h.run('recentConversationRows().length'),2);
});

test('cross-source history restoration blocks import starts until its source is ready and preserves the prompt draft',async()=>{
  const h=harness();setup(h);h.run("state.answerInstructions='Saved writing requirements';showSettings()");
  h.get('answer-instructions').value='An unsaved writing draft';let finish;
  h.respond(url=>{assert.equal(url,'/api/conversations/open');return new Promise(resolve=>{finish=resolve;});});
  const opening=h.run(`openConversation('${sharedId}','${remoteWorkspace}')`);
  h.run("openRemoteSource();openSource('switch')");assert.equal(h.get('remote-source-dialog').open,false);assert.equal(h.get('source-dialog').open,false);
  await h.run("startRemoteSource({url:'https://code.example.invalid/projects/DEMO/repos/source',token:'temporary-secret'})");
  await h.run("startJob({source:'/new/source',output:'/new/output',allow_network:false})");
  assert.equal(h.requests.length,1);assert.equal(h.run('state.busy'),false);
  finish({project:project(remoteWorkspace),conversation:conversation('Remote processing')});await opening;
  assert.equal(h.get('answer-instructions').value,'An unsaved writing draft');assert.equal(h.run('state.answerInstructions'),'Saved writing requirements');
  assert.equal(h.run('state.project.workspace_id'),remoteWorkspace);
});

test('late impact pagination from the old workspace cannot populate a restored conversation with shared message ids',async()=>{
  const h=harness();setup(h);h.run("state.conversation.messages[1].impact_result={handle:'old-handle',next_cursor:1,rows:[{name:'old-first'}]}");let finish;
  h.respond(url=>url.startsWith('/api/impact?')?new Promise(resolve=>{finish=resolve;}):
    {project:project(remoteWorkspace),conversation:conversation('Remote processing')});
  const loading=h.run("loadImpactPage('answer')");
  await h.run(`openConversation('${sharedId}','${remoteWorkspace}')`);
  finish({handle:'old-handle',next_cursor:null,rows:[{name:'old-private-result'}]});await loading;
  assert.equal(h.run('Object.keys(state.impactPages).length'),0);
  assert.equal(h.run('state.conversation.title'),'Remote processing');assert.doesNotMatch(h.get('page-content').innerHTML,/old-private-result/);
});
