'use strict';

const icons = {
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
};
const icon = name => `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.document}</svg>`;
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const formatNumber = value => Number(value || 0).toLocaleString('en');

const preview = {
  question:t('分期保費是如何計算出來的？'),
  snapshot:'illustrative-preview',
  programs:[
    {program_name:'POLICY-ENTRY',relative_path:'programs/policy-entry.cbl',start_line:2},
    {program_name:'BASE-PREMIUM',relative_path:'programs/base-premium.cbl',start_line:2},
    {program_name:'PREMIUM-CALC',relative_path:'programs/premium-calc.cbl',start_line:2},
    {program_name:'RIDER-TOTAL',relative_path:'programs/rider-total.cbl',start_line:2},
    {program_name:'PAYMENT-FACTOR',relative_path:'programs/payment-factor.cbl',start_line:2},
    {program_name:'RESULT-RETURN',relative_path:'programs/result-return.cbl',start_line:2},
  ],
  evidence:[
    {evidence_id:'preview-01',title:t('分期保費計算'),relative_path:'programs/premium-calc.cbl',start_line:42,end_line:47,integrity:'ILLUSTRATIVE',source_text:'COMPUTE INSTALMENT-PREMIUM\n    ROUNDED =\n    ANNUAL-PREMIUM\n    * PAYMENT-FACTOR\nEND-COMPUTE.\nMOVE INSTALMENT-PREMIUM TO OUT-PREMIUM.'},
    {evidence_id:'preview-02',title:t('年繳保費組成'),relative_path:'programs/premium-calc.cbl',start_line:30,end_line:35,integrity:'ILLUSTRATIVE',source_text:'COMPUTE ANNUAL-PREMIUM =\n    BASE-PREMIUM\n    + RIDER-PREMIUM\n    - PREMIUM-DISCOUNT\nEND-COMPUTE.\nPERFORM GET-PAYMENT-FACTOR.'},
    {evidence_id:'preview-03',title:t('繳費係數來源'),relative_path:'programs/payment-factor.cbl',start_line:18,end_line:23,integrity:'ILLUSTRATIVE',source_text:'EXEC SQL\n    SELECT MODE-FACTOR\n    INTO :PAYMENT-FACTOR\n    FROM PAYMENT-OPTIONS\n    WHERE MODE-CODE = :PAYMENT-MODE\nEND-EXEC.'},
  ],
  relations:[
    {source_program:'POLICY-ENTRY',target_name:'PREMIUM-CALC',relation_type:'CALLS',status:'illustrative',evidence_id:'preview-01'},
    {source_program:'PREMIUM-CALC',target_name:'PAYMENT-FACTOR',relation_type:'CALLS',status:'illustrative',evidence_id:'preview-03'},
    {source_program:'PREMIUM-CALC',target_name:'RESULT-RETURN',relation_type:'CALLS',status:'illustrative',evidence_id:'preview-02'},
  ],
};

const state = {mode:'demo',page:'workbench',tab:'answer',connected:false,token:null,apiConfigured:false,
  project:null,question:preview.question,entry:'PREMIUM-CALC',selectedEvidence:'preview-01',
  evidence:preview.evidence[0],busy:false,jobId:null,jobKind:null,error:'',history:[],filter:'',evidenceTicket:0};

function decorate(){document.querySelectorAll('[data-icon]').forEach(el => {el.innerHTML=icon(el.dataset.icon);});}
function toast(message){$('toast').textContent=message;$('toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>{$('toast').hidden=true;},4500);}
function currentPrograms(){return state.mode==='demo'?preview.programs:(state.project?.programs || []);}
function result(){return state.mode==='real'?state.project?.agent?.agent_result:null;}
function diagnosis(){return state.mode==='real'?state.project?.diagnosis:null;}
function draftChanged(){const d=diagnosis();return state.mode==='real' && Boolean(result()) && (state.question.trim()!==(d?.question || '').trim() || state.entry!==(d?.selected_entry?.program_name || d?.entry_requested || ''));}
function currentRefs(){
  if(state.mode==='demo')return preview.evidence;
  if(draftChanged())return [];
  const answer=result();
  if(answer?.evidence_refs?.length)return answer.evidence_refs;
  if(answer?.verified_evidence_refs?.length)return answer.verified_evidence_refs;
  return [];
}
function currentRelations(){return state.mode==='demo'?preview.relations:(state.project?.relations?.edges || []);}
function statusLabel(status){return ({SUPPORTED_WITH_BOUNDARIES:t('已核驗局部語句 · 保留邊界'),CITATION_VERIFIED_ONLY:t('引用已核對 · 解釋待核驗'),ABSTAINED:t('證據不足 · 尚未形成結論'),NOT_READY:t('尚未具備分析條件'),SAFE_STOP:t('調查已停止 · 需要處理'),NETWORK_DISABLED:t('尚未啟用模型問答'),NOT_REQUESTED:t('尚未提出業務問題'),BLOCKED:t('接入受阻'),INDEX_READY:t('源碼已就緒'),NEEDS_ATTENTION:t('源碼已接入 · 有待核對'),PREPARING:t('正在接入源碼')})[status] || status || t('尚未分析');}
function sourceUsable(){const d=diagnosis();return d && ['INDEX_READY','NEEDS_ATTENTION'].includes(d.runner_status) && d.source_manifest_verified && currentPrograms().length>0;}
function renderChrome(){
  const demo=state.mode==='demo';const d=diagnosis();const files=d?.build_report?.files;
  document.querySelectorAll('[data-page]').forEach(el=>{el.classList.toggle('active',el.dataset.page===state.page);if(el.dataset.page===state.page)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');});
  $('demo-mode').classList.toggle('selected',demo);$('real-mode').classList.toggle('selected',!demo);
  $('demo-mode').setAttribute('aria-pressed',String(demo));$('real-mode').setAttribute('aria-pressed',String(!demo));
  const titles={workbench:[t('分析工作台'),t('讓業務邏輯，清晰可見。'),t('從一個業務問題出發，沿著源碼找到可追溯的答案。')],sources:[t('源碼資料庫'),t('先看清楚，分析了甚麼。'),t('程式、來源與版本，都有跡可尋。')],history:[t('分析記錄'),t('每一次分析，都有依據。'),t('回看本次工作階段的問題、來源快照與結論。')]};
  const [page,title,subtitle]=titles[state.page];$('breadcrumb-page').textContent=page;$('page-title').textContent=title;$('page-subtitle').textContent=subtitle;
  $('connection-status').classList.toggle('live',state.connected);
  $('connection-status').innerHTML=`<i></i>${state.connected?t('本機服務已啟動'):t('介面預覽 · 本機服務未啟動')}`;
  $('config-dot').classList.toggle('ready',state.apiConfigured);
  $('project-title').textContent=demo?t('保費計算 · 合成示例'):(state.project?.source?.split(/[\\/]/).filter(Boolean).pop() || t('尚未接入本機源碼'));
  const badge=$('source-badge');badge.textContent=demo?t('示例資料'):(state.busy?t('更新中'):(d?statusLabel(d.runner_status):t('等待接入')));
  badge.className=`badge ${demo?'preview':d?.runner_status==='BLOCKED'?'warning':sourceUsable()?'positive':'neutral'}`;
  $('project-description').textContent=demo?t('8 個檔案 · 6 個程式定義 · 2 個 COPYBOOK'):d?ui`${formatNumber(files?.decoded)} 個檔案 · ${formatNumber(d.program_count)} 個程式定義 · ${formatNumber(d.build_report?.diagnostics?.copybook_count)} 個 COPYBOOK`:t('選擇源碼與結果資料夾，建立自己的分析範圍');
  $('snapshot-info').innerHTML=icon('layers')+(demo?t('介面示例 · 非實際分析'):state.project?.snapshot_id?ui`快照 ${escapeHTML(state.project.snapshot_id.replace('sha256:','').slice(0,8))}`:t('尚未建立快照'));
  $('nav-count').textContent=demo?'8':formatNumber(files?.decoded);
  $('recent-title').innerHTML=demo?t('分期保費計算邏輯<small>合成示例 · 介面預覽</small>'):`${escapeHTML(d?.question || t('尚未建立業務問答'))}<small>${d?t('本機源碼 · 本次工作階段'):t('接入源碼後開始')}</small>`;
  $('footer-scope').textContent=demo?t('介面示例 · 不代表公司業務結果'):t('本機源碼 · 問題完整性仍需覆核');
  $('export-button').disabled=state.busy || (!demo && !d);
  $('connect-button').disabled=state.busy;
  const notice=state.error || (!demo && d?.runner_status==='BLOCKED'?(d.messages || []).join(' '):'');
  $('notice').textContent=notice;$('notice').hidden=!notice;
}

function questionCard(){
  const demo=state.mode==='demo';const programs=currentPrograms();
  return ui`<section class="question-card"><div class="section-eyebrow">本次業務問題</div><form id="question-form"><div class="question-top"><div class="spark-disc">${icon('spark')}</div><div class="question-field"><textarea rows="1" id="question-input" aria-label="業務問題" placeholder="例如：這個程式如何處理輸入、計算與異常？" ${state.busy?'disabled':''}>${escapeHTML(state.question)}</textarea></div></div><div class="question-bottom"><label class="entry-choice" for="entry-select">${icon('branch')}調查起點<select id="entry-select" ${state.busy?'disabled':''}><option value="">從程式目錄探索</option>${programs.map(p=>`<option value="${escapeHTML(p.program_name)}" ${state.entry===p.program_name?'selected':''}>${escapeHTML(p.program_name)}</option>`).join('')}</select></label><button class="button primary" type="submit" ${state.busy || (!demo&&!sourceUsable())?'disabled':''}>${icon(demo?'arrow':'spark')}${demo?t('預覽示例解讀'):t('開始業務分析')}</button></div>${!demo?t('<p class="model-notice">發起分析將使用已設定的模型接口，傳送問題及有限源碼證據。</p>'):''}</form><div class="question-chips ${demo?'hidden':''}"><button data-question="請解釋這個程式的主要處理步驟和輸入輸出。">${icon('document')} 主要處理流程</button><button data-question="哪些程式會被呼叫？請引用對應的源碼。">${icon('branch')} 呼叫與依賴</button><button data-question="發生錯誤時，狀態如何傳回呼叫方？">${icon('shield')} 異常處理</button></div></section>`;
}

function answerTabs(){const count=state.mode==='demo'?3:(draftChanged()?0:result()?.tool_trace?.length || 0);return ui`<div class="answer-tabs" role="tablist" aria-label="分析檢視"><button data-tab="answer" role="tab" aria-selected="${state.tab==='answer'}" class="${state.tab==='answer'?'selected':''}">業務解讀</button><button data-tab="relations" role="tab" aria-selected="${state.tab==='relations'}" class="${state.tab==='relations'?'selected':''}">程式關係</button><button data-tab="trace" role="tab" aria-selected="${state.tab==='trace'}" class="${state.tab==='trace'?'selected':''}">調查記錄<span class="tab-count">${count}</span></button></div>`;}
function cite(id,label){return ui`<button class="citation" data-evidence="${escapeHTML(id)}" aria-label="查看證據 ${escapeHTML(label)}">${escapeHTML(label)}</button>`;}

function previewAnswer(){return ui`<div class="answer-state-row"><span class="badge neutral">${icon('document')} 解讀示例 · 未執行模型分析</span><small>3 段示例證據</small></div><h2>先計算年繳保費，<br>再套用繳費係數。</h2><p class="answer-intro">這個示例展示如何把分散在程式中的計算步驟，整理成可逐項回查的業務解讀。</p><div class="formula-box"><span class="formula-label">計算摘要</span><strong>分期保費<span class="formula-operator">=</span>年繳保費<span class="formula-operator">×</span>繳費係數</strong>${cite('preview-01','1')}</div><div class="steps"><div class="business-step"><span class="step-number">01</span><div><h3>彙總年繳保費 ${cite('preview-02','2')}</h3><p>將基本保費及附加保障保費加總，再扣除適用折扣，形成計算基礎。</p></div></div><div class="business-step"><span class="step-number">02</span><div><h3>取得對應的繳費係數 ${cite('preview-03','3')}</h3><p>以繳費方式查詢係數。源碼顯示查詢來源；實際設定值需要配置資料確認。</p></div></div><div class="business-step"><span class="step-number">03</span><div><h3>計算並回傳分期保費 ${cite('preview-01','1')}</h3><p>年繳保費乘以繳費係數，使用 ROUNDED，並將結果寫入輸出欄位。</p></div></div></div><div class="boundary-box">${icon('alert')}<div><strong>尚待確認</strong><p>實際係數、折扣適用條件與完整異常路徑，需要同版本配置及進一步源碼核對。</p></div></div><div class="process-heading">處理流程概覽<span>示例路徑</span></div><div class="mini-flow">${[[t('接收請求'),'POLICY-ENTRY'],[t('計算年繳保費'),'PREMIUM-CALC'],[t('取得繳費係數'),'PAYMENT-FACTOR'],[t('回傳結果'),'RESULT-RETURN']].map((n,i)=>`${i?`<span class="flow-arrow">${icon('arrow')}</span>`:''}<div class="flow-node"><strong>${n[0]}</strong><small>${n[1]}</small></div>`).join('')}</div><div class="answer-footnote">${icon('info')}以上為介面設計示例；本機模式只呈現實際分析結果。</div>`;}

function boundaryText(value){if(typeof value==='string')return value;return value?.message || value?.detail || value?.reason || JSON.stringify(value);}
function actualAnswer(){
  const answer=result();const d=diagnosis();const status=answer?.status || d?.question_status;
  if(draftChanged())return ui`<div class="answer-empty">${icon('spark')}<h2>準備分析新的問題</h2><p>問題或調查起點已更改。發起分析後，這裡會呈現新問題的結果。<br>上一題摘要保留在分析記錄中。</p></div>`;
  if(!answer || !answer.claims?.length){
    const failed=answer || ['NOT_READY','SAFE_STOP','BLOCKED','NETWORK_DISABLED'].includes(status);
    return `<div class="answer-empty">${icon(failed?'info':'spark')}<h2>${escapeHTML(statusLabel(status))}</h2><p>${failed?t('保留現有資料與阻斷原因。處理缺口後，可重新發起分析。'):t('源碼已就緒。選擇調查起點，輸入業務問題，即可從當前源碼開始。')}</p></div>${renderBoundaries(answer?.boundaries || d?.messages || [])}${state.project?.agent?.reason_code?ui`<p class="source-note">診斷代碼：${escapeHTML(state.project.agent.reason_code)}</p>`:''}`;
  }
  const refs=currentRefs();
  return ui`<div class="real-answer"><div class="answer-state-row"><span class="badge neutral">${icon('check')}${escapeHTML(statusLabel(answer.status))}</span><small>${refs.length} 段引用</small></div><h2>來自當前源碼的分析結果</h2><p class="answer-intro">以下陳述保留實際核驗狀態。點選引用，可回到本次快照的源碼位置。</p><div class="steps">${answer.claims.map((claim,i)=>`<div class="business-step"><span class="step-number">${String(i+1).padStart(2,'0')}</span><div><p class="claim-text">${escapeHTML(claim.claim || claim.text || '')}${(claim.evidence_ids || []).map(id=>cite(id,String(Math.max(1,refs.findIndex(r=>r.evidence_id===id)+1)))).join('')}</p><small class="field-hint">${escapeHTML(({code_fact:t('源碼事實'),business_inference:t('業務推測 · 未核驗'),open_question:t('待確認問題 · 未形成結論')})[claim.kind] || t('候選解讀'))} · ${escapeHTML(({supported:t('局部語句已核驗'),citation_verified_only:t('引用已核對，語義待核驗'),unsupported:t('證據尚不支持')})[claim.support_status] || t('語義未核驗'))}</small></div></div>`).join('')}</div>${renderBoundaries(answer.boundaries || [])}<div class="answer-footnote">${icon('info')}問題相關性與完整性尚未核驗；局部語句通過不代表整條業務流程已驗證。</div></div>`;
}
function renderBoundaries(items){if(!items.length)return '';return ui`<div class="boundary-box">${icon('alert')}<div><strong>待確認與分析邊界</strong>${items.map(b=>`<p>${escapeHTML(boundaryText(b))}</p>`).join('')}</div>`;}
function relationsView(){
  const edges=currentRelations();return ui`<h2>沿著程式，回查關係。</h2><p class="graph-subtitle">${state.mode==='demo'?t('此處是預先編排的關係示例。'):t('以下來自當前源碼的靜態呼叫與 COPY 關係，不表示執行順序或實際可達。')}${state.project?.relations?.truncated && state.mode==='real'?t(' 關係數量已達顯示上限。'):''}</p>${edges.length?edges.slice(0,40).map(edge=>`<div class="relation-row"><div class="relation-program">${escapeHTML(edge.source_program || edge.relative_path || t('來源'))}<span class="relation-meta">${escapeHTML(edge.relation_type==='INCLUDES_COPY'?t('引入定義'):t('呼叫來源'))}</span></div><div class="relation-link">${escapeHTML(edge.relation_type)}</div><div class="relation-program">${escapeHTML(edge.target_name || t('未解析目標'))}<span class="relation-meta">${escapeHTML(({confirmed:t('源碼關係已確認'),candidate:t('候選關係'),unresolved:t('未能確認'),illustrative:t('示例關係')})[edge.status] || edge.status)}</span></div>${edge.evidence_id?cite(edge.evidence_id,'↗'):''}</div>`).join(''):t('<div class="answer-empty"><p>本次沒有可展示的呼叫或 COPY 關係；不能據此推斷不存在依賴。</p></div>')}${edges.length>40?t('<p class="source-note">目前顯示前 40 項關係。完整回傳資料可在匯出檔中查看。</p>'):''}`;
}
function traceView(){
  if(draftChanged())return ui`<div class="answer-empty">${icon('clock')}<h2>新問題尚未執行調查</h2><p>問題或調查起點已更改。發起分析後，這裡會顯示新問題的工具記錄。</p></div>`;
  if(state.mode==='demo')return ui`<h2>每一步，留下可核對的依據。</h2><p class="graph-subtitle">以下是介面呈現方式示例，並非已執行的模型調查記錄。</p>${[[t('定位分析起點'),t('從程式目錄及名稱，選定本次要調查的範圍。')],[t('核對計算與依賴'),t('檢視欄位讀寫及呼叫關係，收集相關源碼引用。')],[t('集中讀取證據'),t('閱讀已發現的源碼片段，逐項綁定結論與未決事項。')]].map(([title,desc])=>ui`<div class="trace-row"><h3>${title}<span class="badge preview">示例步驟</span></h3><p>${desc}</p></div>`).join('')}`;
  const trace=result()?.tool_trace || [];const labels={search_code:t('定位程式與欄位'),inspect_symbol:t('檢視定義與依賴'),trace_relations:t('追蹤源碼關係'),read_evidence:t('讀取源碼證據')};
  return ui`<h2>本次調查記錄</h2><p class="graph-subtitle">只顯示實際工具活動與狀態，不包含模型私有推理。</p>${trace.length?trace.map((item,i)=>{const name=item.tool || item.action || item.tool_name;const outcome=({SUCCEEDED:t('已執行'),FAILED:t('執行失敗'),REJECTED:t('參數未通過核對'),POLICY_REJECTED:t('調查範圍受限'),SNAPSHOT_REJECTED:t('快照核對失敗'),ATTEMPTED:t('已嘗試')})[item.outcome] || item.outcome || t('狀態未提供');return ui`<div class="trace-row"><h3>${String(i+1).padStart(2,'0')} · ${escapeHTML(labels[name] || name || t('工具活動'))}</h3><p><code>${escapeHTML(name)}</code> · ${escapeHTML(outcome)}${item.result?.status?' · '+escapeHTML(item.result.status):''}</p><details class="trace-details"><summary>查看工具資料</summary><pre>${escapeHTML(JSON.stringify(item.arguments || {},null,2))}</pre></details></div>`;}).join(''):t('<div class="answer-empty"><p>尚未執行模型調查。建立索引不會產生模型工具記錄。</p></div>')}`;
}
function codeMarkup(span){
  if(!span)return t('<div class="answer-empty"><p>選擇一段引用，查看源碼內容。</p></div>');
  const lines=String(span.source_text || '').split('\n');
  return lines.map((line,i)=>{let text=escapeHTML(line);text=text.replace(/\b(COMPUTE|ROUNDED|END-COMPUTE|MOVE|TO|EXEC|SQL|SELECT|INTO|FROM|WHERE|END-EXEC|PERFORM|CALL|IF|END-IF|GOBACK)\b/g,'<span class="code-keyword">$1</span>');return `<div class="code-line ${state.mode==='demo'&&i<5?'highlight':''}"><span class="line-number">${Number(span.start_line || 1)+i}</span><span class="code-text">${text || ' '}</span></div>`;}).join('');
}
function evidencePanel(){
  const refs=currentRefs();const span=draftChanged() && state.tab!=='relations'?null:state.evidence;const demo=state.mode==='demo';
  return ui`<aside class="evidence-panel" aria-label="源碼證據"><div class="evidence-heading"><span>${icon('code')}</span><h2>源碼證據</h2><small>${String(refs.length).padStart(2,'0')}</small></div><div class="evidence-summary"><span>${demo?t('示例引用'):t('本次回答引用')}</span><span>${demo?t('非實際源碼驗收'):t('點選檢視原文')}</span></div><div class="evidence-list">${refs.length?refs.map((ref,i)=>`<button class="evidence-item ${state.selectedEvidence===ref.evidence_id?'selected':''}" data-evidence="${escapeHTML(ref.evidence_id)}"><span class="evidence-number">${String(i+1).padStart(2,'0')}</span><span><strong>${escapeHTML(ref.title || ref.relative_path?.split('/').pop() || t('源碼引用'))}</strong><small>${escapeHTML(ref.relative_path?.split('/').pop() || '')} · L${Number(ref.start_line)}–${Number(ref.end_line)}</small></span>${icon('chevron')}</button>`).join(''):t('<div class="answer-empty"><p>分析結論的引用<br>將在這裡顯示。</p></div>')}</div><div class="code-header"><span>${icon('document')} ${escapeHTML(span?.relative_path?.split('/').pop() || t('尚未選擇證據'))}</span><span>COBOL</span></div><div class="code-body" aria-label="源碼原文">${codeMarkup(span)}</div><div class="evidence-location"><div>${icon('folder')}<span>${escapeHTML(span?.relative_path || t('從引用清單選擇'))}</span></div><div>${icon('layers')}<span>${demo?t('介面示例'):ui`快照 ${(state.project?.snapshot_id || '').replace('sha256:','').slice(0,12) || t('未建立')}`}</span></div>${span?`<span class="badge ${demo?'preview':span.integrity==='VALID'?'positive':'warning'}">${icon(demo?'info':'check')}${demo?t('示例內容'):span.integrity==='VALID'?t('快照證據完整性有效'):t('完整性未確認')}${span.span_truncated?t(' · 已截斷'):''}</span>`:''}</div><div class="evidence-scope"><b>讓結論可被覆核</b><br>${demo?t('選擇本機源碼後，此面板會展示實際文件、行號與證據。'):t('引用有效只表示證據可核對，不代表業務含義或所有執行路徑已獲證明。')}</div></aside>`;
}
function renderWorkbench(){
  if(state.mode==='real' && state.busy)return ui`<section class="answer-card"><div class="loading" role="status"><div class="spinner"></div><h2>${state.jobKind==='question'?t('正在調查這次的業務問題'):t('正在接入你的源碼')}</h2><p>請保持此頁開啟，完成後會自動更新。<br>你可以稍後回到分析記錄查看結果。</p><span class="job-phase">${state.jobKind==='question'?t('更新索引 → 檢查接口 → 調查源碼'):t('讀取源碼 → 建立索引 → 核對範圍')}</span></div></section>`;
  if(state.mode==='real'&&!diagnosis())return ui`<section class="empty-panel"><div class="empty-icon">${icon('folder')}</div><div class="eyebrow">YOUR CODE. YOUR CONTEXT.</div><h2>從自己的源碼，開始。</h2><p>接入本機 COBOL 程式與 COPYBOOK，確認分析範圍，<br>再用業務語言提出問題。</p><div class="empty-actions"><button class="button primary" data-connect>${icon('plus')}接入本機源碼</button><button class="button secondary" data-demo>先查看介面示例</button></div></section>`;
  return `<div class="workspace-grid"><div class="primary-column">${questionCard()}<section class="answer-card">${answerTabs()}<div class="answer-content" role="tabpanel">${state.tab==='relations'?relationsView():state.tab==='trace'?traceView():state.mode==='demo'?previewAnswer():actualAnswer()}</div></section></div>${evidencePanel()}</div>`;
}
function sourceRows(){return currentPrograms().filter(p=>(p.program_name+' '+p.relative_path).toLowerCase().includes(state.filter.toLowerCase())).map(p=>ui`<tr><td>${escapeHTML(p.program_name)}</td><td>${escapeHTML(p.relative_path)}</td><td>L${Number(p.start_line)}</td><td><span class="badge ${state.mode==='demo'?'preview':'neutral'}">${state.mode==='demo'?t('介面示例'):t('已識別定義')}</span></td><td><button data-program="${escapeHTML(p.program_name)}">分析程式 ${icon('arrow')}</button></td></tr>`).join('');}
function dependencyGaps(){const gaps=diagnosis()?.unresolved_dependencies || [];if(!gaps.length)return '';return ui`<section class="data-card dependency-gaps"><div class="data-card-heading"><h2>待確認的程式與 COPY 依賴</h2><span class="badge warning">${formatNumber(gaps.length)} 項</span></div><div class="table-wrapper"><table><thead><tr><th>目標名稱</th><th>關係</th><th>來源檔案</th></tr></thead><tbody>${gaps.slice(0,20).map(g=>`<tr><td>${escapeHTML(g.target_name || t('動態目標'))}</td><td>${escapeHTML(g.relation_type)}</td><td>${escapeHTML(g.relative_path)}</td></tr>`).join('')}</tbody></table></div><div class="table-count">${gaps.length>20?t('目前顯示前 20 項；完整清單保留在匯出 JSON。'):t('請補齊對應源碼；動態目標可能仍需設定資料確認。')}</div></section>`;}
function renderSources(){
  if(state.mode==='real'&&!diagnosis())return renderWorkbench();
  const demo=state.mode==='demo';const d=diagnosis();const files=d?.build_report?.files;const programs=currentPrograms();
  return ui`<div class="source-stats"><div class="source-stat"><span>實際讀取檔案</span><strong>${demo?'8':formatNumber(files?.decoded)}</strong><small>${demo?t('介面展示數據'):ui`${formatNumber(files?.candidate)} 個候選檔案`}</small></div><div class="source-stat"><span>程式定義</span><strong>${formatNumber(programs.length)}</strong><small>依 PROGRAM-ID 識別</small></div><div class="source-stat"><span>COPYBOOK 定義</span><strong>${demo?'2':formatNumber(d?.build_report?.diagnostics?.copybook_count)}</strong><small>${demo?t('介面展示數據'):ui`${formatNumber(files?.unreadable_or_binary)} 個候選檔案未能讀取`}</small></div></div><section class="data-card"><div class="data-card-heading"><h2>程式目錄</h2><input id="program-filter" aria-label="搜尋程式名稱或路徑" placeholder="搜尋程式名稱或路徑" value="${escapeHTML(state.filter)}"></div><div class="table-wrapper"><table><thead><tr><th>PROGRAM-ID</th><th>來源路徑</th><th>定義行</th><th>識別狀態</th><th></th></tr></thead><tbody id="program-rows">${sourceRows()}</tbody></table></div><div class="table-count">${formatNumber(programs.length)} 個程式定義 · ${demo?t('合成示例'):t('目前源碼快照')}</div></section>${!demo?ui`<div class="source-note"><strong>源碼位置</strong><br>${escapeHTML(state.project.source)}<br><strong>本次快照</strong><br>${escapeHTML(state.project.snapshot_id || t('未建立有效快照'))}</div>${renderBoundaries(d?.messages || [])}`:''}`;
}
function renderHistory(){
  if(state.mode==='demo')return ui`<section class="data-card"><div class="data-card-heading"><h2>介面示例記錄</h2><span class="badge preview">非實際分析</span></div><div class="history-card">${icon('document')}<div><h3>${preview.question}</h3><p>預先編排的介面內容 · 3 段示例引用</p></div><button data-demo-result>查看示例 ${icon('arrow')}</button></div></section>`;
  return ui`<section class="data-card"><div class="data-card-heading"><h2>本次工作階段</h2><span class="badge neutral">${state.history.length} 項</span></div>${state.history.length?state.history.map((item,i)=>ui`<div class="history-card">${icon('document')}<div><h3>${escapeHTML(item.diagnosis?.question || t('源碼接入'))}</h3><p>${escapeHTML(item.source?.split(/[\\/]/).pop())} · ${escapeHTML(statusLabel(item.agent?.agent_result?.status || item.diagnosis?.question_status))} · ${escapeHTML(item.diagnosis?.generated_at_utc || '')}</p></div><button data-history="${i}">查看摘要</button></div>`).join(''):t('<div class="empty-panel"><h2>尚未有分析記錄</h2><p>本次工作階段完成的分析會列在這裡；完整結果同時保存在你指定的資料夾。</p></div>')}</section>`;
}
function render(){renderChrome();$('page-content').innerHTML=state.page==='sources'?renderSources()+dependencyGaps():state.page==='history'?renderHistory():renderWorkbench();decorate();}

function apiErrorText(error){
  const code=error?.code || 'REQUEST_FAILED';
  if(code==='INVALID_PATH')return t('請使用完整絕對路徑：源碼目錄須存在；輸出須獨立且為空、新目錄或只含分析產物。兩者不可相互嵌套。');
  if(code==='JOB_ACTIVE')return t('目前已有分析正在執行，請等候完成。');
  if(code==='INVALID_SESSION')return t('工作階段已失效，請重新整理頁面。');
  if(['INVALID_ENCODING','INVALID_SOURCE_FORMAT','INVALID_EXTENSIONS'].includes(code))return t('請檢查文字編碼、源碼格式與副檔名設定。');
  if(['NO_CURRENT_INDEX','INDEX_CHANGED','INDEX_UNAVAILABLE','EVIDENCE_NOT_FOUND','EVIDENCE_INTEGRITY_ERROR','INVALID_EVIDENCE_ID'].includes(code))return t('請重新接入源碼，再選取當次快照的引用。');
  if(['INVALID_OPTIONS','REQUEST_TOO_LARGE','INVALID_JSON','INVALID_QUERY'].includes(code))return t('請檢查輸入欄位及請求大小。');
  return t('本機服務暫時無法完成操作。')+' ('+code+')';
}
async function api(path,options={}){
  const response=await fetch(path,{...options,headers:{...(state.token?{'X-Session-Token':state.token}:{}),...(options.body?{'Content-Type':'application/json'}:{}),...options.headers},cache:'no-store'});
  let value;try{value=await response.json();}catch{throw new Error(t('本機服務未回傳有效資料。請重新啟動服務。'));}
  if(!response.ok)throw new Error(apiErrorText(value.error));
  return value;
}
function clearEvidence(){state.selectedEvidence=null;state.evidence=null;state.evidenceTicket++;}
function applyProject(project){state.project=project;clearEvidence();const d=project?.diagnosis;state.entry=d?.selected_entry?.program_name || d?.entry_requested || (d?.question?'':project?.programs?.[0]?.program_name) || '';state.question=d?.question || '';}
async function initialize(){
  try{const data=await api('/api/state');state.connected=true;state.token=data.session_token;state.apiConfigured=Boolean(data.api_configured);
    if(data.project?.diagnosis){applyProject(data.project);state.mode='real';state.history=[data.project];}
    if(data.active_job_id){state.mode='real';state.busy=true;state.jobId=data.active_job_id;pollJob(data.active_job_id);}
  }catch{state.connected=false;}
  render();if(state.mode==='real')loadFirstEvidence();
}
function setMode(mode){
  state.mode=mode;state.error='';state.filter='';state.tab='answer';clearEvidence();
  if(mode==='demo'){state.question=preview.question;state.entry='PREMIUM-CALC';state.selectedEvidence='preview-01';state.evidence=preview.evidence[0];}
  else{state.question=state.project?.diagnosis?.question || '';state.entry=state.project?.diagnosis?.selected_entry?.program_name || state.project?.diagnosis?.entry_requested || (state.project?.diagnosis?.question?'':state.project?.programs?.[0]?.program_name) || '';}
  render();if(mode==='real')loadFirstEvidence();
}
function openSource(){
  $('source-error').hidden=true;
  if(state.project){$('source-path').value=state.project.source || '';$('output-path').value=state.project.output || '';const opts=state.project.diagnosis?.source_options || {};$('source-encoding').value=opts.encoding || 'auto';$('source-format').value=opts.source_format || 'auto';$('source-extensions').value=(opts.extensions || []).join(',');}
  $('index-submit').disabled=!state.connected || state.busy;
  if(!state.connected){$('source-error').textContent=t('請先以 python poc/web_app.py 啟動本機服務，再透過服務地址開啟此頁。');$('source-error').hidden=false;}
  $('source-dialog').showModal();
}
async function startJob(options){
  state.error='';state.mode='real';state.page='workbench';state.busy=true;state.jobKind=options.question?'question':'index';state.tab='answer';clearEvidence();
  state.project={source:options.source,output:options.output,diagnosis:null,programs:[],agent:null,relations:{edges:[]}};render();
  try{const response=await api('/api/analyze',{method:'POST',body:JSON.stringify(options)});state.jobId=response.job_id;await pollJob(response.job_id);}
  catch(error){state.busy=false;state.error=error.message;render();}
}
async function pollJob(jobId){
  try{const job=await api(`/api/jobs/${encodeURIComponent(jobId)}`);if(state.jobId!==jobId)return;
    if(job.status==='RUNNING'){setTimeout(()=>pollJob(jobId),800);return;}
    state.busy=false;state.jobId=null;
    if(job.result){if(state.mode==='real')applyProject(job.result);else state.project=job.result;if(job.result.diagnosis)state.history.unshift(job.result);}
    if(job.status==='FAILED')state.error=apiErrorText(job.error);
    if(state.mode==='real' && state.jobKind==='index' && sourceUsable())state.page='sources';
    render();if(state.mode==='real')loadFirstEvidence();
  }catch(error){state.busy=false;state.jobId=null;state.error=error.message;render();}
}
async function loadFirstEvidence(){const first=currentRefs()[0];if(first&&!state.evidence&&!state.busy)await loadEvidence(first.evidence_id);}
async function loadEvidence(id){
  state.selectedEvidence=id;const ticket=++state.evidenceTicket;const runId=state.project?.run_id;
  if(state.mode==='demo'){state.evidence=preview.evidence.find(s=>s.evidence_id===id) || null;render();return;}
  state.evidence=null;render();
  try{const data=await api(`/api/evidence?id=${encodeURIComponent(id)}`);if(ticket!==state.evidenceTicket || state.mode!=='real' || runId!==state.project?.run_id)return;
    if(data.run_id && runId && data.run_id!==runId)throw new Error(t('源碼快照已更新，請重新選擇本次引用。'));
    const span=data.spans?.find(s=>s.evidence_id===id);if(!span || span.integrity!=='VALID')throw new Error(t('這段證據未通過快照完整性核對，暫不展示原文。'));
    state.evidence=span;render();
  }catch(error){if(ticket===state.evidenceTicket){toast(error.message);render();}}
}
function markdownSummary(project=state.project){
  if(state.mode==='demo' && project===state.project)return ui`# COBOL Lens · 介面示例\n\n資料模式：預先編排的合成示例，未執行模型分析。\n\n## ${preview.question}\n\n分期保費 = 年繳保費 × 繳費係數。\n\n本內容用於展示回答、關係與引用的介面，不代表公司業務結果。\n\n## 示例引用\n\n${preview.evidence.map(r=>`- ${r.relative_path}:${r.start_line}–${r.end_line}`).join('\n')}\n\n## 待確認\n\n實際係數、折扣適用條件與完整異常路徑需額外資料核對。\n`;
  const d=project?.diagnosis;const answer=project?.agent?.agent_result;
  const entry=d?.selected_entry;const refs=answer?.evidence_refs || answer?.verified_evidence_refs || [];
  return ui`# COBOL Lens · 本機源碼分析\n\n資料模式：本機源碼\n匯出時間： ${new Date().toISOString()}\n來源： ${project?.source || ''}\n調查起點： ${entry?.program_name || d?.entry_requested || t('從程式目錄探索')}${entry?.relative_path?' · '+entry.relative_path:''}\n快照： ${project?.snapshot_id || t('未建立')}\n接入狀態： ${d?.runner_status || ''}\n問答狀態： ${answer?.status || d?.question_status || ''}\n\n## 業務問題\n\n${d?.question || t('尚未提出問題')}\n\n${answer?.answer || t('本次沒有生成業務答案。')}\n\n## 本次引用索引\n\n${refs.length?refs.map(r=>`- ${r.relative_path}:${r.start_line}–${r.end_line} · ${r.evidence_id}`).join('\n'):t('本次沒有回答引用。')}\n\n## 接入診斷\n\n${(d?.messages || []).map(m=>'- '+m).join('\n')}\n\n問題相關性與完整性尚未核驗。完整診斷與分析邊界可一併匯出為 JSON。\n`;
}
function download(content,type,name){const blob=new Blob([content],{type});const url=URL.createObjectURL(blob);const link=document.createElement('a');link.href=url;link.download=name;link.hidden=true;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);}

document.addEventListener('click',event=>{
  const target=event.target.closest('button,a');if(!target)return;
  if(target.dataset.page){state.page=target.dataset.page;render();}
  if(target.dataset.tab){state.tab=target.dataset.tab;render();}
  if(target.dataset.evidence)loadEvidence(target.dataset.evidence);
  if(target.dataset.question){if(state.mode==='demo'){toast(t('這是介面示例。請接入本機源碼，才能調查自己的問題。'));return;}state.question=target.dataset.question;render();$('question-input')?.focus();}
  if(target.dataset.program){state.entry=target.dataset.program;state.question=state.mode==='demo'?preview.question:ui`請解釋 ${target.dataset.program} 的主要處理步驟、輸入輸出及呼叫。`;state.page='workbench';state.tab='answer';render();}
  if(target.hasAttribute('data-connect'))openSource();
  if(target.hasAttribute('data-demo')){state.page='workbench';setMode('demo');}
  if(target.hasAttribute('data-demo-result')){state.page='workbench';state.tab='answer';render();}
  if(target.hasAttribute('data-history')){const item=state.history[Number(target.dataset.history)];download(markdownSummary(item),'text/markdown;charset=utf-8','source-analysis-history.md');toast(t('已匯出該次摘要；目前源碼範圍保持不變。'));}
  if(target.classList.contains('close-dialog'))target.closest('dialog').close();
});
document.addEventListener('input',event=>{
  if(event.target.id==='question-input'){state.question=event.target.value;if(state.mode==='real'){const count=document.querySelector('.tab-count');if(count)count.textContent=draftChanged()?0:result()?.tool_trace?.length || 0;if(state.tab!=='relations'){const content=document.querySelector('.answer-content');if(content)content.innerHTML=state.tab==='trace'?traceView():actualAnswer();const panel=document.querySelector('.evidence-panel');if(panel)panel.outerHTML=evidencePanel();}}}
  if(event.target.id==='program-filter'){state.filter=event.target.value;$('program-rows').innerHTML=sourceRows();}
});
document.addEventListener('change',event=>{if(event.target.id==='entry-select'){state.entry=event.target.value;render();}});
document.addEventListener('submit',async event=>{
  if(event.target.id==='question-form'){
    event.preventDefault();if(state.busy)return;
    if(state.mode==='demo'){if(state.question!==preview.question || state.entry!=='PREMIUM-CALC'){toast(t('示例只展示預先編排的保費問題。接入本機源碼後，可分析自己的程式。'));return;}state.tab='answer';render();toast(t('目前為介面示例，未發起模型請求。'));return;}
    if(!state.question.trim()){toast(t('請先輸入一個業務問題。'));return;}
    if(!sourceUsable()){toast(t('請先成功接入本機源碼。'));return;}
    if(!state.apiConfigured){showSettings();return;}
    const p=state.project;const opts=p.diagnosis.source_options || {};
    await startJob({source:p.source,output:p.output,encoding:opts.encoding || 'auto',source_format:opts.source_format || 'auto',extensions:opts.extensions?.join(',') || null,entry:state.entry || null,question:state.question.trim(),allow_network:true});
  }
});
function pastedPath(value){const text=value.trim();return text.startsWith('"')&&text.endsWith('"')?text.slice(1,-1):text;}
$('source-form').addEventListener('submit',async event=>{event.preventDefault();if(!state.connected || state.busy)return;const options={source:pastedPath($('source-path').value),output:pastedPath($('output-path').value),encoding:$('source-encoding').value,source_format:$('source-format').value,extensions:$('source-extensions').value.trim() || null,allow_network:false};$('source-dialog').close();await startJob(options);});
$('connect-button').addEventListener('click',openSource);
$('demo-mode').addEventListener('click',()=>setMode('demo'));
$('real-mode').addEventListener('click',()=>setMode('real'));
$('recent-analysis').addEventListener('click',()=>{state.page='workbench';render();});
function showSettings(){$('settings-description').textContent=state.apiConfigured?t('接口設定已提供，發起問答時會進行實際能力探測。'):t('源碼索引可離線使用。模型問答需要啟動服務時提供接口設定。');$('settings-local').textContent=state.connected?t('已啟動'):t('尚未啟動');$('settings-api').textContent=state.apiConfigured?t('已提供 · 待實際驗證'):t('尚未提供');$('settings-dialog').showModal();}
$('settings-button').addEventListener('click',showSettings);
$('connection-status').addEventListener('click',showSettings);
$('export-button').addEventListener('click',()=>$('export-dialog').showModal());
$('export-markdown').addEventListener('click',()=>{download(markdownSummary(),'text/markdown;charset=utf-8',state.mode==='demo'?'interface-preview-summary.md':'source-analysis-summary.md');$('export-dialog').close();toast(t('已匯出摘要，包含資料模式與分析邊界。'));});
$('export-json').addEventListener('click',()=>{download(JSON.stringify(state.mode==='demo'?{mode:'illustrative_preview',model_called:false,...preview}:{mode:'local_source',...state.project},null,2),'application/json;charset=utf-8',state.mode==='demo'?'interface-preview.json':'source-analysis.json');$('export-dialog').close();toast(t('已匯出本次資料。'));});
$('language-select').addEventListener('change',event=>{
  const previousPreviewQuestion=preview.question;
  if(!selectInterfaceLocale(event.target.value))return;
  preview.question=t('分期保費是如何計算出來的？');
  [t('分期保費計算'),t('年繳保費組成'),t('繳費係數來源')].forEach((title,i)=>{preview.evidence[i].title=title;});
  if(state.mode==='demo' && state.question===previousPreviewQuestion)state.question=preview.question;
  render();
  if($('settings-dialog').open)showSettings();
  $('toast').hidden=true;
});
render();initialize();
