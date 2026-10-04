'use strict';

const icons = {
  menu:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
  spark:'<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z"/><path d="m20 2 .6 1.4L22 4l-1.4.6L20 6l-.6-1.4L18 4l1.4-.6Z"/>',
  folder:'<path d="M3 7V5a1 1 0 0 1 1-1h5l2 3h9a1 1 0 0 1 1 1v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/><path d="M3 9h18"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  shield:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8.5 11.5 2.5 2.5 4.5-5"/>',
  settings:'<path d="m9 3-1 3-3 1v3l-2 2 2 2v3l3 1 1 3h6l1-3 3-1v-3l2-2-2-2V7l-3-1-1-3Z"/><circle cx="12" cy="12" r="3"/>',
  chevron:'<path d="m9 5 7 7-7 7"/>',
  arrow:'<path d="M4 12h16m-6-6 6 6-6 6"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  download:'<path d="M12 3v12m-5-5 5 5 5-5M4 16v4h16v-4"/>',
  layers:'<path d="m12 3 10 5-10 5L2 8Zm-9 9 9 5 9-5m-18 5 9 5 9-5"/>',
  code:'<path d="m7 6-5 6 5 6m10-12 5 6-5 6m-3-15-4 18"/>',
  document:'<path d="M14 3H5v18h14V8Zm0 0v5h5M8 12h8m-8 4h8"/>',
  check:'<path d="m5 12 4 4L19 6"/>',
  alert:'<path d="m12 3 10 18H2Z"/><path d="M12 9v5m0 3v.1"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 10v6m0-10v.1"/>',
  close:'<path d="m6 6 12 12M6 18 18 6"/>',
  archive:'<path d="M3 3h18v5H3Zm2 5v13h14V8m-10 5h6"/>',
  branch:'<circle cx="6" cy="5" r="2"/><circle cx="6" cy="19" r="2"/><circle cx="18" cy="5" r="2"/><path d="M6 7v10m12-10c0 8-12 2-12 8"/>',
  search:'<circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/>',
  trash:'<path d="M4 7h16m-10-3h4m-8 3 1 13h10l1-13M10 11v5m4-5v5"/>',
};
const icon = name => `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.document}</svg>`;
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const formatNumber = value => Number(value || 0).toLocaleString('en');

const preview = {catalog:null,selectedCase:null};

const state = {mode:'demo',page:'workbench',tab:'answer',connected:false,token:null,apiConfigured:false,apiConfigurationError:null,frameworkKnowledge:null,captureApiResponses:false,
  conversationSupported:false,conversation:null,conversations:[],pendingMessage:null,conversationEvidence:null,conversationLoading:false,impactPages:{},
  project:null,question:'',entry:'',readingStrategy:'focused',answerDetail:'detailed',maxSourcePages:4,selectedEvidence:null,
  evidence:null,demoLoading:true,demoError:'',demoRestore:null,sourceJobBackup:null,sourceIntent:'connect',busy:false,jobId:null,jobKind:null,error:'',errorDiagnostic:null,history:[],filter:'',sourcePage:0,entryFilter:'',progress:null,progressReceived:0,progressTimer:null,pollError:'',cancelRequested:false,evidenceTicket:0,modelChecking:false,modelCheckStatus:null,modelCheckFeedback:'',folderPicking:false,deletingConversationId:null};

function demoText(value){return frameworkDemoText(value ?? '');}
function demoCase(){return preview.catalog?.cases?.find(item=>item.id===preview.selectedCase) || null;}
function syntheticProject(project=state.project){return project?.source_origin==='synthetic_framework';}
function demoKind(id){return ({online:t('傳統聯機'),traditional_online:t('傳統聯機'),client_server:t('控制器分段聯機'),screen_controlled_online:t('控制器分段聯機'),batch:t('批處理')})[id] || id || '';}
function conversationTitle(value){const title=String(value || '').trim();return !title || ['新对话','新的对话','新的對話','New conversation'].includes(title)?t('新的對話'):title;}
function selectDemoCase(id,shouldRender=true){
  const selected=preview.catalog?.cases?.find(item=>item.id===id);if(!selected)return;
  preview.selectedCase=id;
  if(state.mode==='demo'){
    clearEvidence();state.question=demoText(selected.question);state.entry=selected.entry_path || selected.entry_program || '';
    state.evidence=selected.evidence?.[0] || null;state.selectedEvidence=state.evidence?.evidence_id || null;
    state.page='workbench';state.tab='answer';state.filter='';state.sourcePage=0;
  }
  if(shouldRender)render();
}
function frameworkDemoUnavailable(){
  return ui`<section class="empty-panel"><div class="empty-icon">${icon('layers')}</div><h2>業務案例工作台</h2><p>${state.demoLoading?t('正在讀取業務案例'):state.connected?t('業務案例暫時無法讀取，請重試或檢查本機演示文件。'):t('請先啟動本機服務，才能讀取業務案例及來源證據。')}</p>${!state.demoLoading && state.connected?ui`<button class="button secondary" data-retry-demo>重新讀取案例</button>`:''}${!state.connected && !state.demoLoading?ui`<p>在項目目錄啟動 poc/run_web.bat，或執行 python poc/web_app.py，再開啟本機服務地址。</p>`:''}</section>`;
}
function frameworkCaseSelector(){
  const cases=preview.catalog?.cases || [];
  return ui`<section class="framework-demo-selector" aria-label="業務案例"><div class="demo-section-heading"><div><div class="section-eyebrow">合成源碼 · 示例起點</div><h2>選一個問題，開始對話。</h2></div><span class="badge preview">合成業務案例 · 未呼叫模型</span></div><p class="demo-selector-intro">案例只提供起始問題。你可以改寫問題，接著自由追問；回答由目前源碼與模型生成。</p><div class="framework-case-cards">${cases.map((item,index)=>`<button class="framework-case-card ${item.id===preview.selectedCase?'selected':''}" data-demo-case="${escapeHTML(item.id)}" aria-pressed="${item.id===preview.selectedCase}"><span class="demo-case-number">${String(index+1).padStart(2,'0')}</span><strong>${escapeHTML(demoText(item.title))}</strong><span class="demo-case-type">${escapeHTML(demoKind(item.kind || item.id))}</span></button>`).join('')}</div></section>`;
}
async function loadFrameworkDemo(){
  state.demoLoading=true;state.demoError='';
  try{
    const catalog=await api('/api/framework-demo');
    if(!catalog || !Array.isArray(catalog.cases) || !catalog.cases.length || !Array.isArray(catalog.programs))throw Error('DEMO_UNAVAILABLE');
    preview.catalog=catalog;const selected=catalog.cases.find(item=>item.id===preview.selectedCase) || catalog.cases.find(item=>item.id===catalog.default_case) || catalog.cases[0];selectDemoCase(selected.id,false);
  }catch{preview.catalog=null;preview.selectedCase=null;state.demoError='DEMO_UNAVAILABLE';if(state.mode==='demo')clearEvidence();}
  state.demoLoading=false;render();
}
async function prepareDemo(){
  const selected=demoCase();if(!selected || !state.connected || state.busy)return;
  const draft=state.question.trim() || demoText(selected.question);
  state.demoRestore={project:state.project,conversation:state.conversation};
  state.error='';state.pollError='';state.cancelRequested=false;state.progress=null;state.jobId=null;state.progressReceived=Date.now();
  state.mode='real';state.page='workbench';state.tab='answer';state.busy=true;state.jobKind='demo';state.question=draft;state.entry='';state.readingStrategy='retrieval';state.pendingMessage=null;clearEvidence();startProgressClock();render();
  try{
    const response=await api('/api/framework-demo/prepare',{method:'POST',body:JSON.stringify({case_id:selected.id,locale:currentLocale})});
    if(response.status==='READY'){
      if(!response.project)throw new Error(t('案例索引暫時不可用，請重試。'));
      state.busy=false;stopProgressClock();applyProject(response.project);state.entry='';state.question=draft;state.readingStrategy='retrieval';
      if(response.conversation)applyConversation(response.conversation);else state.conversation=null;
      state.demoRestore=null;render();promptDemoQuestion();return;
    }
    if(!response.job_id)throw new Error(t('案例索引暫時不可用，請重試。'));
    state.jobId=response.job_id;rememberProgress(response.progress);await pollJob(response.job_id);
  }catch(error){state.busy=false;stopProgressClock();state.mode='demo';state.error=error.message;state.question=draft;state.project=state.demoRestore?.project || null;state.conversation=state.demoRestore?.conversation || null;state.demoRestore=null;render();}
}
function promptDemoQuestion(){$('question-input')?.focus?.();toast(t('案例已載入，按「發送」開始提問；之後可繼續追問。'));}

function decorate(){document.querySelectorAll('[data-icon]').forEach(el => {el.innerHTML=icon(el.dataset.icon);});}
function toast(message){$('toast').textContent=message;$('toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>{$('toast').hidden=true;},4500);}
function currentPrograms(){return state.mode==='demo'?(preview.catalog?.programs || []):(state.project?.programs || []);}
function result(){return state.mode==='real'?state.project?.agent?.agent_result:null;}
function diagnosis(){return state.mode==='real'?state.project?.diagnosis:null;}
function entryValue(program){return program?.entry_key || program?.program_name || '';}
function selectedEntryValue(d){return d?.investigation?.mode==='repository' || d?.scope?.selection_mode==='repository' || d?.scope?.mode==='repository_question'?'':entryValue(d?.selected_entry) || d?.entry_requested || ''; }
function sourceFiles(){return diagnosis()?.catalog_report?.files || diagnosis()?.build_report?.files || {};}
function projectReadingStrategy(project=state.project){const strategy=project?.diagnosis?.source_options?.reading_strategy;return ['focused','full_chain'].includes(strategy)?strategy:'focused';}
function draftChanged(){if(conversationMode())return false;const d=diagnosis();return state.mode==='real' && Boolean(state.project?.agent) && (state.question.trim()!==(d?.question || '').trim() || state.entry!==selectedEntryValue(d) || state.maxSourcePages!==Number(d?.source_options?.max_source_pages || 4) || state.readingStrategy!==projectReadingStrategy());}
function conversationMode(){return state.mode==='real' && state.conversationSupported;}
function sortConversations(items){
  const timestamp=value=>Number.isFinite(Date.parse(value))?Date.parse(value):0;
  return [...items].sort((a,b)=>{
    const first=timestamp(a.first_question_at || a.created_at),second=timestamp(b.first_question_at || b.created_at);
    if(first!==second)return second-first;
    if(!first)return 0;
    const created=timestamp(b.created_at)-timestamp(a.created_at);
    return created || (a.id===b.id?0:a.id<b.id?1:-1);
  });
}
function applyConversation(conversation){
  if(!conversation || typeof conversation.id!=='string' || !Array.isArray(conversation.messages))return;
  state.conversation=conversation;state.conversationEvidence=null;
  const index=state.conversations.findIndex(item=>item.id===conversation.id),previous=state.conversations[index] || {};
  const summary={...previous,id:conversation.id,title:conversation.title,message_count:conversation.messages.length,
    created_at:conversation.created_at || previous.created_at,
    first_question_at:conversation.first_question_at || conversation.messages.find(message=>message.role==='user')?.created_at || previous.first_question_at || conversation.created_at || previous.created_at};
  const items=[...state.conversations];
  if(index<0)items.unshift(summary);else items[index]=summary;
  state.conversations=sortConversations(items);
}
function currentRefs(){
  if(state.mode==='demo')return demoCase()?.evidence || [];
  if(draftChanged() || unacceptedResponse())return [];
  const answer=result();
  if(answer?.evidence_refs?.length)return answer.evidence_refs;
  if(answer?.verified_evidence_refs?.length)return answer.verified_evidence_refs;
  return [];
}
function currentRelations(){return state.mode==='demo'?(demoCase()?.relations || []):(state.project?.relations?.edges || []);}
function statusLabel(status){return ({ANALYZED:t('已生成業務解讀'),SCOPE_LIMIT:t('入口超出本次解析範圍'),PARTIAL:t('部分業務解讀'),CANCELLED:t('已取消'),SUPPORTED_WITH_BOUNDARIES:t('已核驗局部語句 · 保留邊界'),CITATION_VERIFIED_ONLY:t('引用已核對 · 解釋待核驗'),ABSTAINED:t('證據不足 · 尚未形成結論'),NOT_READY:t('尚未具備分析條件'),SAFE_STOP:t('調查已停止 · 需要處理'),NETWORK_DISABLED:t('尚未啟用模型問答'),NOT_REQUESTED:t('尚未提出業務問題'),BLOCKED:t('接入受阻'),INDEX_READY:t('源碼已就緒'),NEEDS_ATTENTION:t('源碼已接入 · 有待核對'),PREPARING:t('正在接入源碼')})[status] || status || t('尚未分析');}
function sourceUsable(){const d=diagnosis();return Boolean(d && (d.catalog_ready || (d.source_manifest_verified===true && state.project?.snapshot_id)) && (currentPrograms().length>0 || Number(sourceFiles().candidate || sourceFiles().decoded)>0));}
function repositoryIndexReady(){const d=diagnosis();return Boolean(d?.source_manifest_verified===true && state.project?.snapshot_id && d.runner_status!=='BLOCKED' && ['repository_index','repository_question'].includes(d.scope?.mode) && d.repository_search?.full_text_complete===true);}
function sourceReadinessLabel(){return repositoryIndexReady()?t('源碼索引已就緒'):statusLabel(diagnosis()?.runner_status);}
function frameworkStatusLabel(status){return ({LOADED:t('框架資料已載入'),MATCHED:t('已找到相關框架資料'),NO_MATCH:t('框架資料已載入'),UNAVAILABLE:t('框架資料無法讀取'),NOT_CONFIGURED:t('尚未設定框架資料')})[status] || t('尚未設定框架資料');}
function frameworkConfigurationMessage(reason){
  const messages={
    FRAMEWORK_REFERENCE_DIRECTORY_EMPTY:'資料夾內沒有可讀資料；請選擇 .md、.markdown、.txt 文件或包含這些文件的資料夾。',
    FRAMEWORK_REFERENCE_DIRECTORY_NO_READABLE_DOCUMENTS:'已找到文件，但無法讀取內容。請檢查檔案權限與文字編碼。',
    FRAMEWORK_REFERENCE_UNREADABLE:'目前電腦無法讀取這個位置。請確認文件或資料夾存在，並具有讀取權限。',
    FRAMEWORK_REFERENCE_INVALID:'資料不是有效文字。請另存為 UTF-8 或帶 BOM 的 UTF-16 文字。',
    FRAMEWORK_REFERENCE_CONFIG_INVALID:'框架資料設定無法讀取。請重新填寫路徑並儲存。',
    FRAMEWORK_REFERENCE_CHANGED:'文件在讀取期間發生變更。請儲存文件後重新讀取。',
    FRAMEWORK_REFERENCE_TOO_LARGE:'單份資料過大。可按章節拆分到同一資料夾，再指定該資料夾。',
    FRAMEWORK_REFERENCE_COLLECTION_LIMIT:'已讀取部分文件，其餘文件未納入本次框架資料。',
  };
  if(messages[reason])return t(messages[reason]);
  if(/TOO_LARGE$/.test(reason || ''))return t('框架資料超過讀取上限。請依使用手冊縮小參考文件，再重新啟動服務。');
  if(/(INVALID|UNSUPPORTED|ENCODING|EMPTY)$/.test(reason || ''))return t('框架資料格式無效。請使用含有章節標題的 UTF-8 Markdown 文件，並核對本機設定。');
  return t('請檢查 FRAMEWORK_REFERENCE_PATH 指向的文件是否存在且可讀，修改設定後重新啟動服務。');
}
function frameworkContext(){
  const context=diagnosis()?.framework_context || result()?.framework_context;
  const summary=state.frameworkKnowledge;
  const d=diagnosis();const changed=d?.question && (state.question.trim()!==d.question.trim() || state.entry!==selectedEntryValue(d));
  if(summary && ['UNAVAILABLE','NOT_CONFIGURED'].includes(summary.status))return summary;
  if(!state.busy && !changed && !draftChanged() && context){
    if(summary?.document?.sha256 && context.document?.sha256 && summary.document.sha256!==context.document.sha256)return {...summary,document_changed:true};
    return context;
  }
  return summary || {status:'NOT_CONFIGURED'};
}
function frameworkAnchor(id){return 'framework-reference-'+encodeURIComponent(String(id));}
function frameworkCite(id,index){return `<a class="citation framework-citation" href="#${escapeHTML(frameworkAnchor(id))}" data-framework-reference="${escapeHTML(id)}" aria-label="${escapeHTML(t('查看框架引用'))} ${index}">${escapeHTML(t('框架'))} ${index}</a>`;}
function frameworkKnowledgeCard(){
  if(state.mode!=='real')return '';
  const context=frameworkContext();const document=context.document || {};const references=Array.isArray(context.references)?context.references:[];const matches=Array.isArray(context.source_matches)?context.source_matches:[];
  const unavailable=context.status==='UNAVAILABLE';const loaded=['LOADED','MATCHED','NO_MATCH'].includes(context.status);
  return ui`<section class="framework-card" id="framework-knowledge" aria-label="框架知識"><div class="framework-heading"><div>${icon('layers')}<h2>框架知識</h2></div><span class="badge ${unavailable?'warning':'neutral'}">${escapeHTML(frameworkStatusLabel(context.status))}</span></div>${loaded?`<div class="framework-document"><strong>${escapeHTML(document.title || t('本機參考文件'))}</strong><small>${escapeHTML(t('文件指紋'))} <code title="${escapeHTML(document.sha256 || '')}">${escapeHTML(String(document.sha256 || '').replace('sha256:','').slice(0,12) || '—')}</code> · ${formatNumber(document.section_count)} ${escapeHTML(t('個資料片段'))}</small></div>`:''}<p class="framework-description">${unavailable?escapeHTML(frameworkConfigurationMessage(context.reason_code)):!loaded?t('將本機框架文件接入後，系統會結合相關章節與源碼證據解讀程式。'):context.document_changed?t('參考文件已更新。請重新分析，讓引用對應目前內容。'):context.status==='MATCHED'?t('下列章節與本次問題或源碼相關；名稱及文字匹配僅是線索，需要結合源碼覆核。'):context.status==='NO_MATCH'?t('本次範圍內未確認框架與源碼的對應，仍可分析已有源碼。依問題選取的章節僅供背景參考。'):context.reason_code==='FRAMEWORK_INDEX_UNAVAILABLE'?t('參考文件已載入，但本次源碼索引未能完成框架匹配。請重新接入或選擇有效入口。'):t('已載入參考文件。開始業務分析後，可查看本次選用的章節和源碼對應。')}</p>${loaded?ui`<p class="framework-boundary">文件知識不等於現場執行驗證；閉源實作、實際配置與執行結果仍可能未知。</p>`:''}${references.length?`<details class="framework-references"><summary>${escapeHTML(t('查看本次框架依據'))} · ${formatNumber(references.length)} ${escapeHTML(t('段框架引用'))} · ${formatNumber(matches.length)} ${escapeHTML(t('處源碼線索'))}</summary>${references.map((ref,i)=>{const bindings=matches.filter(match=>(match.reference_ids || []).includes(ref.reference_id));return `<details class="framework-reference" id="${escapeHTML(frameworkAnchor(ref.reference_id))}"><summary><span class="framework-reference-number">${i+1}</span><span>${escapeHTML(ref.heading || ref.reference_id)}<small>${ref.page!==null && ref.page!==undefined?escapeHTML(t('頁碼'))+' '+Number(ref.page)+' · ':''}L${Number(ref.start_line)}–${Number(ref.end_line)}</small></span></summary><pre>${escapeHTML(ref.text || '')}</pre>${bindings.length?`<div class="framework-source-links"><strong>${escapeHTML(t('對應源碼線索'))}</strong>${bindings.map(match=>`${cite(match.evidence_id,`${match.relative_path || match.program_name || t('源碼引用')} · L${Number(match.start_line)}–${Number(match.end_line)}`)}`).join('')}</div>`:`<p class="framework-unbound">${escapeHTML(t('依問題選取的背景章節；未確認對應源碼。'))}</p>`}</details>`;}).join('')}</details>`:''}${(context.coverage?.truncated || context.coverage?.document_truncated)?ui`<p class="framework-boundary">已達本次框架檢索上限，未涵蓋的章節或源碼不代表不相關。</p>`:''}${!loaded?ui`<button class="framework-settings" data-settings>查看本機設定 ${icon('arrow')}</button>`:''}</section>`;
}
function supportingContext(){
  if(state.mode!=='real')return '';
  return ui`<details class="supporting-context"><summary>技術與框架依據</summary>${frameworkKnowledgeCard()}</details>`;
}
function renderChrome(){
  const demo=state.mode==='demo';const d=diagnosis();const files=sourceFiles();
  document.documentElement.classList?.toggle('conversation-layout',conversationMode() || demo);
  document.documentElement.classList?.toggle('chat-page',conversationMode() && state.page==='workbench');
  document.querySelectorAll('[data-page]').forEach(el=>{el.classList.toggle('active',el.dataset.page===state.page);if(el.dataset.page===state.page)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');});
  $('demo-mode').classList.toggle('selected',demo);$('real-mode').classList.toggle('selected',!demo);
  $('demo-mode').setAttribute('aria-pressed',String(demo));$('real-mode').setAttribute('aria-pressed',String(!demo));
  const titles={workbench:[t('分析工作台'),t('讓業務邏輯，清晰可見。'),t('從一個業務問題出發，沿著源碼找到可追溯的答案。')],sources:[t('源碼資料庫'),t('先看清楚，分析了甚麼。'),t('程式、來源與讀取狀態，都有跡可尋。')],history:[t('分析記錄'),t('每一次分析，都有依據。'),t('回看本次工作階段的問題、來源快照與結論。')]};
  const [page,title,subtitle]=titles[state.page];$('breadcrumb-page').textContent=page;$('page-title').textContent=title;$('page-subtitle').textContent=subtitle;
  if(conversationMode() && state.page==='workbench'){$('page-title').textContent=t('分析工作台');$('page-subtitle').textContent=t('沿用代碼庫索引與對話上下文，持續追問。');}
  if($('new-conversation-button')){$('new-conversation-button').hidden=!state.conversationSupported;$('new-conversation-button').disabled=state.busy || state.conversationLoading || !sourceUsable();}
  if($('conversation-list'))$('conversation-list').innerHTML=state.conversationSupported?state.conversations.filter(item=>item.message_count!==0).map(item=>`<div class="conversation-row"><button class="conversation-link ${item.id===state.conversation?.id?'active':''}" ${item.id===state.conversation?.id?'aria-current="true"':''} title="${escapeHTML(conversationTitle(item.title))}" data-conversation="${escapeHTML(item.id)}" ${state.busy || state.conversationLoading?'disabled':''}>${escapeHTML(conversationTitle(item.title))}</button><button class="conversation-delete" type="button" data-delete-conversation="${escapeHTML(item.id)}" aria-label="${escapeHTML(t('刪除對話'))}：${escapeHTML(conversationTitle(item.title))}" title="${escapeHTML(t('刪除對話'))}" ${state.busy || state.conversationLoading?'disabled':''}>${icon('trash')}</button></div>`).join(''):'';
  if($('conversation-list') && !$('conversation-list').innerHTML)$('conversation-list').innerHTML=ui`<p class="conversation-empty">尚無對話，提出問題後會顯示在這裡。</p>`;
  $('connection-status').classList.toggle('live',state.connected);
  $('connection-status').innerHTML=`<i></i>${state.connected?t('本機服務已啟動'):t('介面預覽 · 本機服務未啟動')}`;
  $('config-dot').classList.toggle('ready',state.apiConfigured);
  $('project-title').textContent=demo?demoText(preview.catalog?.title || t('業務案例工作台')):((state.project?.source?.split(/[\\/]/).filter(Boolean).pop() || t('尚未接入本機源碼'))+(syntheticProject()?t(' · 合成源碼'):''));
  const badge=$('source-badge');badge.textContent=demo?t('合成業務案例'):(state.busy?t('更新中'):(d?sourceReadinessLabel():t('等待接入')));
  badge.className=`badge ${demo?'preview':d?.runner_status==='BLOCKED'?'warning':sourceUsable()?'positive':'neutral'}`;
  $('project-description').textContent=demo?(preview.catalog?ui`${formatNumber(preview.catalog.file_count)} 個檔案 · ${formatNumber(currentPrograms().length)} 個程式定義`:t('正在讀取業務案例')):d?(repositoryIndexReady()?ui`${formatNumber(files?.candidate || files?.decoded)} 個檔案 · ${formatNumber(currentPrograms().length)} 個程式`:ui`${formatNumber(files?.candidate || files?.decoded)} 個檔案 · ${formatNumber(currentPrograms().length)} 個目錄入口`):t('選擇源碼與結果資料夾，建立自己的分析範圍');
  $('snapshot-info').innerHTML=icon('layers')+(demo?t('合成業務案例 · 未呼叫模型'):state.project?.snapshot_id?ui`快照 ${escapeHTML(state.project.snapshot_id.replace('sha256:','').slice(0,8))}`:state.project?.catalog_snapshot_id?t('目錄已就緒 · 詳細分析按需建立'):t('尚未建立快照'));
  $('nav-count').textContent=demo?formatNumber(preview.catalog?.file_count):formatNumber(files?.candidate || files?.decoded);
  $('recent-title').innerHTML=demo?`${escapeHTML(demoText(demoCase()?.title || t('業務案例工作台')))}<small>${escapeHTML(t('合成業務案例 · 未呼叫模型'))}</small>`:`${escapeHTML(d?.question || t('尚未建立業務問答'))}<small>${d?t(syntheticProject()?'合成源碼 · 本次工作階段':'本機源碼 · 本次工作階段'):t('接入源碼後開始')}</small>`;
  $('footer-scope').textContent=demo?t('合成業務案例 · 未呼叫模型'):syntheticProject()?t('合成源碼 · 業務解讀仍需覆核'):t('本機代碼庫 · 業務問答');
  $('export-button').disabled=state.busy || demo || !d;
  $('connect-button').disabled=state.busy;
  const sourceAction=state.project?.source && !syntheticProject()?t('更新本機源碼'):t('接入本機源碼');
  $('connect-button').setAttribute('aria-label',sourceAction);
  $('connect-button').setAttribute('title',state.busy?t('目前工作完成後，可更新源碼'):sourceAction);
  if($('connect-label'))$('connect-label').textContent=sourceAction;
  const inlineFailure=conversationMode() && state.page==='workbench' && state.error;
  const notice=(!inlineFailure && state.error?(state.errorDiagnostic?failureSummaryText(state.errorDiagnostic):state.error):'') || (!demo && d?.runner_status==='BLOCKED'?(d.messages || []).join(' '):'');
  $('notice').textContent=notice;$('notice').hidden=!notice;
}

function entryOptions(){
  const programs=currentPrograms();const query=state.entryFilter.toLowerCase();
  const matches=programs.filter(p=>(p.program_name+' '+p.relative_path).toLowerCase().includes(query)).slice(0,100);
  const selected=programs.find(p=>entryValue(p)===state.entry);
  if(selected&&!matches.includes(selected))matches.unshift(selected);
  return ui`<option value="" ${!state.entry?'selected':''}>整個代碼庫 · 自動查找</option>${matches.map(p=>`<option value="${escapeHTML(entryValue(p))}" ${state.entry===entryValue(p)?'selected':''}>${escapeHTML(p.program_name)}${state.mode==='real'?' · '+escapeHTML(p.relative_path || ''):''}${['path','filename'].includes(p.name_origin)?' · '+escapeHTML(t('檔名入口')):''}</option>`).join('')}`;
}
function readingDepthControl(){
  const full=state.readingStrategy==='full_chain';
  return ui`<div class="reading-depth"><label for="reading-strategy">${t('閱讀方式')}</label><select id="reading-strategy" ${state.busy?'disabled':''}><option value="full_chain" ${full?'selected':''}>${t('完整業務鏈 · 自動分批閱讀')}</option><option value="focused" ${!full?'selected':''}>${t('按問題重點閱讀')}</option></select><label for="reading-depth">${full?t('每批閱讀量'):t('閱讀深度')}</label><select id="reading-depth" ${state.busy?'disabled':''}><option value="4" ${state.maxSourcePages===4?'selected':''}>${full?t('每批 4 段'):t('快速閱讀 · 最多 4 段')}</option><option value="12" ${state.maxSourcePages===12?'selected':''}>${full?t('每批 12 段'):t('按問題閱讀 · 最多 12 段')}</option><option value="48" ${state.maxSourcePages===48?'selected':''}>${full?t('每批 48 段'):t('深入閱讀 · 最多 48 段')}</option>${![4,12,48].includes(state.maxSourcePages)?ui`<option value="${state.maxSourcePages}" selected>${full?ui`${t('每批 ')}${state.maxSourcePages}${t(' 段')}`:ui`${t('自訂 · 最多 ')}${state.maxSourcePages}${t(' 段')}`}</option>`:''}</select><small>${full?t('自動分批讀取本次範圍內所有可讀源碼，再整合業務解讀。每批數量不是整次分析的總段數上限。'):t('快速模式只閱讀與問題最相關的少量源碼；需要完整鏈路時請切換上方選項。')}</small></div>`;
}
function questionCard(){
  const demo=state.mode==='demo';const programs=currentPrograms();
  if(conversationMode())return conversationComposer();
  if(demo)return ui`<section class="question-card demo-question-card"><div class="section-eyebrow">建議業務問題</div><p class="demo-question">${escapeHTML(demoText(demoCase()?.question))}</p><div class="demo-question-actions"><p>載入案例後可自由修改問題，再按「開始業務分析」呼叫模型。</p><button class="button primary" data-prepare-demo ${state.busy || !state.connected || !demoCase()?'disabled':''}>${icon('folder')}載入此案例</button></div><p class="model-notice">載入只建立本機目錄與源碼索引，不呼叫模型。</p></section>`;
  return ui`<section class="question-card"><div class="section-eyebrow">本次業務問題</div><form id="question-form"><div class="question-top"><div class="spark-disc">${icon('spark')}</div><div class="question-field"><textarea rows="1" id="question-input" aria-label="業務問題" placeholder="用業務語言提問，系統會自動查找相關程式、定義與處理規則。" ${state.busy?'disabled':''}>${escapeHTML(state.question)}</textarea></div></div><div class="question-bottom"><label class="entry-choice" for="entry-select">${icon('branch')}分析範圍${programs.length>100?ui`<input id="entry-filter" aria-label="搜尋調查入口" placeholder="搜尋名稱或路徑，顯示前 100 項" value="${escapeHTML(state.entryFilter)}">`:''}<select id="entry-select" ${state.busy?'disabled':''}>${entryOptions()}</select></label><button class="button primary" type="submit" ${state.busy || !sourceUsable()?'disabled':''}>${icon('spark')}開始業務分析</button></div>${readingDepthControl()}<p class="model-notice">發起分析將使用已設定的模型接口，傳送問題、有限源碼證據及相關框架文件節錄。</p></form><div class="question-chips"><button data-question="這個代碼庫支援哪些業務功能？請整理主要功能、處理過程與業務結果。">${icon('document')} 可辦理的業務</button><button data-question="請找出主要業務規則與計算方式，說明輸入、適用條件和例外。">${icon('branch')} 規則與計算</button><button data-question="哪些情況會中止、退回或延後處理？會影響哪些業務結果？">${icon('shield')} 異常與影響</button></div></section>`;
}

function answerTabs(){const count=state.mode==='demo'?0:(draftChanged()?0:result()?.tool_trace?.length || 0);return ui`<div class="answer-tabs" role="tablist" aria-label="分析檢視"><button data-tab="answer" role="tab" aria-selected="${state.tab==='answer'}" class="${state.tab==='answer'?'selected':''}">${state.mode==='demo'?t('業務說明'):t('業務解讀')}</button><button data-tab="relations" role="tab" aria-selected="${state.tab==='relations'}" class="${state.tab==='relations'?'selected':''}">程式關係</button>${state.mode==='real'?ui`<button data-tab="trace" role="tab" aria-selected="${state.tab==='trace'}" class="${state.tab==='trace'?'selected':''}">調查記錄<span class="tab-count">${count}</span></button><button data-tab="api" role="tab" aria-selected="${state.tab==='api'}" class="${state.tab==='api'?'selected':''}">API 返回</button>`:''}</div>`;}

function cite(id,label){return ui`<button class="citation" data-evidence="${escapeHTML(id)}" aria-label="查看證據 ${escapeHTML(label)}">${escapeHTML(label)}</button>`;}

function previewAnswer(){
  const selected=demoCase();if(!selected)return frameworkDemoUnavailable();
  const evidence=selected.evidence || [];const known=new Set(evidence.map(ref=>ref.evidence_id));
  return ui`<article class="framework-guide business-guide"><div class="answer-state-row"><span class="badge preview">合成業務案例 · 未呼叫模型</span><small>${evidence.length} 段引用</small></div><h2>${escapeHTML(demoText(selected.title))}</h2><p class="answer-intro">${escapeHTML(demoText(selected.purpose))}</p>
    <h3 class="guide-section-title">業務處理過程</h3><p class="field-hint">以下說明由合成源碼整理，不代表已執行程式或模型分析。</p><div class="steps">${(selected.steps || []).map((step,index)=>`<div class="business-step"><span class="step-number">${String(index+1).padStart(2,'0')}</span><div><h3>${escapeHTML(demoText(step.title))}${known.has(step.evidence_id)?cite(step.evidence_id,String(evidence.findIndex(ref=>ref.evidence_id===step.evidence_id)+1)):''}</h3><p>${escapeHTML(demoText(step.description))}</p></div></div>`).join('')}</div>
    ${selected.metadata?.length?ui`<section class="guide-metadata"><h3 class="guide-section-title">業務規則與影響</h3><dl>${selected.metadata.map(item=>`<div><dt>${escapeHTML(demoText(item.label))}</dt><dd>${escapeHTML(demoText(item.value))}</dd></div>`).join('')}</dl><p class="field-hint">業務設定為案例中的合成聲明，不代表已匯入生產配置或執行記錄。</p></section>`:''}
    ${selected.boundaries?.length?ui`<details class="narrative-boundaries"><summary>案例範圍與待確認事項</summary>${selected.boundaries.map(item=>`<p>${escapeHTML(demoText(item))}</p>`).join('')}</details>`:''}
    <details class="guide-technical"><summary>技術與框架依據</summary><dl><div><dt>框架類型</dt><dd>${escapeHTML(demoKind(selected.kind || selected.id))}</dd></div><div><dt>來源程式</dt><dd><code>${escapeHTML(selected.entry_program || '')}</code></dd></div><div><dt>來源路徑</dt><dd><code>${escapeHTML(selected.entry_path || '')}</code></dd></div></dl><button class="button secondary" data-tab="relations">查看程式關係 ${icon('arrow')}</button></details>
    <div class="answer-footnote">${icon('info')}點選引用可查看演示文件中的實際源碼；自由問題需載入案例後分析。</div></article>`;
}

function boundaryText(value){
  if(typeof value==='string')return value;
  if(value?.type==='framework_reference_boundary' && value?.reason==='document_rules_are_conditional')return t('框架解讀結合可見源碼與文件規則；現場配置、生成或閉源實作及執行效果尚未獨立核驗。');
  if(value?.type==='snapshot_coverage')return t('目前只核對已索引的源碼；資料庫記錄、執行參數與完整執行狀態未核驗。');
  if(value?.reason==='target_not_found')return t('呼叫或 COPY 的實作未提供，相關內部行為仍待確認。');
  if(value?.reason==='bounded_or_incomplete_tool_result')return t('部分依賴或工具結果不完整，本次回答只涵蓋已有證據。');
  return value?.message || value?.detail || value?.reason || JSON.stringify(value);
}
function investigationStop(){
  const agent=state.project?.agent;const reason=agent?.stop_detail?.reason || result()?.stop_reason || '';
  const outcomeReasons=['completed','model_abstained','sufficient_material','MODEL_OUTPUT_TRUNCATED','output_truncated','answer_incomplete','request_budget','usable_draft_retained','source_identity_ambiguous','source_identity_not_found','unsupported_citations','evidence_incomplete','question_evidence_incomplete','evidence_read_incomplete','working_set_transmission_incomplete','tool_unavailable'];
  const stopped=agent?.runner_status==='SAFE_STOP' || agent?.reason_code==='AGENT_SAFETY_STOPPED' || result()?.status==='SAFE_STOP' || (reason && !outcomeReasons.includes(reason));
  return stopped?{reason,code:agent?.stop_detail?.code || (reason?reason.toUpperCase():agent?.reason_code || 'SAFE_STOP')}:null;
}
function stopExplanation(reason){
  const messages={
    model_protocol_error:'模型回應未符合 Agent 動作協議，請先查看 API 返回。',
    invalid_final_answer:'模型的最終答案未符合答案協議，請先查看 API 返回。',
    model_client_error:'模型請求或回應處理失敗，請查看 HTTP 狀態與 API 返回。',
    tool_budget_exhausted:'已達本次工具呼叫上限，調查尚未完成。',
    model_turn_budget_exhausted:'已達本次模型輪次上限，調查尚未完成。',
    no_progress:'連續調查未取得新的可用事實，流程已停止。',
    model_output_budget_exceeded:'模型輸出超過本次處理上限。',
    output_truncated:'模型輸出尚未完成，已保留取得的解讀。',
    unauthorized_tool:'模型請求了未開放的工具。',
    invalid_tool_arguments:'模型提供的工具參數未通過核對。',
    evidence_phase_closed:'證據讀取階段已結束，模型仍請求繼續讀取。',
    tool_execution_error:'本機調查工具執行失敗。',
    tool_contract_mismatch:'本機工具的返回資料未符合工具協議。',
    tool_status_invalid:'本機工具返回了不支援的狀態。',
    snapshot_missing:'工具結果缺少有效的源碼快照。',
    snapshot_mismatch:'工具結果與本次源碼快照不一致。',
    snapshot_integrity_error:'源碼證據完整性核對失敗。',
    tool_policy_denied:'工具結果超出本次允許的處理範圍。',
    tool_capability_unavailable:'本次需要的調查工具能力不可用。',
    evidence_scope_violation:'模型引用超出本次調查範圍的證據。',
    invalid_evidence_reference:'模型引用了尚未核驗的證據。',
    unsupported_claim_content:'候選結論與源碼內容未能對應。',
    unsupported_claim:'候選結論未獲現有證據支持。',
    claim_checker_error:'本機結論核對程序執行失敗。',
  };
  return t(messages[reason] || '分析流程因下列原因停止；請查看診斷與 API 返回。');
}
function apiExchanges(){
  if(state.mode!=='real' || draftChanged())return [];
  const exchanges=state.project?.agent?.api_diagnostics?.exchanges;
  return Array.isArray(exchanges)?exchanges.filter(item=>item && typeof item==='object'):[];
}
function apiChoice(exchange){
  try{
    const body=JSON.parse(exchange.body_text);const choices=body?.choices;if(!Array.isArray(choices))return null;
    const investigations=apiExchanges().filter(item=>item.phase==='investigation' && /(^|\/)chat\/completions\/?$/.test(item.endpoint || '')).sort((a,b)=>(a.sequence || 0)-(b.sequence || 0));
    const ordinal=investigations.indexOf(exchange);
    const summary=result()?.diagnostic_summary || state.project?.agent?.diagnostic_summary;
    const selected=ordinal>=0?summary?.requests?.[ordinal]?.choice_index:null;
    const index=Number.isInteger(selected) && selected>=0 && selected<choices.length?selected:0;
    const choice=choices[index];return choice && typeof choice==='object'?choice:null;
  }catch{return null;}
}
function apiMessage(exchange){
  const message=apiChoice(exchange)?.message;return message && typeof message==='object'?message:null;
}
function apiBodyOmission(reason){
  return t(({
    HTTP_ERROR_BODY_NOT_COLLECTED:'接口返回 HTTP 錯誤；本次傳輸未收集錯誤正文。',
    ERROR_RESPONSE_BODY_OMITTED:'錯誤正文已省略以避免回顯敏感信息。',
    NO_RESPONSE:'請求未收到回應，因此沒有返回正文。',
    RESPONSE_BODY_INVALID:'收到的返回正文格式無效，無法保存為文字。',
    TRANSPORT_RESPONSE_INVALID:'傳輸返回資料無效，無法取得可展示的正文。',
  })[reason] || '未記錄返回內容。');
}
function unacceptedResponse(){
  const response=state.project?.agent?.unaccepted_response;
  return response && typeof response.text==='string' && response.text.trim()?response:null;
}
function unacceptedResponseView(){
  const response=unacceptedResponse();if(!response)return '';
  return ui`<section class="api-exchange api-unaccepted"><h3>未接受的模型正文</h3><p class="api-privacy-note">以下文字已保留供診斷，但未通過本次源碼或結果核對，不能作為目前業務結論或引用依據。</p><p class="api-exchange-meta"><code>${escapeHTML(response.reason_code || state.project?.agent?.reason_code || '')}</code></p><pre class="api-raw-text">${escapeHTML(response.text)}</pre></section>`;
}
function apiSummary(){
  const agent=state.project?.agent;const exchanges=apiExchanges();const answer=result();const stopped=investigationStop();
  const received=exchanges.some(item=>Number.isInteger(item.http_status) && item.http_status>=100);
  const chatReceived=exchanges.some(item=>{
    if(!/(^|\/)chat\/completions\/?$/.test(item.endpoint || '') || item.http_status<200 || item.http_status>=300 || !Number.isInteger(item.http_status))return false;
    const message=apiMessage(item);return message?.role==='assistant' && (typeof message.content==='string' && Boolean(message.content.trim()) || Array.isArray(message.tool_calls) && message.tool_calls.length>0);
  });
  const probeChat=agent?.capability_report?.capabilities?.chat?.status==='SUPPORTED';
  const investigationReceived=exchanges.some(item=>item.phase==='investigation');
  const transport=chatReceived || probeChat?t('聊天回應已收到'):received?t('已收到 HTTP 回應；聊天回應尚未確認'):exchanges.length?t('未收到 HTTP 回應'):t('尚無本次請求記錄');
  const flow=stopped?stopExplanation(stopped.reason):['SOURCE_ANALYSIS_FAILED','SOURCE_SNAPSHOT_MISMATCH','ANSWER_SNAPSHOT_MISMATCH','SOURCE_SNAPSHOT_UNVERIFIED'].includes(agent?.reason_code)?t('源碼核對或分析失敗，本次業務結果未獲接受。'):agent?.reason_code==='COMPANY_API_NOT_READY'?t('能力探測未通過，業務調查尚未開始。'):answer?.finish_reason==='length' || answer?.answer_truncated===true?t('業務回答未完整輸出；已保留收到的正文。'):answer?.status==='PARTIAL' && answer?.model_answer_recorded===true?t('已取得部分業務回答；尚未完成的範圍可繼續分析。'):answer?.stop_reason==='completed' || answer?.stop_reason==='model_abstained' || agent?.runner_status==='COMPLETED' && answer?.model_answer_recorded===true?t('Agent 流程已完成；業務證據需另行核對。'):investigationReceived?t('已發起業務調查；完成狀態尚未確認。'):probeChat?t('聊天能力探測已通過；尚無業務調查結果。'):t('尚無 Agent 完成記錄。');
  const evidence=unacceptedResponse()?t('已保留未接受正文，不能作為目前業務結論'):narrativeText(answer)?t('已有模型解讀，業務含義尚待覆核'):stopped?t('流程中斷，尚未形成業務結論'):answer?.claims?.length?statusLabel(answer.status):answer?.status==='ABSTAINED'?t('證據不足 · 尚未形成結論'):t('尚未形成業務結論');
  return ui`<div class="api-summary"><div><span>API 連線</span><strong>${escapeHTML(transport)}</strong></div><div><span>Agent 流程</span><strong>${escapeHTML(flow)}</strong>${stopped?`<code>${escapeHTML(stopped.code)}</code>`:''}</div><div><span>業務證據</span><strong>${escapeHTML(evidence)}</strong></div></div>`;
}
const SAFE_API_EXPLANATIONS={
  request_too_large:['單次請求正文超過限制。','縮小本次供應的源碼或資料範圍後再試。'],
  context_too_large:['本次請求超過模型上下文限制。','減少歷史消息和供應資料，分段分析後再試。'],
  model_unavailable:['所選模型不可用或目前帳號無法訪問。','檢查模型配置和帳號可訪問的模型。'],
  unsupported_feature:['目前接口明確不支持所請求的功能或參數。','使用接口支持的功能或調整請求參數。'],
  output_limit:['單次輸出額度或輸出參數超過限制。','降低單次最大輸出額度後再試。'],
  quota_exhausted:['帳號配額或累計使用額度不足。','檢查帳號配額或帳單狀態，恢復額度後再試。'],
  rate_limit:['接口明確報告請求速率受限。','等待限流窗口結束後再手動重試。'],
  authentication:['接口認證失敗。','檢查 API 密鑰及認證配置。'],
  permission:['接口拒絕目前帳號的訪問權限。','檢查帳號權限和資源訪問授權。'],
  timeout:['請求超過等待時間。','檢查網絡和服務狀態，再手動重試。'],
  connection:['未能連接到接口。','檢查網絡、接口地址和服務連通性。'],
  http_429_unknown:['接口返回 HTTP 429，但未提供可確認的原因。','檢查帳號額度和服務限流說明，再決定是否重試。'],
  http_5xx_unknown:['接口返回服務端錯誤，具體原因尚未確認。','憑關聯編號檢查服務端日誌和服務狀態。'],
  request_rejected:['接口拒絕本次請求，具體原因尚未確認。','憑關聯編號檢查接口支持的請求格式和服務日誌。'],
  invalid_response:['接口返回的響應格式無效。','檢查接口兼容性和服務端響應日誌。'],
  system_error:['請求處理發生系統錯誤，具體原因尚未確認。','憑關聯編號檢查應用和服務端日誌。'],
};
const SAFE_PROVIDER_CODES=['request_too_large','payload_too_large','context_length_exceeded','context_window_exceeded','model_not_found','model_not_available','model_unavailable','unsupported_parameter','unsupported_value','unsupported_feature','max_tokens_exceeded','max_output_tokens_exceeded','output_token_limit_exceeded','insufficient_quota','quota_exceeded','billing_hard_limit_reached','rate_limit_exceeded','invalid_api_key','authentication_error','permission_denied','access_denied','internal_error','server_error'];
const SAFE_TRANSPORT_EXPLANATIONS={
  dns_resolution_failed:'找不到接口地址，請檢查網路或地址。',
  tls_certificate_invalid:'連線驗證失敗，請聯絡管理員檢查憑證。',
  tls_handshake_failed:'無法建立安全連線，請聯絡管理員。',
  connection_refused:'接口拒絕連線，請確認服務已啟動。',
  connection_reset:'接口連線中斷，請稍後重試。',
  network_unreachable:'無法連上接口網路，請檢查網路連線。',
  timeout:'接口回應逾時，請稍後重試。'
};
function safeApiDiagnostic(value){
  if(!value || value.schema_version!=='safe-api-error/v1' || typeof value.category!=='string' || !Object.hasOwn(SAFE_API_EXPLANATIONS,value.category) || typeof value.request_id!=='string' || !/^local-[a-f0-9]{32}$/.test(value.request_id))return null;
  const messages=SAFE_API_EXPLANATIONS[value.category];
  const safe={schema_version:'safe-api-error/v1',category:value.category,reason:t(messages[0]),next_step:t(messages[1]),
    evidence_source:['local','provider_code','provider_message','http_status','unknown'].includes(value.evidence_source)?value.evidence_source:'unknown',request_id:value.request_id,request_id_source:'local'};
  if(safe.evidence_source==='local' && ['connection','timeout'].includes(safe.category) && typeof value.transport_reason==='string' && Object.hasOwn(SAFE_TRANSPORT_EXPLANATIONS,value.transport_reason) && (safe.category!=='timeout' || value.transport_reason==='timeout')){
    safe.transport_reason=value.transport_reason;safe.reason=t(SAFE_TRANSPORT_EXPLANATIONS[value.transport_reason]);
  }
  if(Number.isInteger(value.http_status) && value.http_status>=100 && value.http_status<=599)safe.http_status=value.http_status;
  if(SAFE_PROVIDER_CODES.includes(value.provider_code))safe.provider_code=value.provider_code;
  if(Number.isInteger(value.retry_after_seconds) && value.retry_after_seconds>=0 && value.retry_after_seconds<=86400)safe.retry_after_seconds=value.retry_after_seconds;
  if(typeof value.upstream_request_id==='string' && /^(?:(?:req|request)[-_][a-fA-F0-9]{8,64}|[a-fA-F0-9]{8}-(?:[a-fA-F0-9]{4}-){3}[a-fA-F0-9]{12})$/.test(value.upstream_request_id) && ['header','body'].includes(value.upstream_request_id_source)){
    safe.upstream_request_id=value.upstream_request_id;safe.upstream_request_id_source=value.upstream_request_id_source;
  }
  return safe;
}
function safeFailureDiagnostics(subject=null){
  const answer=subject || result() || {},agent=subject?{}:(state.project?.agent || {});
  const summary=answer.diagnostic_summary || agent.diagnostic_summary || {};
  const candidates=[...(Array.isArray(answer.diagnostics)?answer.diagnostics:[]),...(Array.isArray(summary.api_failures)?summary.api_failures:[]),
    {diagnostic:answer.diagnostic},{diagnostic:agent.diagnostic}];
  const found=[],seen=new Set();
  for(const item of candidates.slice(0,128)){
    const diagnostic=safeApiDiagnostic(item?.diagnostic || item);if(!diagnostic || seen.has(diagnostic.request_id))continue;
    seen.add(diagnostic.request_id);
    const stage=['answer','discover','investigate','synthesis_review','continuation','unknown','capability_probe','provider_request','context_assembly','response_parse','response_validation','configuration','direct','map','reduce','model_request','repository_search','page','program','synthesis'].includes(item.stage)?item.stage:'unknown';
    found.push({stage,...diagnostic});if(found.length>=64)break;
  }
  return found;
}
function failureDiagnosticText(value){
  const safe=safeApiDiagnostic(value);if(!safe)return '';
  return (safe.evidence_source==='local'?t('本機處理')+'：':'')+safe.reason+' '+t('下一步')+'：'+safe.next_step+(safe.http_status?' HTTP '+safe.http_status+'。':'')+' '+t('本地關聯編號')+'：'+safe.request_id+(safe.upstream_request_id?' '+t('上游關聯編號')+'：'+safe.upstream_request_id:'');
}
function failureSummaryText(value){
  const safe=safeApiDiagnostic(value);if(!safe)return '';
  return safe.reason+' '+t('下一步')+'：'+safe.next_step+(safe.http_status && !safe.reason.includes('HTTP '+safe.http_status)?' HTTP '+safe.http_status+'。':'');
}
function failureDiagnosticsView(subject=null){
  if(!subject && (state.mode!=='real' || draftChanged()))return '';
  const failures=safeFailureDiagnostics(subject);if(!failures.length)return '';
  return ui`<section class="narrative-notes request-failure" role="status"><div class="failure-heading">${icon('alert')}本次請求未完成</div>${failures.slice(-4).map(failure=>`<p><strong>${failure.evidence_source==='local'?escapeHTML(t('本機處理'))+'：':''}${escapeHTML(failure.reason)}</strong>${failure.http_status?' · HTTP '+failure.http_status:''}</p><p>${escapeHTML(t('下一步'))}：${escapeHTML(failure.next_step)}</p>`).join('')}<details><summary>查看安全錯誤詳情</summary>${failures.slice(-4).map(failure=>`<p><small>${escapeHTML(t('本地關聯編號'))}：<code>${escapeHTML(failure.request_id)}</code>${failure.upstream_request_id?` · ${escapeHTML(t('上游關聯編號'))}：<code>${escapeHTML(failure.upstream_request_id)}</code>`:''}</small></p>`).join('')}<pre class="api-raw-text">${escapeHTML(JSON.stringify(failures,null,2))}</pre></details></section>`;
}
function browserDiagnostic(category,httpStatus=null){
  let identifier;
  if(globalThis.crypto?.randomUUID)identifier=globalThis.crypto.randomUUID().replaceAll('-','');
  else identifier=Array.from({length:32},()=>Math.floor(Math.random()*16).toString(16)).join('');
  return safeApiDiagnostic({schema_version:'safe-api-error/v1',category,http_status:httpStatus,evidence_source:'local',request_id:'local-'+identifier});
}
function diagnosticSummaryData(){
  const summary=result()?.diagnostic_summary || state.project?.agent?.diagnostic_summary;
  if(!summary || summary.schema_version!=='business-answer-diagnostics/v1')return null;
  const number=value=>Number.isSafeInteger(value) && value>=0?value:null;
  const enumeration=(value,allowed)=>allowed.includes(value)?value:null;
  const boolean=value=>typeof value==='boolean'?value:null;
  const finish=value=>enumeration(value,['stop','length','content_filter','tool_calls','function_call','other','unknown']);
  const fingerprint=value=>typeof value==='string' && /^[a-f0-9]{64}$/.test(value)?value:null;
  const integers=(source,keys)=>Object.fromEntries(keys.map(key=>[key,number(source?.[key])]));
  const runtime=summary.runtime || {},configured=summary.configured || {},coverage=summary.source_coverage || {},output=summary.output || {};
  const requests=Array.isArray(summary.requests)?summary.requests.filter(item=>item && typeof item==='object').slice(0,64).map(item=>({
    round_id:Number.isSafeInteger(item.round_id) && item.round_id>=0?item.round_id:typeof item.round_id==='string' && /^round-[0-9]{1,6}$/.test(item.round_id)?item.round_id:null,
    stage:enumeration(item.stage,['answer','discover','investigate','synthesis_review','continuation','unknown']),
    ...integers(item,['request_bytes','source_file_count','page_count','source_characters','source_line_count','trim_event_count','history_message_count','history_characters','raw_content_characters','parsed_answer_characters','choice_index']),
    finish_reason:finish(item.finish_reason),
  })):[];
  const answer=result();const currentMessage=state.conversation?.messages?.findLast(item=>item.role==='assistant' && item.run_id===state.project?.run_id);
  const renderInput=conversationMode()?currentMessage?.content:narrativeText(answer);
  const omissions=(state.project?.agent?.display_projection?.omitted || []).filter(item=>['agent_result.answer','agent_result.narrative.text'].includes(item?.path));
  const omitted=omissions.reduce((largest,item)=>Math.max(largest,Math.max(0,(number(item.total_characters) || 0)-(number(item.retained_characters) || 0))),0);
  return {
    schema_version:'business-answer-diagnostics/v1',
    runtime:{commit:typeof runtime.commit==='string' && (/^[a-f0-9]{7,64}$/.test(runtime.commit) || runtime.commit==='unknown')?runtime.commit:null,
      profile:enumeration(runtime.profile,['workbench','adapter','analysis','custom','unknown']),model_fingerprint:fingerprint(runtime.model_fingerprint),
      output_limit_source:enumeration(runtime.output_limit_source,['profile','environment','dotenv','explicit','unknown'])},
    configured:{...integers(configured,['max_output_tokens','max_model_requests','max_source_characters','max_complete_source_characters','max_request_bytes']),requested_detail:enumeration(configured.requested_detail,['brief','detailed'])},
    requests,
    api_failures:safeFailureDiagnostics({diagnostic_summary:summary}),
    source_coverage:{...integers(coverage,['indexed_files','selected_files','provided_files','pages','unread_tasks','omitted_complete_files','supplied_complete_files']),
      working_set_status:enumeration(coverage.working_set_status,['not_applicable','fallback','partial','supplied','unknown']),
      working_set_reason:enumeration(coverage.working_set_reason,['source_identity_not_eligible','source_set_unavailable','snapshot_changed','source_not_indexed','dependency_depth_budget','dependency_file_budget','source_byte_budget','source_characters','complete_source_characters','source_hash_mismatch','source_read_unavailable','candidate_source_set_supplied','unknown']),
      physical_complete:boolean(coverage.physical_complete),closure_complete:boolean(coverage.closure_complete),complete_root_budget_applied:boolean(coverage.complete_root_budget_applied),
      transmission_fallback_reason:enumeration(coverage.transmission_fallback_reason,['complete_source_request_bytes','unknown']),
      identity_status:enumeration(coverage.identity_status,['resolved','ambiguous','not_found','not_requested','unknown']),
      retrieval_status:enumeration(coverage.retrieval_status,['source_candidates','framework_candidates','unresolved','not_attempted','unknown']),
      limitation_codes:Array.isArray(coverage.limitation_codes)?coverage.limitation_codes.filter(value=>['index_not_ready','explicit_source_not_indexed','source_identity_ambiguous','search_no_match','business_terms_unresolved','located_source_not_read','source_budget_omitted','source_read_failed','external_implementation_missing','runtime_target_unresolved','output_limit_reached','parser_failed','request_budget_exhausted','answer_incomplete','unresolved_inputs'].includes(value)).slice(0,16):[]},
    output:{finish_reason:finish(output.finish_reason),answer_truncated:output.answer_truncated===true,
      continuation_attempted:output.continuation_attempted===true,final_answer_characters:number(output.final_answer_characters)},
    stop_reason:enumeration(summary.stop_reason,['sufficient_material','MODEL_OUTPUT_TRUNCATED','answer_incomplete','request_budget','usable_draft_retained','source_identity_ambiguous','source_identity_not_found','unsupported_citations','evidence_incomplete','question_evidence_incomplete','evidence_read_incomplete','working_set_transmission_incomplete','tool_unavailable','RETRIEVAL_UNRESOLVED','REQUEST_TIMEOUT','MODEL_CLIENT_ERROR','MODEL_RESPONSE_INVALID','MODEL_REFUSED','MODEL_CONTENT_FILTERED','MODEL_REQUEST_BUDGET_EXHAUSTED','completed','model_abstained','unknown','other']),
    rendering:{view:conversationMode()?'conversation':'answer',render_input_characters:typeof renderInput==='string'?Array.from(renderInput).length:null,
      persisted_conversation_characters:typeof currentMessage?.content==='string'?Array.from(currentMessage.content).length:null,
      projected_answer_truncated:omitted>0,projected_answer_omitted_characters:omitted},
  };
}
function diagnosticSummaryView(){
  const summary=diagnosticSummaryData();if(!summary)return '';
  return ui`<section class="api-exchange safe-diagnostics"><h3>可分享的診斷摘要</h3><p class="field-hint">僅含配置限制、覆蓋數量、完成狀態、字數與安全錯誤診斷。問題、源碼、回答正文、路徑及 API 配置值不會匯出。</p><button class="button secondary" data-copy-safe-diagnostics>複製診斷 JSON</button><button class="button secondary" data-export-safe-diagnostics>匯出診斷 JSON</button><details><summary>查看診斷摘要</summary><pre class="api-raw-text">${escapeHTML(JSON.stringify(summary,null,2))}</pre></details></section>`;
}
async function copyDiagnosticSummary(){
  const summary=diagnosticSummaryData();if(!summary)return;
  try{await navigator.clipboard.writeText(JSON.stringify(summary,null,2));toast(t('已複製診斷摘要。'));}
  catch{toast(t('複製暫不可用，請使用匯出診斷 JSON。'));}
}
function apiResponseView(){
  if(state.mode!=='real')return '';
  if(draftChanged())return ui`<div class="answer-empty">${icon('clock')}<h2>新問題尚未取得 API 返回</h2><p>問題、調查起點或閱讀方式已更改。發起分析後，這裡會顯示本次請求的返回資料。</p></div>`;
  const diagnostic=state.project?.agent?.api_diagnostics;const exchanges=apiExchanges();
  const ordered=exchanges.map((item,index)=>({item,sequence:Number.isInteger(item.sequence)?item.sequence:index+1})).sort((a,b)=>Number(b.item.phase==='investigation')-Number(a.item.phase==='investigation') || b.sequence-a.sequence);
  return ui`<div class="api-response-view"><div class="answer-state-row"><span class="badge warning">未核驗的 API 返回</span><small>${exchanges.length} 次請求</small></div><h2>先確認接口返回，再核對分析結果。</h2><p class="answer-intro">這裡展示接口實際返回的文字，用於核對連線與返回格式；不代表已通過業務證據核驗。${exchanges.some(item=>item.phase==='capability_probe')?t('能力探測成功不代表業務分析已完成。'):''}</p>${apiSummary()}${diagnosticSummaryView()}${unacceptedResponseView()}${reportDisplayNotice()}<p class="api-privacy-note">API 請求記錄已遮蔽配置值，內容可能因大小限制而截斷。保留的模型正文可能包含源碼或業務內容，請在分享前核對。</p>${diagnostic?.truncated || diagnostic?.omitted_exchange_count>0?ui`<p class="api-truncated">已達診斷保存上限，部分返回內容或請求記錄未保存。${diagnostic.omitted_exchange_count>0?ui` 未保存的請求記錄：${formatNumber(diagnostic.omitted_exchange_count)}`:''}</p>`:''}${exchanges.length?ui`<p class="field-hint">業務調查優先；各階段的最新請求優先，序號保留實際呼叫順序。</p>`:''}${ordered.length?ordered.map(({item,sequence})=>{
    const message=apiMessage(item);const content=message?.content;
    const contentText=typeof content==='string'?content:content===null || content===undefined?'':JSON.stringify(content,null,2);
    const phase=item.phase==='capability_probe'?t('能力探測'):item.phase==='investigation'?t('業務調查'):t('未知階段');
    const http=Number.isInteger(item.http_status)?'HTTP '+item.http_status:t('未收到 HTTP 回應');
    return ui`<section class="api-exchange"><div class="api-exchange-heading"><h3>${sequence} · ${escapeHTML(phase)}</h3><span class="badge ${item.http_status>=200 && item.http_status<300?'neutral':'warning'}">${escapeHTML(http)}</span></div><p class="api-exchange-meta"><code>${escapeHTML(item.endpoint || '—')}</code> · ${escapeHTML(item.outcome_code || t('狀態未提供'))}${Number.isFinite(item.elapsed_ms)?' · '+formatNumber(item.elapsed_ms)+' ms':''}${Number.isFinite(item.body_bytes)?' · '+formatBytes(item.body_bytes):''}</p>${item.body_truncated?ui`<p class="api-truncated">返回內容已截斷；未顯示部分不能作為缺失資料判斷。</p>`:''}${item.body_omitted_reason?`<p class="api-truncated">${escapeHTML(apiBodyOmission(item.body_omitted_reason))}</p>`:''}${contentText?ui`<div class="api-message"><h4>模型返回文字 · 未核驗</h4><pre class="api-raw-text">${escapeHTML(contentText)}</pre></div>`:ui`<p class="field-hint">未提取到 message.content；請查看下方原始返回（可能是工具呼叫、錯誤或截斷內容）。</p>`}<details class="api-raw-body" open><summary>原始返回內容 · 已遮蔽配置值</summary><pre class="api-raw-text">${escapeHTML(typeof item.body_text==='string' && item.body_text?item.body_text:t('未記錄返回內容。'))}</pre></details></section>`;
  }).join(''):ui`<div class="answer-empty"><p>${state.project?.agent?.runner_status==='NETWORK_DISABLED'?t('尚未發起模型分析；接入源碼本身不會呼叫模型。'):diagnostic?.captured?t('本次尚未發出接口請求，請先處理接入或配置問題。'):state.project?.agent?t('這份分析未保存 API 返回原文。重新發起分析後，可在這裡查看。'):t('尚未發起模型分析；接入源碼本身不會呼叫模型。')}</p></div>`}</div>`;
}
function narrativeText(answer){
  if(answer?.status==='ABSTAINED' || answer?.model_answer_recorded===false)return '';
  if(typeof answer?.narrative?.text==='string' && answer.narrative.text.trim())return answer.narrative.text;
  return answer?.analysis_mode==='source_reading' && typeof answer.answer==='string' && answer.answer.trim()?answer.answer:'';
}
function narrativeMarkup(text,refs,frameworkRefs,allowedSourceIds=null){
  const sourceIds=new Map(refs.filter(ref=>typeof ref?.evidence_id==='string').map((ref,index)=>[ref.evidence_id,index+1]).filter(([id])=>!allowedSourceIds || allowedSourceIds.has(id)));
  const frameworkIds=new Map(frameworkRefs.filter(ref=>typeof ref?.reference_id==='string').map((ref,index)=>[ref.reference_id,index+1]));
  return answerMarkdown.render(text,id=>sourceIds.has(id)?cite(id,String(sourceIds.get(id))):frameworkIds.has(id)?frameworkCite(id,frameworkIds.get(id)):null);
}
function readingCoverageText(coverage){
  if(!coverage || typeof coverage!=='object')return '';
  const known=value=>Number.isFinite(value) && value>=0;const items=[];
  if(known(coverage.summarized_pages) && known(coverage.total_pages))items.push(ui`已解讀 ${formatNumber(coverage.summarized_pages)} / ${formatNumber(coverage.total_pages)} 頁（本次源碼範圍）`);
  else if(known(coverage.selected_pages) && known(coverage.total_pages))items.push(ui`已讀 ${formatNumber(coverage.selected_pages)} / ${formatNumber(coverage.total_pages)} 頁（本次源碼範圍）`);
  if(known(coverage.sent_pages))items.push(ui`已發送 ${formatNumber(coverage.sent_pages)} 頁`);
  if(known(coverage.planned_pages))items.push(ui`本次選取 ${formatNumber(coverage.planned_pages)} 頁`);
  const lines=known(coverage.summarized_lines)?coverage.summarized_lines:coverage.selected_lines;
  if(known(lines) && known(coverage.total_lines))items.push(ui`${formatNumber(lines)} / ${formatNumber(coverage.total_lines)} 行`);
  if(known(coverage.total_files))items.push(ui`${formatNumber(coverage.total_files)} 個檔案`);
  if(coverage.reading_strategy==='full_chain')items.push(t('完整業務鏈 · 分批閱讀'));
  if(known(coverage.completed_batches) && known(coverage.total_batches))items.push(ui`已完成批次 ${formatNumber(coverage.completed_batches)} / ${formatNumber(coverage.total_batches)}`);
  return items.join(' · ');
}
function programSummaries(answer){return Array.isArray(answer?.program_summaries)?answer.program_summaries.filter(item=>typeof item?.text==='string' && item.text.trim()):[];}
function programSummariesView(answer,refs,frameworkRefs){
  const summaries=programSummaries(answer);if(!summaries.length)return '';
  return ui`<details class="narrative-sources program-summaries"><summary>各來源的業務解讀 · ${summaries.length}</summary><p class="field-hint">按來源保留的模型解讀，可補充上方業務摘要；仍需業務覆核。</p>${summaries.map(item=>{
    const names=Array.isArray(item.program_names)?item.program_names.filter(name=>typeof name==='string'):[];
    const label=names.join(', ') || item.program_name || item.relative_path || t('源碼引用');
    const allowed=new Set(Array.isArray(item.evidence_ids)?item.evidence_ids:[]);
    return `<details class="program-summary"><summary>${escapeHTML(label)}${item.complete===false?` <span class="badge neutral">${escapeHTML(t('部分解讀'))}</span>`:''}<small>${escapeHTML(item.relative_path || '')}</small></summary><div class="narrative-body">${narrativeMarkup(item.text,refs,frameworkRefs,allowed)}</div></details>`;
  }).join('')}</details>`;
}
function callChainCoverageText(coverage){
  const chain=coverage?.call_chain;
  if(!chain || !['covered_calls','resolved_calls','uncovered_calls'].every(key=>Number.isFinite(chain[key]) && chain[key]>=0))return '';
  return ui`已選入相關片段的呼叫：${formatNumber(chain.covered_calls)} / ${formatNumber(chain.resolved_calls)} 個已定位目標的呼叫 · ${formatNumber(chain.uncovered_calls)} 個尚未覆蓋${Number.isFinite(chain.unresolved_calls) && chain.unresolved_calls>0?ui` · 另有 ${formatNumber(chain.unresolved_calls)} 個呼叫目標未確認`:''}`;
}
function callChainCoverageNote(){return t('這是本次入口閱讀計劃的資料覆蓋，只表示呼叫位置、被呼叫程式入口與介面片段已選入；不代表模型已讀完、整條呼叫鏈完整或業務結果已驗證。');}
function narrativeNotes(answer,project=state.project){
  const coverage=answer?.reading_coverage || {};const notes=[];
  const read=Number.isFinite(coverage.sent_pages)?coverage.sent_pages:coverage.selected_pages;
  if(Number.isFinite(coverage.total_pages) && Number.isFinite(read) && coverage.total_pages>read)notes.push(ui`尚有 ${formatNumber(coverage.total_pages-read)} 頁未讀，本次解讀先涵蓋已讀內容。`);
  const failed=Number(coverage.failed_pages || coverage.failed_requests || 0);
  if(failed>0 || (answer?.stop_reason && !['completed','model_abstained','output_truncated'].includes(answer.stop_reason)))notes.push(t('部分分析請求未完成，已保留取得的解讀；詳情可查看 API 返回。'));
  const truncated=Array.isArray(coverage.truncated_response_pages)?coverage.truncated_response_pages.length:Number(coverage.truncated_response_pages || 0);
  if(truncated>0 || answer?.stop_reason==='output_truncated')notes.push(t('部分模型回應尚未完整輸出，已保留可用的解讀內容。'));
  if(answer?.status==='PARTIAL' && !notes.length)notes.push(t('目前呈現已取得的解讀，尚未完成的範圍可繼續分析。'));
  return notes;
}
function answerCompletionNotice(answer,projection=null){
  const displayOmission=Array.isArray(projection?.omitted) && projection.omitted.some(item=>['agent_result.answer','agent_result.narrative.text'].includes(item?.path) && Number(item.total_characters)>Number(item.retained_characters));
  const displayNotice=answer?.answer_display_truncated===true || displayOmission?ui`<div class="narrative-notes answer-incomplete" role="status"><p>已達頁面展示字數上限，這裡僅呈現部分正文。完整模型正文仍保存在本機結果檔。</p></div>`:'';
  if(answer?.finish_reason==='length' || answer?.stop_reason==='output_truncated')return displayNotice+ui`<div class="narrative-notes answer-incomplete" role="status"><p>回答未完整輸出：模型達到輸出長度限制（finish_reason=length）。已保留收到的完整正文，可繼續追問尚未說明的部分。</p></div>`;
  if(answer?.answer_truncated===true || answer?.stop_reason==='MODEL_OUTPUT_TRUNCATED')return displayNotice+ui`<div class="narrative-notes answer-incomplete" role="status"><p>部分模型回應尚未完整輸出，已保留可用的解讀內容。</p></div>`;
  if(answer?.status==='PARTIAL' || answer?.analysis_status==='PARTIAL')return displayNotice+ui`<div class="narrative-notes answer-incomplete" role="status"><p>部分業務解讀：已保留取得的正文，尚未完成的範圍可繼續分析。</p></div>`;
  return displayNotice;
}
function investigationView(answer=result()){
  const investigation=answer?.investigation || diagnosis()?.investigation;
  if(!investigation || investigation.mode!=='repository')return '';
  const searches=Array.isArray(investigation.searches)?investigation.searches:[];
  const count=value=>Number.isFinite(value) && value>=0?formatNumber(value):'—';
  const chosen=investigation.selected_file_count;
  const total=investigation.repository_file_count;
  return ui`<details class="repository-investigation"><summary>本次查找 · ${count(chosen)} 個相關檔案</summary><p>整個代碼庫 · 自動查找</p><p>目錄共有 ${count(total)} 個檔案，本次選入 ${count(chosen)} 個檔案繼續閱讀。這是問題相關範圍，不表示全庫源碼已讀完。</p>${Array.isArray(investigation.selected_paths) && investigation.selected_paths.length?`<details><summary>${escapeHTML(t('本次選入的檔案'))}</summary><ul>${investigation.selected_paths.map(path=>`<li>${escapeHTML(path)}</li>`).join('')}</ul></details>`:''}${investigation.deferred_candidates?.length?ui`<p>另有 ${formatNumber(investigation.deferred_candidates.length)} 個候選檔案尚未納入閱讀。</p>`:''}${searches.length?`<ul>${searches.map(search=>`<li><strong>${escapeHTML(search.query || '')}</strong> · ${escapeHTML(t('匹配檔案'))} ${count(search.matched_file_count ?? (Array.isArray(search.matched_files)?search.matched_files.length:search.matched_files))}${Array.isArray(search.matched_files) && search.matched_files.length?`<small>${search.matched_files.map(file=>escapeHTML(typeof file==='string'?file:file.relative_path || file.path || '')).join(' · ')}</small>`:''}</li>`).join('')}</ul>`:''}${Array.isArray(investigation.selection_reasons)?investigation.selection_reasons.map(reason=>`<p>${escapeHTML(typeof reason==='string'?reason:JSON.stringify(reason))}</p>`).join(''):investigation.selection_reason?`<p>${escapeHTML(investigation.selection_reason)}</p>`:''}</details>`;
}
function reportDisplayNotice(){
  const projections=[state.project?.agent?.display_projection,state.project?.display_projection].filter(Boolean);
  if(!projections.length)return '';
  return ui`<p class="source-note">大型結果已按頁面展示量整理。完整逐頁解讀、引用與接口記錄仍保存在結果資料夾。</p>`;
}
function narrativeAnswer(answer){
  const refs=currentRefs();const frameworkRefs=answer.framework_context?.references || diagnosis()?.framework_context?.references || [];
  const coverage=answer.reading_coverage;const coverageText=readingCoverageText(coverage);const chainText=callChainCoverageText(coverage);const notes=narrativeNotes(answer);
  const boundaries=(answer.boundaries || []).filter(item=>!['framework_reference_boundary','snapshot_coverage'].includes(item?.type));
  return ui`<article class="narrative-answer"><div class="answer-state-row"><span class="badge neutral">業務解讀</span><small>${answer.status==='PARTIAL'?t('部分解讀'):''}</small></div>${answerCompletionNotice(answer,state.project?.agent?.display_projection)}<div class="narrative-body">${narrativeMarkup(narrativeText(answer),refs,frameworkRefs)}</div><footer class="narrative-provenance">${investigationView(answer)}${programSummariesView(answer,refs,frameworkRefs)}<details class="answer-support"><summary>依據與補充說明</summary>${notes.length?`<div class="narrative-notes">${notes.map(note=>`<p>${escapeHTML(note)}</p>`).join('')}</div>`:''}${reportDisplayNotice()}${coverageText?ui`<div class="reading-coverage"><strong>閱讀覆蓋</strong><p>${escapeHTML(coverageText)}</p><small>${coverage.complete?t('本次源碼範圍已讀完；閱讀完整不等於業務結論已驗證。'):t('閱讀覆蓋描述本次讀取範圍，不代表已驗證所有業務路徑。')}</small></div>`:''}${chainText?ui`<details class="narrative-boundaries call-chain-coverage"><summary>${escapeHTML(chainText)}</summary><p>${escapeHTML(callChainCoverageNote())}</p></details>`:''}${refs.length?ui`<details class="narrative-sources"><summary>閱讀來源 · ${refs.length} 頁</summary><div>${refs.map((ref,index)=>cite(ref.evidence_id,`${index+1} · ${ref.relative_path || ref.path || t('源碼引用')} · L${Number(ref.start_line)}–${Number(ref.end_line)}`)).join('')}</div></details>`:''}${boundaries.length?ui`<details class="narrative-boundaries"><summary>補充上下文</summary>${boundaries.map(item=>`<p>${escapeHTML(boundaryText(item))}</p>`).join('')}</details>`:''}</details></footer></article>`;
}
function actualAnswer(){return failureDiagnosticsView()+actualAnswerBody();}
function actualAnswerBody(){
  const answer=result();const d=diagnosis();const status=answer?.status || d?.question_status;
  if(syntheticProject() && sourceUsable() && state.project?.agent?.runner_status==='NETWORK_DISABLED'){
    const files=sourceFiles();const gaps=d?.unresolved_dependencies || [];
    return ui`<article class="prepared-case"><div class="answer-state-row"><span class="badge positive">${icon('check')}案例已載入</span><small>合成源碼</small></div><h2>案例已載入，尚未呼叫模型</h2><p class="answer-intro">可修改問題並按「開始業務分析」，從目前源碼生成自己的業務解讀。</p><div class="guide-type"><span>調查起點</span><strong>${escapeHTML(state.entry)}</strong></div><p class="source-note">${formatNumber(files.candidate || files.decoded)} 個檔案 · ${formatNumber(currentPrograms().length)} 個目錄入口</p>${gaps.length?ui`<details class="narrative-boundaries"><summary>${formatNumber(gaps.length)} 項未確認依賴</summary><p>部分產生式或閉源依賴未提供，仍可先分析已有源碼。</p>${dependencyGaps()}</details>`:''}</article>`;
  }
  if(draftChanged())return ui`<div class="answer-empty">${icon('spark')}<h2>準備分析新的問題</h2><p>問題、調查起點或閱讀方式已更改。發起分析後，這裡會呈現新問題的結果。<br>上一題摘要保留在分析記錄中。</p></div>`;
  if(unacceptedResponse())return ui`<div class="answer-empty">${icon('info')}<h2>本次業務結果未獲接受</h2><p>源碼或結果核對未通過，已保留收到的模型文字供診斷。</p><button class="button secondary" data-tab="api">查看 API 返回 ${icon('arrow')}</button></div>`;
  if(narrativeText(answer))return narrativeAnswer(answer);
  const stopped=investigationStop();
  if(stopped)return ui`<div class="answer-empty">${icon('alert')}<h2>分析流程中斷</h2><p>${escapeHTML(stopExplanation(stopped.reason))}</p><p class="stop-detail"><code>${escapeHTML(stopped.code)}</code></p><button class="button secondary" data-tab="api">查看 API 返回 ${icon('arrow')}</button></div>${apiSummary()}${renderBoundaries(answer?.boundaries || d?.messages || [])}`;
  if(!answer || !answer.claims?.length){
    const failed=answer || Boolean(d?.question) && ['NOT_READY','SAFE_STOP','BLOCKED','SCOPE_LIMIT'].includes(status);
    return `<div class="answer-empty">${icon(failed?'info':'spark')}<h2>${escapeHTML(failed?statusLabel(status):t('源碼已就緒'))}</h2><p>${failed?t('本次尚未取得業務解讀，請查看 API 返回中的具體原因後重試。'):t('直接輸入業務問題，系統會從代碼庫自動查找相關實作。也可以選擇程式縮小範圍。')}</p>${state.project?.agent?ui`<button class="button secondary" data-tab="api">查看 API 返回 ${icon('arrow')}</button>`:''}</div>${renderBoundaries(answer?.boundaries || d?.messages || [])}${state.project?.agent?.reason_code?ui`<p class="source-note">診斷代碼：${escapeHTML(state.project.agent.reason_code)}</p>`:''}`;
  }
  const refs=currentRefs();const frameworkRefs=answer.framework_context?.references || d?.framework_context?.references || [];
  return ui`<div class="real-answer"><div class="answer-state-row"><span class="badge neutral">${icon('check')}${escapeHTML(statusLabel(answer.status))}</span><small>${refs.length} 段引用</small></div><h2>來自當前源碼的分析結果</h2><p class="answer-intro">以下陳述保留實際核驗狀態。點選引用，可回到本次快照的源碼位置。</p>${scopeNotice()}<div class="steps">${answer.claims.map((claim,i)=>`<div class="business-step"><span class="step-number">${String(i+1).padStart(2,'0')}</span><div><div class="claim-text narrative-body">${narrativeMarkup(claim.claim || claim.text || '',refs,frameworkRefs)}${(claim.evidence_ids || []).map(id=>cite(id,String(Math.max(1,refs.findIndex(r=>r.evidence_id===id)+1)))).join('')}${(claim.framework_reference_ids || []).map(id=>{const index=frameworkRefs.findIndex(ref=>ref.reference_id===id);return index<0?'':frameworkCite(id,index+1);}).join('')}</div><small class="field-hint">${escapeHTML(({code_fact:t('源碼事實'),framework_interpretation:t('框架條件解讀 · 需源碼與現場覆核'),business_inference:t('業務推測 · 未核驗'),open_question:t('待確認問題 · 未形成結論')})[claim.kind] || t('候選解讀'))} · ${escapeHTML(({supported:t('局部語句已核驗'),citation_verified_only:t('引用已核對，語義待核驗'),unsupported:t('證據尚不支持')})[claim.support_status] || t('語義未核驗'))}</small></div></div>`).join('')}</div>${renderBoundaries(answer.boundaries || [])}<div class="answer-footnote">${icon('info')}問題相關性與完整性尚未核驗；局部語句通過不代表整條業務流程已驗證。</div></div>`;
}
function renderBoundaries(items){if(!items.length)return '';return ui`<details class="answer-support"><summary>依據與補充說明</summary>${items.map(b=>`<p>${escapeHTML(boundaryText(b))}</p>`).join('')}</details>`;}
function relationFrameworkOperation(edge){
  const answer=result();
  if(state.mode!=='real' || !answer?.snapshot_id || answer.snapshot_id!==state.project?.snapshot_id || edge.relation_type!=='CALLS')return null;
  if(!edge.relative_path || !edge.source_program || !edge.target_name || !Number.isInteger(edge.start_line) || !Number.isInteger(edge.end_line))return null;
  return (answer.framework_context?.facts || []).find(fact=>fact.dependency_covered===true && fact.relation_type==='CALLS'
    && fact.relative_path===edge.relative_path && fact.program_name===edge.source_program && fact.target_name===edge.target_name
    && fact.start_line===edge.start_line && fact.end_line===edge.end_line && typeof fact.operation?.meaning==='string' && fact.operation.meaning.trim())?.operation || null;
}
function relationsView(){
  const edges=currentRelations();return ui`<h2>沿著程式，回查關係。</h2><p class="graph-subtitle">${state.mode==='demo'?t('以下關係來自合成案例源碼中的呼叫語句，不代表實際執行順序。'):t('以下來自當前源碼的靜態呼叫與 COPY 關係，不表示執行順序或實際可達。')}${state.project?.relations?.truncated && state.mode==='real'?t(' 關係數量已達顯示上限。'):''}</p>${edges.length?edges.slice(0,40).map(edge=>{const operation=relationFrameworkOperation(edge);return `<div class="relation-row"><div class="relation-program">${escapeHTML(edge.source_program || edge.relative_path || t('來源'))}<span class="relation-meta">${escapeHTML(edge.relation_type==='INCLUDES_COPY'?t('引入定義'):t('呼叫來源'))}</span></div><div class="relation-link">${escapeHTML(edge.relation_type)}</div><div class="relation-program">${escapeHTML(edge.target_name || t('未解析目標'))}<span class="relation-meta">${escapeHTML(operation?t('框架操作已識別'):({confirmed:t('源碼關係已確認'),candidate:t('候選關係'),unresolved:t('未能確認'),illustrative:t('示例關係'),source_observed:t('源碼呼叫已讀取')})[edge.status] || edge.status)}</span>${operation?`<small class="relation-meta">${escapeHTML(operation.meaning)}${operation.caveat?' '+escapeHTML(operation.caveat):''}</small><small class="relation-meta">${escapeHTML(t('框架操作說明不確認本次返回值或實際執行結果。'))}</small>`:''}</div>${edge.evidence_id && (state.mode!=='demo' || currentRefs().some(ref=>ref.evidence_id===edge.evidence_id))?cite(edge.evidence_id,'↗'):''}</div>`;}).join(''):t('<div class="answer-empty"><p>本次沒有可展示的呼叫或 COPY 關係；不能據此推斷不存在依賴。</p></div>')}${edges.length>40?t('<p class="source-note">目前顯示前 40 項關係。完整回傳資料可在匯出檔中查看。</p>'):''}`;
}
function traceView(){
  if(draftChanged())return ui`<div class="answer-empty">${icon('clock')}<h2>新問題尚未執行調查</h2><p>問題、調查起點或閱讀方式已更改。發起分析後，這裡會顯示新問題的工具記錄。</p></div>`;
  if(state.mode==='demo')return ui`<div class="answer-empty"><h2>尚未發起模型分析</h2><p>合成業務案例不會生成調查記錄。載入案例並開始分析後，才會顯示本次活動。</p></div>`;
  const trace=result()?.tool_trace || [];const labels={read_source_page:t('逐頁讀取源碼'),read_source_pages:t('逐頁讀取源碼'),analyze_source_pages:t('解讀已讀源碼'),synthesize_analysis:t('整合業務解讀'),search_repository:t('查找相關業務實作'),plan_repository_search:t('理解問題並展開查找'),search_code:t('定位程式與欄位'),inspect_symbol:t('檢視定義與依賴'),trace_relations:t('追蹤源碼關係'),read_evidence:t('讀取源碼證據')};
  return ui`<h2>本次調查記錄</h2><p class="graph-subtitle">只顯示實際工具活動與狀態，不包含模型私有推理。</p>${trace.length?trace.map((item,i)=>{const name=item.tool || item.action || item.tool_name;const outcome=({SUCCEEDED:t('已執行'),FAILED:t('執行失敗'),REJECTED:t('參數未通過核對'),POLICY_REJECTED:t('調查範圍受限'),SNAPSHOT_REJECTED:t('快照核對失敗'),ATTEMPTED:t('已嘗試')})[item.outcome] || item.outcome || t('狀態未提供');return ui`<div class="trace-row"><h3>${String(i+1).padStart(2,'0')} · ${escapeHTML(labels[name] || name || t('工具活動'))}</h3><p><code>${escapeHTML(name)}</code> · ${escapeHTML(outcome)}${item.result?.status?' · '+escapeHTML(item.result.status):''}</p><details class="trace-details"><summary>查看工具資料</summary><pre>${escapeHTML(JSON.stringify(item.arguments || {},null,2))}</pre></details></div>`;}).join(''):t('<div class="answer-empty"><p>尚未執行模型調查。建立索引不會產生模型工具記錄。</p></div>')}`;
}
function codeMarkup(span){
  if(!span)return t('<div class="answer-empty"><p>選擇一段引用，查看源碼內容。</p></div>');
  const lines=String(span.source_text || '').split('\n');
  return lines.map((line,i)=>{let text=escapeHTML(line);text=text.replace(/\b(COMPUTE|ROUNDED|END-COMPUTE|MOVE|TO|EXEC|SQL|SELECT|INTO|FROM|WHERE|END-EXEC|PERFORM|CALL|IF|END-IF|GOBACK)\b/g,'<span class="code-keyword">$1</span>');return `<div class="code-line"><span class="line-number">${Number(span.start_line || 1)+i}</span><span class="code-text">${text || ' '}</span></div>`;}).join('');
}
function evidencePanel(){
  const refs=currentRefs();const span=draftChanged() && state.tab!=='relations'?null:state.evidence;const demo=state.mode==='demo';
  return ui`<aside class="evidence-panel" aria-label="源碼證據"><div class="evidence-heading"><span>${icon('code')}</span><h2>源碼證據</h2><small>${String(refs.length).padStart(2,'0')}</small></div><div class="evidence-summary"><span>${demo?t('案例源碼引用'):t('本次回答引用')}</span><span>${demo?t('合成文件原文'):t('點選檢視原文')}</span></div><div class="evidence-list">${refs.length?refs.map((ref,i)=>`<button class="evidence-item ${state.selectedEvidence===ref.evidence_id?'selected':''}" data-evidence="${escapeHTML(ref.evidence_id)}"><span class="evidence-number">${String(i+1).padStart(2,'0')}</span><span><strong>${escapeHTML((demo?demoText(ref.title):ref.title) || ref.relative_path?.split('/').pop() || t('源碼引用'))}</strong><small>${escapeHTML(ref.relative_path?.split('/').pop() || '')} · L${Number(ref.start_line)}–${Number(ref.end_line)}</small></span>${icon('chevron')}</button>`).join(''):t('<div class="answer-empty"><p>分析結論的引用<br>將在這裡顯示。</p></div>')}</div><div class="code-header"><span>${icon('document')} ${escapeHTML(span?.relative_path?.split('/').pop() || t('尚未選擇證據'))}</span><span>COBOL</span></div><div class="code-body" aria-label="源碼原文">${codeMarkup(span)}</div><div class="evidence-location"><div>${icon('folder')}<span>${escapeHTML(span?.relative_path || t('從引用清單選擇'))}</span></div><div>${icon('layers')}<span>${demo?ui`快照 ${escapeHTML((preview.catalog?.snapshot_id || '').replace('sha256:','').slice(0,12) || t('未建立'))}`:ui`快照 ${escapeHTML((state.project?.snapshot_id || '').replace('sha256:','').slice(0,12) || t('未建立'))}`}</span></div>${span?`<span class="badge ${demo?'preview':span.integrity==='VALID'?'positive':'warning'}">${icon(demo?'info':'check')}${demo?t('合成源碼 · 文件內容已讀取'):span.integrity==='VALID'?t('快照證據完整性有效'):t('完整性未確認')}${span.span_truncated?t(' · 已截斷'):''}</span>`:''}</div><div class="evidence-scope"><b>讓結論可被覆核</b><br>${demo?t('這些片段來自隨附的合成源碼，行號與雜湊由本機服務讀取，不是執行結果。'):t('引用有效只表示證據可核對，不代表業務含義或所有執行路徑已獲證明。')}</div></aside>`;
}
function formatDuration(seconds){
  if(!Number.isFinite(seconds) || seconds<0)return '—';
  const total=Math.floor(seconds);const hours=Math.floor(total/3600);const minutes=Math.floor((total%3600)/60);const rest=total%60;
  return (hours?String(hours).padStart(2,'0')+':':'')+String(minutes).padStart(2,'0')+':'+String(rest).padStart(2,'0');
}
function formatBytes(bytes){if(!Number.isFinite(bytes))return '—';const units=['B','KiB','MiB','GiB','TiB'];let value=Math.max(0,bytes),unit=0;while(value>=1024 && unit<units.length-1){value/=1024;unit++;}return value.toFixed(unit?1:0)+' '+units[unit];}
function phaseLabel(phase){
  const labels={
    using_index:t('使用已建立的代碼庫索引'),retrieving:t('查找與問題相關的依據'),answering:t('整理業務回答'),
    reading_sources:t('逐頁讀取源碼'),analyzing_pages:t('解讀已讀源碼'),synthesizing:t('整合業務解讀'),
    framework:t('檢索相關框架資料'),preparing:t('準備中'),queued:t('等待開始'),
    discovering:t('掃描檔案目錄'),discover:t('掃描檔案目錄'),discovery:t('掃描檔案目錄'),
    catalog:t('更新輕量目錄'),catalog_scan:t('更新輕量目錄'),repository_search:t('建立業務查找索引'),
    discovering_business:t('查找相關業務實作'),searching_repository:t('查找相關業務實作'),
    planning_search:t('理解問題並展開查找'),scope:t('定位相關源碼'),select_scope:t('定位相關源碼'),
    removing:t('移除過期索引'),expanding_copy:t('整理 COPY 引用'),resolving_relations:t('整理程式關係'),
    binding_calls:t('整理呼叫參數'),index:t('建立本次詳細索引'),indexing:t('建立本次詳細索引'),
    reading:t('讀取當前檔案'),parsing:t('解析當前檔案'),writing:t('寫入索引'),
    relations:t('整理程式關係'),resolving:t('整理程式關係'),finalizing:t('核對並保存結果'),
    verifying:t('核對源碼是否更新'),model:t('模型正在調查已有源碼'),
    investigation:t('模型正在調查已有源碼'),investigating:t('模型正在調查已有源碼'),
    completed:t('已完成'),cancelled:t('已取消'),
  };
  return labels[phase] || phase || t('準備中');
}
function progressViewModel(){
  const p=state.progress || {};const age=Math.max(0,(Date.now()-state.progressReceived)/1000);
  const known=Number.isFinite(p.total) && p.total>0 && Number.isFinite(p.completed);
  const completed=known?Math.min(p.total,Math.max(0,p.completed)):Math.max(0,Number(p.completed)||0);
  return {p,known,completed,remaining:known?Math.max(0,p.total-completed):null,percent:known?Math.min(100,100*completed/p.total):null,
    elapsed:Math.max(0,Number(p.elapsed_seconds)||0)+age,
    eta:!state.pollError&&Number.isFinite(p.eta_seconds)&&p.eta_seconds>=0?p.eta_seconds:null,
    idle:Math.max(0,Number(p.last_update_seconds)||0)+age};
}
function progressPanel(){const v=progressViewModel();return ui`<section class="answer-card progress-card" aria-label="接入進度"><div class="progress-heading"><div><div class="section-eyebrow">本次執行</div><h2 id="job-phase-name">${escapeHTML(phaseLabel(v.p.phase))}</h2></div><span id="job-percent">${v.known?v.percent.toFixed(1)+'%':t('統計中')}</span></div><progress id="job-progress" max="100" ${v.known?'value="'+v.percent+'"':''} aria-label="目前階段進度"></progress><div class="progress-counts" id="job-counts">${progressCounts(v)}</div><div class="progress-metrics"><div><span>已運行</span><strong id="job-elapsed">${formatDuration(v.elapsed)}</strong></div><div><span id="job-eta-label">${v.p.eta_scope==='current_file'?t('本檔案預計剩餘'):t('本階段預計剩餘')}</span><strong id="job-eta">${v.eta===null?t('估算中'):formatDuration(v.eta)}</strong></div><div><span>本階段已處理資料</span><strong id="job-bytes">${formatBytes(v.p.bytes_completed)}</strong></div></div><div class="progress-file"><span>目前處理</span><code id="job-file">${escapeHTML(v.p.current_file || '—')}</code></div><p id="job-file-lines" class="field-hint">${fileProgress(v)}</p><p class="progress-explanation">進度按目前階段計算。首次接入會建立整個代碼庫的查找索引；提問後再選取相關源碼閱讀。預估會隨檔案大小及處理速度調整。</p><div id="job-health" class="progress-health" role="status">${escapeHTML(progressHealth(v))}</div><div class="progress-actions"><button id="cancel-job" class="button secondary" ${state.cancelRequested || !state.jobId?'disabled':''}>${state.cancelRequested?t('正在停止，等待目前步驟結束'):t('停止本次工作')}</button><small>停止後可再次接入，重用已完成的目錄快取。</small></div></section>`;}
function progressCounts(v){const unit=v.p.unit==='pages'?t('頁'):v.p.unit==='bytes'?t('位元組'):v.p.unit==='lines'?t('行'):v.p.unit==='files'?t('檔案'):t('項');return v.known?ui`已完成 ${formatNumber(v.completed)} / ${formatNumber(v.p.total)} ${unit} · 剩餘 ${formatNumber(v.remaining)} ${unit}`:ui`已完成 ${formatNumber(v.completed)} ${unit} · 正在確認總量`;}
function fileProgress(v){return Number.isFinite(v.p.file_total)?ui`目前檔案已解析 ${formatNumber(v.p.file_completed)} / ${formatNumber(v.p.file_total)} 行`:'';}
function progressHealth(v){return state.pollError || (v.idle>=15?ui`目前步驟仍在處理，距上次進度更新 ${Math.floor(v.idle)} 秒。`:t('進度連線正常'));}
function rememberProgress(progress){if(progress){state.progress=progress;state.progressReceived=Date.now();}}
function updateProgressPanel(){
  if(state.busy && conversationMode()){
    const v=progressViewModel();if($('conversation-phase'))$('conversation-phase').textContent=phaseLabel(v.p.phase);
    if($('conversation-elapsed'))$('conversation-elapsed').textContent=formatDuration(v.elapsed);
    if($('conversation-health'))$('conversation-health').textContent=state.pollError || '';
    if($('cancel-job')){$('cancel-job').disabled=state.cancelRequested || !state.jobId;$('cancel-job').textContent=state.cancelRequested?t('正在停止，等待目前步驟結束'):t('停止');}return;
  }
  if(!state.busy || !$('job-progress'))return;
  const v=progressViewModel();$('job-phase-name').textContent=phaseLabel(v.p.phase);$('job-percent').textContent=v.known?v.percent.toFixed(1)+'%':t('統計中');
  if(v.known)$('job-progress').value=v.percent;else $('job-progress').removeAttribute('value');
  $('job-eta-label').textContent=v.p.eta_scope==='current_file'?t('本檔案預計剩餘'):t('本階段預計剩餘');$('job-file-lines').textContent=fileProgress(v);$('job-counts').textContent=progressCounts(v);$('job-elapsed').textContent=formatDuration(v.elapsed);$('job-eta').textContent=v.eta===null?t('估算中'):formatDuration(v.eta);
  $('job-bytes').textContent=formatBytes(v.p.bytes_completed);$('job-file').textContent=v.p.current_file || '—';$('job-health').textContent=progressHealth(v);
  $('cancel-job').disabled=state.cancelRequested || !state.jobId;$('cancel-job').textContent=state.cancelRequested?t('正在停止，等待目前步驟結束'):t('停止本次工作');
}
function startProgressClock(){if(!state.progressTimer)state.progressTimer=setInterval(updateProgressPanel,1000);}
function stopProgressClock(){if(state.progressTimer)clearInterval(state.progressTimer);state.progressTimer=null;}
async function cancelJob(){if(!state.jobId || state.cancelRequested)return;const jobId=state.jobId;state.cancelRequested=true;updateProgressPanel();try{await api(`/api/jobs/${encodeURIComponent(jobId)}/cancel`,{method:'POST',body:'{}'});}catch(error){if(state.jobId===jobId){state.cancelRequested=false;toast(error.message);updateProgressPanel();}}}
function scopeNotice(){
  const d=diagnosis();if(!d || !d.catalog_ready)return '';
  const detailed=Boolean(state.project?.snapshot_id);const count=d.scope?.detail_file_count || d.build_report?.files?.candidate;const scope=d.scope || {};
  if(d.source_options?.analysis_mode==='business' || ['repository_question','repository_index'].includes(scope.mode))return ui`<div class="scope-notice">${icon('info')}<div><strong>已建立業務查找索引</strong><p>已讀取源碼結構，可直接提出業務問題；系統會按問題選取相關檔案繼續閱讀。</p><p>${formatNumber(count || sourceFiles().candidate)} ${t('已索引檔案')}</p></div></div>`;
  return ui`<div class="scope-notice">${icon('info')}<div><strong>${detailed?t('本次問題的局部解析'):t('輕量目錄已就緒')}</strong><p>${detailed?ui`本次詳細索引包含 ${formatNumber(count)} 個檔案。缺失或閉源的依賴會標示邊界，已有源碼仍可分析。`:t('直接提出業務問題，系統會自動查找相關程式與 COPYBOOK。')}</p>${detailed?ui`<p>${formatNumber(count)} / ${formatNumber(scope.max_files || 24)} 個檔案 · ${formatBytes(scope.total_scope_bytes)} / ${formatBytes(scope.max_scope_bytes || 16777216)}</p>`:''}${scope.truncated?ui`<p><strong>已達本次範圍限制，部分依賴未納入。可另選相關入口繼續分析。</strong></p>`:''}</div></div>`;
}

function conversationComposer(){
  return ui`<section class="conversation-composer"><form id="question-form"><label class="sr-only" for="question-input">業務問題</label><textarea id="question-input" rows="2" maxlength="8000" aria-describedby="composer-hint" placeholder="提出業務問題，或接著上一個回答繼續追問…" ${!sourceUsable()?'disabled':''}>${escapeHTML(state.question)}</textarea><div class="composer-bottom"><details class="conversation-options"><summary>分析範圍</summary><label for="entry-select">從哪個程式開始</label><select id="entry-select" ${state.busy?'disabled':''}>${entryOptions()}</select><small>不選擇程式時，系統會在整個代碼庫中查找。</small><label for="answer-detail">回答詳略</label><select id="answer-detail" ${state.busy?'disabled':''}><option value="detailed" ${state.answerDetail==='detailed'?'selected':''}>詳細 · 說明條件、流程與依據</option><option value="brief" ${state.answerDetail==='brief'?'selected':''}>簡短 · 重點與結論</option></select></details><span class="composer-hint" id="composer-hint">Enter 發送 · Shift + Enter 換行</span><button class="button primary" type="submit" ${state.busy || state.conversationLoading || !sourceUsable()?'disabled':''}>${icon('arrow')}發送</button></div></form></section>`;
}
function sourceIllustration(){
  return ui`<figure class="source-illustration"><svg viewBox="0 0 560 170" fill="none" aria-hidden="true"><defs><pattern id="source-grid" width="16" height="16" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r=".65" fill="currentColor"/></pattern></defs><rect class="illustration-grid" width="560" height="170" fill="url(#source-grid)"/><path class="illustration-track" d="M150 51h48c35 0 35 35 70 35h35M150 119h48c35 0 35-33 70-33h35M365 86h37c28 0 28-35 58-35h31M365 86h37c28 0 28 33 58 33h31"/><g class="illustration-paper"><rect x="37" y="24" width="122" height="60" rx="9"/><path d="M55 42h29m-29 10h67m-55 10h43"/><rect x="52" y="98" width="108" height="49" rx="8"/><path d="M70 115h42m-42 11h66"/></g><g class="illustration-core"><rect x="254" y="48" width="111" height="77" rx="16"/><path d="M274 69h13c16 0 4 36 20 36h38M274 87h71M274 105h13c16 0 4-36 20-36h38"/></g><g class="illustration-node"><rect x="447" y="31" width="73" height="40" rx="8"/><rect x="447" y="99" width="73" height="40" rx="8"/><path d="M463 47h12m-12 8h39m-39 58h26m-26 8h39"/></g><circle class="illustration-junction" cx="211" cy="86" r="3"/><circle class="illustration-junction" cx="399" cy="86" r="3"/></svg><figcaption><span>源碼</span><span>追溯證據</span><span>理解業務</span></figcaption></figure>`;
}
function conversationWelcome(ready){
  return ui`<div class="conversation-welcome">${sourceIllustration()}<div class="welcome-kicker">從一個問題，理清業務脈絡</div><h1>${ready?t('想了解哪一項業務？'):t('接入代碼庫，開始對話。')}</h1><p>${ready?t('描述你想確認的規則、例外或影響，沿著源碼一起找答案。'):t('首次建立本機索引，之後的問題會重用索引與對話。')}</p>${!ready?ui`<button class="button primary" data-connect ${state.busy?'disabled':''}>${icon('folder')}接入本機源碼</button>`:''}</div>`;
}
function starterQuestions(){
  return ui`<div class="starter-questions"><button data-question="這段流程的受理條件是甚麼？"><span class="starter-icon">${icon('branch')}</span><span><strong>梳理業務規則</strong><small>條件、計算與處理流程</small></span>${icon('arrow')}</button><button data-question="有哪些例外和待確認的依賴？"><span class="starter-icon">${icon('shield')}</span><span><strong>查找例外與依賴</strong><small>找出尚待確認的部分</small></span>${icon('arrow')}</button><button data-question="修改這個欄位會影響哪些程式？"><span class="starter-icon">${icon('layers')}</span><span><strong>了解變更影響</strong><small>追蹤欄位與相關程式</small></span>${icon('arrow')}</button></div>`;
}
// Build navigation from sanitized rendered headings, never from raw model HTML.
function answerReadingView(markup,messageId){
  const sections=[];
  const content=markup.replace(/<h([2-6])>([\s\S]*?)<\/h\1>/g,(heading,level,label)=>{
    const id='answer-'+encodeURIComponent(messageId)+'-section-'+sections.length;
    sections.push({id,label:label.replace(/<[^>]+>/g,'')});
    return `<h${level} id="${escapeHTML(id)}" tabindex="-1">${label}</h${level}>`;
  });
  const outline=sections.length>=3?ui`<details class="answer-outline"><summary>本頁內容</summary><nav aria-label="回答目錄">${sections.map(section=>`<button data-answer-section="${escapeHTML(section.id)}">${section.label}</button>`).join('')}</nav></details>`:'';
  return {content,outline};
}
function conversationMessage(message){
  const user=message.role==='user';
  const id=String(message.id || '');
  const status=String(message.status || '').toUpperCase();
  const refs=Array.isArray(message.evidence_refs)?message.evidence_refs:[];
  const framework=Array.isArray(message.framework_references)?message.framework_references:[];
  const related=Array.isArray(message.related_sources)?message.related_sources:[];
  const impact=message.impact_result;
  const impactPage=state.impactPages[id] || impact;
  const impactMarkup=!user && impact?.handle?`<details class="message-related"><summary>${escapeHTML(t('相關對象'))} · ${Number(impact.total)}</summary><p>${escapeHTML(t('已索引文字與靜態關係範圍'))}：${escapeHTML(JSON.stringify(impact.counts || {}))}</p><ul>${(impactPage.rows || []).map(item=>`<li>${escapeHTML(item.name)} <small>${escapeHTML(item.relative_path || '')} · ${escapeHTML(item.type)} · ${escapeHTML(item.match_type)}</small></li>`).join('')}</ul>${impactPage.next_cursor!==null?`<button data-impact-more="${escapeHTML(id)}">${escapeHTML(t('載入更多'))}</button>`:''}<button data-impact-export="${escapeHTML(id)}">${escapeHTML(t('匯出完整 JSONL'))}</button></details>`:'';
  const answer=typeof message.content==='string'?message.content:'';
  const markup=user?`<p>${escapeHTML(answer)}</p>`:narrativeMarkup(answer,refs,framework)
    .replace(/data-evidence="/g,`data-message-id="${escapeHTML(id)}" data-turn-evidence="`)
    .replace(/data-framework-reference="/g,`data-message-id="${escapeHTML(id)}" data-turn-framework="`)
    .replace(/href="#framework-reference-([^"]+)"/g,`href="#turn-${escapeHTML(id)}-framework-$1"`);
  const failed=['FAILED','CANCELLED','INTERRUPTED','ABSTAINED','SAFE_STOP','BLOCKED'].includes(status);
  const relatedMarkup=!user && related.length>1?`<details class="message-related"><summary>${escapeHTML(t('查看相關源碼'))} · ${related.length}</summary><ul>${related.map(item=>`<li>${escapeHTML((item.program_names || []).join('、') || item.relative_path)} <small>${escapeHTML(item.relative_path)}</small></li>`).join('')}</ul></details>`:'';
  const referenceMarkup=!user && (refs.length || framework.length)?`<details class="message-references"><summary>${escapeHTML(t('查看回答依據'))} · ${refs.length+framework.length}</summary>${refs.map((ref,index)=>`<button class="citation" data-message-id="${escapeHTML(id)}" data-turn-evidence="${escapeHTML(ref.evidence_id)}">${index+1} · ${escapeHTML(ref.relative_path || ref.path || '')} · L${Number(ref.start_line)}–${Number(ref.end_line)}</button>`).join('')}${framework.map(ref=>`<details id="turn-${escapeHTML(id)}-framework-${escapeHTML(encodeURIComponent(ref.reference_id))}" class="message-framework"><summary>${escapeHTML(ref.heading || ref.reference_id)}</summary><pre>${escapeHTML(ref.text || '')}</pre></details>`).join('')}</details>`:'';
  const retryMarkup=user && failed?`<button class="message-retry" data-retry-message="${escapeHTML(id)}" ${state.busy || state.conversationLoading?'disabled':''}>${escapeHTML(t('重試這個問題'))}</button>`:'';
  const priorSnapshot=!user && message.snapshot_id && state.project?.snapshot_id && message.snapshot_id!==state.project.snapshot_id;
  const versionMarkup=priorSnapshot?ui`<small class="message-source-version">此回答依據較早的本機版本 <code>${escapeHTML(shortSourceVersion(message.snapshot_id))}</code>；原引用仍可查看。新提問會使用目前源碼。</small>`:'';
  const reading=user?{content:markup,outline:''}:answerReadingView(markup,id);
  return ui`<article class="conversation-message ${user?'from-user':'from-assistant'}" data-message="${escapeHTML(id)}">${reading.outline}<div class="answer-main"><div class="message-author">${user?t('你'):t('知序')}<span>${user?'':t('業務解讀 · 請結合證據覆核')}</span></div>${versionMarkup}${user?'':answerCompletionNotice(message)+failureDiagnosticsView(message)}<div class="message-content narrative-body">${reading.content || (failed?`<p>${escapeHTML(t('本次未取得回答。可保留對話並重試。'))}</p>`:'')}</div>${relatedMarkup}${impactMarkup}${referenceMarkup}${retryMarkup}</div></article>`;
}
function conversationProgress(){
  const v=progressViewModel();
  return ui`<div class="conversation-progress" id="conversation-progress" role="status"><span class="working-dot"></span><div><strong id="conversation-phase">${escapeHTML(phaseLabel(v.p.phase))}</strong><small id="conversation-elapsed">${formatDuration(v.elapsed)}</small><small id="conversation-health">${escapeHTML(state.pollError || '')}</small></div><button id="cancel-job" ${state.cancelRequested || !state.jobId?'disabled':''}>${state.cancelRequested?t('正在停止，等待目前步驟結束'):t('停止')}</button></div>`;
}
function conversationEvidenceView(){
  const item=state.conversationEvidence;if(!item)return '';
  return ui`<dialog id="conversation-evidence-dialog" class="conversation-evidence" aria-labelledby="evidence-title"><div><strong id="evidence-title">${escapeHTML(item.ref?.relative_path || item.ref?.path || t('源碼引用'))}</strong><button class="icon-button" data-close-conversation-evidence aria-label="關閉" autofocus>${icon('close')}</button></div><small>L${Number(item.ref?.start_line)}–${Number(item.ref?.end_line)}</small>${item.error?`<p>${escapeHTML(item.error)}</p>`:item.span?`<div class="code-body" tabindex="0" role="region" aria-label="${escapeHTML(t('源碼證據'))}">${codeMarkup(item.span)}</div>`:ui`<p role="status">正在讀取引用…</p>`}</dialog>`;
}
function conversationWorkbench(){
  const messages=state.conversation?.messages || [];
  const pending=state.pendingMessage && !messages.some(message=>message.role==='user' && message.id===state.pendingMessage.id)?state.pendingMessage:null;
  const empty=!messages.length && !pending;
  const ready=sourceUsable();
  return ui`<section class="conversation-workspace ${empty?'is-empty':'has-messages'}"><${empty?'h2':'h1'} class="sr-only">${escapeHTML(conversationTitle(state.conversation?.title))}</${empty?'h2':'h1'}>${empty?conversationWelcome(ready):''}<div class="conversation-messages" aria-label="業務對話">${messages.map(conversationMessage).join('')}${pending?conversationMessage(pending):''}${state.busy?conversationProgress():''}</div>${state.error?ui`<div class="conversation-failure" role="status"><div class="failure-heading">${icon('alert')}本次請求未完成</div><p>${escapeHTML(state.errorDiagnostic?failureSummaryText(state.errorDiagnostic):state.error)}</p>${state.errorDiagnostic?ui`<details><summary>查看安全錯誤詳情</summary><pre class="api-raw-text">${escapeHTML(JSON.stringify(safeApiDiagnostic(state.errorDiagnostic),null,2))}</pre></details>`:''}${pending?ui`<button data-retry-pending>編輯後重新發送</button>`:''}</div>`:''}${conversationComposer()}${empty && ready?starterQuestions():''}<div class="conversation-context"><span>${frameworkStatusLabel(frameworkContext().status)}</span><button data-settings>框架與連線設定</button><details class="conversation-diagnostics"><summary>本次調查詳情</summary>${supportingContext()}${state.project?.agent?`${traceView()}${apiResponseView()}`:''}</details></div>${conversationEvidenceView()}</section>`;
}
function demoConversationStart(){
  return ui`<section class="demo-chat-start">${frameworkCaseSelector()}<form id="demo-question-form" class="demo-composer"><label for="demo-question-input">你的第一個業務問題</label><textarea id="demo-question-input" rows="3" placeholder="輸入任何業務問題，或修改上方案例的建議問題…" ${state.busy?'disabled':''}>${escapeHTML(state.question)}</textarea><div class="demo-composer-actions"><small>載入只在本機建立索引。進入對話後按「發送」才會呼叫模型；追問會重用索引。</small><button class="button primary" type="submit" ${state.busy || !state.connected?'disabled':''}>${icon('arrow')}載入案例，前往提問</button></div></form></section>`;
}
function renderWorkbench(){
  if(state.mode==='real' && state.jobKind==='index' && state.busy)return progressPanel()+supportingContext();
  if(state.mode==='real' && state.jobKind==='demo' && state.busy)return progressPanel();
  if(conversationMode())return conversationWorkbench();
  if(state.mode==='demo' && !demoCase())return frameworkDemoUnavailable();
  if(state.mode==='demo')return demoConversationStart();
  if(state.mode==='real' && state.busy)return progressPanel()+supportingContext();
  if(state.mode==='real'&&!diagnosis()&&!state.project?.agent)return ui`<section class="empty-panel"><div class="empty-icon">${icon('folder')}</div><div class="eyebrow">YOUR CODE. YOUR CONTEXT.</div><h2>從自己的源碼，開始。</h2><p>接入本機 COBOL 程式與 COPYBOOK，確認分析範圍，<br>再用業務語言提出問題。</p><div class="empty-actions"><button class="button primary" data-connect>${icon('plus')}接入本機源碼</button><button class="button secondary" data-demo>先查看業務案例</button></div></section>`+supportingContext();
  return `${state.mode==='demo'?frameworkCaseSelector():''}<div class="workspace-grid"><div class="primary-column">${questionCard()}<section class="answer-card">${answerTabs()}<div class="answer-content" role="tabpanel">${state.tab==='relations'?relationsView():state.tab==='trace'?traceView():state.tab==='api' && state.mode==='real'?apiResponseView():state.mode==='demo'?previewAnswer():actualAnswer()}</div></section>${supportingContext()}</div>${evidencePanel()}</div>`;
}
const SOURCE_PAGE_SIZE=100;
function filteredPrograms(){return currentPrograms().filter(p=>(p.program_name+' '+p.relative_path).toLowerCase().includes(state.filter.toLowerCase()));}
function sourceRows(){
  const programs=filteredPrograms();state.sourcePage=Math.min(state.sourcePage,Math.max(0,Math.ceil(programs.length/SOURCE_PAGE_SIZE)-1));
  const rows=programs.slice(state.sourcePage*SOURCE_PAGE_SIZE,(state.sourcePage+1)*SOURCE_PAGE_SIZE);
  return rows.map(p=>{
    const selectedCase=state.mode==='demo'?preview.catalog?.cases?.find(item=>item.entry_path===p.relative_path || item.entry_program===p.program_name):null;
    const action=state.mode==='demo'?(selectedCase?ui`<button data-demo-case="${escapeHTML(selectedCase.id)}">查看業務案例 ${icon('arrow')}</button>`:t('共用程式')):ui`<button data-program="${escapeHTML(entryValue(p))}" data-program-label="${escapeHTML(p.program_name)}">分析程式 ${icon('arrow')}</button>`;
    return ui`<tr><td>${escapeHTML(p.program_name)}</td><td>${escapeHTML(p.relative_path)}</td><td>${p.start_line?'L'+Number(p.start_line):'—'}</td><td><span class="badge ${state.mode==='demo'?'preview':'neutral'}">${state.mode==='demo'?t('合成文件原文'):repositoryIndexReady()?t('源碼已索引'):['path','filename'].includes(p.name_origin)?t('檔名入口 · 待詳細解析'):t('目錄已識別 · 按需解析')}</span></td><td>${action}</td></tr>`;
  }).join('') || ui`<tr><td colspan="5">沒有符合的入口</td></tr>`;
}

function sourcePagination(){const total=filteredPrograms().length;const first=total?state.sourcePage*SOURCE_PAGE_SIZE+1:0;const last=Math.min(total,(state.sourcePage+1)*SOURCE_PAGE_SIZE);return ui`<span>${first}–${last} / ${formatNumber(total)} 個目錄入口</span><div><button data-source-page="previous" ${state.sourcePage===0?'disabled':''}>上一頁</button><button data-source-page="next" ${last>=total?'disabled':''}>下一頁</button></div>`;}

function dependencyGaps(){const gaps=diagnosis()?.unresolved_dependencies || [];if(!gaps.length)return '';return ui`<section class="data-card dependency-gaps"><div class="data-card-heading"><h2>待確認的程式與 COPY 依賴</h2><span class="badge warning">${formatNumber(gaps.length)} 項</span></div><div class="table-wrapper"><table><thead><tr><th>目標名稱</th><th>關係</th><th>來源檔案</th></tr></thead><tbody>${gaps.slice(0,20).map(g=>`<tr><td>${escapeHTML(g.target_name || t('動態目標'))}</td><td>${escapeHTML(g.relation_type)}</td><td>${escapeHTML(g.relative_path)}</td></tr>`).join('')}</tbody></table></div><div class="table-count">${gaps.length>20?t('目前顯示前 20 項；完整清單保留在匯出 JSON。'):''}${t('公共呼叫可按已匹配框架約定解釋；未覆蓋的內部行為才需補充資料。')}</div></section>`;}
function shortSourceVersion(value){return String(value || '').replace(/^local:|^sha256:/,'').slice(0,12);}
function sourceVersionTime(value){const date=new Date(value);return value && Number.isFinite(date.getTime())?date.toLocaleString(currentLocale,{hour12:false}):'—';}
function sourceVersionView(){
  const current=state.project?.source_version;
  const history=(Array.isArray(state.project?.source_versions)?state.project.source_versions:[])
    .filter(version=>!current?.source_key || version.source_key===current.source_key).slice(0,20);
  if(!current && !history.length)return '';
  return ui`<section class="data-card source-version-card"><div class="data-card-heading"><h2>本機源碼版本</h2>${current?`<strong title="${escapeHTML(current.version_id)}">${escapeHTML(shortSourceVersion(current.version_id))}</strong>`:''}</div>${current?ui`<p>最近檢查：${escapeHTML(sourceVersionTime(current.checked_at || current.created_at))}</p>`:''}<p>更新會校驗檔案內容，識別新增、修改和刪除。版本記錄保留檔案指紋，舊回答保留當時引用。</p><details><summary>查看版本記錄 · ${history.length}</summary>${history.map(v=>`<div class="history-card"><div><strong title="${escapeHTML(v.version_id)}">${escapeHTML(shortSourceVersion(v.version_id))}</strong><p>${escapeHTML(sourceVersionTime(v.created_at))} · ${formatNumber(v.file_count)} ${escapeHTML(t('個檔案'))}</p><small>${escapeHTML(t('新增'))} ${formatNumber(v.changes?.added)} · ${escapeHTML(t('修改'))} ${formatNumber(v.changes?.modified)} · ${escapeHTML(t('刪除'))} ${formatNumber(v.changes?.removed)}</small></div></div>`).join('')}</details></section>`;
}
function renderSources(){
  if(state.mode==='real'&&!diagnosis() || state.mode==='demo'&&!demoCase())return renderWorkbench();
  const demo=state.mode==='demo';const d=diagnosis();const files=sourceFiles();const programs=currentPrograms();
  return ui`${scopeNotice()}${reportDisplayNotice()}${!demo?ui`<div class="source-actions"><button class="button primary" data-source-update ${state.busy?'disabled':''}>更新本機源碼</button><button class="button secondary" data-source-switch ${state.busy?'disabled':''}>切換源碼目錄</button></div>${sourceVersionView()}`:''}<div class="source-stats"><div class="source-stat"><span>目錄檔案</span><strong>${demo?formatNumber(preview.catalog?.file_count):formatNumber(files?.candidate || files?.decoded)}</strong><small>${demo?t('由合成文件實際讀取'):ui`${formatNumber(files?.candidate)} 個候選檔案`}</small></div><div class="source-stat"><span>${repositoryIndexReady()?t('程式'):t('可選入口')}</span><strong>${formatNumber(programs.length)}</strong><small>${repositoryIndexReady()?t('已建立索引，可直接提出業務問題'):t('先建立目錄，提問時解析相關源碼')}</small></div><div class="source-stat"><span>本次重用快取</span><strong>${demo?'—':formatNumber(files?.cached || files?.skipped_unchanged)}</strong><small>${demo?t('由合成文件實際讀取'):ui`${formatNumber(files?.indexed_or_updated)} 個檔案已更新`}</small></div></div><section class="data-card"><div class="data-card-heading"><h2>程式目錄</h2><input id="program-filter" aria-label="搜尋程式名稱或路徑" placeholder="搜尋程式名稱或路徑" value="${escapeHTML(state.filter)}"></div><div class="table-wrapper"><table><thead><tr><th>PROGRAM-ID</th><th>來源路徑</th><th>定義行</th><th>識別狀態</th><th></th></tr></thead><tbody id="program-rows">${sourceRows()}</tbody></table></div><div class="table-count source-pagination" id="source-pagination">${sourcePagination()}</div></section>${frameworkKnowledgeCard()}${!demo?ui`<div class="source-note"><strong>源碼位置</strong><br>${escapeHTML(state.project.source)}<br><strong>本次快照</strong><br>${escapeHTML(state.project.snapshot_id || state.project.catalog_snapshot_id || t('未建立有效快照'))}</div>${renderBoundaries(d?.messages || [])}`:''}`;
}
function renderHistory(){
  if(conversationMode())return ui`<section class="data-card"><div class="data-card-heading"><h2>業務對話</h2><span class="badge neutral">${state.conversations.length} 項</span></div>${state.conversations.map(item=>`<div class="history-card">${icon('document')}<div><h3>${escapeHTML(conversationTitle(item.title))}</h3></div><button data-conversation="${escapeHTML(item.id)}" ${state.busy || state.conversationLoading?'disabled':''}>${escapeHTML(t('繼續對話'))}</button><button class="history-delete" data-delete-conversation="${escapeHTML(item.id)}" ${state.busy || state.conversationLoading?'disabled':''}>${escapeHTML(t('刪除對話'))}</button></div>`).join('')}</section>`;
  if(state.mode==='demo')return ui`<section class="empty-panel"><h2>案例尚未開始對話</h2><p>選擇一個示例問題，載入合成源碼後即可提問和追問。</p><button class="button secondary" data-demo-result>返回示例問題 ${icon('arrow')}</button></section>`;
  return ui`<section class="data-card"><div class="data-card-heading"><h2>本次工作階段</h2><span class="badge neutral">${state.history.length} 項</span></div>${state.history.length?state.history.map((item,i)=>ui`<div class="history-card">${icon('document')}<div><h3>${escapeHTML(item.diagnosis?.question || t('源碼接入'))}</h3><p>${escapeHTML(item.source?.split(/[\\/]/).pop())} · ${escapeHTML(statusLabel(item.agent?.agent_result?.status || item.diagnosis?.question_status))} · ${escapeHTML(item.diagnosis?.generated_at_utc || '')}</p></div><button data-history="${i}">查看摘要</button></div>`).join(''):t('<div class="empty-panel"><h2>尚未有分析記錄</h2><p>本次工作階段完成的分析會列在這裡；完整結果同時保存在你指定的資料夾。</p></div>')}</section>`;
}
function render(){
  const focused=document.activeElement;
  const focusedId=focused?.id;
  const selection=focusedId==='question-input'?[focused.selectionStart,focused.selectionEnd]:null;
  const optionsOpen=document.querySelector('.conversation-options')?.open;
  renderChrome();$('page-content').innerHTML=state.page==='sources'?renderSources()+dependencyGaps():state.page==='history'?renderHistory():renderWorkbench();decorate();
  const options=document.querySelector('.conversation-options');if(options && optionsOpen)options.open=true;
  const evidenceDialog=$('conversation-evidence-dialog');
  if(state.conversationEvidence && state.page==='workbench' && conversationMode())evidenceDialog?.showModal?.();
  else if(focusedId && focused?.isConnected===false){const replacement=$(focusedId);replacement?.focus?.({preventScroll:true});if(selection)replacement?.setSelectionRange?.(...selection);}
  resizeComposer();
  syncPageRoute();
}
function resizeComposer(){const input=$('question-input');if(input?.style){input.style.height='auto';input.style.height=Math.min(180,Math.max(64,input.scrollHeight))+'px';}}
function closeConversationEvidence(){
  const previous=state.conversationEvidence;
  state.conversationEvidence=null;state.evidenceTicket++;render();
  const trigger=Array.from(document.querySelectorAll('[data-turn-evidence]')).find(button=>button.dataset.messageId===previous?.messageId && button.dataset.turnEvidence===previous?.ref?.evidence_id);
  trigger?.focus?.({preventScroll:true});
}
function closeActivity(){if($('activity-dialog')?.open)$('activity-dialog').close();}
function openActivity(){
  const dialog=$('activity-dialog'),sidebar=$('activity-sidebar');
  if(!dialog || !sidebar || dialog.open)return;
  closeNavigation();
  dialog.append(sidebar);dialog.showModal();$('activity-toggle').setAttribute('aria-expanded','true');
  $('activity-close').focus();
}
function closeNavigation(){if($('navigation-dialog')?.open)$('navigation-dialog').close();}
function openNavigation(){
  const dialog=$('navigation-dialog'),sidebar=$('workspace-sidebar');
  if(!dialog || !sidebar || dialog.open)return;
  closeActivity();
  dialog.append(sidebar);dialog.showModal();$('navigation-toggle').setAttribute('aria-expanded','true');
}
// Page routes preserve in-memory answers and drafts; no analysis request is made.
function syncPageRoute(){
  if(typeof window==='undefined' || !window.history?.replaceState)return;
  const route='#'+state.page;
  if(window.location.hash===route)return;
  const method=window.location.hash && ['#workbench','#sources','#history'].includes(window.location.hash)?'pushState':'replaceState';
  window.history[method](null,'',route);
}
function restorePageRoute(){
  const page=window.location.hash.slice(1);
  if(['workbench','sources','history'].includes(page)){state.page=page;closeNavigation();render();}
}

function apiErrorText(error){
  const diagnostic=safeApiDiagnostic(error?.diagnostic);if(diagnostic)return failureDiagnosticText(diagnostic);
  const code=error?.code || 'REQUEST_FAILED';
  if(code==='CONVERSATION_NOT_FOUND')return t('找不到這段對話。請從對話記錄選擇，或開始新的對話。');
  if(code==='RETRY_NOT_AVAILABLE' || code==='INVALID_RETRY')return t('這條問題已無法重試，請在對話末尾繼續提問。');
  if(code==='SOURCE_REQUIRED')return t('請先接入源碼，再開始對話。');
  if(code==='INVALID_PATH')return t('請使用完整絕對路徑：源碼目錄須存在；輸出須獨立且為空、新目錄或只含分析產物。兩者不可相互嵌套。');
  if(code==='JOB_ACTIVE')return t('目前已有分析正在執行，請等候完成。');
  if(code==='INVALID_SESSION')return t('工作階段已失效，請重新整理頁面。');
  if(['INVALID_ENCODING','INVALID_SOURCE_FORMAT','INVALID_EXTENSIONS'].includes(code))return t('請檢查文字編碼、源碼格式與副檔名設定。');
  if(['INDEX_CHANGED','EVIDENCE_INTEGRITY_ERROR'].includes(code))return t('源碼或索引已更新，舊引用暫時不可用。請重新分析目前源碼。');
  if(['NO_CURRENT_INDEX','INDEX_UNAVAILABLE','EVIDENCE_NOT_FOUND','INVALID_EVIDENCE_ID'].includes(code))return t('請重新接入源碼，再選取當次快照的引用。');
  if(['INVALID_OPTIONS','REQUEST_TOO_LARGE','INVALID_JSON','INVALID_QUERY'].includes(code))return t('請檢查輸入欄位及請求大小。');
  return t('本機服務暫時無法完成操作。')+' ('+(['ANALYSIS_FAILED','REQUEST_FAILED','MODEL_CHECK_FAILED'].includes(code)?code:'REQUEST_FAILED')+')';
}
async function api(path,options={}){
  let response;
  try{response=await fetch(path,{...options,headers:{...(state.token?{'X-Session-Token':state.token}:{}),...(options.body?{'Content-Type':'application/json'}:{}),...options.headers},cache:'no-store'});}
  catch{const error=new Error();error.diagnostic=browserDiagnostic('connection');error.browserDiagnostic=true;error.message=failureDiagnosticText(error.diagnostic)+' '+t('此關聯編號由瀏覽器產生，不是服務端請求編號。');throw error;}
  let value;try{value=await response.json();if(!value || typeof value!=='object' || Array.isArray(value))throw Error('INVALID_RESPONSE');}
  catch{const category=response.ok?'invalid_response':response.status===429?'http_429_unknown':response.status>=500?'system_error':'request_rejected';const error=new Error();error.status=response.status;error.diagnostic=browserDiagnostic(category,response.status);error.browserDiagnostic=true;error.message=failureDiagnosticText(error.diagnostic)+' '+t('此關聯編號由瀏覽器產生，不是服務端請求編號。');throw error;}
  if(!response.ok){const error=new Error(apiErrorText(value.error));error.status=response.status;error.diagnostic=safeApiDiagnostic(value.error?.diagnostic);error.code=['REQUEST_FAILED','ANALYSIS_FAILED','MODEL_CHECK_FAILED','CONFIGURATION_INVALID','REQUEST_TIMEOUT'].includes(value.error?.code)?value.error.code:'REQUEST_FAILED';throw error;}
  return value;
}
function clearEvidence(){state.selectedEvidence=null;state.evidence=null;state.evidenceTicket++;}
function applyProject(project){state.project=project;clearEvidence();state.sourcePage=0;state.entryFilter='';const d=project?.diagnosis;state.maxSourcePages=Number(d?.source_options?.max_source_pages || 4);state.readingStrategy=projectReadingStrategy(project);if(['brief','detailed'].includes(d?.source_options?.answer_detail))state.answerDetail=d.source_options.answer_detail;state.entry=selectedEntryValue(d);state.question=d?.question || '';}
function restoreConversationFailure(job){
  if(!job || !state.conversation?.id || job.conversation?.id!==state.conversation.id)return;
  const status=String(job.status).toUpperCase();
  if(!['FAILED','CANCELLED'].includes(status) || !state.conversation.messages?.some(message=>message.role==='user' && message.run_id===job.job_id && String(message.status).toUpperCase()===status))return;
  state.errorDiagnostic=status==='FAILED'?safeApiDiagnostic(job.error?.diagnostic):null;
  state.error=status==='FAILED'?apiErrorText(job.error):t('已停止本次工作。對話已保留，可以繼續提問。');
}
async function initialize(){
  try{const data=await api('/api/state');state.connected=true;state.token=data.session_token;state.apiConfigured=Boolean(data.api_configured);state.apiConfigurationError=data.api_configuration_error || null;state.frameworkKnowledge=data.framework_knowledge || null;
    state.conversationSupported=Array.isArray(data.conversations) || Object.hasOwn(data,'conversation');state.conversations=sortConversations(Array.isArray(data.conversations)?data.conversations:[]);
    state.answerDetail=data.answer_detail==='brief'?'brief':'detailed';
    if(data.project?.source){applyProject(data.project);state.mode='real';if(data.project.diagnosis)state.history=[data.project];}
    if(state.conversationSupported){state.mode='real';state.question='';state.entry='';state.readingStrategy='retrieval';applyConversation(data.conversation || data.project?.conversation);restoreConversationFailure(data.job);}
    if(data.active_job_id){state.mode='real';state.busy=true;state.jobId=data.active_job_id;state.cancelRequested=Boolean(data.job?.cancel_requested);rememberProgress(data.job?.progress);startProgressClock();pollJob(data.active_job_id);}
  }catch{state.connected=false;state.demoLoading=false;}
  if(state.connected)await loadFrameworkDemo();
  render();if(state.mode==='real' && !conversationMode())loadFirstEvidence();
}
function setMode(mode){
  state.mode=mode;state.error='';state.filter='';state.sourcePage=0;state.entryFilter='';state.tab='answer';clearEvidence();
  if(mode==='demo'){selectDemoCase(preview.selectedCase || preview.catalog?.default_case,false);}
  else{state.readingStrategy=state.conversationSupported?'retrieval':projectReadingStrategy();state.maxSourcePages=Number(state.project?.diagnosis?.source_options?.max_source_pages || 4);state.question=state.conversationSupported?'':state.project?.diagnosis?.question || '';state.entry=selectedEntryValue(state.project?.diagnosis);}
  render();if(mode==='real')loadFirstEvidence();
}
function openSource(intent='update'){
  closeNavigation();
  state.sourceIntent=state.project?.source && !syntheticProject()?intent:'connect';
  $('source-error').hidden=true;$('verify-content').checked=true;
  $('source-dialog-title').textContent=t(state.sourceIntent==='update'?'更新本機源碼':state.sourceIntent==='switch'?'切換源碼目錄':'接入本機源碼');
  $('index-submit').textContent=t(state.sourceIntent==='update'?'更新索引與版本':'建立源碼索引');
  if($('source-picker-feedback')){$('source-picker-feedback').hidden=true;$('source-picker-feedback').textContent='';}
  if($('source-framework-path'))$('source-framework-path').value=frameworkConfiguredPath();
  if(state.project){$('source-path').value=state.project.source || '';$('output-path').value=state.project.output || '';const opts=state.project.diagnosis?.source_options || {};$('source-encoding').value=opts.encoding || 'auto';$('source-format').value=opts.source_format || 'auto';$('source-extensions').value=(opts.extensions || []).join(',');}
  $('index-submit').disabled=!state.connected || state.busy;
  if(!state.connected){$('source-error').textContent=t('請先以 python poc/web_app.py 啟動本機服務，再透過服務地址開啟此頁。');$('source-error').hidden=false;}
  $('source-dialog').showModal();
}
async function pickFolder(fieldId){
  if(!['source-path','source-framework-path','output-path'].includes(fieldId) || state.folderPicking || !state.connected)return;
  state.folderPicking=true;const feedback=$('source-picker-feedback');feedback.hidden=false;feedback.textContent=t('正在開啟本機文件夾選擇窗口…');
  document.querySelectorAll('[data-pick-folder]').forEach(button=>{button.disabled=true;});
  try{
    const initial=pastedPath($(fieldId).value || '');
    const response=await api('/api/pick-folder',{method:'POST',body:JSON.stringify(initial?{initial_path:initial}:{})});
    if(response.cancelled){feedback.textContent=t('已取消選取；仍可手動填入路徑。');return;}
    if(typeof response.path!=='string' || !response.path.trim())throw new Error('EMPTY_PATH');
    $(fieldId).value=response.path;feedback.textContent=t('已選取本機文件夾。');
    $('source-error').hidden=true;
  }catch(error){feedback.textContent=error.status===503?t('目前無法開啟本機文件夾窗口，請手動填入路徑。'):t('無法選取文件夾，請手動填入路徑。');}
  finally{state.folderPicking=false;document.querySelectorAll('[data-pick-folder]').forEach(button=>{button.disabled=false;});}
}
function modelCheckMessage(response){
  if(response?.usable===true && response?.model_returned===true)return t('連線成功：模型已返回有效回覆。');
  const diagnostic=safeApiDiagnostic(response?.diagnostic);if(diagnostic)return failureDiagnosticText(diagnostic);
  const status=Number(response?.http_status || response?.status || 0);
  const code=String(response?.code || '').toUpperCase();
  if(status===401 || code.includes('AUTH') || code.includes('API_KEY'))return t('驗證失敗：API Key 無效或未被接受，請核對本機設定。');
  if(status===403 || code.includes('FORBIDDEN'))return t('訪問被拒絕：目前帳號沒有使用此模型的權限。');
  if(status===408 || status===504 || code.includes('TIMEOUT'))return t('連線逾時：請檢查網路或模型服務後重試。');
  if(status===429)return t('接口返回 HTTP 429，但未提供可確認的原因。')+' '+t('檢查帳號額度和服務限流說明，再決定是否重試。');
  if(['INVALID_JSON_RESPONSE','INVALID_RESPONSE_SHAPE'].includes(code) || code.includes('RESPONSE_FORMAT') || code.includes('RESPONSE_SHAPE'))return t('接口已回應，但返回格式不兼容；請核對模型接口類型與路徑。');
  if(status===400 || status===404)return t('接口拒絕本次請求，具體原因尚未確認。')+' '+t('請核對請求格式、模型配置與服務日誌。');
  if(code.includes('CONFIG') || code.includes('MISSING') || code.includes('INVALID'))return t('模型設定不完整或無效，請核對 .env 後重試。');
  if(code.includes('EMPTY') || response?.model_returned===false && response?.usable===true)return t('接口已回應，但模型沒有返回可用文字。');
  return t('模型連線測試未通過，請檢查本機設定及模型服務。');
}
async function checkModelConnection(){
  if(state.modelChecking)return;
  state.modelChecking=true;const button=$('model-check-button');const feedback=$('model-check-feedback');button.disabled=true;feedback.textContent=t('正在測試模型連線…');
  try{
    const response=await api('/api/model-check',{method:'POST',body:'{}'});
    state.modelCheckStatus=response.usable===true && response.model_returned===true?'success':'failure';
    state.modelCheckFeedback=modelCheckMessage(response);feedback.textContent=state.modelCheckFeedback;
    $('settings-api').textContent=state.modelCheckStatus==='success'?t('已驗證可用'):t('連線測試未通過');
  }catch(error){state.modelCheckStatus='failure';state.modelCheckFeedback=error.browserDiagnostic?error.message:modelCheckMessage({status:error.status,code:error.code,diagnostic:error.diagnostic});feedback.textContent=state.modelCheckFeedback;$('settings-api').textContent=t('連線測試未通過');}
  finally{state.modelChecking=false;button.disabled=false;}
}
async function startJob(options){
  if(options.question && options.allow_network){options.capture_api_responses=state.captureApiResponses;state.captureApiResponses=false;if($('capture-api-response'))$('capture-api-response').checked=false;}
  const chat=state.conversationSupported;const retry=Boolean(options.retry_message_id);const draft=state.question;
  if(!options.question){options.verify_content=true;state.sourceJobBackup={project:state.project,conversation:state.conversation,conversations:state.conversations,question:state.question,mode:state.mode,page:state.page,entry:state.entry,pendingMessage:state.pendingMessage,evidence:state.evidence,selectedEvidence:state.selectedEvidence,conversationEvidence:state.conversationEvidence};}
  state.error='';state.errorDiagnostic=null;state.pollError='';state.cancelRequested=false;state.progress=null;state.jobId=null;state.progressReceived=Date.now();state.mode='real';state.page='workbench';state.busy=true;startProgressClock();state.jobKind=options.question?'question':'index';state.tab='answer';clearEvidence();
  if(chat && options.question){options.conversation_id=state.conversation?.id || null;if(!retry){state.pendingMessage={id:'pending-'+Date.now(),role:'user',content:options.question,status:'PENDING'};state.question='';}else state.pendingMessage=null;}
  else{state.pendingMessage=null;}
  render();
  if(chat && options.question)$('conversation-progress')?.scrollIntoView?.({block:'center'});
  try{const response=await api('/api/analyze',{method:'POST',body:JSON.stringify(options)});state.jobId=response.job_id;if(options.framework_reference_path)state.frameworkKnowledge={...state.frameworkKnowledge,configured_path:options.framework_reference_path};if(response.conversation){applyConversation(response.conversation);state.pendingMessage=null;}rememberProgress(response.progress);if(chat)render();await pollJob(response.job_id);}
  catch(error){state.busy=false;stopProgressClock();restoreSourceJob();state.error=error.message;state.errorDiagnostic=safeApiDiagnostic(error.diagnostic);if(chat && !state.question)state.question=draft;render();}
}
function restoreSourceJob(){if(state.sourceJobBackup){Object.assign(state,state.sourceJobBackup);state.sourceJobBackup=null;}}
function sameSourceWorkspace(left,right){
  if(!left || !right)return false;
  const normalized=value=>{const path=String(value || '').replace(/\\/g,'/').replace(/\/+$/,'');return /^[A-Z]:\//i.test(path)?path.toLowerCase():path;};
  const leftKey=left.source_version?.source_key,rightKey=right.source_version?.source_key;
  const sameSource=leftKey && rightKey?leftKey===rightKey:normalized(left.source)===normalized(right.source);
  return sameSource && normalized(left.output)===normalized(right.output);
}
function analysisOptions(question,extra={}){
  const p=state.project;const opts=p.diagnosis.source_options || {};
  return {source:p.source,output:p.output,encoding:opts.encoding || 'auto',source_format:opts.source_format || 'auto',extensions:opts.extensions?.join(',') || null,entry:state.entry || null,question,max_source_pages:state.maxSourcePages,reading_strategy:conversationMode()?'retrieval':state.readingStrategy,answer_detail:state.answerDetail,allow_network:true,index_mode:opts.index_mode || 'catalog',...(conversationMode()?{conversation_id:state.conversation?.id || null}:{}),...extra};
}
async function pollJob(jobId){
  try{const job=await api(`/api/jobs/${encodeURIComponent(jobId)}`);if(state.jobId!==jobId)return;
    const demoPreparation=state.jobKind==='demo';const demoDraft=demoPreparation?state.question:null;
    rememberProgress(job.progress);state.pollError='';state.cancelRequested=Boolean(job.cancel_requested || state.cancelRequested);if(job.status==='RUNNING'){updateProgressPanel();setTimeout(()=>pollJob(jobId),1000);return;}
    state.busy=false;state.jobId=null;stopProgressClock();
    const sourceBackup=state.sourceJobBackup;
    const failedSource=state.jobKind==='index' && job.status!=='COMPLETED';
    if(failedSource)restoreSourceJob();
    if(job.result && !failedSource){const draft=state.question;if(state.mode==='real')applyProject(job.result);else state.project=job.result;if(state.conversationSupported){state.question=draft;state.readingStrategy='retrieval';applyConversation(job.result.conversation || job.conversation);if(job.result.conversation || job.conversation)state.pendingMessage=null;}if(job.result.diagnosis)state.history.unshift(job.result);const context=job.result.diagnosis?.framework_context;if(context)state.frameworkKnowledge={...state.frameworkKnowledge,status:['MATCHED','NO_MATCH'].includes(context.status)?'LOADED':context.status,reason_code:context.reason_code,document:context.document,runtime_verified:false};}
    else if(job.conversation && !failedSource){applyConversation(job.conversation);state.pendingMessage=null;}
    if(job.status==='FAILED'){state.error=apiErrorText(job.error);state.errorDiagnostic=safeApiDiagnostic(job.error?.diagnostic);}if(job.status==='CANCELLED')state.error=conversationMode()?t('已停止本次工作。對話已保留，可以繼續提問。'):t('已取消。再次接入會重用已完成的目錄快取。');
    if(state.jobKind==='index' && job.status==='COMPLETED'){
      const sameScope=sameSourceWorkspace(sourceBackup?.project,state.project);
      if(Array.isArray(job.result?.conversations))state.conversations=sortConversations(job.result.conversations);
      else if(!sameScope)state.conversations=[];
      if(!(job.result?.conversation || job.conversation))state.conversation=sameScope?sourceBackup?.conversation || null:null;
      state.question=sameScope?sourceBackup?.question || '':'';
      state.sourceJobBackup=null;state.entry='';
    }
    if(demoPreparation){state.question=demoDraft;if(job.status==='COMPLETED'){state.entry='';state.readingStrategy='retrieval';if(!job.result?.conversation && !job.conversation)state.conversation=null;}else{state.mode='demo';state.project=state.demoRestore?.project || null;state.conversation=state.demoRestore?.conversation || null;}state.demoRestore=null;}
    if(state.mode==='real' && state.jobKind==='index' && sourceUsable())state.page=state.conversationSupported?'workbench':'sources';
    render();if(demoPreparation && job.status==='COMPLETED')promptDemoQuestion();if(state.mode==='real' && !conversationMode())loadFirstEvidence();
  }catch(error){if(state.jobId!==jobId)return;if(error.status===404 || error.status===403){state.busy=false;state.jobId=null;stopProgressClock();restoreSourceJob();state.error=error.message;if(state.jobKind==='demo'){state.mode='demo';state.project=state.demoRestore?.project || null;state.conversation=state.demoRestore?.conversation || null;state.demoRestore=null;}render();return;}state.pollError=t('進度連線中斷，正在重試；後台工作可能仍在繼續。');updateProgressPanel();setTimeout(()=>pollJob(jobId),2500);}
}
async function loadFirstEvidence(){const first=currentRefs()[0];if(first&&!state.evidence&&!state.busy)await loadEvidence(first.evidence_id);}
async function newConversation(){
  if(state.busy || state.conversationLoading)return;state.conversationLoading=true;render();
  try{const data=await api('/api/conversations',{method:'POST',body:'{}'});applyConversation(data.conversation);state.pendingMessage=null;state.question='';state.error='';state.mode='real';state.page='workbench';state.entry='';state.readingStrategy='retrieval';}
  catch(error){state.error=error.message;}finally{state.conversationLoading=false;render();$('question-input')?.focus?.();}
}
async function openConversation(id){
  if(state.busy || state.conversationLoading)return;state.conversationLoading=true;render();
  try{const data=await api('/api/conversations/'+encodeURIComponent(id));applyConversation(data.conversation);state.pendingMessage=null;state.question='';state.error='';state.mode='real';state.page='workbench';state.readingStrategy='retrieval';}
  catch(error){state.error=error.message;}finally{state.conversationLoading=false;render();}
}
function promptDeleteConversation(id){
  if(state.busy || state.conversationLoading)return;
  const item=state.conversations.find(conversation=>conversation.id===id);
  if(!item)return;
  state.deletingConversationId=id;
  $('delete-conversation-name').textContent=conversationTitle(item.title);
  $('delete-conversation-dialog').showModal();
}
async function deleteConversation(){
  const id=state.deletingConversationId;
  if(!id || state.busy || state.conversationLoading)return;
  state.conversationLoading=true;
  $('delete-conversation-confirm').disabled=true;
  $('delete-conversation-cancel').disabled=true;
  render();
  try{
    const data=await api('/api/conversations/'+encodeURIComponent(id),{method:'DELETE'});
    state.conversations=sortConversations(Array.isArray(data.conversations)?data.conversations:state.conversations.filter(item=>item.id!==id));
    if(state.conversation?.id===id){
      state.conversation=null;state.pendingMessage=null;state.conversationEvidence=null;
      state.impactPages={};state.question='';state.error='';
      if(state.project?.conversation?.id===id)delete state.project.conversation;
      if(state.project)state.project.agent=null;
      state.page='workbench';
    }
    $('delete-conversation-dialog').close();
    state.deletingConversationId=null;
    toast(t('對話已刪除。'));
  }catch(error){toast(error.message);}
  finally{state.conversationLoading=false;$('delete-conversation-confirm').disabled=false;$('delete-conversation-cancel').disabled=false;render();}
}
async function loadConversationEvidence(messageId,id){
  const conversationId=state.conversation?.id;const message=state.conversation?.messages.find(item=>item.id===messageId);
  const ref=message?.evidence_refs?.find(item=>item.evidence_id===id);if(!ref)return;
  const ticket=++state.evidenceTicket;state.conversationEvidence={messageId,ref,span:null,error:''};render();
  try{const data=await api('/api/evidence?id='+encodeURIComponent(id)+'&conversation_id='+encodeURIComponent(conversationId)+'&message_id='+encodeURIComponent(messageId));
    if(ticket!==state.evidenceTicket || state.conversation?.id!==conversationId)return;
    const span=data.spans?.find(item=>item.evidence_id===id);if(!span || span.integrity!=='VALID')throw Error(t('源碼已變更，保留原回答的引用位置；請重新提問以取得目前源碼依據。'));
    state.conversationEvidence={messageId,ref,span,error:''};
  }catch(error){if(ticket!==state.evidenceTicket || state.conversation?.id!==conversationId)return;state.conversationEvidence={messageId,ref,span:null,error:error.message};}
  render();
}
async function retryConversationMessage(id){
  if(state.busy || state.conversationLoading || !sourceUsable())return;
  const question=state.conversation?.messages.find(item=>item.id===id && item.role==='user');
  if(!question || !['failed','cancelled','interrupted'].includes(String(question.status).toLowerCase()))return;
  if(!state.apiConfigured){showSettings();return;}
  await startJob(analysisOptions(question.content,{retry_message_id:id}));
}
async function loadEvidence(id){
  state.selectedEvidence=id;const ticket=++state.evidenceTicket;const runId=state.project?.run_id;
  if(state.mode==='demo'){state.evidence=currentRefs().find(s=>s.evidence_id===id) || null;render();return;}
  state.evidence=null;render();
  try{const data=await api(`/api/evidence?id=${encodeURIComponent(id)}`);if(ticket!==state.evidenceTicket || state.mode!=='real' || runId!==state.project?.run_id)return;
    if(data.run_id && runId && data.run_id!==runId)throw new Error(t('源碼已更新，請重新分析後選擇引用。'));
    const span=data.spans?.find(s=>s.evidence_id===id);if(!span || span.integrity!=='VALID')throw new Error(t('這段證據未通過快照完整性核對，暫不展示原文。'));
    state.evidence=span;render();
  }catch(error){if(ticket===state.evidenceTicket){toast(error.message);render();}}
}
function markdownSummary(project=state.project){
  if(conversationMode() && project===state.project && state.conversation)return `# ${state.conversation.title || t('業務對話')}\n\n${t('來源：')} ${project?.source || ''}\n\n${state.conversation.messages.map(message=>`## ${message.role==='user'?t('你'):t('業務分析助手')}\n\n${message.content || ''}\n\n${(message.evidence_refs || []).map(ref=>`- ${ref.relative_path || ref.path}:${ref.start_line}–${ref.end_line} · ${ref.evidence_id}`).join('\n')}`).join('\n\n')}`;
  if(state.mode==='demo' && project===state.project){
    const selected=demoCase();if(!selected)return t('業務案例尚未載入。');
    return ui`# ${t('知序')} · 業務說明\n\n合成業務案例 · 未呼叫模型\n\n## ${demoText(selected.title)}\n\n${demoText(selected.purpose)}\n\n## 建議業務問題\n\n${demoText(selected.question)}\n\n## 業務處理過程\n\n${(selected.steps || []).map((step,index)=>`${index+1}. ${demoText(step.title)} — ${demoText(step.description)} [${step.evidence_id || ''}]`).join('\n')}\n\n## 業務規則與影響\n\n${(selected.metadata || []).map(item=>`- ${demoText(item.label)}: ${demoText(item.value)}`).join('\n')}\n\n業務設定為案例中的合成聲明，不代表已匯入生產配置或執行記錄。\n\n## 技術與框架依據\n\n框架類型： ${demoKind(selected.kind || selected.id)}\n調查起點： ${selected.entry_program || ''} · ${selected.entry_path || ''}\n快照： ${preview.catalog.snapshot_id || ''}\n\n## 案例源碼引用\n\n${(selected.evidence || []).map(ref=>`- ${ref.relative_path}:${ref.start_line}–${ref.end_line} · ${ref.evidence_id} · ${ref.source_sha256 || ''}`).join('\n')}\n\n## 案例範圍與待確認事項\n\n${(selected.boundaries || []).map(item=>'- '+demoText(item)).join('\n')}\n`;
  }
  const d=project?.diagnosis;const answer=project?.agent?.agent_result;
  const entry=d?.selected_entry;const refs=answer?.evidence_refs || answer?.verified_evidence_refs || [];
  const framework=d?.framework_context || answer?.framework_context;
  const frameworkSummary=framework?ui`\n\n## 框架知識\n\n${frameworkStatusLabel(framework.status)}\n\n${framework.document?.title || ''}\n${framework.document?.sha256 || ''}\n\n文件知識不等於現場執行驗證；閉源實作、實際配置與執行結果仍可能未知。\n\n${(framework.references || []).map(ref=>`- ${ref.reference_id} · ${ref.heading || ''} · ${ref.page!==null && ref.page!==undefined?t('頁碼')+' '+ref.page+' · ':''}L${ref.start_line}–${ref.end_line}`).join('\n')}\n\n${(answer?.claims || []).filter(claim=>claim.kind==='framework_interpretation').map(claim=>`- ${t('框架條件解讀 · 需源碼與現場覆核')}: ${claim.claim || claim.text || ''} [${(claim.evidence_ids || []).join(', ')}] [${(claim.framework_reference_ids || []).join(', ')}]`).join('\n')}`:'';
  const coverageSummary=narrativeText(answer)?ui`\n\n模型解讀 · 需業務覆核\n\n## 閱讀覆蓋\n\n${readingCoverageText(answer.reading_coverage)}${callChainCoverageText(answer.reading_coverage)?'\n\n'+callChainCoverageText(answer.reading_coverage)+'\n'+callChainCoverageNote():''}\n\n${answer.reading_coverage?.complete?t('本次源碼範圍已讀完；閱讀完整不等於業務結論已驗證。'):t('閱讀覆蓋描述本次讀取範圍，不代表已驗證所有業務路徑。')}\n\n${narrativeNotes(answer,project).join('\n')}`:'';
  const investigation=answer?.investigation || d?.investigation;
  const searchSummary=investigation?.mode==='repository'?ui`\n\n## 本次查找\n\n整個代碼庫 · 自動查找\n\n${(investigation.searches || []).map(search=>`- ${search.query || ''} · ${t('匹配檔案')} ${search.matched_file_count ?? (Array.isArray(search.matched_files)?search.matched_files.length:search.matched_files) ?? 0}`).join('\n')}\n\n${(investigation.selected_paths || []).join('\n')}`:'';
  const sourceSummaries=narrativeText(answer) && programSummaries(answer).length?ui`\n\n## 各來源的業務解讀\n\n按來源保留的模型解讀，可補充上方業務摘要；仍需業務覆核。\n\n${programSummaries(answer).map(item=>`### ${item.relative_path || item.program_name || t('源碼引用')}\n\n${item.text}${item.complete===false?'\n\n'+t('部分解讀'):''}`).join('\n\n')}`:'';
  return ui`# ${t('知序')} · 業務分析摘要\n\n資料模式：${syntheticProject(project)?t('本機合成源碼'):t('本機源碼')}\n匯出時間： ${new Date().toISOString()}\n來源： ${project?.source || ''}\n調查起點： ${entry?.program_name || d?.entry_requested || t('整個代碼庫 · 自動查找')}${entry?.relative_path?' · '+entry.relative_path:''}\n快照： ${project?.snapshot_id || t('未建立')}\n接入狀態： ${d?.runner_status || ''}\n問答狀態： ${answer?.status || d?.question_status || ''}\n\n## 業務問題\n\n${d?.question || t('尚未提出問題')}\n\n${narrativeText(answer) || answer?.answer || t('本次沒有生成業務答案。')}${searchSummary}${coverageSummary}${sourceSummaries}\n\n## 本次引用索引\n\n${refs.length?refs.map(r=>`- ${r.relative_path || r.path}:${r.start_line}–${r.end_line} · ${r.evidence_id}`).join('\n'):t('本次沒有回答引用。')}\n\n${frameworkSummary}\n\n## 接入診斷\n\n${(d?.messages || []).map(m=>'- '+m).join('\n')}\n\n問題相關性與完整性尚未核驗。完整診斷與分析邊界可一併匯出為 JSON。\n`;
}
async function loadImpactPage(messageId){
  const message=state.conversation?.messages?.find(item=>item.id===messageId);
  const impact=state.impactPages[messageId] || message?.impact_result;
  if(!impact || impact.next_cursor===null)return;
  try{const path=`/api/impact?handle=${encodeURIComponent(impact.handle)}&conversation_id=${encodeURIComponent(state.conversation.id)}&message_id=${encodeURIComponent(messageId)}&cursor=${impact.next_cursor}`;
    const next=await api(path);state.impactPages[messageId]={...next,rows:[...(impact.rows || []),...next.rows]};render();
  }catch(error){toast(error.message);}
}
async function exportImpact(messageId){
  const message=state.conversation?.messages?.find(item=>item.id===messageId);
  const impact=message?.impact_result;if(!impact)return;
  try{const path=`/api/impact?handle=${encodeURIComponent(impact.handle)}&conversation_id=${encodeURIComponent(state.conversation.id)}&message_id=${encodeURIComponent(messageId)}&export=jsonl`;
    const data=await api(path);download(data.jsonl,'application/x-ndjson;charset=utf-8','related-objects.jsonl');
  }catch(error){toast(error.message);}
}
function download(content,type,name){const blob=new Blob([content],{type});const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=name;link.hidden=true;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}
function exportData(){
  if(state.mode==='demo')return {mode:'synthetic_source_guide',source_origin:'synthetic_framework',model_called:false,snapshot_id:preview.catalog?.snapshot_id || null,case:demoCase(),file_count:preview.catalog?.file_count || 0,programs:preview.catalog?.programs || []};
  return {mode:syntheticProject()?'synthetic_source_analysis':'local_source',...state.project,...(conversationMode()?{conversation:state.conversation}:{})};
}

document.addEventListener('click',event=>{
  const target=event.target.closest('button,a');if(!target)return;
  if(target.classList?.contains('brand')){event.preventDefault();closeNavigation();state.page='workbench';render();return;}
  if(target.hasAttribute('data-copy-safe-diagnostics')){copyDiagnosticSummary();return;}
  if(target.hasAttribute('data-export-safe-diagnostics')){const summary=diagnosticSummaryData();if(summary)download(JSON.stringify(summary,null,2),'application/json;charset=utf-8','business-answer-diagnostics.json');return;}
  if(target.dataset.deleteConversation){closeActivity();promptDeleteConversation(target.dataset.deleteConversation);return;}
  if(target.hasAttribute('data-new-conversation')){closeNavigation();newConversation();}
  if(target.dataset.conversation){closeNavigation();closeActivity();openConversation(target.dataset.conversation);}
  if(target.dataset.turnEvidence)loadConversationEvidence(target.dataset.messageId,target.dataset.turnEvidence);
  if(target.dataset.turnFramework){event.preventDefault();const ref=$('turn-'+target.dataset.messageId+'-framework-'+encodeURIComponent(target.dataset.turnFramework));if(ref){ref.open=true;const list=ref.closest('.message-references');if(list)list.open=true;ref.scrollIntoView({block:'nearest'});}}
  if(target.hasAttribute('data-close-conversation-evidence'))closeConversationEvidence();
  if(target.dataset.retryMessage)retryConversationMessage(target.dataset.retryMessage);
  if(target.dataset.impactMore)loadImpactPage(target.dataset.impactMore);
  if(target.dataset.impactExport)exportImpact(target.dataset.impactExport);
  if(target.hasAttribute('data-retry-pending') && state.pendingMessage){state.question=state.pendingMessage.content;state.pendingMessage=null;render();$('question-input')?.focus?.();}
  if(target.dataset.demoCase){if(state.busy)return;selectDemoCase(target.dataset.demoCase);}
  if(target.hasAttribute('data-prepare-demo'))prepareDemo();
  if(target.hasAttribute('data-retry-demo'))loadFrameworkDemo();
  if(target.dataset.answerSection){
    const heading=$(target.dataset.answerSection);
    if(heading){heading.focus({preventScroll:true});heading.scrollIntoView({block:'start'});}
  }
  if(target.dataset.page){closeNavigation();state.page=target.dataset.page;render();}
  if(target.dataset.tab){state.tab=target.dataset.tab;render();}
  if(target.dataset.evidence)loadEvidence(target.dataset.evidence);
  if(target.dataset.frameworkReference){event.preventDefault();const ref=$(frameworkAnchor(target.dataset.frameworkReference));if(ref){ref.open=true;const list=ref.closest('.framework-references');if(list)list.open=true;const support=ref.closest('.supporting-context');if(support)support.open=true;ref.scrollIntoView({block:'nearest'});}}
  if(target.hasAttribute('data-settings'))showSettings();
  if(target.dataset.question){if(state.mode==='demo'){toast(t('請先載入案例或接入本機源碼，再分析自己的問題。'));return;}state.question=target.dataset.question;render();$('question-input')?.focus();}
  if(target.dataset.program){state.entry=target.dataset.program;state.question=state.mode==='demo'?demoText(demoCase()?.question):ui`請說明 ${target.dataset.programLabel || target.dataset.program} 支援的業務、受理條件、狀態變化與異常影響。`;state.page='workbench';state.tab='answer';render();}
  if(target.id==='cancel-job')cancelJob();
  if(target.dataset.sourcePage){state.sourcePage+=target.dataset.sourcePage==='next'?1:-1;render();}
  if(target.hasAttribute('data-connect'))openSource();
  if(target.hasAttribute('data-source-update'))openSource('update');
  if(target.hasAttribute('data-source-switch'))openSource('switch');
  if(target.dataset.pickFolder)pickFolder(target.dataset.pickFolder);
  if(target.hasAttribute('data-demo')){state.page='workbench';setMode('demo');}
  if(target.hasAttribute('data-demo-result')){state.page='workbench';state.tab='answer';render();}
  if(target.hasAttribute('data-history')){const item=state.history[Number(target.dataset.history)];download(markdownSummary(item),'text/markdown;charset=utf-8','source-analysis-history.md');toast(t('已匯出該次摘要；目前源碼範圍保持不變。'));}
  if(target.classList.contains('close-dialog'))target.closest('dialog').close();
});
document.addEventListener('cancel',event=>{if(event.target.id==='conversation-evidence-dialog'){event.preventDefault();closeConversationEvidence();}},true);
document.addEventListener('keydown',event=>{
  const dialog=document.querySelector('#conversation-evidence-dialog[open], #navigation-dialog[open], #activity-dialog[open]');
  if(event.key==='Tab' && dialog){
    const controls=Array.from(dialog.querySelectorAll('button:not([disabled]), a[href], [tabindex="0"]')).filter(el=>el.getClientRects().length);
    const first=controls[0],last=controls.at(-1);
    if(event.shiftKey && document.activeElement===first){event.preventDefault();last?.focus();}
    else if(!event.shiftKey && document.activeElement===last){event.preventDefault();first?.focus();}
  }
  if(event.key==='Escape' && !document.querySelector('dialog[open]')){const options=document.querySelector('.conversation-options[open]');if(options){options.open=false;options.querySelector('summary')?.focus();}}
});
$('activity-toggle')?.addEventListener('click',openActivity);
$('activity-close')?.addEventListener('click',closeActivity);
$('activity-dialog')?.addEventListener('close',()=>{document.querySelector('.app-shell')?.append($('activity-sidebar'));$('activity-toggle').setAttribute('aria-expanded','false');if($('activity-toggle').getClientRects().length)$('activity-toggle').focus();});
$('navigation-toggle')?.addEventListener('click',openNavigation);
$('navigation-close')?.addEventListener('click',closeNavigation);
$('navigation-dialog')?.addEventListener('close',()=>{document.querySelector('.app-shell')?.prepend($('workspace-sidebar'));$('navigation-toggle').setAttribute('aria-expanded','false');$('navigation-toggle').focus();});
if(typeof window!=='undefined'){
  window.matchMedia?.('(min-width: 1181px)').addEventListener?.('change',event=>{if(event.matches)closeActivity();});
  window.addEventListener('popstate',restorePageRoute);
  window.addEventListener('hashchange',restorePageRoute);
  window.matchMedia?.('(min-width: 761px)').addEventListener?.('change',event=>{if(event.matches)closeNavigation();});
  const initialPage=window.location.hash.slice(1);if(['workbench','sources','history'].includes(initialPage))state.page=initialPage;
}
$('delete-conversation-confirm').addEventListener('click',deleteConversation);
$('delete-conversation-cancel').addEventListener('click',()=> $('delete-conversation-dialog').close());
$('delete-conversation-dialog').addEventListener('close',()=>{if(!state.conversationLoading)state.deletingConversationId=null;});
document.addEventListener('input',event=>{
  if(event.target.id==='demo-question-input'){state.question=event.target.value;return;}
  if(event.target.id==='question-input' && conversationMode()){state.question=event.target.value;resizeComposer();return;}
  if(event.target.id==='question-input'){state.question=event.target.value;if(state.mode==='real'){const count=document.querySelector('.tab-count');if(count)count.textContent=draftChanged()?0:result()?.tool_trace?.length || 0;if(state.tab!=='relations'){const content=document.querySelector('.answer-content');if(content)content.innerHTML=state.tab==='trace'?traceView():state.tab==='api'?apiResponseView():actualAnswer();const panel=document.querySelector('.evidence-panel');if(panel)panel.outerHTML=evidencePanel();const framework=document.querySelector('.framework-card');if(framework)framework.outerHTML=frameworkKnowledgeCard();}}}
  if(event.target.id==='program-filter'){state.filter=event.target.value;state.sourcePage=0;$('program-rows').innerHTML=sourceRows();$('source-pagination').innerHTML=sourcePagination();}
  if(event.target.id==='entry-filter'){state.entryFilter=event.target.value;$('entry-select').innerHTML=entryOptions();}
});
document.addEventListener('change',event=>{if(event.target.id==='entry-select'){state.entry=event.target.value;render();}if(event.target.id==='answer-detail' && ['brief','detailed'].includes(event.target.value)){state.answerDetail=event.target.value;render();}if(event.target.id==='reading-depth'){state.maxSourcePages=Number(event.target.value);render();}if(event.target.id==='reading-strategy' || event.target.id==='conversation-strategy'){state.readingStrategy=event.target.value;render();}});
document.addEventListener('keydown',event=>{if(event.target.id==='question-input' && conversationMode() && event.key==='Enter' && !event.shiftKey && !event.isComposing){event.preventDefault();if(!state.busy && !state.conversationLoading)$('question-form')?.requestSubmit?.();}});
document.addEventListener('submit',async event=>{
  if(event.target.id==='demo-question-form'){
    event.preventDefault();if(state.busy)return;
    if(!state.question.trim()){toast(t('請先輸入一個業務問題。'));return;}
    await prepareDemo();return;
  }
  if(event.target.id==='question-form'){
    event.preventDefault();if(state.busy || state.conversationLoading)return;
    if(state.mode==='demo'){toast(t('請先載入案例或接入本機源碼，再分析自己的問題。'));return;}
    if(!state.question.trim()){toast(t('請先輸入一個業務問題。'));return;}
    if(!sourceUsable()){toast(t('請先成功接入本機源碼。'));return;}
    if(!state.apiConfigured){showSettings();return;}
    await startJob(analysisOptions(state.question.trim()));
  }
});
function pastedPath(value){const text=value.trim();return text.startsWith('"')&&text.endsWith('"')?text.slice(1,-1):text;}
$('source-form').addEventListener('submit',async event=>{event.preventDefault();if(!state.connected || state.busy)return;const options={source:pastedPath($('source-path').value),output:pastedPath($('output-path').value),encoding:$('source-encoding').value,source_format:$('source-format').value,extensions:$('source-extensions').value.trim() || null,index_mode:'catalog',verify_content:$('verify-content').checked,reading_strategy:state.readingStrategy,answer_detail:state.answerDetail,max_source_pages:state.maxSourcePages,allow_network:false};if(state.conversationSupported && $('source-framework-path')?.value.trim())options.framework_reference_path=pastedPath($('source-framework-path').value);$('source-dialog').close();await startJob(options);});
$('connect-button').addEventListener('click',openSource);
$('demo-mode').addEventListener('click',()=>setMode('demo'));
$('real-mode').addEventListener('click',()=>setMode('real'));
$('recent-analysis').addEventListener('click',()=>{closeActivity();state.page='workbench';render();});
function apiConfigurationMessage(){
  if(!state.apiConfigurationError)return '';
  const messages={
    BASE_URL_MISSING:'尚未填寫接口地址。請在 .env 設定 COMPANY_API_BASE_URL。',
    CHAT_MODEL_MISSING:'尚未填寫模型。請在 .env 設定 COMPANY_CHAT_MODEL。',
    API_KEY_MISSING:'尚未填寫密鑰。請在 .env 設定 COMPANY_API_KEY。',
    BASE_URL_INVALID:'接口地址格式無效。請核對 COMPANY_API_BASE_URL 與接口要求的路徑。',
    API_STYLE_UNSUPPORTED:'接口風格不受支援。請核對 COMPANY_API_STYLE；預設為 openai_compatible。',
    ENV_FILE_INVALID:'.env 格式無效。請使用 UTF-8 文字，每行填寫一個「變數名稱=值」，並核對引號。',
    ENV_FILE_UNREADABLE:'本機服務無法讀取 .env。請檢查檔案是否可由目前使用者讀取。',
    ENV_FILE_TOO_LARGE:'.env 檔案過大。請只保留接口設定，不要把源碼或其他內容貼入檔案。',
  };
  return t(messages[state.apiConfigurationError] || '接口配置無效。請核對 .env 與啟動服務時的環境變數，修改後重新啟動。');
}
function frameworkConfiguredPath(){const framework=state.frameworkKnowledge;return framework?.configured_path || framework?.path || framework?.document?.path || '';}
function frameworkFeedback(){const framework=state.frameworkKnowledge || {};const document=framework.document || {};return frameworkStatusLabel(framework.status)+(document.title?' · '+document.title:'')+(framework.status==='UNAVAILABLE'?' · '+frameworkConfigurationMessage(framework.reason_code):'');}
function frameworkDocumentList(){const knowledge=state.frameworkKnowledge || {};const documents=knowledge.documents || [];return documents.length?ui`<details class="framework-loaded-files"><summary>已讀取的框架文件 · ${documents.length}</summary><ul>${documents.map(item=>`<li>${escapeHTML(item.name || item.title || '')}<small>${escapeHTML(item.encoding || '')}</small></li>`).join('')}</ul>${(knowledge.loading_warnings || []).map(item=>`<p>${escapeHTML(item.name || '')} ${escapeHTML(frameworkConfigurationMessage(item.reason_code))}</p>`).join('')}</details>`:'';}
function showSettings(){if($('capture-api-response'))$('capture-api-response').checked=state.captureApiResponses;$('settings-description').textContent=apiConfigurationMessage() || (state.apiConfigured?t('接口設定已提供，發起分析時會使用已設定的接口生成業務解讀。'):t('源碼索引可離線使用。模型問答需要先在項目根目錄的 .env 設定接口。'));$('settings-local').textContent=state.connected?t('已啟動'):t('尚未啟動');$('settings-api').textContent=state.modelCheckStatus==='success'?t('已驗證可用'):state.modelCheckStatus==='failure'?t('連線測試未通過'):state.apiConfigurationError?t('設定需修正'):(state.apiConfigured?t('已提供 · 待實際驗證'):t('尚未提供'));$('settings-framework').textContent=frameworkStatusLabel(frameworkContext().status);if($('model-check-feedback'))$('model-check-feedback').textContent=state.modelCheckFeedback;if($('framework-form'))$('framework-form').hidden=!state.conversationSupported;if($('framework-path'))$('framework-path').value=frameworkConfiguredPath();if($('framework-feedback'))$('framework-feedback').textContent=frameworkFeedback();if($('framework-document-list'))$('framework-document-list').innerHTML=frameworkDocumentList();$('settings-dialog').showModal();}
$('framework-form')?.addEventListener('submit',async event=>{event.preventDefault();if(state.busy)return;$('framework-save').disabled=true;$('framework-feedback').textContent=t('正在讀取框架資料…');try{const data=await api('/api/framework',{method:'POST',body:JSON.stringify({path:pastedPath($('framework-path').value)})});state.frameworkKnowledge=data.framework_knowledge;render();$('settings-framework').textContent=frameworkStatusLabel(frameworkContext().status);$('framework-feedback').textContent=frameworkFeedback();if($('framework-document-list'))$('framework-document-list').innerHTML=frameworkDocumentList();}catch(error){$('framework-feedback').textContent=error.message;}finally{$('framework-save').disabled=false;}});
$('model-check-button')?.addEventListener('click',checkModelConnection);
$('capture-api-response')?.addEventListener('change',event=>{state.captureApiResponses=Boolean(event.target.checked);});
$('settings-button').addEventListener('click',showSettings);
$('connection-status').addEventListener('click',showSettings);
$('export-button').addEventListener('click',()=>$('export-dialog').showModal());
$('export-markdown').addEventListener('click',()=>{download(markdownSummary(),'text/markdown;charset=utf-8',state.mode==='demo'?'business-case-guide.md':'source-analysis-summary.md');$('export-dialog').close();toast(t('已匯出摘要，包含資料模式與分析邊界。'));});
$('export-json').addEventListener('click',()=>{download(JSON.stringify(exportData(),null,2),'application/json;charset=utf-8',state.mode==='demo'?'business-case-guide.json':'source-analysis.json');$('export-dialog').close();toast(t('已匯出本次資料。'));});
$('language-select').addEventListener('change',event=>{
  const priorCaseQuestion=state.mode==='demo'?demoText(demoCase()?.question):null;
  if(!selectInterfaceLocale(event.target.value))return;
  if(state.mode==='demo' && state.question===priorCaseQuestion)state.question=demoText(demoCase()?.question);
  render();
  if($('settings-dialog').open)showSettings();
  $('toast').hidden=true;
});
render();initialize();
