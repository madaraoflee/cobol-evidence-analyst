'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const web=path.join(__dirname,'../web');
const flush=async()=>{await new Promise(resolve=>setImmediate(resolve));await new Promise(resolve=>setImmediate(resolve));};

function environment(locale){
  const elements=new Map(),events={},requests=[],downloads=[];
  const element=id=>{
    if(!elements.has(id))elements.set(id,{id,value:'',hidden:false,disabled:false,innerHTML:'',textContent:'',dataset:{},children:[],isConnected:true,
      classList:{toggle(){},contains(){return false;}},addEventListener:(name,callback)=>events[id+':'+name]=callback,
      append(child){this.children.push(child);},prepend(child){this.children.unshift(child);},setAttribute(name,value){this[name]=value;},getAttribute(name){return this[name]??null;},
      showModal(){this.open=true;},close(){this.open=false;},reset(){},focus(){},click(){},remove(){},getClientRects(){return [];}});
    return elements.get(id);
  };
  const document={documentElement:{classList:{toggle(){}}},body:{append(){}},title:'',getElementById:element,
    createElement:()=>({click(){},remove(){}}),querySelectorAll:()=>[],querySelector:()=>null,
    createTreeWalker:()=>({nextNode:()=>false}),addEventListener:(name,callback)=>events[name]=callback};
  class LocalURL extends URL {}
  LocalURL.createObjectURL=blob=>{downloads.push(blob);return 'blob:inventory';};LocalURL.revokeObjectURL=()=>{};
  let responder=async()=>({});
  const context=vm.createContext({document,NodeFilter:{SHOW_TEXT:4},localStorage:{getItem:()=>locale,setItem:()=>{}},URL:LocalURL,Blob,
    fetch:async(url,options={})=>{requests.push({url,options});const data=await responder(url,options);return {ok:!data?.error,status:data?.error?409:200,json:async()=>data};},
    setTimeout:()=>1,clearTimeout(){},setInterval:()=>1,clearInterval(){},addEventListener:(name,callback)=>events['window:'+name]=callback});
  return {context,element,events,requests,downloads,respond:value=>responder=value,run:code=>vm.runInContext(code,context)};
}
const localRow=n=>({candidate_id:'site-'+n,relative_path:`entry-${n}.cbl`,start_line:12,end_line:12,summary:`Business ${n} 原文`,formula:`Premium ${n} = base × rate`,conditions:'When eligible',evidence_ids:['source-'+n]});
const localDescriptor=()=>({handle:'fixed-result',snapshot_id:'sha256:snapshot',total:10001,counts:{pending:0},complete:true,paged:true,page_size:50,rows:[localRow(0)],next_cursor:50});
function localHarness(locale='en'){
  const h=environment(locale);
  for(const name of ['i18n.js','marked.umd.js','markdown.js'])h.run(fs.readFileSync(path.join(web,name),'utf8'));
  h.run(fs.readFileSync(path.join(web,'app.js'),'utf8').replace(/render\(\);initialize\(\);\s*$/,''));
  h.run(`state.token='test';state.conversation={id:'conversation-a',messages:[{id:'answer-a',role:'assistant',content:'Business narrative',repository_inventory:${JSON.stringify(localDescriptor())}}]};`);
  h.markup=()=>h.run('repositoryInventoryMarkup(state.conversation.messages[0])');
  return h;
}
const onlineRow=n=>({candidate_id:'site-'+n,path:`entry-${n}.cbl`,start_line:12,end_line:12,summary:`Business ${n} 原文; Premium = base × rate`,citations:['E'+String(n).padStart(24,'0')]});
const onlineDescriptor=()=>({id:'fixed-result',revision:'c'.repeat(64),total:10001,counts:{pending:0},complete:true,offset:0,findings:[onlineRow(0)],next_offset:40});
async function onlineHarness(locale='en'){
  const h=environment(locale);
  const workspace={id:'a'.repeat(64),repository:{identity:'example/source',commit:'b'.repeat(40)},revision:'c'.repeat(64),connected:true,coverage:{}};
  h.current={workspaces:[workspace],conversations:[],current_workspace_id:workspace.id,current_conversation_id:'conversation-a',active_job:null,
    conversation:{id:'conversation-a',status:'answered',messages:[{id:'answer-a',role:'assistant',status:'answered',content:'Business narrative',inventory_result:onlineDescriptor()}]}};
  let responder=async()=>({});
  h.respond=fn=>responder=fn;
  h.context.fetch=async(url,options={})=>{
    if(url==='/api/state'){h.requests.push({url,options});return {ok:true,json:async()=>({session_token:'test',api_configured:true})};}
    if(url==='/api/online/state'){h.requests.push({url,options});return {ok:true,json:async()=>h.current};}
    h.requests.push({url,options});const data=await responder(url,options);return {ok:!data?.error,json:async()=>data};
  };
  h.run(fs.readFileSync(path.join(web,'online.js'),'utf8'));
  await flush();
  h.click=async dataset=>{h.events['messages:click']({target:{closest:()=>({dataset})}});await flush();};
  h.markup=()=>h.element('messages').innerHTML;
  return h;
}

test('local inventory has localized controls and retains source prose in all locales',()=>{
  for(const [locale,label] of [['en','Business formula inventory'],['zh-CN','业务公式清单'],['zh-HK','業務公式清單']]){
    const h=localHarness(locale),html=h.markup();assert.ok(html.includes(label));assert.match(html,/Business 0 原文/);assert.match(html,/10,001/);
    assert.match(html,/data-inventory-page="10000"/);assert.match(html,/data-inventory-page="0" disabled/);assert.equal(h.requests.length,0);
  }
});
test('local pages replace the current view and can directly reach the last of 10001 positions',async()=>{
  const h=localHarness();h.respond(url=>{
    const cursor=Number(new URL(url,'http://localhost').searchParams.get('cursor'));
    return {...localDescriptor(),rows:[localRow(cursor)],next_cursor:cursor===10000?null:50};
  });
  await h.run("loadRepositoryInventoryPage('answer-a',null,10000)");assert.match(h.markup(),/Business 10000 原文/);assert.doesNotMatch(h.markup(),/Business 0 原文/);
  await h.run("loadRepositoryInventoryPage('answer-a',null,0)");assert.match(h.markup(),/Business 0 原文/);
  assert.equal(h.run('state.conversation.messages[0].repository_inventory.total'),10001);
  assert.ok(h.requests.every(request=>request.url.startsWith('/api/repository-inventory?')));
});
test('local stale concurrent pages and foreign snapshot results cannot replace the latest view',async()=>{
  const h=localHarness();let release;
  h.respond(url=>url.includes('cursor=50&')?new Promise(resolve=>release=resolve):({...localDescriptor(),rows:[localRow(10000)],next_cursor:null}));
  const early=h.run("loadRepositoryInventoryPage('answer-a',null,50)");
  await h.run("loadRepositoryInventoryPage('answer-a',null,10000)");release({...localDescriptor(),rows:[localRow(50)]});await early;
  assert.match(h.markup(),/Business 10000 原文/);assert.doesNotMatch(h.markup(),/Business 50 原文/);
  h.respond(()=>({...localDescriptor(),snapshot_id:'different',rows:[localRow(99)]}));await h.run("loadRepositoryInventoryPage('answer-a',null,50)");
  assert.match(h.markup(),/Business 10000 原文/);assert.doesNotMatch(h.markup(),/Business 99 原文/);
});
test('local full export downloads server-complete results instead of the visible page',async()=>{
  const h=localHarness();h.respond(()=>({...localDescriptor(),jsonl:'FIRST\nMIDDLE\nLAST-10001\n'}));
  await h.run("exportRepositoryInventory('answer-a',null)");assert.equal(await h.downloads[0].text(),'FIRST\nMIDDLE\nLAST-10001\n');
  assert.match(h.requests[0].url,/export=jsonl/);assert.doesNotMatch(h.requests[0].url,/cursor=/);
});
test('local inventory source uses its immutable handle and verified archived text',async()=>{
  const h=localHarness();h.respond(()=>({status:'READY',spans:[{relative_path:'entry-0.cbl',start_line:12,end_line:12,source_text:'COMPUTE 原文',integrity:'VALID'}]}));
  await h.run("loadRepositoryInventoryEvidence('answer-a',null,'site-0',0)");
  assert.equal(h.run('state.conversationEvidence.span.source_text'),'COMPUTE 原文');assert.match(h.requests[0].url,/action=evidence&candidate_id=site-0&index=0/);
  h.respond(()=>({status:'READY',spans:[{source_text:'WRONG',integrity:'INVALID'}]}));await h.run("loadRepositoryInventoryEvidence('answer-a',null,'site-0',0)");
  assert.equal(h.run('state.conversationEvidence.span.source_text'),'COMPUTE 原文');
});
test('local prior-answer inventory access stays bound to the selected revision',async()=>{
  const h=localHarness();h.run(`state.conversation.messages[0].answer_revisions=[{id:'answer-a',revision_id:'revision-old',repository_inventory:{...state.conversation.messages[0].repository_inventory,handle:'old-fixed-result'}}]`);
  h.respond(()=>({...localDescriptor(),handle:'old-fixed-result',rows:[localRow(10000)],next_cursor:null}));
  await h.run("loadRepositoryInventoryPage('answer-a','revision-old',10000)");assert.match(h.requests[0].url,/handle=old-fixed-result/);assert.match(h.requests[0].url,/revision_id=revision-old/);
  assert.match(h.markup(),/Business 0 原文/);
});
test('online inventory pagination is localized and reaches the complete last page without a model call',async()=>{
  for(const [locale,label] of [['en','Business formula inventory'],['zh-CN','业务公式清单'],['zh-HK','業務公式清單']]){
    const h=await onlineHarness(locale);assert.ok(h.markup().includes(label));assert.match(h.markup(),/Business 0 原文/);
    h.respond(()=>({...onlineDescriptor(),offset:10000,findings:[onlineRow(10000)],next_offset:null}));
    await h.click({inventoryMessage:'answer-a',inventoryPage:'10000'});assert.match(h.markup(),/Business 10000 原文/);assert.doesNotMatch(h.markup(),/Business 0 原文/);
    assert.ok(h.requests.some(r=>r.url.includes('/inventories/fixed-result?conversation_id=conversation-a&offset=10000&limit=40')));
    assert.ok(!h.requests.some(r=>/\/(question|resume)/.test(r.url)));
  }
});
test('online inventory full export and evidence use the saved result rather than visible-row data',async()=>{
  const h=await onlineHarness();h.respond(url=>url.includes('/export?')?{content:'FIRST\nMIDDLE\nLAST-10001',filename:'complete.jsonl'}:
    {citation:{path:'entry-10000.cbl',start:12,end:12,commit:'b'.repeat(40),revision:'c'.repeat(64)},text:'IMMUTABLE 原文'});
  await h.click({inventoryMessage:'answer-a',inventoryExport:''});assert.equal(await h.downloads[0].text(),'FIRST\nMIDDLE\nLAST-10001');
  await h.click({inventoryMessage:'answer-a',inventorySource:'E000000000000000000010000'});assert.equal(h.element('evidence-text').textContent,'IMMUTABLE 原文');
  assert.ok(h.requests.some(r=>r.url.includes('/inventories/fixed-result/evidence?conversation_id=conversation-a&citation_id=')));
});
test('online inventory rejects mismatched snapshots and late page responses',async()=>{
  const h=await onlineHarness();h.respond(()=>({...onlineDescriptor(),revision:'wrong',findings:[onlineRow(42)]}));
  await h.click({inventoryMessage:'answer-a',inventoryPage:'40'});assert.match(h.markup(),/Business 0 原文/);assert.doesNotMatch(h.markup(),/Business 42 原文/);
  let release;h.respond(url=>url.includes('offset=40&')?new Promise(resolve=>release=resolve):({...onlineDescriptor(),offset:10000,findings:[onlineRow(10000)],next_offset:null}));
  const old=h.click({inventoryMessage:'answer-a',inventoryPage:'40'});await flush();await h.click({inventoryMessage:'answer-a',inventoryPage:'10000'});
  release({...onlineDescriptor(),offset:40,findings:[onlineRow(40)],next_offset:80});await old;await flush();
  assert.match(h.markup(),/Business 10000 原文/);assert.doesNotMatch(h.markup(),/Business 40 原文/);
});
test('ordinary answers gain no inventory UI or requests and inventory content is escaped',async()=>{
  const local=localHarness();local.run('delete state.conversation.messages[0].repository_inventory');assert.equal(local.markup(),'');assert.equal(local.requests.length,0);
  const online=await onlineHarness();online.current.conversation.messages[0].inventory_result.findings[0].path='<img src=x onerror=alert(1)>';
  online.events['online-language:change']();await flush();assert.doesNotMatch(online.markup(),/<img src=x/);assert.match(online.markup(),/&lt;img/);
  delete online.current.conversation.messages[0].inventory_result;online.events['online-language:change']();await flush();
  assert.doesNotMatch(online.markup(),/data-inventory-page/);
});
test('restored inventory handles load their first page explicitly without fetching all historical results',async()=>{
  const local=localHarness();local.run('state.conversation.messages[0].repository_inventory.rows=[];state.conversation.messages[0].repository_inventory.next_cursor=0');
  assert.match(local.markup(),/Load formula inventory/);assert.equal(local.requests.length,0);
  local.respond(()=>localDescriptor());await local.run("loadRepositoryInventoryPage('answer-a',null,0)");assert.match(local.markup(),/Business 0 原文/);
  const online=await onlineHarness();online.current.conversation.messages[0].inventory_result.findings=[];online.current.conversation.messages[0].inventory_result.next_offset=0;
  online.events['online-language:change']();await flush();assert.match(online.markup(),/Load formula inventory/);
  assert.ok(!online.requests.some(r=>r.url.includes('/inventories/')));
  online.respond(()=>onlineDescriptor());await online.click({inventoryMessage:'answer-a',inventoryPage:'0'});assert.match(online.markup(),/Business 0 原文/);
});
test('online saved explanation previews expand their immutable complete text without a model request',async()=>{
  for(const [locale,label] of [['en','Show the complete business explanation'],['zh-CN','展开完整业务说明'],['zh-HK','展開完整業務說明']]){
    const h=await onlineHarness(locale),message=h.current.conversation.messages[0];
    message.inventory_answer_preview=true;message.content='SHORT PREVIEW';h.events['online-language:change']();await flush();
    assert.match(h.markup(),/SHORT PREVIEW/);assert.ok(h.markup().includes(label));
    const full='FULL FIRST '+ 'Original business explanation. '.repeat(3000)+' FULL LAST';
    h.respond(()=>({...onlineDescriptor(),answer:full,answer_citations:[{id:'E-full-tail',citation:{path:'last-source.cbl',start:1,end:9}}]}));
    await h.click({inventoryMessage:'answer-a',inventoryAnswer:''});
    assert.match(h.markup(),/FULL FIRST/);assert.match(h.markup(),/FULL LAST/);assert.doesNotMatch(h.markup(),/SHORT PREVIEW/);
    assert.match(h.markup(),/data-inventory-source="E-full-tail"/);assert.match(h.markup(),/last-source.cbl/);
    await h.click({inventoryMessage:'answer-a',inventoryAnswer:''});assert.match(h.markup(),/SHORT PREVIEW/);assert.doesNotMatch(h.markup(),/FULL LAST/);
    assert.equal(h.requests.filter(r=>r.url.includes('/inventories/')).length,1);
    assert.ok(!h.requests.some(r=>/\/(question|resume)/.test(r.url)));
    h.respond(()=>({...onlineDescriptor(),revision:'wrong',answer:'FOREIGN FULL ANSWER'}));
    await h.click({inventoryMessage:'answer-a',inventoryAnswer:''});assert.doesNotMatch(h.markup(),/FOREIGN FULL ANSWER/);
  }
});
