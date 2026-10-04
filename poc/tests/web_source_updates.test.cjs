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
  const get=id=>{if(!elements.has(id))elements.set(id,{id,value:'',checked:false,open:false,hidden:false,innerHTML:'',textContent:'',disabled:false,
    classList:{toggle(){},contains(){return false;}},setAttribute(){},removeAttribute(){},addEventListener:(name,handler)=>events[id+':'+name]=handler,
    showModal(){this.open=true;},close(){this.open=false;},getClientRects(){return [{}];},focus(){}});return elements.get(id);};
  const document={documentElement:{classList:{toggle(){}}},createTreeWalker:()=>({nextNode:()=>false}),querySelectorAll:()=>[],querySelector:()=>null,
    getElementById:get,addEventListener:(name,handler)=>events[name]=handler};
  const context=vm.createContext({document,NodeFilter:{SHOW_TEXT:4},localStorage:{getItem:()=>locale,setItem(){}},
    fetch:async(url,options)=>{requests.push({url,options});const data=await responder(url,options);const status=data.__status || 200;
      return {ok:status>=200 && status<300,status,json:async()=>data};},setTimeout:()=>1,clearTimeout(){},setInterval:()=>1,clearInterval(){}});
  for(const file of ['i18n.js','marked.umd.js','markdown.js'])vm.runInContext(fs.readFileSync(path.join(web,file),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(web,'app.js'),'utf8').replace(/render\(\);initialize\(\);\s*$/,''),context);
  return {run:code=>vm.runInContext(code,context),get,events,requests,respond:fn=>responder=fn};
}
function version(hash='a',sourceKey='source-one'){
  return {version_id:'local:'+hash.repeat(64),snapshot_id:'sha256:'+hash.repeat(64),created_at:'2026-10-04T01:00:00Z',checked_at:'2026-10-04T02:00:00Z',
    provider:'local',file_count:2,changes:{added:1,modified:2,removed:3,unchanged:4},source_key:sourceKey};
}
function project(hash='a',source='/local/source',sourceKey='source-one'){
  const v=version(hash,sourceKey);
  return {source,output:'/local/output',snapshot_id:v.snapshot_id,source_version:v,source_versions:[v],programs:[{program_name:'REQUEST',relative_path:'request.cbl'}],
    diagnosis:{catalog_ready:true,source_manifest_verified:true,runner_status:'INDEX_READY',scope:{mode:'repository_index'},repository_search:{full_text_complete:true},
      build_report:{files:{candidate:2}},source_options:{encoding:'cp950',source_format:'fixed',extensions:['.cbl','.cpy'],reading_strategy:'retrieval'}},agent:null,relations:{edges:[]}};
}
function conversation(){return {id:'old-conversation',title:'Existing question',snapshot_id:version().snapshot_id,
  messages:[{id:'old-answer',role:'assistant',status:'completed',snapshot_id:version().snapshot_id,content:'Original answer. [ev:old]',
    evidence_refs:[{evidence_id:'ev:old',relative_path:'request.cbl',start_line:1,end_line:2}]}]};}
function setup(h){h.run(`state.connected=true;state.conversationSupported=true;state.mode='real';applyProject(${JSON.stringify(project())});applyConversation(${JSON.stringify(conversation())});state.question='Unsent follow-up';`);}
function indexOptions(source='/local/source'){return {source,output:'/local/output',index_mode:'catalog',allow_network:false,verify_content:false};}

test('an existing source has explicit update and switch actions, and reopening preserves import settings',()=>{
  const h=harness();setup(h);const html=h.run('renderSources()');
  assert.match(html,/data-source-update[^>]*>Update local source/);assert.match(html,/data-source-switch[^>]*>Switch source folder/);
  h.run('openSource()');assert.equal(h.get('source-dialog-title').textContent,'Update local source');assert.equal(h.get('index-submit').textContent,'Update index and version');
  assert.equal(h.get('source-path').value,'/local/source');assert.equal(h.get('output-path').value,'/local/output');
  assert.equal(h.get('source-encoding').value,'cp950');assert.equal(h.get('source-format').value,'fixed');assert.equal(h.get('source-extensions').value,'.cbl,.cpy');
  h.run("openSource('switch')");assert.equal(h.get('source-dialog-title').textContent,'Switch source folder');
  const first=harness();first.run('state.connected=true;openSource()');assert.equal(first.get('source-dialog-title').textContent,'Connect local source');
});

test('the actual update form sends a full local verification without a model call',async()=>{
  const h=harness();setup(h);h.run('openSource()');h.get('verify-content').checked=false;
  h.respond(url=>url==='/api/analyze'?{job_id:'source-job'}:{status:'COMPLETED',result:{...project('b'),conversations:[{id:'old-conversation',title:'Existing question',message_count:1}]}});
  await h.events['source-form:submit']({preventDefault(){}});
  const request=JSON.parse(h.requests.find(item=>item.url==='/api/analyze').options.body);
  assert.equal(request.verify_content,true);assert.equal(request.allow_network,false);assert.equal(request.index_mode,'catalog');
  assert.equal(request.source,'/local/source');assert.equal(request.output,'/local/output');assert.equal(request.question,undefined);
  assert.equal(h.requests.some(item=>item.url==='/api/state'),false);assert.equal(h.run('state.project.snapshot_id'),version('b').snapshot_id);
});

test('a rejected update retains the usable source, current conversation, draft and references',async()=>{
  const h=harness();setup(h);h.respond(()=>({__status:409,error:{code:'INVALID_PATH'}}));
  await h.run(`startJob(${JSON.stringify(indexOptions())})`);
  assert.equal(h.run('sourceUsable()'),true);assert.equal(h.run('state.project.snapshot_id'),version().snapshot_id);
  assert.equal(h.run('state.conversation.id'),'old-conversation');assert.equal(h.run('state.question'),'Unsent follow-up');
  assert.match(h.run('conversationWorkbench()'),/Original answer|data-turn-evidence="ev:old"/);
  assert.equal(h.run('state.busy'),false);assert.equal(h.run('state.sourceJobBackup'),null);
});

test('a source update failure keeps its local cause through HTTP and background errors while restoring prior context',async()=>{
  const privateValue='PRIVATE-SOURCE-UPDATE-MARKER';
  for(const locale of ['en','zh-CN','zh-HK']){
    for(const transport of ['http','background']){
      const h=harness(locale);setup(h);
      const diagnostic={schema_version:'safe-api-error/v1',category:'source_encoding_invalid',evidence_source:'local',
        request_id:'local-'+'a'.repeat(32),reason:privateValue,next_step:privateValue,source_path:privateValue};
      const error={code:'SOURCE_UPDATE_FAILED',message:privateValue,diagnostic};
      h.respond(url=>transport==='http'?{__status:500,error}:url==='/api/analyze'?{job_id:'source-job'}:{status:'FAILED',error,
        result:{source:'/other/source',output:'/local/output',diagnosis:null,programs:[],snapshot_id:null}});
      await h.run(`startJob(${JSON.stringify(indexOptions())})`);
      assert.equal(h.run('state.errorDiagnostic.category'),'source_encoding_invalid',locale+' '+transport);
      assert.equal(h.run('state.errorDiagnostic.evidence_source'),'local');
      assert.match(h.run('state.error'),locale==='en'?/selected encoding/:locale==='zh-CN'?/所选编码/:/所選編碼/);
      assert.doesNotMatch(h.run('state.error'),/REQUEST_FAILED|PRIVATE-SOURCE-UPDATE-MARKER/);
      const html=h.run('conversationWorkbench()');
      assert.match(html,locale==='en'?/selected encoding/:locale==='zh-CN'?/所选编码/:/所選編碼/);
      assert.doesNotMatch(html,/REQUEST_FAILED|PRIVATE-SOURCE-UPDATE-MARKER/);
      assert.equal(h.run('state.project.snapshot_id'),version().snapshot_id);
      assert.equal(h.run('state.conversation.id'),'old-conversation');assert.equal(h.run('state.question'),'Unsent follow-up');
      assert.match(html,/Original answer|data-turn-evidence="ev:old"/);
      assert.equal(h.run('state.busy'),false);assert.equal(h.run('state.sourceJobBackup'),null);
    }
  }
});

test('a failed rollback revokes source authority, keeps the draft and displays its warning with the safe local cause',async()=>{
  const privateValue='PRIVATE-ROLLBACK-RECOVERY-MARKER';
  const warnings={en:/source update and recovery did not complete/,'zh-CN':/源码更新与恢复均未完成/,'zh-HK':/源碼更新與恢復均未完成/};
  const causes={en:/Could not write local analysis files/,'zh-CN':/无法写入本机分析文件/,'zh-HK':/無法寫入本機分析文件/};
  for(const locale of ['en','zh-CN','zh-HK']){
    for(const transport of ['job-scope','job-paths','target','http']){
      const h=harness(locale);setup(h);
      h.run("state.evidence={source_text:'Old evidence'};state.selectedEvidence='old-evidence';state.conversationEvidence={span:{source_text:'Old evidence'}};state.impactPages={old:{rows:['Old impact']}};");
      const options={...indexOptions('/import/source'),output:'/import/output'};
      const error={code:'SOURCE_UPDATE_ROLLBACK_FAILED',message:privateValue,recovery_directory:privateValue,
        diagnostic:{schema_version:'safe-api-error/v1',category:'storage_permission',evidence_source:'local',
          request_id:'local-'+'a'.repeat(32),reason:privateValue,next_step:privateValue,raw_body:privateValue}};
      const scope=transport==='job-scope'?{scope:{source:'/blocked/source',output:'/blocked/output'},source:'/unused/source',output:'/unused/output'}:
        transport==='job-paths'?{source:'/blocked/source',output:'/blocked/output'}:{};
      h.respond(url=>transport==='http'?{__status:500,error}:url==='/api/analyze'?{job_id:'source-job'}:
        {...scope,status:'FAILED',error,result:{...project('b'),conversation:conversation()},conversation:conversation()});
      await h.run(`startJob(${JSON.stringify(options)})`);
      const expectedSource=transport.startsWith('job-')?'/blocked/source':'/import/source';
      const expectedOutput=transport.startsWith('job-')?'/blocked/output':'/import/output';
      assert.deepEqual(JSON.parse(h.run('JSON.stringify(state.project)')),{source:expectedSource,output:expectedOutput,
        snapshot_id:null,diagnosis:null,programs:[],agent:null,relations:{edges:[]},restore_blocked:true});
      assert.equal(h.run('sourceUsable()'),false);assert.equal(h.run('state.question'),'Unsent follow-up');
      assert.equal(h.run('state.conversation'),null);assert.equal(h.run('state.conversations.length'),0);
      for(const key of ['sourceJobBackup','sourceJobTarget','evidence','selectedEvidence','conversationEvidence','pendingMessage'])assert.equal(h.run('state.'+key),null,key);
      assert.equal(h.run('Object.keys(state.impactPages).length'),0);
      assert.equal(h.run('state.busy'),false);assert.equal(h.run('state.errorDiagnostic.category'),'storage_permission');
      const html=h.get('page-content').innerHTML;
      assert.match(html,warnings[locale]);assert.match(html,causes[locale]);assert.match(html,/type="submit" disabled/);
      assert.doesNotMatch(html,/Original answer|Old evidence|Old impact|PRIVATE-ROLLBACK-RECOVERY-MARKER/);
      assert.doesNotMatch(h.run('JSON.stringify(state)'),new RegExp(privateValue));
      h.run("state.page='sources';render()");
      assert.match(h.get('notice').textContent,warnings[locale]);assert.match(h.get('notice').textContent,causes[locale]);
      assert.doesNotMatch(h.get('notice').textContent,new RegExp(privateValue));
      h.run('restoreSourceJob()');assert.equal(h.run('sourceUsable()'),false);assert.equal(h.run('state.project.snapshot_id'),null);
      const requests=h.requests.length;
      await h.events.submit({target:{id:'question-form'},preventDefault(){}});
      assert.equal(h.requests.length,requests,'a blocked source must not send a question');
      assert.equal(h.run('state.question'),'Unsent follow-up');
      if(locale==='en')assert.doesNotMatch(html+h.get('notice').textContent,/[\u3400-\u9fff]/);
    }
  }
});

test('a failed or cancelled background update cannot replace the restored source with an incomplete result',async()=>{
  for(const status of ['FAILED','CANCELLED']){
    const h=harness();setup(h);
    h.respond(url=>url==='/api/analyze'?{job_id:'source-job'}:{status,error:{code:'INDEX_UNAVAILABLE'},
      result:{source:'/other/source',output:'/local/output',diagnosis:null,programs:[],snapshot_id:null}});
    await h.run(`startJob(${JSON.stringify(indexOptions())})`);
    assert.equal(h.run('sourceUsable()'),true,status);assert.equal(h.run('state.project.source'),'/local/source',status);
    assert.equal(h.run('state.conversation.id'),'old-conversation',status);assert.equal(h.run('state.question'),'Unsent follow-up',status);
  }
});

test('a running update keeps existing context available while disabling further questions',async()=>{
  const h=harness();setup(h);h.respond(url=>url==='/api/analyze'?{job_id:'source-job'}:{status:'RUNNING',progress:{phase:'verifying',completed:1,total:2,unit:'files'}});
  await h.run(`startJob(${JSON.stringify(indexOptions())})`);
  assert.equal(h.run('state.busy'),true);assert.equal(h.run('sourceUsable()'),true);assert.equal(h.run('state.conversation.id'),'old-conversation');
  assert.match(h.run('conversationComposer()'),/type="submit" disabled/);assert.match(h.run('renderWorkbench()'),/id="job-progress"/);
});

test('successive asynchronous import polls update the full panel even with an existing chat workspace',async()=>{
  const h=harness();setup(h);
  const progress=[{phase:'preparing',completed:0,total:null,unit:'files'},
    {phase:'backing_up',completed:2048,total:8192,unit:'bytes',bytes_completed:2048,current_file:'source-index.sqlite',file_completed:2048,file_total:8192,file_unit:'bytes'},
    {phase:'discovery',completed:37,total:null,unit:'files',current_file:'source/batch'},
    {phase:'discovery',completed:129,total:null,unit:'files',current_file:'source/screens'},
    {phase:'catalog',completed:6000,total:12000,unit:'files',current_file:'request.cbl'}];
  let round=0;h.respond(url=>url==='/api/analyze'?{job_id:'source-job'}:{status:'RUNNING',progress:progress[round++]});
  await h.run(`startJob(${JSON.stringify(indexOptions())})`);
  assert.equal(h.get('job-phase-name').textContent,'Preparing');
  await h.run("pollJob('source-job')");
  assert.equal(h.get('job-phase-name').textContent,'Preserving the previous version');assert.equal(h.get('job-percent').textContent,'25.0%');
  assert.match(h.get('job-counts').textContent,/2,048 \/ 8,192 bytes/);assert.equal(h.get('job-file').textContent,'source-index.sqlite');
  assert.match(h.get('job-file-lines').textContent,/Copied in the current file 2,048 \/ 8,192 bytes/);assert.doesNotMatch(h.get('job-file-lines').textContent,/lines/);
  assert.equal(h.get('job-bytes').textContent,'2.0 KiB');assert.match(h.get('job-progress-explanation').textContent,/not overall intake completion/);
  await h.run("pollJob('source-job')");assert.match(h.get('job-counts').textContent,/Scanned 37 files/);assert.equal(h.get('job-percent').textContent,'Counting');
  assert.doesNotMatch(h.run('progressPanel()'),/<progress[^>]*value=/);assert.match(h.get('job-progress-explanation').textContent,/total is known after scanning finishes/);
  await h.run("pollJob('source-job')");assert.match(h.get('job-counts').textContent,/Scanned 129 files/);assert.equal(h.get('job-file').textContent,'source/screens');
  await h.run("pollJob('source-job')");assert.match(h.get('job-counts').textContent,/6,000 \/ 12,000 files/);assert.equal(h.get('job-percent').textContent,'50.0%');
  assert.equal(h.run('state.conversation.id'),'old-conversation');assert.equal(h.run('state.project.snapshot_id'),version().snapshot_id);
});

test('the pending import scope is explicit while the previous version and conversation remain intact',async()=>{
  const h=harness();setup(h);h.respond(url=>url==='/api/analyze'?{job_id:'source-job'}:{status:'RUNNING',progress:{phase:'discovery',completed:12,total:null,unit:'files'}});
  await h.run(`startJob(${JSON.stringify(indexOptions('/other/new-source'))})`);
  const html=h.run('renderWorkbench()');assert.match(html,/Source for this import/);assert.match(html,/\/other\/new-source/);assert.match(html,/Output folder for this import/);
  assert.match(html,/Previous source:<code>\/local\/source/);assert.equal(h.get('project-title').textContent,'new-source');
  assert.match(h.get('project-description').textContent,/previous version is retained/);assert.match(h.get('snapshot-info').innerHTML,/new version is not ready/);
  assert.equal(h.run('state.project.source'),'/local/source');assert.equal(h.run('state.conversation.id'),'old-conversation');
  assert.match(h.run('renderSources()'),/job-progress|\/other\/new-source/);assert.doesNotMatch(h.run('renderSources()'),/Program catalog|business search index is ready/);
});

test('reload restores an active index job from its own scope metadata and keeps polling full import progress',async()=>{
  const h=harness();const job={kind:'index',source:'/other/new-source',output:'/other/output',status:'RUNNING',progress:{phase:'backing_up',completed:1024,total:4096,unit:'bytes',current_file:'source-index.sqlite'}};
  h.respond(url=>url==='/api/state'?{session_token:'test',project:project(),conversation:conversation(),conversations:[conversation()],active_job_id:'source-job',job}:
    url==='/api/jobs/source-job'?{...job,progress:{phase:'discovery',completed:80,total:null,unit:'files'}}:
    url==='/api/framework-demo'?frameworkDemoFixture():assert.fail(url));
  await h.run('initialize()');
  assert.equal(h.run('state.jobKind'),'index');assert.equal(h.run('state.sourceJobTarget.output'),'/other/output');
  assert.match(h.get('page-content').innerHTML,/id="job-progress"/);assert.match(h.get('page-content').innerHTML,/Scanned 80 files/);
  assert.match(h.get('page-content').innerHTML,/\/other\/new-source|\/other\/output/);assert.doesNotMatch(h.get('page-content').innerHTML,/id="conversation-progress"/);
  assert.equal(h.run('state.conversation.id'),'old-conversation');assert.equal(h.run('state.project.source'),'/local/source');
});

test('a reloaded failed index job restores the previous version instead of applying an incomplete new result',async()=>{
  const h=harness();const job={kind:'index',source:'/other/new-source',output:'/other/output',status:'RUNNING',progress:{phase:'discovery',completed:10,total:null,unit:'files'}};
  h.respond(url=>url==='/api/state'?{session_token:'test',project:project(),conversation:conversation(),conversations:[conversation()],active_job_id:'source-job',job}:
    url==='/api/jobs/source-job'?{status:'FAILED',error:{code:'INDEX_UNAVAILABLE'},result:{source:'/other/new-source',diagnosis:null,programs:[]}}:
    url==='/api/framework-demo'?frameworkDemoFixture():assert.fail(url));
  await h.run('initialize()');
  assert.equal(h.run('state.busy'),false);assert.equal(h.run('sourceUsable()'),true);assert.equal(h.run('state.project.source'),'/local/source');
  assert.equal(h.run('state.conversation.id'),'old-conversation');assert.equal(h.run('state.sourceJobTarget'),null);assert.match(h.run('renderWorkbench()'),/Original answer/);
});

test('reload keeps a blocked rollback scope unusable and restores its warning even without a retained job',async()=>{
  const privateValue='PRIVATE-RELOADED-ROLLBACK-MARKER';
  const warnings={en:/source update and recovery did not complete/,'zh-CN':/源码更新与恢复均未完成/,'zh-HK':/源碼更新與恢復均未完成/};
  const causes={en:/Local storage is full/,'zh-CN':/本机存储空间不足/,'zh-HK':/本機儲存空間不足/};
  for(const locale of ['en','zh-CN','zh-HK']){
    for(const retainedJob of [false,true]){
      const h=harness(locale);
      const job=retainedJob?{kind:'index',status:'FAILED',source:'/blocked/source',output:'/blocked/output',error:{code:'SOURCE_UPDATE_ROLLBACK_FAILED',
        message:privateValue,recovery_directory:privateValue,diagnostic:{schema_version:'safe-api-error/v1',category:'storage_full',evidence_source:'local',
          request_id:'local-'+'a'.repeat(32),reason:privateValue,next_step:privateValue}}}:null;
      h.respond(url=>url==='/api/state'?{session_token:'test',api_configured:true,project:{source:'/blocked/source',output:'/blocked/output',restore_blocked:true},
        conversation:null,conversations:[],active_job_id:null,job}:url==='/api/framework-demo'?frameworkDemoFixture():assert.fail(url));
      await h.run('initialize()');
      assert.equal(h.run('sourceUsable()'),false);assert.equal(h.run('state.project.snapshot_id'),null);assert.equal(h.run('state.project.restore_blocked'),true);
      assert.equal(h.run('state.conversation'),null);assert.equal(h.run('state.sourceJobBackup'),null);
      const html=h.get('page-content').innerHTML;assert.match(html,warnings[locale]);assert.match(html,/type="submit" disabled/);
      if(retainedJob){assert.match(html,causes[locale]);assert.equal(h.run('state.errorDiagnostic.category'),'storage_full');}
      else assert.equal(h.run('state.errorDiagnostic'),null);
      assert.doesNotMatch(html,new RegExp(privateValue));assert.doesNotMatch(h.run('JSON.stringify(state)'),new RegExp(privateValue));
      if(locale==='en')assert.doesNotMatch(html,/[\u3400-\u9fff]/);
      const requests=h.requests.length;h.run("state.question='Kept local draft'");
      await h.events.submit({target:{id:'question-form'},preventDefault(){}});
      assert.equal(h.requests.length,requests);assert.equal(h.run('state.question'),'Kept local draft');
    }
  }
});

test('reload preserves the compact chat progress for an active question job',async()=>{
  const h=harness();const job={kind:'question',source:'/local/source',output:'/local/output',status:'RUNNING',progress:{phase:'retrieving',completed:1,total:4,unit:'requests'}};
  h.respond(url=>url==='/api/state'?{session_token:'test',project:project(),conversation:conversation(),conversations:[conversation()],active_job_id:'question-job',job}:
    url==='/api/jobs/question-job'?job:url==='/api/framework-demo'?frameworkDemoFixture():assert.fail(url));
  await h.run('initialize()');assert.equal(h.run('state.jobKind'),'question');assert.equal(h.run('state.sourceJobBackup'),null);
  assert.match(h.get('page-content').innerHTML,/id="conversation-progress"/);assert.doesNotMatch(h.get('page-content').innerHTML,/id="job-progress"/);
  assert.match(h.get('page-content').innerHTML,/Original answer/);assert.doesNotMatch(h.get('page-content').innerHTML,/25.0%/);
});

test('intake stages, unknown totals and backup page counts are readable in every locale without overall percentages',()=>{
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale);setup(h);h.run("state.busy=true;state.jobKind='index';state.sourceJobTarget={source:'/source/<img>',output:'/output'}");
    for(const phase of ['backing_up','framework_reference','source_version']){
      h.run(`rememberProgress({phase:'${phase}',completed:2,total:8,unit:'pages',file_completed:2,file_total:8,file_unit:'pages',current_file:'source-index.sqlite'})`);
      const html=h.run('progressPanel()');assert.match(html,/&lt;img&gt;/);assert.doesNotMatch(html,/<img>/);
      if(locale==='en'){assert.doesNotMatch(html,/[\u3400-\u9fff]/);assert.match(html,/2 \/ 8 pages/);}
    }
    h.run("rememberProgress({phase:'discovery',completed:12000,total:null,unit:'files',eta_seconds:null})");
    const scanning=h.run('progressPanel()');assert.doesNotMatch(scanning,/<progress[^>]*value=|NaN|Infinity|\d+(?:\.\d+)?%/);
    if(locale==='en'){assert.match(scanning,/Scanned 12,000 files/);assert.match(scanning,/No overall completion percentage/);assert.doesNotMatch(scanning,/[\u3400-\u9fff]/);}
    h.run("rememberProgress({phase:'reading',completed:0,total:1,unit:'files',file_completed:4096,file_total:8192,file_unit:'bytes'})");
    if(locale==='en'){assert.match(h.run('fileProgress(progressViewModel())'),/Processed in the current file 4,096 \/ 8,192 bytes/);assert.doesNotMatch(h.run('fileProgress(progressViewModel())'),/lines/);}
  }
});

test('same-source success uses the job conversation list and preserves the current dialogue and draft',async()=>{
  const h=harness();setup(h);const current=project('b');current.source_versions=[version('b'),version()];
  h.respond(url=>url==='/api/analyze'?{job_id:'source-job'}:{status:'COMPLETED',result:{...current,conversations:[{id:'old-conversation',title:'Existing question',message_count:1}]}});
  await h.run(`startJob(${JSON.stringify(indexOptions())})`);
  assert.equal(h.requests.length,2);assert.equal(h.run('state.project.source_versions.length'),2);
  assert.equal(h.run('state.conversation.id'),'old-conversation');assert.equal(h.run('state.conversation.messages[0].content'),'Original answer. [ev:old]');
  assert.equal(h.run('state.question'),'Unsent follow-up');assert.equal(h.run('state.conversations[0].id'),'old-conversation');
});

test('switching the source root isolates the prior dialogue, drafts and recent list',async()=>{
  const h=harness();setup(h);const switched=project('c','/other/source','source-two');
  h.respond(url=>url==='/api/analyze'?{job_id:'source-job'}:{status:'COMPLETED',result:{...switched,conversation:null,conversations:[]}});
  await h.run(`startJob(${JSON.stringify(indexOptions('/other/source'))})`);
  assert.equal(h.run('state.project.source'),'/other/source');assert.equal(h.run('state.conversation'),null);
  assert.equal(h.run('state.conversations.length'),0);assert.equal(h.run('state.question'),'');assert.doesNotMatch(h.run('renderWorkbench()'),/Original answer/);
});

test('version history is scoped to the current source root and capped, with actual change counts',()=>{
  const h=harness();setup(h);const current=version('b');const history=[current,version('c','other-root'),...Array.from({length:25},(_,i)=>({...version('a'),version_id:'local:history-'+i}))];
  h.run(`state.project.source_version=${JSON.stringify(current)};state.project.source_versions=${JSON.stringify(history)};`);
  const html=h.run('sourceVersionView()');assert.match(html,/Local source versions|Last checked|Version history · 20/);
  assert.match(html,/Added 1 · Changed 2 · Removed 3/);assert.doesNotMatch(html,/cccccccccccc|history-19|history-24/);
  assert.doesNotMatch(html,/2026-10-04T02:00:00Z/);assert.equal((html.match(/class="history-card"/g)||[]).length,20);
  const simplified=harness('zh-CN');setup(simplified);assert.match(simplified.run('sourceVersionView()'),/2 个文件/);assert.doesNotMatch(simplified.run('sourceVersionView()'),/個文件/);
});

test('unchanged source retains its version and shows the new check time without inventing a history entry',async()=>{
  const h=harness();setup(h);const current=project();current.source_version.checked_at='2026-10-05T03:00:00Z';current.source_versions=[current.source_version];
  h.respond(url=>url==='/api/analyze'?{job_id:'source-job'}:{status:'COMPLETED',result:{...current,conversations:[]}});
  await h.run(`startJob(${JSON.stringify(indexOptions())})`);
  assert.equal(h.run('state.project.source_version.version_id'),version().version_id);assert.equal(h.run('state.project.source_versions.length'),1);
  assert.equal(h.run('state.project.source_version.checked_at'),'2026-10-05T03:00:00Z');
});

test('earlier answers expose their source version without changing their text or archived citation link',()=>{
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale);setup(h);h.run(`state.project.snapshot_id=${JSON.stringify(version('b').snapshot_id)};`);
    const html=h.run('conversationWorkbench()');assert.match(html,/message-source-version/);assert.match(html,/aaaaaaaaaaaa/);
    assert.match(html,/Original answer|data-turn-evidence="ev:old"/);assert.equal(h.run('state.conversation.messages[0].content'),'Original answer. [ev:old]');
    if(locale==='en'){assert.match(html,/earlier local version/);assert.doesNotMatch(html,/[\u3400-\u9fff]/);}
    if(locale==='zh-CN')assert.match(html,/此回答依据较早的本地版本/);
  }
});

function frameworkProject(){
  const p=project();
  const edge={source_program:'REQUEST',target_name:'COMMON-SERVICE',relation_type:'CALLS',status:'unresolved',
    relative_path:'request.cbl',start_line:12,end_line:14};
  const fact={kind:'framework_operation',dependency_covered:true,relation_type:'CALLS',program_name:'REQUEST',target_name:'COMMON-SERVICE',
    relative_path:'request.cbl',start_line:12,end_line:14,operation:{meaning:'Fetch the requested record.',caveat:'The returned value needs runtime confirmation.'}};
  p.relations.edges=[edge];p.agent={agent_result:{snapshot_id:p.snapshot_id,framework_context:{facts:[fact]}}};
  p.diagnosis.unresolved_dependencies=[edge];
  return p;
}

test('a framework-covered call shows its operation only for the current source snapshot and exact callsite',()=>{
  const h=harness();const p=frameworkProject();h.run(`state.mode='real';applyProject(${JSON.stringify(p)})`);
  const html=h.run('relationsView()');assert.match(html,/Framework operation identified/);assert.match(html,/Fetch the requested record/);
  assert.match(html,/returned value needs runtime confirmation/);assert.match(html,/does not confirm this call&#39;s returned values or actual execution outcome/);
  assert.doesNotMatch(html,/Source relation confirmed/);assert.equal(h.run('state.project.relations.edges[0].status'),'unresolved');
});

test('an older or missing snapshot never applies framework coverage to current relations',()=>{
  for(const snapshot of [version('b').snapshot_id,null,undefined]){
    const h=harness();const p=frameworkProject();p.agent.agent_result.snapshot_id=snapshot;
    if(snapshot===undefined)delete p.snapshot_id;
    h.run(`state.mode='real';applyProject(${JSON.stringify(p)})`);
    assert.equal(h.run('relationFrameworkOperation(currentRelations()[0])'),null);
    assert.doesNotMatch(h.run('relationsView()'),/Framework operation identified|Fetch the requested record/);
  }
});

test('same-named calls do not share framework coverage across paths, programs, targets, lines or relation types',()=>{
  const mismatches=[{relative_path:'other.cbl'},{program_name:'OTHER'},{target_name:'OTHER-SERVICE'},
    {start_line:13},{end_line:15},{relation_type:'INCLUDES_COPY'},{dependency_covered:false}];
  for(const change of mismatches){
    const h=harness();const p=frameworkProject();Object.assign(p.agent.agent_result.framework_context.facts[0],change);
    h.run(`state.mode='real';applyProject(${JSON.stringify(p)})`);
    assert.equal(h.run('relationFrameworkOperation(currentRelations()[0])'),null,JSON.stringify(change));
    assert.doesNotMatch(h.run('relationsView()'),/Framework operation identified|Fetch the requested record/,JSON.stringify(change));
  }
});

test('framework meanings are displayed as escaped data with translated status and runtime limits',()=>{
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale);const p=frameworkProject();p.agent.agent_result.framework_context.facts[0].operation={meaning:'Read <img src=x>.',caveat:'Return <script>unknown</script>.'};
    h.run(`state.mode='real';applyProject(${JSON.stringify(p)})`);const html=h.run('relationsView()');
    assert.match(html,/Read &lt;img src=x&gt;/);assert.match(html,/Return &lt;script&gt;unknown&lt;\/script&gt;/);assert.doesNotMatch(html,/<img|<script>/);
    if(locale==='en'){assert.match(html,/Framework operation identified/);assert.doesNotMatch(html,/[\u3400-\u9fff]/);}
    if(locale==='zh-CN')assert.match(html,/框架操作已识别/);
    if(locale==='zh-HK')assert.match(html,/框架操作已識別/);
  }
});

test('the dependency list keeps raw callsites but asks for material only for uncovered internal behaviour',()=>{
  const h=harness();const p=frameworkProject();h.run(`state.mode='real';applyProject(${JSON.stringify(p)})`);
  const html=h.run('dependencyGaps()');assert.match(html,/COMMON-SERVICE|request.cbl/);
  assert.match(html,/Public calls can be explained using matched framework conventions; only uncovered internal behaviour needs additional material/);
  assert.doesNotMatch(html,/Provide the corresponding source|Please provide/);
  assert.doesNotMatch(h.run('scopeNotice()'),/unresolved dependencies/);
});
