'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const state = {token:null, data:null, modelConfigured:false, submitting:false, poll:null, updateWorkspace:null, refreshGeneration:0, pollFailures:0, refreshError:false};
  const messages = {
    INVALID_SESSION:'本机会话已失效，请刷新页面。',
    ONLINE_BUSY:'当前任务仍在运行，请先停止或等待完成。',
    ONLINE_NOT_CONNECTED:'请先更新当前资料的读取授权。',
    ONLINE_CREDENTIALS_REQUIRED:'请更新当前资料的读取授权。',
    ONLINE_RECONNECT_REQUIRED:'当前运行尚无读取授权，请先点击“更新授权”。',
    ONLINE_SOURCE_TRANSFER_REQUIRED:'请勾选本次分析所需的模型传输授权。',
    SOURCE_UNAUTHORIZED:'源码授权已失效，请更新读取授权。',
    SOURCE_FORBIDDEN:'当前账号没有读取该源码版本的权限。',
    SOURCE_NOT_FOUND:'未能取得该固定版本的源码，请核对仓库和版本保留情况。',
    SOURCE_RATE_LIMIT:'源码服务正在限流，请稍后再继续。',
    MATERIAL_UNAUTHORIZED:'分析资料授权已失效，请更新读取授权。',
    MATERIAL_FORBIDDEN:'当前账号没有读取分析资料的权限。',
    MATERIAL_NOT_FOUND:'未找到指定的分析资料，请检查发布是否完成。',
    MATERIAL_COMMIT_MISMATCH:'源码提交与分析资料不一致，未切换版本。',
    MATERIAL_REVISION_MISMATCH:'分析资料修订不一致，未采用该资料。',
    MATERIAL_HASH_MISMATCH:'分析资料校验失败，请重新发布完整版本。',
    MATERIAL_QUERY_BUDGET_EXCEEDED:'本轮读取范围达到上限，请缩小问题范围或继续已有调查。',
    ONLINE_INVALID_REPOSITORY:'请填写 Bitbucket Cloud 仓库链接或 workspace/repository。',
    ONLINE_INVALID_COMMIT:'请填写完整的固定提交哈希。',
    REQUEST_TOO_LARGE:'提交内容超过大小上限，请缩短问题或输入内容。',
    ONLINE_MATERIAL_REAUTH_REQUIRED:'资料位置已改变，请明确填写新位置的读取授权。',
    ONLINE_RELEASE_REQUIRED:'请先读取并选择已发布的分析版本。',
    ONLINE_CONNECTION_FAILED:'未能连接服务。请确认当前网络可以访问公司仓库及资料位置。',
    ONLINE_RETRY_CONFIRMATION_REQUIRED:'上次模型请求结果未知，请确认后再重新调用。',
  };

  function repositoryIdentity(value) {
    let text = String(value || '').trim();
    if (text.startsWith('https://')) {
      let url; try { url = new URL(text); } catch { throw new Error('ONLINE_INVALID_REPOSITORY'); }
      if (url.hostname !== 'bitbucket.org' || url.username || url.password || url.port || url.search || url.hash) throw new Error('ONLINE_INVALID_REPOSITORY');
      const parts = url.pathname.split('/').filter(Boolean);
      if (parts.length !== 2) throw new Error('ONLINE_INVALID_REPOSITORY');
      text = parts.join('/');
    }
    text = text.replace(/\.git$/, '');
    if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}\/[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$/.test(text)) throw new Error('ONLINE_INVALID_REPOSITORY');
    return text;
  }

  function errorText(error) {
    const code = typeof error === 'string' ? error : error?.message || 'ONLINE_CONNECTION_FAILED';
    if (messages[code]) return messages[code];
    if (/^[A-Z][A-Z0-9_]{1,100}$/.test(code)) return `操作未完成（${code}）。已有版本和历史会保留。`;
    return messages.ONLINE_CONNECTION_FAILED;
  }
  function showError(error, id='error') { if (id === 'error') state.refreshError = false; $(id).textContent = errorText(error); $(id).hidden = false; }
  function clearError(id='error') { if (id === 'error') state.refreshError = false; $(id).hidden = true; $(id).textContent = ''; }
  function currentWorkspace() { return state.data?.workspaces?.find(item => item.id === state.data.current_workspace_id); }
  function activeJob() { return state.data?.active_job; }
  function running(job=activeJob()) { return !!job && ['running','queued','cancelling'].includes(String(job.status).toLowerCase()); }
  function busy() { return state.submitting || running(); }
  function conversation() { return state.data?.conversation; }
  function statusText(status) { return ({answered:'已回答',needs_continuation:'仍有待查项',cancelled:'已停止',failed:'未完成',retry_confirmation_required:'请求结果待确认',interrupted:'已中断',running:'调查中',completed:'已完成',ready:'已就绪',pending:'等待处理'})[String(status || '').toLowerCase()] || '已记录'; }
  function sourceBasisText(basis) { return basis === 'sync_attested' ? '同步方已声明完成；分析固定在所选提交。' : '按所选源码提交分析；上游周同步完成情况未确认。'; }

  async function api(path, body, method=body === undefined ? 'GET' : 'POST') {
    const headers = {'X-Session-Token':state.token || ''};
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const encoded = body === undefined ? undefined : JSON.stringify(body);
    if (encoded !== undefined && new Blob([encoded]).size > 32768) throw new Error('REQUEST_TOO_LARGE');
    let response, data;
    try {
      response = await fetch(path.startsWith('/api/') ? path : '/api/online' + path, {method, headers, body:encoded, cache:'no-store', credentials:'same-origin'});
      data = await response.json();
    } catch { throw new Error('ONLINE_CONNECTION_FAILED'); }
    if (!response.ok || data?.error) throw new Error(typeof data?.error === 'string' ? data.error : data?.error?.code || 'ONLINE_OPERATION_FAILED');
    return data;
  }

  function render() {
    const workspace = currentWorkspace(), selected = conversation(), isBusy = busy();
    $('source-title').textContent = workspace?.label || workspace?.repository?.identity || '从业务问题开始';
    $('source-version').textContent = workspace ? `${workspace.repository?.identity || ''} · 提交 ${String(workspace.repository?.commit || '').slice(0,12)} · 资料 ${String(workspace.revision || '').slice(0,12)}${workspace.connected ? '' : ' · 需重新授权后继续调查'}` : '接入已发布的在线分析资料，查找流程、规则与源码依据。';
    $('source-basis').textContent = workspace ? sourceBasisText(workspace.source_basis) : '';
    $('source-basis').hidden = !workspace;
    const coverage = workspace?.coverage || {};
    $('coverage').hidden = !workspace;
    $('coverage-title').textContent = `资料覆盖范围 · 已纳入 ${Number(coverage.indexed_files || 0).toLocaleString()} 个文件${coverage.input_skip_count ? ` · 跳过 ${coverage.input_skip_count} 个文件` : ''}`;
    $('coverage-summary').textContent = `纳入源码约 ${(Number(coverage.source_bytes || 0)/1048576).toFixed(2)} MiB。${coverage.input_coverage_complete === false ? '存在未成功读取的源码；' : ''}以下范围只说明预计算资料，不能代表本次问题已读完整仓库。`;
    const boundaryNames = {REPOSITORY_COMPLETENESS_NOT_VERIFIED:'全仓完整性尚未独立验证',SPARSE_SEMANTICS_ONLY:'静态事实不能替代完整语义或运行证明',RULE_OCCURRENCE_DETAILS_NOT_EXPORTED:'部分语句出现位置未展开',UNRESOLVED_DEPENDENCY:'仍有未确定的调用或 COPY 关系',SOURCE_SCOPE_BOUNDARY:'源码范围存在待确认项'};
    $('coverage-boundaries').innerHTML = (coverage.boundaries || []).slice(0,12).map(item=>`<li>${escape(boundaryNames[item.code] || item.code || '待确认项')}${item.count != null ? `（${escape(item.count)}）` : ''}${item.relative_path ? `：${escape(item.relative_path)}` : ''}</li>`).join('');
    $('coverage-skips').innerHTML = (coverage.input_skips || []).slice(0,16).map(item=>`<li>未纳入：${escape(item.relative_path)}${item.reason_code ? `（${escape(item.reason_code)}）` : ''}</li>`).join('') + (coverage.input_skips_truncated || Number(coverage.input_skip_count || 0)>16 ? '<li>这里只显示部分跳过记录；完整范围保留在所选资料中。</li>' : '');
    $('workspace-list').innerHTML = (state.data?.workspaces || []).map(item => `<button class="item ${item.id === workspace?.id ? 'selected' : ''}" data-workspace="${escape(item.id)}" ${isBusy ? 'disabled' : ''}><strong>${escape(item.label || item.repository?.identity)}</strong><small>${escape(String(item.repository?.commit || '').slice(0,12))} · ${item.connected ? '已授权' : '历史可查看'}</small></button>`).join('') || '<p class="muted small">尚未接入在线资料</p>';
    $('conversation-list').innerHTML = (state.data?.conversations || []).filter(item => !workspace || item.workspace_id === workspace.id).map(item => `<button class="item ${item.id === selected?.id ? 'selected' : ''}" data-conversation="${escape(item.id)}" data-workspace="${escape(item.workspace_id)}" ${isBusy ? 'disabled' : ''}><strong>${escape(item.title || '新对话')}</strong><small>${escape(statusText(item.status))}</small></button>`).join('') || '<p class="muted small">分析记录将在这里保留</p>';
    for (const id of ['update','credentials','disconnect','new-conversation']) $(id).disabled = !workspace || isBusy;
    $('connect').disabled = isBusy;
    $('send').disabled = !workspace?.connected || !state.modelConfigured || isBusy || !$('allow-source-transfer').checked;
    for (const id of ['export-markdown','export-json','delete-conversation']) $(id).disabled = !selected || isBusy;
    $('model-status').textContent = state.modelConfigured ? '使用现有公司模型配置；只发送本次调查选取的资料。' : '公司模型尚未配置，请返回原工作台设置模型接口，再刷新本页。';
    $('connection-status').textContent = state.token ? '本机服务已连接' : '本机服务未连接';
    const job = activeJob();
    $('progress').hidden = !running(job);
    if (running(job)) {
      $('progress-title').textContent = job.kind === 'connect' || job.kind === 'update' ? '正在核对分析版本' : '正在调查业务问题';
      const progress = job.progress || {};
      $('progress-detail').textContent = typeof progress === 'string' ? progress : [progress.message || progress.phase || '正在读取资料', progress.completed != null ? `${progress.completed}${progress.total != null ? '/' + progress.total : ''}` : ''].filter(Boolean).join(' · ');
      $('cancel').disabled = state.submitting;
    }
    const list = selected?.messages || [];
    if (list.length) {
      $('messages').innerHTML = list.map(message => {
        const isUser = message.role === 'user', citations = Array.isArray(message.citations) ? message.citations : [];
        const text = message.content ?? message.answer ?? '';
        const html = isUser ? escape(text) : (typeof answerMarkdown !== 'undefined' ? answerMarkdown.render(text) : `<p>${escape(text)}</p>`);
        return `<article class="message ${isUser ? 'user' : 'assistant'}"><div class="message-label"><span>${isUser ? '业务问题' : '业务分析'}</span><span>${escape(statusText(message.status))}</span></div><div class="answer">${html || '<p class="muted">本次调查还没有形成回答。</p>'}</div>${citations.length ? `<div class="citations">${citations.map(item => { const value=item.citation || item; return `<button data-citation="${escape(item.key || item.id || item.citation_id || value.id || '')}">${escape(value.path || value.relative_path || '源码依据')} · ${escape(value.start ?? value.start_line ?? '')}–${escape(value.end ?? value.end_line ?? '')}</button>`; }).join('')}</div>` : ''}${message.coverage ? '<p class="coverage">结论以本次已核对资料为依据；未解析关系与未读取范围仍需确认。</p>' : ''}</article>`;
      }).join('');
    } else {
      $('messages').innerHTML = '<div class="empty"><div class="empty-mark">⌕</div><h2>开始一次业务调查</h2><p>当前对话固定在所选源码版本。可以描述业务目标，也可以从已知程序或字段开始。</p></div>';
    }
    const last = [...list].reverse().find(item => item.role === 'assistant');
    const status = String(selected?.status || last?.status || '').toLowerCase();
    const resumable = ['needs_continuation','cancelled','interrupted','failed','retry_confirmation_required'].includes(status) && !!selected && !isBusy;
    $('continuation').hidden = !resumable;
    const unknown = status === 'retry_confirmation_required' || !!selected?.pending?.retry_confirmation_required;
    $('resume').hidden = unknown;
    $('retry').hidden = !unknown;
    $('resume').disabled = $('retry').disabled = !workspace?.connected || !$('allow-source-transfer').checked;
    $('continuation-description').textContent = unknown ? '上次模型请求结果未知。重新调用可能再次消耗额度，请明确确认。' : '已保留本次问题、已核对依据与待查项，可以从检查点继续。';
  }

  function pauseRefresh() {
    clearTimeout(state.poll);
    state.poll = null;
    return ++state.refreshGeneration;
  }
  function scheduleRefresh(delay) {
    clearTimeout(state.poll);
    state.poll = setTimeout(() => { state.poll = null; refresh(); }, delay);
  }
  async function refresh() {
    const generation = pauseRefresh();
    try {
      const data = await api('/state');
      if (generation !== state.refreshGeneration) return;
      state.data = data;
      state.pollFailures = 0;
      if (state.refreshError) clearError();
      render();
      if (running()) scheduleRefresh(800);
      else if (activeJob()?.error) showError(typeof activeJob().error === 'string' ? activeJob().error : activeJob().error.code);
    } catch (error) {
      if (generation !== state.refreshGeneration) return;
      showError(error);
      state.refreshError = true;
      state.pollFailures = Math.min(state.pollFailures + 1, 5);
      // Only a read is retried. A lost mutation/model response is never replayed.
      if (error?.message !== 'INVALID_SESSION') scheduleRefresh(Math.min(800 * 2 ** (state.pollFailures - 1), 8000));
    }
  }
  async function perform(action) {
    if (state.submitting) return;
    state.submitting = true; pauseRefresh(); clearError(); render();
    try { await action(); } catch (error) { showError(error); }
    finally { await refresh(); state.submitting = false; render(); }
  }
  function clearCredentials(prefix='') {
    for (const name of ['material-token','source-token','material-username','source-username']) $(prefix + name).value = '';
  }
  function connectCredentials() {
    const credentials = {};
    for (const name of ['material-token','material-username','source-token','source-username']) {
      const value = $(name).value.trim(); if (value) credentials[name.replace('-', '_')] = value;
    }
    if ($('same-credentials').checked) {
      if (credentials.material_token) credentials.source_token = credentials.material_token;
      if (credentials.material_username) credentials.source_username = credentials.material_username;
    }
    return credentials;
  }
  function showLocation() {
    const downloads = $('location-kind').value === 'downloads';
    $('downloads-fields').hidden = !downloads; $('https-fields').hidden = downloads;
    $('release-name').required = downloads; $('material-url').required = !downloads; $('source-commit').required = !downloads;
  }
  function openConnect(update=false) {
    const workspace = currentWorkspace(); state.updateWorkspace = update ? workspace?.id : null;
    $('connect-form').reset(); clearCredentials(); clearError('connect-error');
    $('release-name').innerHTML = '<option value="">请先读取版本列表</option>';
    $('release-status').textContent = ''; $('source-credentials').hidden = true;
    $('connect-title').textContent = update ? '选择新的分析版本' : '接入在线分析资料';
    if (update && workspace) $('source-repository').value = workspace.repository.identity;
    showLocation(); $('connect-dialog').showModal();
  }
  async function loadReleases() {
    clearError('connect-error'); $('load-releases').disabled = true;
    try {
      const repository = repositoryIdentity($('material-repository').value || $('source-repository').value);
      const [workspace, name] = repository.split('/');
      const result = await api('/releases', {workspace, repository:name, credentials:connectCredentials()});
      const releases = result.release_items || result.release_names || result.releases || [];
      $('release-name').innerHTML = '<option value="">选择一个已发布版本</option>' + releases.map(item => { const name = typeof item === 'string' ? item : item.name; const label = typeof item === 'string' ? name : [item.created_on?.slice(0,16)?.replace('T',' '),item.label || name].filter(Boolean).join(' · '); return `<option value="${escape(name)}">${escape(label)}</option>`; }).join('');
      $('release-status').textContent = releases.length ? `找到 ${releases.length} 个版本，请明确选择` : '尚无已发布版本，请先完成公司环境的资料发布';
    } catch (error) { showError(error,'connect-error'); }
    finally { $('load-releases').disabled = false; }
  }
  async function inspectRelease() {
    if (!$('release-name').value) return;
    const selected = $('release-name').value;
    try {
      const [workspace,repository] = repositoryIdentity($('material-repository').value || $('source-repository').value).split('/');
      const result = await api('/release',{workspace,repository,release_name:selected,credentials:connectCredentials()});
      if ($('release-name').value !== selected) return;
      const release = result.release;
      $('release-status').textContent = `${release.repository.identity} · 源码提交 ${release.repository.commit.slice(0,12)} · ${sourceBasisText(release.source_basis)}`;
    } catch (error) { showError(error,'connect-error'); }
  }
  async function submitConnect(event) {
    event.preventDefault(); clearError('connect-error'); $('connect-submit').disabled = true;
    try {
      const repository = repositoryIdentity($('source-repository').value), credentials = connectCredentials();
      let location, commit, revision;
      if ($('location-kind').value === 'downloads') {
        const [workspace,name] = repositoryIdentity($('material-repository').value || repository).split('/');
        if (!$('release-name').value) throw new Error('ONLINE_RELEASE_REQUIRED');
        location = {kind:'downloads',workspace,repository:name,release_name:$('release-name').value};
      } else {
        commit = $('source-commit').value.trim(); revision = $('material-revision').value.trim() || undefined;
        if (!/^(?:[a-f0-9]{40}|[a-f0-9]{64})$/.test(commit)) throw new Error('ONLINE_INVALID_COMMIT');
        location = {kind:'https',base_url:$('material-url').value.trim()};
      }
      const payload = {repository,location,commit,revision,credentials};
      if (state.updateWorkspace) payload.workspace_id = state.updateWorkspace;
      clearCredentials();
      await api(state.updateWorkspace ? '/update' : '/connect',payload);
      $('connect-dialog').close(); await refresh();
    } catch (error) { showError(error,'connect-error'); }
    finally { $('connect-submit').disabled = false; }
  }
  async function submitQuestion(event) {
    event.preventDefault();
    if (!$('allow-source-transfer').checked || busy()) return;
    const question = $('question').value.trim(); if (!question) return;
    await perform(async () => {
      if (!conversation()) { await api('/conversations',{workspace_id:state.data.current_workspace_id}); state.data = await api('/state'); }
      await api('/question',{conversation_id:state.data.current_conversation_id,question,allow_source_transfer:true});
      $('question').value = '';
    });
  }
  async function exportConversation(format) {
    await perform(async () => {
      const result = await api(`/export?conversation_id=${encodeURIComponent(state.data.current_conversation_id)}&format=${format}`);
      const blob = new Blob([typeof result.content === 'string' ? result.content : JSON.stringify(result.content,null,2)],{type:result.mime_type || 'text/plain;charset=utf-8'});
      const url = URL.createObjectURL(blob), link = document.createElement('a');
      link.href = url; link.download = result.filename || `online-analysis.${format === 'json' ? 'json' : 'md'}`; link.click(); URL.revokeObjectURL(url);
    });
  }
  async function openEvidence(id) {
    if (!id || !conversation()) return;
    await perform(async () => {
      const result = await api(`/evidence?conversation_id=${encodeURIComponent(state.data.current_conversation_id)}&citation_id=${encodeURIComponent(id)}`);
      const citation = result.citation || result;
      $('evidence-title').textContent = `${citation.path || '源码依据'} · ${citation.start || ''}–${citation.end || ''}`;
      $('evidence-version').textContent = `提交 ${citation.commit || ''} · 资料 ${citation.revision || ''}${result.truncated ? ' · 当前片段已截断' : ''}`;
      $('evidence-text').textContent = result.text || ''; $('evidence-dialog').showModal();
    });
  }

  // Only pure helpers are public; credentials and session state stay in closure.
  globalThis.OnlineUI = {repositoryIdentity,errorText,escape};
  if (typeof document === 'undefined') return;
  $('connect').addEventListener('click',()=>openConnect());
  $('update').addEventListener('click',()=>openConnect(true));
  $('connect-form').addEventListener('submit',submitConnect);
  $('load-releases').addEventListener('click',loadReleases);
  $('release-name').addEventListener('change',inspectRelease);
  $('location-kind').addEventListener('change',showLocation);
  $('same-credentials').addEventListener('change',()=>{$('source-credentials').hidden=$('same-credentials').checked;});
  $('allow-source-transfer').addEventListener('change',render);
  $('question-form').addEventListener('submit',submitQuestion);
  $('new-conversation').addEventListener('click',()=>perform(()=>api('/conversations',{workspace_id:state.data.current_workspace_id})));
  $('workspace-list').addEventListener('click',event=>{const button=event.target.closest('[data-workspace]');if(button&&!busy())perform(()=>api('/open',{workspace_id:button.dataset.workspace}));});
  $('conversation-list').addEventListener('click',event=>{const button=event.target.closest('[data-conversation]');if(button&&!busy())perform(()=>api('/open',{workspace_id:button.dataset.workspace,conversation_id:button.dataset.conversation}));});
  $('messages').addEventListener('click',event=>{const button=event.target.closest('[data-citation]');if(button)openEvidence(button.dataset.citation);});
  $('cancel').addEventListener('click',()=>perform(()=>api('/cancel',{job_id:activeJob().job_id})));
  for (const id of ['resume','retry']) $(id).addEventListener('click',()=>{if($('allow-source-transfer').checked)perform(()=>api('/resume',{conversation_id:state.data.current_conversation_id,allow_source_transfer:true,approve_retry:id==='retry'}));});
  $('disconnect').addEventListener('click',()=>perform(()=>api('/disconnect',{workspace_id:state.data.current_workspace_id})));
  $('credentials').addEventListener('click',()=>{clearCredentials('auth-');clearError('credentials-error');$('credentials-dialog').showModal();});
  $('credentials-form').addEventListener('submit',async event=>{event.preventDefault();const credentials={};for(const name of ['material-token','source-token','material-username','source-username']){const value=$('auth-'+name).value.trim();if(value)credentials[name.replace('-','_')]=value;}clearCredentials('auth-');try{await api('/credentials',{workspace_id:state.data.current_workspace_id,credentials});$('credentials-dialog').close();await refresh();}catch(error){showError(error,'credentials-error');}});
  $('export-markdown').addEventListener('click',()=>exportConversation('markdown'));
  $('export-json').addEventListener('click',()=>exportConversation('json'));
  $('delete-conversation').addEventListener('click',()=>{if(confirm('删除当前在线对话及其记录的引用？'))perform(()=>api('/conversations/'+state.data.current_conversation_id,undefined,'DELETE'));});
  document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>button.closest('dialog').close()));
  $('connect-dialog').addEventListener('close',()=>clearCredentials());
  $('credentials-dialog').addEventListener('close',()=>clearCredentials('auth-'));
  (async()=>{try{const bootstrap=await api('/api/state');state.token=bootstrap.session_token;state.modelConfigured=!!bootstrap.api_configured;await refresh();}catch(error){showError(error);}})();
})();
