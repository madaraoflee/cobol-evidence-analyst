'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const web=path.join(__dirname,'../web');

function harness(locale='en'){
  const events={},elements=new Map();
  const get=id=>{if(!elements.has(id))elements.set(id,{id,value:'',innerHTML:'',hidden:false,
    setAttribute(){},addEventListener(){},focus(){this.focused=true;},classList:{toggle(){}}});return elements.get(id);};
  const document={documentElement:{},createTreeWalker:()=>({nextNode:()=>false}),querySelectorAll:()=>[],
    querySelector:selector=>selector==='[data-clear-conversation-search]'?get('search-clear'):null,
    getElementById:get,addEventListener:(name,handler)=>events[name]=handler};
  const context=vm.createContext({document,NodeFilter:{SHOW_TEXT:4},localStorage:{getItem:()=>locale},
    fetch:()=>assert.fail('History filtering must not send a request'),setTimeout(){},setInterval(){},clearInterval(){}});
  for(const file of ['i18n.js','marked.umd.js','markdown.js'])vm.runInContext(fs.readFileSync(path.join(web,file),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(web,'app.js'),'utf8').replace(/render\(\);initialize\(\);\s*$/,''),context);
  const click=(name,value='')=>events.click({target:{closest:()=>({dataset:name==='data-conversation-group'?{conversationGroup:value}:{},
    hasAttribute:attribute=>attribute===name})}});
  const input=value=>{get('conversation-search').value=value;events.input({target:get('conversation-search')});};
  return {run:code=>vm.runInContext(code,context),get,click,input};
}
function rows(count=25){return Array.from({length:count},(_,index)=>({id:'conversation-'+index,workspace_id:'workspace-one',
  source_origin:index%2?'remote_repository':'local_source',source_label:index%2?'Remote ledger':'Local billing',
  title:'Processing question '+index,message_count:2,created_at:new Date(Date.UTC(2030,0,30-index)).toISOString()}));}
function setup(h,items=rows()){h.run(`state.conversationSupported=true;state.recentConversations=${JSON.stringify(items)};`);}

test('recent history starts with eight rows, expands in batches and collapses without changing saved data',()=>{
  const h=harness();setup(h);
  const rowCount=()=> (h.run('recentConversationGroups()').match(/class="conversation-row"/g)||[]).length;
  assert.equal(rowCount(),8);assert.match(h.run('recentConversationGroups()'),/Show more · 17/);
  h.click('data-more-conversations');assert.equal(rowCount(),16);assert.equal(h.run('state.recentConversations.length'),25);
  h.click('data-more-conversations');assert.equal(rowCount(),24);
  h.click('data-less-conversations');assert.equal(rowCount(),8);assert.doesNotMatch(h.run('recentConversationGroups()'),/data-less-conversations/);
});

test('an older active conversation remains visible in the collapsed recent window with the right workspace',()=>{
  const h=harness();setup(h);
  h.run("state.conversation={id:'conversation-24'};state.project={workspace_id:'workspace-one'};");
  const html=h.run('recentConversationGroups()');assert.equal((html.match(/class="conversation-row"/g)||[]).length,9);
  assert.match(html,/aria-current="true"[^>]*data-conversation="conversation-24" data-workspace="workspace-one"/);
  h.run("state.project.workspace_id='workspace-two'");assert.doesNotMatch(h.run('recentConversationGroups()'),/conversation-24|aria-current/);
});

test('source groups collapse with accessible state and search reopens matching groups',()=>{
  const h=harness();setup(h);h.click('data-conversation-group','local_source');
  let html=h.run('recentConversationGroups()');assert.match(html,/data-conversation-group="local_source" aria-expanded="false"/);
  assert.match(html,/id="conversation-group-local_source" hidden/);
  h.click('data-conversation-group','local_source');assert.match(h.run('recentConversationGroups()'),/data-conversation-group="local_source" aria-expanded="true"/);
  h.click('data-conversation-group','local_source');h.input('billing');
  html=h.get('conversation-history-results').innerHTML;assert.match(html,/aria-expanded="true"/);assert.doesNotMatch(html,/data-source-origin="remote_repository"/);
});

test('search covers titles and source names across older rows, supports Unicode and keeps the input intact',()=>{
  const h=harness();const items=rows();items[24].title='核对 生效条件';items[24].source_label='Local billing';setup(h,items);
  h.input('核对 billing');let html=h.get('conversation-history-results').innerHTML;
  assert.match(html,/Matching conversations: 1/);assert.match(html,/核对 生效条件/);assert.match(html,/conversation-24/);
  assert.equal(h.get('conversation-search').value,'核对 billing');assert.equal(h.get('search-clear').hidden,false);
  h.input('ＰＲＯＣＥＳＳＩＮＧ REMOTE');html=h.get('conversation-history-results').innerHTML;
  assert.match(html,/Matching conversations: 12/);assert.doesNotMatch(html,/Local billing/);
  h.input('missing-title');assert.match(h.get('conversation-history-results').innerHTML,/No matching conversations/);
  h.click('data-clear-conversation-search');assert.equal(h.get('conversation-search').value,'');
  assert.equal(h.get('conversation-search').focused,true);assert.equal(h.get('search-clear').hidden,true);
  assert.equal(h.run('state.conversationVisible'),8);
});

test('search and labels remain escaped and controls translate in all supported locales',()=>{
  for(const locale of ['en','zh-CN','zh-HK']){
    const h=harness(locale);const items=rows();items[0].title='<img src=x>';setup(h,items);
    h.input('<img');const html=h.run('recentConversationGroups()');assert.match(html,/&lt;img/);assert.doesNotMatch(html,/<img/);
    if(locale==='en'){assert.match(html,/Search titles or sources/);assert.doesNotMatch(html,/[\u3400-\u9fff]/);}
    else assert.match(html,locale==='zh-CN'?/搜索标题或来源/:/搜尋標題或來源/);
  }
});
