'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  // Only authored interface strings enter this catalog. Values and model/source text do not.
  const LOCALES = ['zh-CN','zh-HK','en'];
  const LOCALE_KEY = 'cobol-lens.locale';
  let locale = 'zh-CN';
  try { const saved = localStorage.getItem(LOCALE_KEY); if (LOCALES.includes(saved)) locale = saved; } catch {}
  const translations = new Map([
    ["业务公式清单", "業務公式清單", "Business formula inventory"],
    ["加载公式清单", "載入公式清單", "Load formula inventory"],
    ["展开完整业务说明", "展開完整業務說明", "Show the complete business explanation"],
    ["收起完整业务说明", "收起完整業務說明", "Collapse the complete business explanation"],
    ["当前显示说明预览，完整内容已保存。", "目前顯示說明預覽，完整內容已保存。", "This is an explanation preview. The complete text is saved."],
    ["已记录内容完整保存；分页只控制本页显示。", "已記錄內容完整保存；分頁只控制本頁顯示。", "All recorded results are saved. Pagination only controls this view."],
    ["仍有范围待核对", "仍有範圍待核對", "Some scope still needs review"],
    ["查看源码依据", "查看源碼依據", "View source evidence"],
    ["当前显示", "目前顯示", "Showing"],
    ["公式清单分页", "公式清單分頁", "Formula inventory pages"],
    ["首页", "首頁", "First page"],
    ["上一页", "上一頁", "Previous page"],
    ["下一页", "下一頁", "Next page"],
    ["末页", "末頁", "Last page"],
    ["导出完整分析清单", "匯出完整分析清單", "Export the complete analysis inventory"],
    ["清单版本不一致，请重新读取对话。", "清單版本不一致，請重新讀取對話。", "The inventory version differs. Reload the conversation."],
    ["在线源码业务分析", "在線源碼業務分析", "Online source business analysis"],
    ["源码业务分析", "源碼業務分析", "Source business analysis"],
    ["跳至提问", "跳至提問", "Skip to question"],
    ["返回本地工作台", "返回本地工作台", "Back to local workbench"],
    ["界面语言", "介面語言", "Interface language"],
    ["语言", "語言", "Language"],
    ["在线资料与历史", "在線資料與歷史", "Online materials and history"],
    ["在线资料", "在線資料", "Online materials"],
    ["接入", "接入", "Connect"],
    ["按需读取分析资料与源码，引用固定在所选版本。", "按需讀取分析資料與源碼，引用固定在所選版本。", "Read analysis materials and source as needed; citations remain tied to the selected version."],
    ["＋ 新对话", "＋ 新對話", "＋ New conversation"],
    ["新对话", "新對話", "New conversation"],
    ["最近对话", "最近對話", "Recent conversations"],
    ["正在连接本机服务…", "正在連線本機服務…", "Connecting to the local service…"],
    ["在线资料、对话和授权独立管理。", "在線資料、對話和授權獨立管理。", "Online materials, conversations and authorization are managed separately."],
    ["从业务问题开始", "從業務問題開始", "Start with a business question"],
    ["接入已发布的在线分析资料，查找流程、规则与源码依据。", "接入已發佈的在線分析資料，查找流程、規則與源碼依據。", "Connect published online analysis materials to investigate processes, rules and source evidence."],
    ["选择新版本", "選擇新版本", "Choose a new version"],
    ["更新授权", "更新授權", "Update authorization"],
    ["结束授权", "結束授權", "End authorization"],
    ["资料覆盖范围", "資料覆蓋範圍", "Material coverage"],
    ["正在分析", "正在分析", "Analyzing"],
    ["停止", "停止", "Stop"],
    ["业务分析对话", "業務分析對話", "Business analysis conversation"],
    ["让每个结论有源码依据", "讓每個結論有源碼依據", "Ground each conclusion in source evidence"],
    ["可以询问一项业务如何处理、条件如何判断，或变更会影响哪些程序。系统会记录已核对的范围和待确认事项。", "可以詢問一項業務如何處理、條件如何判斷，或變更會影響哪些程式。系統會記錄已核對的範圍和待確認事項。", "Ask how a business process works, how conditions are evaluated or which programs a change affects. The system records reviewed scope and open questions."],
    ["继续调查", "繼續調查", "Continue investigation"],
    ["确认重新调用模型", "確認重新呼叫模型", "Confirm another model request"],
    ["导出 Markdown", "匯出 Markdown", "Export Markdown"],
    ["导出 JSON", "匯出 JSON", "Export JSON"],
    ["删除对话", "刪除對話", "Delete conversation"],
    ["业务问题", "業務問題", "Business question"],
    ["例如：从接收请求到完成处理，这项业务经过哪些步骤？异常时会怎样？", "例如：從接收請求到完成處理，這項業務經過哪些步驟？異常時會怎樣？", "For example: what happens from receiving the request through completion, and how are exceptions handled?"],
    ["允许将本次分析所需片段发送到已配置的公司模型", "允許將本次分析所需片段傳送到已設定的公司模型", "Allow the source excerpts needed for this investigation to be sent to the configured company model"],
    ["开始分析", "開始分析", "Start analysis"],
    ["接入在线分析资料", "接入在線分析資料", "Connect online analysis materials"],
    ["关闭", "關閉", "Close"],
    ["使用公司网络中已发布的分析版本。接入只读取清单，提问后才读取相关资料与源码。", "使用公司網絡中已發佈的分析版本。接入只讀取清單，提問後才讀取相關資料與源碼。", "Use an analysis version published on the company network. Connecting reads its manifest; relevant materials and source are read after a question is submitted."],
    ["源码仓库", "源碼倉庫", "Source repository"],
    ["分析资料位置", "分析資料位置", "Analysis material location"],
    ["获准的 HTTPS 资料地址", "獲准的 HTTPS 資料位址", "Approved HTTPS material address"],
    ["资料所在仓库（留空沿用源码仓库）", "資料所在倉庫（留空沿用源碼倉庫）", "Material repository (leave blank to use the source repository)"],
    ["读取已发布版本", "讀取已發佈版本", "Load published versions"],
    ["分析版本", "分析版本", "Analysis version"],
    ["请先读取版本列表", "請先讀取版本列表", "Load the version list first"],
    ["资料地址", "資料位址", "Material address"],
    ["完整提交版本", "完整提交版本", "Full commit version"],
    ["40 位完整提交哈希", "40 位完整提交雜湊", "Full 40-character commit hash"],
    ["资料修订（可选，填写后严格核对）", "資料修訂（可選，填寫後嚴格核對）", "Material revision (optional; verified exactly when supplied)"],
    ["读取授权", "讀取授權", "Read authorization"],
    ["凭据仅留在本次运行内存中。退出服务后需重新授权，已有对话仍可查看。", "憑據只保留在本次執行記憶體中。退出服務後需重新授權，已有對話仍可查看。", "Credentials remain in memory for this service session. Authorize again after restarting; existing conversations remain available."],
    ["资料读取账号邮箱（使用 API token 时填写）", "資料讀取帳號電郵（使用 API token 時填寫）", "Material account email (required when using an API token)"],
    ["资料读取令牌", "資料讀取權杖", "Material read token"],
    ["源码读取沿用同一授权", "源碼讀取沿用同一授權", "Use the same authorization to read source"],
    ["源码读取账号邮箱", "源碼讀取帳號電郵", "Source account email"],
    ["源码读取令牌", "源碼讀取權杖", "Source read token"],
    ["取消", "取消", "Cancel"],
    ["接入所选版本", "接入所選版本", "Connect selected version"],
    ["更新当前资料的读取授权", "更新目前資料的讀取授權", "Update read authorization for these materials"],
    ["授权更新不会切换源码版本。使用两个不同账号时，请分别填写。", "授權更新不會切換源碼版本。使用兩個不同帳號時，請分別填寫。", "Updating authorization keeps the selected source version. Enter each account separately when using different accounts."],
    ["资料账号邮箱", "資料帳號電郵", "Material account email"],
    ["资料令牌", "資料權杖", "Material token"],
    ["源码账号邮箱", "源碼帳號電郵", "Source account email"],
    ["源码令牌", "源碼權杖", "Source token"],
    ["源码依据", "源碼依據", "Source evidence"],
    ["本机会话已失效，请刷新页面。", "本機工作階段已失效，請重新整理頁面。", "The local session expired. Refresh the page."],
    ["当前任务仍在运行，请先停止或等待完成。", "目前工作仍在執行，請先停止或等待完成。", "The current task is still running. Stop it or wait for completion."],
    ["请先更新当前资料的读取授权。", "請先更新目前資料的讀取授權。", "Update read authorization for the current materials first."],
    ["请更新当前资料的读取授权。", "請更新目前資料的讀取授權。", "Update read authorization for the current materials."],
    ["当前运行尚无读取授权，请先点击“更新授权”。", "目前執行尚無讀取授權，請先點擊「更新授權」。", "This session has no read authorization. Select “Update authorization” first."],
    ["请勾选本次分析所需的模型传输授权。", "請勾選本次分析所需的模型傳輸授權。", "Select the model transfer consent for this investigation."],
    ["源码授权已失效，请更新读取授权。", "源碼授權已失效，請更新讀取授權。", "Source authorization expired. Update read authorization."],
    ["当前账号没有读取该源码版本的权限。", "目前帳號沒有讀取該源碼版本的權限。", "The current account cannot read this source version."],
    ["未能取得该固定版本的源码，请核对仓库和版本保留情况。", "未能取得該固定版本的源碼，請核對倉庫和版本保留情況。", "The fixed source version could not be retrieved. Check the repository and version retention."],
    ["源码服务正在限流，请稍后再继续。", "源碼服務正在限流，請稍後再繼續。", "The source service is rate limited. Continue later."],
    ["分析资料授权已失效，请更新读取授权。", "分析資料授權已失效，請更新讀取授權。", "Analysis material authorization expired. Update read authorization."],
    ["当前账号没有读取分析资料的权限。", "目前帳號沒有讀取分析資料的權限。", "The current account cannot read the analysis materials."],
    ["未找到指定的分析资料，请检查发布是否完成。", "未找到指定的分析資料，請檢查發佈是否完成。", "The analysis materials were not found. Check whether publication completed."],
    ["源码提交与分析资料不一致，未切换版本。", "源碼提交與分析資料不一致，未切換版本。", "The source commit does not match the materials. The version was not changed."],
    ["分析资料修订不一致，未采用该资料。", "分析資料修訂不一致，未採用該資料。", "The material revision does not match. These materials were not used."],
    ["分析资料校验失败，请重新发布完整版本。", "分析資料驗證失敗，請重新發佈完整版本。", "Material verification failed. Publish a complete version again."],
    ["本轮读取范围达到上限，请缩小问题范围或继续已有调查。", "本輪讀取範圍達到上限，請縮小問題範圍或繼續已有調查。", "This round reached its reading limit. Narrow the question or continue the existing investigation."],
    ["请填写 Bitbucket Cloud 仓库链接或 workspace/repository。", "請填寫 Bitbucket Cloud 倉庫連結或 workspace/repository。", "Enter a Bitbucket Cloud repository URL or workspace/repository."],
    ["请填写完整的固定提交哈希。", "請填寫完整的固定提交雜湊。", "Enter the full fixed commit hash."],
    ["提交内容超过大小上限，请缩短问题或输入内容。", "提交內容超過大小上限，請縮短問題或輸入內容。", "The submission exceeds the size limit. Shorten the question or input."],
    ["资料位置已改变，请明确填写新位置的读取授权。", "資料位置已改變，請明確填寫新位置的讀取授權。", "The material location changed. Supply read authorization for the new location."],
    ["请先读取并选择已发布的分析版本。", "請先讀取並選擇已發佈的分析版本。", "Load and select a published analysis version first."],
    ["未能连接服务。请确认当前网络可以访问公司仓库及资料位置。", "未能連線服務。請確認目前網絡可以存取公司倉庫及資料位置。", "Could not connect. Check access to the company repository and material location."],
    ["上次模型请求结果未知，请确认后再重新调用。", "上次模型請求結果未知，請確認後再重新呼叫。", "The previous model request outcome is unknown. Confirm before requesting again."],
    ["操作未完成（{code}）。已有版本和历史会保留。", "操作未完成（{code}）。已有版本和歷史會保留。", "The operation did not complete ({code}). Existing versions and history are retained."],
    ["已回答", "已回答", "Answered"],
    ["仍有待查项", "仍有待查項", "Open investigation items"],
    ["已停止", "已停止", "Stopped"],
    ["未完成", "未完成", "Incomplete"],
    ["请求结果待确认", "請求結果待確認", "Request outcome unconfirmed"],
    ["已中断", "已中斷", "Interrupted"],
    ["调查中", "調查中", "Investigating"],
    ["已完成", "已完成", "Completed"],
    ["已就绪", "已就緒", "Ready"],
    ["等待处理", "等待處理", "Pending"],
    ["已记录", "已記錄", "Recorded"],
    ["同步方已声明完成；分析固定在所选提交。", "同步方已聲明完成；分析固定在所選提交。", "The sync owner declared completion; analysis is fixed to the selected commit."],
    ["按所选源码提交分析；上游周同步完成情况未确认。", "按所選源碼提交分析；上游每週同步完成情況未確認。", "Analysis uses the selected source commit; completion of the upstream weekly sync is unconfirmed."],
    ["提交", "提交", "Commit"],
    ["资料", "資料", "Materials"],
    ["需重新授权后继续调查", "需重新授權後繼續調查", "Authorize again to continue"],
    ["资料覆盖范围 · 已纳入 {count} 个文件", "資料覆蓋範圍 · 已納入 {count} 個檔案", "Material coverage · {count} files included"],
    ["跳过 {count} 个文件", "略過 {count} 個檔案", "{count} files skipped"],
    ["纳入源码约 {size} MiB。", "納入源碼約 {size} MiB。", "Included source: approximately {size} MiB. "],
    ["存在未成功读取的源码；", "存在未成功讀取的源碼；", "Some source files could not be read. "],
    ["以下范围只说明预计算资料，不能代表本次问题已读完整仓库。", "以下範圍只說明預先計算資料，不能代表本次問題已讀完整倉庫。", "This scope describes precomputed materials, not how much of the repository this question has read."],
    ["全仓完整性尚未独立验证", "全庫完整性尚未獨立驗證", "Repository completeness has not been independently verified"],
    ["静态事实不能替代完整语义或运行证明", "靜態事實不能替代完整語義或執行證明", "Static facts do not establish complete semantics or runtime behavior"],
    ["部分语句出现位置未展开", "部分語句出現位置未展開", "Some statement occurrences are not expanded"],
    ["仍有未确定的调用或 COPY 关系", "仍有未確定的呼叫或 COPY 關係", "Some call or COPY relationships are unresolved"],
    ["源码范围存在待确认项", "源碼範圍存在待確認項", "The source scope has unresolved boundaries"],
    ["待确认项", "待確認項", "Unconfirmed item"],
    ["未纳入", "未納入", "Not included"],
    ["这里只显示部分跳过记录；完整范围保留在所选资料中。", "這裡只顯示部分略過記錄；完整範圍保留在所選資料中。", "Only some skipped entries are shown here; the complete scope remains in the selected materials."],
    ["已授权", "已授權", "Authorized"],
    ["历史可查看", "歷史可查看", "History available"],
    ["尚未接入在线资料", "尚未接入在線資料", "No online materials connected"],
    ["分析记录将在这里保留", "分析記錄將在這裡保留", "Analysis history will appear here"],
    ["使用现有公司模型配置；只发送本次调查选取的资料。", "使用現有公司模型設定；只傳送本次調查選取的資料。", "Uses the configured company model and sends only materials selected for this investigation."],
    ["公司模型尚未配置，请返回原工作台设置模型接口，再刷新本页。", "公司模型尚未設定，請返回原工作台設定模型接口，再重新整理本頁。", "Configure the company model in the local workbench, then refresh this page."],
    ["本机服务已连接", "本機服務已連線", "Local service connected"],
    ["本机服务未连接", "本機服務未連線", "Local service disconnected"],
    ["正在核对分析版本", "正在核對分析版本", "Checking the analysis version"],
    ["正在调查业务问题", "正在調查業務問題", "Investigating the business question"],
    ["正在读取资料", "正在讀取資料", "Reading materials"],
    ["正在请求模型", "正在請求模型", "Requesting the model"],
    ["已收到模型响应", "已收到模型回應", "Model response received"],
    ["模型请求未完成", "模型請求未完成", "Model request did not complete"],
    ["已应用模型响应", "已套用模型回應", "Model response applied"],
    ["已完成源码补查", "已完成源碼補查", "Source investigation step completed"],
    ["回答已准备好", "回答已準備好", "Answer ready"],
    ["业务分析", "業務分析", "Business analysis"],
    ["本次调查还没有形成回答。", "本次調查還沒有形成回答。", "This investigation has not produced an answer yet."],
    ["结论以本次已核对资料为依据；未解析关系与未读取范围仍需确认。", "結論以本次已核對資料為依據；未解析關係與未讀取範圍仍需確認。", "Conclusions use the reviewed materials; unresolved relationships and unread scope still need verification."],
    ["开始一次业务调查", "開始一次業務調查", "Start a business investigation"],
    ["当前对话固定在所选源码版本。可以描述业务目标，也可以从已知程序或字段开始。", "目前對話固定在所選源碼版本。可以描述業務目標，也可以從已知程式或欄位開始。", "This conversation is fixed to the selected source version. Describe a business goal or start from a known program or field."],
    ["上次模型请求结果未知。重新调用可能再次消耗额度，请明确确认。", "上次模型請求結果未知。重新呼叫可能再次消耗額度，請明確確認。", "The previous model request outcome is unknown. Another request may consume allowance again; explicit confirmation is required."],
    ["已保留本次问题、已核对依据与待查项，可以从检查点继续。", "已保留本次問題、已核對依據與待查項，可以從檢查點繼續。", "The question, reviewed evidence and open items are saved. Continue from the checkpoint."],
    ["选择新的分析版本", "選擇新的分析版本", "Choose a new analysis version"],
    ["选择一个已发布版本", "選擇一個已發佈版本", "Select a published version"],
    ["找到 {count} 个版本，请明确选择", "找到 {count} 個版本，請明確選擇", "Found {count} versions; select one explicitly"],
    ["尚无已发布版本，请先完成公司环境的资料发布", "尚無已發佈版本，請先完成公司環境的資料發佈", "No published versions are available. Publish the materials in the company environment first."],
    ["源码提交", "源碼提交", "Source commit"],
    ["当前片段已截断", "目前片段已截斷", "This excerpt is truncated"],
    ["删除当前在线对话及其记录的引用？", "刪除目前在線對話及其記錄的引用？", "Delete this online conversation and its recorded citations?"],
    ["单次请求正文超过限制。", "單次請求正文超過限制。", "The request body exceeds the size limit."],
    ["缩小本次供应的源码或资料范围后再试。", "縮小本次供應的源碼或資料範圍後再試。", "Reduce the source code or reference material supplied in this request, then try again."],
    ["本次请求超过模型上下文限制。", "本次請求超過模型上下文限制。", "This request exceeds the model's context limit."],
    ["减少历史消息和供应资料，分段分析后再试。", "減少歷史消息和供應資料，分段分析後再試。", "Reduce the conversation history and supplied material, then analyze it in smaller parts."],
    ["所选模型不可用或当前账号无法访问。", "所選模型不可用或目前帳號無法訪問。", "The selected model is unavailable or inaccessible to this account."],
    ["检查模型配置和账号可访问的模型。", "檢查模型配置和帳號可訪問的模型。", "Check the model configuration and the models available to the account."],
    ["当前接口明确不支持所请求的功能或参数。", "目前接口明確不支持所請求的功能或參數。", "The API explicitly does not support the requested feature or parameter."],
    ["使用接口支持的功能或调整请求参数。", "使用接口支持的功能或調整請求參數。", "Use a supported feature or adjust the request parameters."],
    ["单次输出额度或输出参数超过限制。", "單次輸出額度或輸出參數超過限制。", "The requested output allowance or output parameter exceeds the limit."],
    ["降低单次最大输出额度后再试。", "降低單次最大輸出額度後再試。", "Reduce the maximum output allowance for this request, then try again."],
    ["账号配额或累计使用额度不足。", "帳號配額或累計使用額度不足。", "The account has insufficient quota or remaining usage allowance."],
    ["检查账号配额或账单状态，恢复额度后再试。", "檢查帳號配額或帳單狀態，恢復額度後再試。", "Check the account quota or billing status, then try again after the allowance is restored."],
    ["接口明确报告请求速率受限。", "接口明確報告請求速率受限。", "The API explicitly reported a request rate limit."],
    ["等待限流窗口结束后再手动重试。", "等待限流窗口結束後再手動重試。", "Wait for the rate limit window to end before retrying manually."],
    ["接口认证失败。", "接口認證失敗。", "API authentication failed."],
    ["检查 API 密钥及认证配置。", "檢查 API 密鑰及認證配置。", "Check the API key and authentication configuration."],
    ["接口拒绝当前账号的访问权限。", "接口拒絕目前帳號的訪問權限。", "The API denied access to the current account."],
    ["检查账号权限和资源访问授权。", "檢查帳號權限和資源訪問授權。", "Check the account permissions and resource access authorization."],
    ["请求超过等待时间。", "請求超過等待時間。", "The request timed out."],
    ["检查网络和服务状态，再手动重试。", "檢查網絡和服務狀態，再手動重試。", "Check the network and service status before retrying manually."],
    ["本机仍有未结束的模型连接，本次请求未发送。", "本機仍有未結束的模型連線，本次請求未發送。", "A local model connection is still active; this request was not sent."],
    ["等待连接结束后再手动重试。", "等待連線結束後再手動重試。", "Wait for the connection to finish before retrying manually."],
    ["本次调查已达到时间预算，已停止继续补查。", "本次調查已達到時間預算，已停止繼續補查。", "This investigation reached its time budget and further checks have stopped."],
    ["查看已保存的证据和待查事项；远程请求结果未知时，先确认请求状态，再决定是否重试。", "查看已保存的證據和待查事項；遠端請求結果未知時，先確認請求狀態，再決定是否重試。", "Review the saved evidence and pending checks. If a remote request's result is unknown, confirm its status before deciding whether to retry."],
    ["接口的隐藏推理输出超过本次请求的内容额度。", "接口的隱藏推理輸出超過本次請求的內容額度。", "The API's hidden reasoning exceeded this request's content allowance."],
    ["已保留收到的正文和源码证据；先核对远端请求状态，再调整该接口支持的推理强度，避免重复收费。", "已保留收到的正文和源碼證據；先核對遠端請求狀態，再調整該接口支援的推理強度，避免重複收費。", "Received answer text and source evidence were preserved. Confirm the remote request status before adjusting the API's supported reasoning effort or retrying."],
    ["未能连接到接口。", "未能連接到接口。", "The API could not be reached."],
    ["检查网络、接口地址和服务连通性。", "檢查網絡、接口地址和服務連通性。", "Check the network, API address, and service connectivity."],
    ["接口返回 HTTP 429，但未提供可确认的原因。", "接口返回 HTTP 429，但未提供可確認的原因。", "The API returned HTTP 429 without a confirmed cause."],
    ["检查账号额度和服务限流说明，再决定是否重试。", "檢查帳號額度和服務限流說明，再決定是否重試。", "Check the account allowance and service rate limit information before deciding whether to retry."],
    ["接口返回服务端错误，具体原因尚未确认。", "接口返回服務端錯誤，具體原因尚未確認。", "The API returned a server error; the specific cause is unconfirmed."],
    ["凭关联编号检查服务端日志和服务状态。", "憑關聯編號檢查服務端日誌和服務狀態。", "Use the correlation ID to check server logs and service status."],
    ["接口拒绝本次请求，具体原因尚未确认。", "接口拒絕本次請求，具體原因尚未確認。", "The API rejected this request; the specific cause is unconfirmed."],
    ["凭关联编号检查接口支持的请求格式和服务日志。", "憑關聯編號檢查接口支持的請求格式和服務日誌。", "Use the correlation ID to check supported request formats and service logs."],
    ["接口返回的响应格式无效。", "接口返回的響應格式無效。", "The API returned an invalid response format."],
    ["检查接口兼容性和服务端响应日志。", "檢查接口兼容性和服務端響應日誌。", "Check API compatibility and server response logs."],
    ["接口返回的流式回答格式无法解析。", "接口返回的流式回答格式無法解析。", "The API's streamed answer could not be parsed."],
    ["检查接口是否支持标准流式回答，并凭关联编号核对服务端记录。", "檢查接口是否支持標準流式回答，並憑關聯編號核對服務端記錄。", "Check whether the API supports the standard streaming format and use the correlation ID to review server records."],
    ["接口连接已结束，但回答尚未完整传回。", "接口連線已結束，但回答尚未完整傳回。", "The API connection ended before the full answer was received."],
    ["检查连接和服务端记录；已显示内容仍是草稿，确认请求状态后再决定是否重试。", "檢查連線和服務端記錄；已顯示內容仍是草稿，確認請求狀態後再決定是否重試。", "Check the connection and server records. The displayed content remains a draft; confirm the request status before deciding whether to retry."],
    ["本机实时回答处理未能正确初始化，本次模型请求未发送。", "本機即時回答處理未能正確初始化，本次模型請求未發送。", "Local live answer processing could not initialize; this model request was not sent."],
    ["重新启动工作台；若仍失败，凭关联编号检查应用日志。", "重新啟動工作台；若仍失敗，憑關聯編號檢查應用日誌。", "Restart the workbench. If the failure persists, use the correlation ID to check application logs."],
    ["本机处理实时回答时发生错误。", "本機處理即時回答時發生錯誤。", "An error occurred while processing the live answer locally."],
    ["凭关联编号检查应用日志；远程请求可能仍在完成，确认请求状态后再决定是否重试。", "憑關聯編號檢查應用日誌；遠端請求可能仍在完成，確認請求狀態後再決定是否重試。", "Use the correlation ID to check application logs. The remote request may still be completing; confirm its status before deciding whether to retry."],
    ["请求处理发生系统错误，具体原因尚未确认。", "請求處理發生系統錯誤，具體原因尚未確認。", "A system error occurred while processing the request; the specific cause is unconfirmed."],
    ["凭关联编号检查应用和服务端日志。", "憑關聯編號檢查應用和服務端日誌。", "Use the correlation ID to check application and server logs."],
    ["本机存储空间不足。", "本機儲存空間不足。", "Local storage is full."],
    ["释放结果目录与临时目录磁盘空间后重试。", "釋放結果目錄與暫存目錄磁碟空間後重試。", "Free disk space in the results and temporary directories, then try again."],
    ["无法写入本机分析文件。", "無法寫入本機分析文件。", "Local analysis files could not be written."],
    ["检查结果目录和工作台配置目录的写权限。", "檢查結果目錄和工作台設定目錄的寫入權限。", "Check write permissions for the results and workbench configuration directories."],
    ["本机数据库或文件被占用。", "本機資料庫或文件被佔用。", "A local database or file is in use."],
    ["停止其它使用同一结果目录的工作台后重试。", "停止其他使用同一結果目錄的工作台後重試。", "Stop other workbench instances using the same results directory, then try again."],
    ["本机分析数据库损坏或格式无效。", "本機分析資料庫損壞或格式無效。", "The local analysis database is corrupt or has an invalid format."],
    ["保留原结果目录并用新的独立结果目录重新建立索引。", "保留原結果目錄並用新的獨立結果目錄重新建立索引。", "Preserve the original results directory and rebuild the index in a new, separate results directory."],
    ["本机分析报告无法读取或格式无效。", "本機分析報告無法讀取或格式無效。", "The local analysis report could not be read or has an invalid format."],
    ["保留原文件，检查导入详情并重新生成分析产物。", "保留原文件，檢查匯入詳情並重新生成分析產物。", "Preserve the original file, check the import details, and regenerate the analysis output."],
    ["本次导入的程序清单与索引快照不一致。", "本次匯入的程式清單與索引快照不一致。", "The imported program list does not match the index snapshot."],
    ["保留原资料库并重新执行源码更新。", "保留原資料庫並重新執行源碼更新。", "Preserve the original library and update the source code again."],
    ["已有索引与当前解析器或编译配置不兼容。", "已有索引與目前解析器或編譯設定不相容。", "The existing index is incompatible with the current parser or compilation settings."],
    ["使用“更新源码”完成索引更新后再提问。", "使用「更新源碼」完成索引更新後再提問。", "Complete an index update using Update source before asking another question."],
    ["本次导入遇到无法按所选编码读取的文件。", "本次匯入遇到無法按所選編碼讀取的文件。", "An imported file could not be read using the selected encoding."],
    ["核对导入进度显示的文件，确认它是文本源码，并选择正确编码后重试。", "核對匯入進度顯示的文件，確認它是文字源碼，並選擇正確編碼後重試。", "Check the file shown in the import progress, confirm it is text source code, and select the correct encoding before retrying."],
    ["导入期间源码发生了变化，无法建立一致索引。", "匯入期間源碼發生了變化，無法建立一致索引。", "Source code changed during import, preventing a consistent index."],
    ["暂停源码编辑或同步，待文件稳定后重新导入。", "暫停源碼編輯或同步，待文件穩定後重新匯入。", "Pause source editing or synchronization and import again after the files are stable."],
    ["所选目录没有符合设置的可用源码。", "所選目錄沒有符合設定的可用源碼。", "The selected directory has no usable source files matching the settings."],
    ["核对源码目录、扩展名和无扩展名文件设置。", "核對源碼目錄、副檔名和無副檔名文件設定。", "Check the source directory, file extensions, and settings for files without an extension."],
    ["当前 Python 的 SQLite 不支持全文索引。", "目前 Python 的 SQLite 不支援全文索引。", "This Python installation's SQLite does not support full-text indexing."],
    ["使用包含 FTS5 的 Python 3.10 或更高版本后重新启动工作台。", "使用包含 FTS5 的 Python 3.10 或更高版本後重新啟動工作台。", "Use Python 3.10 or later with FTS5 support, then restart the workbench."],
    ["找不到接口地址，请检查网络或地址。", "找不到接口地址，請檢查網路或地址。", "The API address could not be found. Check the network or address."],
    ["连接验证失败，请联系管理员检查证书。", "連線驗證失敗，請聯絡管理員檢查憑證。", "Connection verification failed. Contact an administrator to check the certificate."],
    ["安全连接的 TLS 通信失败，具体阶段尚未确认，请联系管理员。", "安全連線的 TLS 通訊失敗，具體階段尚未確認，請聯絡管理員。", "TLS communication failed on the secure connection; the failing phase is unconfirmed. Contact an administrator."],
    ["接口拒绝连接，请确认服务已启动。", "接口拒絕連線，請確認服務已啟動。", "The API refused the connection. Confirm that the service is running."],
    ["接口连接中断，请稍后重试。", "接口連線中斷，請稍後重試。", "The API connection was interrupted. Try again later."],
    ["无法连上接口网络，请检查网络连接。", "無法連上接口網路，請檢查網路連線。", "The API network could not be reached. Check the network connection."],
    ["接口响应超时，请稍后重试。", "接口回應逾時，請稍後重試。", "The API response timed out. Try again later."],
    ["本地关联编号", "本地關聯編號", "Local correlation ID"],
    ["上游关联编号", "上游關聯編號", "Upstream correlation ID"],
    ["错误分类", "錯誤分類", "Error category"],
    ["下一步", "下一步", "Next step"],
    ["业务调查", "業務調查", "BUSINESS INVESTIGATION"],
    ["正在准备调查", "正在準備調查", "Preparing the investigation"]
  ].map(row => [row[0], row]));
  function t(text, values={}) {
    const original = String(text ?? ''), key = original.trim(), row = translations.get(key);
    const translated = row ? original.replace(key, row[LOCALES.indexOf(locale)]) : original;
    return translated.replace(/\{([a-z]+)\}/g, (match, name) => Object.hasOwn(values,name) ? String(values[name]) : match);
  }
  const staticTexts = [], staticAttributes = [];
  function captureStaticLocale() {
    if (!document.createTreeWalker || !document.documentElement || typeof NodeFilter === 'undefined') return;
    const walker = document.createTreeWalker(document.documentElement, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (!node.parentElement?.closest('script, style, [data-language-names]') && translations.has(node.data.trim())) staticTexts.push([node,node.data]);
    }
    for (const node of document.querySelectorAll('[placeholder], [aria-label], [title]')) {
      for (const name of ['placeholder','aria-label','title']) {
        const value = node.getAttribute(name);
        if (value && translations.has(value.trim())) staticAttributes.push([node,name,value]);
      }
    }
  }
  function applyStaticLocale() {
    if (document.documentElement) document.documentElement.lang = locale;
    document.title = t('在线源码业务分析');
    for (const [node,text] of staticTexts) if (node.isConnected !== false) node.data = t(text);
    for (const [node,name,text] of staticAttributes) if (node.isConnected !== false) node.setAttribute(name,t(text));
    if ($('online-language')) $('online-language').value = locale;
  }
  function selectLocale(value, persist=true) {
    if (!LOCALES.includes(value)) return;
    locale = value;
    if (persist) try { localStorage.setItem(LOCALE_KEY,locale); } catch {}
    applyStaticLocale(); render(); renderConnectState(); renderEvidence();
    for (const [id,error] of Object.entries(state.errors)) renderError(error,id);
  }
  const state = {token:null, data:null, modelConfigured:false, submitting:false, poll:null, updateWorkspace:null, refreshGeneration:0, pollFailures:0, refreshError:false, errors:{}, releases:null, release:null, evidence:null};
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
    if (messages[code]) return t(messages[code]);
    if (/^[A-Z][A-Z0-9_]{1,100}$/.test(code)) return t('操作未完成（{code}）。已有版本和历史会保留。',{code});
    return t(messages.ONLINE_CONNECTION_FAILED);
  }
  const diagnosticMessages = {
    "request_too_large": ["单次请求正文超过限制。", "缩小本次供应的源码或资料范围后再试。"],
    "context_too_large": ["本次请求超过模型上下文限制。", "减少历史消息和供应资料，分段分析后再试。"],
    "model_unavailable": ["所选模型不可用或当前账号无法访问。", "检查模型配置和账号可访问的模型。"],
    "unsupported_feature": ["当前接口明确不支持所请求的功能或参数。", "使用接口支持的功能或调整请求参数。"],
    "output_limit": ["单次输出额度或输出参数超过限制。", "降低单次最大输出额度后再试。"],
    "quota_exhausted": ["账号配额或累计使用额度不足。", "检查账号配额或账单状态，恢复额度后再试。"],
    "rate_limit": ["接口明确报告请求速率受限。", "等待限流窗口结束后再手动重试。"],
    "authentication": ["接口认证失败。", "检查 API 密钥及认证配置。"],
    "permission": ["接口拒绝当前账号的访问权限。", "检查账号权限和资源访问授权。"],
    "timeout": ["请求超过等待时间。", "检查网络和服务状态，再手动重试。"],
    "transport_busy": ["本机仍有未结束的模型连接，本次请求未发送。", "等待连接结束后再手动重试。"],
    "question_time_budget": ["本次调查已达到时间预算，已停止继续补查。", "查看已保存的证据和待查事项；远程请求结果未知时，先确认请求状态，再决定是否重试。"],
    "reasoning_limit": ["接口的隐藏推理输出超过本次请求的内容额度。", "已保留收到的正文和源码证据；先核对远端请求状态，再调整该接口支持的推理强度，避免重复收费。"],
    "connection": ["未能连接到接口。", "检查网络、接口地址和服务连通性。"],
    "http_429_unknown": ["接口返回 HTTP 429，但未提供可确认的原因。", "检查账号额度和服务限流说明，再决定是否重试。"],
    "http_5xx_unknown": ["接口返回服务端错误，具体原因尚未确认。", "凭关联编号检查服务端日志和服务状态。"],
    "request_rejected": ["接口拒绝本次请求，具体原因尚未确认。", "凭关联编号检查接口支持的请求格式和服务日志。"],
    "invalid_response": ["接口返回的响应格式无效。", "检查接口兼容性和服务端响应日志。"],
    "invalid_stream": ["接口返回的流式回答格式无法解析。", "检查接口是否支持标准流式回答，并凭关联编号核对服务端记录。"],
    "stream_incomplete": ["接口连接已结束，但回答尚未完整传回。", "检查连接和服务端记录；已显示内容仍是草稿，确认请求状态后再决定是否重试。"],
    "stream_callback_invalid": ["本机实时回答处理未能正确初始化，本次模型请求未发送。", "重新启动工作台；若仍失败，凭关联编号检查应用日志。"],
    "stream_callback_failed": ["本机处理实时回答时发生错误。", "凭关联编号检查应用日志；远程请求可能仍在完成，确认请求状态后再决定是否重试。"],
    "system_error": ["请求处理发生系统错误，具体原因尚未确认。", "凭关联编号检查应用和服务端日志。"],
    "storage_full": ["本机存储空间不足。", "释放结果目录与临时目录磁盘空间后重试。"],
    "storage_permission": ["无法写入本机分析文件。", "检查结果目录和工作台配置目录的写权限。"],
    "storage_locked": ["本机数据库或文件被占用。", "停止其它使用同一结果目录的工作台后重试。"],
    "storage_corrupt": ["本机分析数据库损坏或格式无效。", "保留原结果目录并用新的独立结果目录重新建立索引。"],
    "report_invalid": ["本机分析报告无法读取或格式无效。", "保留原文件，检查导入详情并重新生成分析产物。"],
    "source_snapshot_mismatch": ["本次导入的程序清单与索引快照不一致。", "保留原资料库并重新执行源码更新。"],
    "index_update_required": ["已有索引与当前解析器或编译配置不兼容。", "使用“更新源码”完成索引更新后再提问。"],
    "source_encoding_invalid": ["本次导入遇到无法按所选编码读取的文件。", "核对导入进度显示的文件，确认它是文本源码，并选择正确编码后重试。"],
    "source_changed": ["导入期间源码发生了变化，无法建立一致索引。", "暂停源码编辑或同步，待文件稳定后重新导入。"],
    "source_empty": ["所选目录没有符合设置的可用源码。", "核对源码目录、扩展名和无扩展名文件设置。"],
    "fts_unavailable": ["当前 Python 的 SQLite 不支持全文索引。", "使用包含 FTS5 的 Python 3.10 或更高版本后重新启动工作台。"]
  };
  const transportMessages = {
    "dns_resolution_failed": "找不到接口地址，请检查网络或地址。",
    "tls_certificate_invalid": "连接验证失败，请联系管理员检查证书。",
    "tls_handshake_failed": "安全连接的 TLS 通信失败，具体阶段尚未确认，请联系管理员。",
    "connection_refused": "接口拒绝连接，请确认服务已启动。",
    "connection_reset": "接口连接中断，请稍后重试。",
    "network_unreachable": "无法连上接口网络，请检查网络连接。",
    "timeout": "接口响应超时，请稍后重试。"
  };
  const providerCodes = ["request_too_large", "payload_too_large", "context_length_exceeded", "context_window_exceeded", "model_not_found", "model_not_available", "model_unavailable", "unsupported_parameter", "unsupported_value", "unsupported_feature", "max_tokens_exceeded", "max_output_tokens_exceeded", "output_token_limit_exceeded", "insufficient_quota", "quota_exceeded", "billing_hard_limit_reached", "rate_limit_exceeded", "invalid_api_key", "authentication_error", "permission_denied", "access_denied", "internal_error", "server_error"];
  function safeDiagnostic(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value) || typeof value.category !== 'string' || typeof value.request_id !== 'string' || value.schema_version !== 'safe-api-error/v1' || !Object.hasOwn(diagnosticMessages,value.category) || !/^local-[a-f0-9]{32}$/.test(value.request_id || '')) return null;
    const safe = {category:value.category,request_id:value.request_id};
    if (Number.isInteger(value.http_status) && value.http_status >= 100 && value.http_status <= 599) safe.http_status = value.http_status;
    if (providerCodes.includes(value.provider_code)) safe.provider_code = value.provider_code;
    if (value.evidence_source === 'local' && ['connection','timeout'].includes(value.category) && Object.hasOwn(transportMessages,value.transport_reason) && (value.category !== 'timeout' || value.transport_reason === 'timeout')) safe.transport_reason = value.transport_reason;
    if (typeof value.upstream_request_id === 'string' && /^(?:(?:req|request)[_-][0-9a-fA-F]{8,64}|[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})$/.test(value.upstream_request_id) && ['header','body'].includes(value.upstream_request_id_source)) safe.upstream_request_id = value.upstream_request_id;
    return safe;
  }
  function diagnosticText(value) {
    const safe = safeDiagnostic(value); if (!safe) return '';
    const [reason,next] = diagnosticMessages[safe.category];
    return [t(safe.transport_reason ? transportMessages[safe.transport_reason] : reason), t('下一步') + ': ' + t(next),
      t('错误分类') + ': ' + safe.category, safe.http_status ? 'HTTP ' + safe.http_status : '',
      t('本地关联编号') + ': ' + safe.request_id, safe.upstream_request_id ? t('上游关联编号') + ': ' + safe.upstream_request_id : '',safe.provider_code || ''].filter(Boolean).join(' · ');
  }
  function renderError(error, id) { $(id).textContent = diagnosticText(error?.diagnostic) || errorText(error); $(id).hidden = false; }
  function showError(error, id='error') { if (id === 'error') state.refreshError = false; state.errors[id]=error; renderError(error,id); }
  function clearError(id='error') { if (id === 'error') state.refreshError = false; delete state.errors[id]; $(id).hidden = true; $(id).textContent = ''; }
  function currentWorkspace() { return state.data?.workspaces?.find(item => item.id === state.data.current_workspace_id); }
  function activeJob() { return state.data?.active_job; }
  function selectedJob() {
    const job = activeJob();
    if (!job || (job.workspace_id && job.workspace_id !== state.data?.current_workspace_id)
      || (job.conversation_id && job.conversation_id !== conversation()?.id)) return null;
    return job;
  }
  function running(job=activeJob()) { return !!job && ['running','queued','cancelling'].includes(String(job.status).toLowerCase()); }
  function busy() { return state.submitting || running(); }
  function conversation() { return state.data?.conversation; }
  function statusText(status) { return t(({answered:'已回答',needs_continuation:'仍有待查项',cancelled:'已停止',failed:'未完成',retry_confirmation_required:'请求结果待确认',interrupted:'已中断',running:'调查中',completed:'已完成',ready:'已就绪',pending:'等待处理'})[String(status || '').toLowerCase()] || '已记录'); }
  function sourceBasisText(basis) { return t(basis === 'sync_attested' ? '同步方已声明完成；分析固定在所选提交。' : '按所选源码提交分析；上游周同步完成情况未确认。'); }

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
    if (!response.ok || data?.error) { const error = new Error(typeof data?.error === 'string' ? data.error : data?.error?.code || 'ONLINE_OPERATION_FAILED'); error.diagnostic = data?.diagnostic; throw error; }
    return data;
  }

  const phaseNames = {preparing:'正在准备调查',investigating:'正在调查业务问题',ready:'已就绪',model_request_started:'正在请求模型',model_response_received:'已收到模型响应',model_request_failed:'模型请求未完成',response_applied:'已应用模型响应',source_action_completed:'已完成源码补查',answer_ready:'回答已准备好'};
  const inventoryPages = new Map(), inventoryRequests = new Map(), inventoryAnswers = new Map();
  function inventoryKey(message) {
    return JSON.stringify([conversation()?.id,message.id,message.inventory_result?.id]);
  }
  function inventoryMarkup(message) {
    const descriptor=message.inventory_result;
    if(!descriptor || typeof descriptor.id!=='string')return '';
    const page=inventoryPages.get(inventoryKey(message)) || descriptor;
    const rows=Array.isArray(page.findings)?page.findings:[],total=Number(descriptor.total)||0;
    const offset=Number(page.offset)||0,size=40,attrs=`data-inventory-message="${escape(message.id)}"`;
    const button=(label,cursor,disabled)=>`<button ${attrs} data-inventory-page="${cursor}" ${disabled?'disabled':''}>${escape(t(label))}</button>`;
    const navigation=`<nav class="inventory-pagination" aria-label="${escape(t('公式清单分页'))}">${button(rows.length?'首页':'加载公式清单',0,rows.length>0&&offset===0 || total===0)}${button('上一页',Math.max(0,offset-size),offset===0)}${button('下一页',Number(page.next_offset)||0,page.next_offset==null || rows.length===0)}${button('末页',Math.max(0,Math.floor((total-1)/size)*size),offset+rows.length>=total)}<button ${attrs} data-inventory-export>${escape(t('导出完整分析清单'))}</button></nav>`;
    return `<details class="inventory" open><summary>${escape(t('业务公式清单'))} · ${total.toLocaleString(locale)}</summary><p>${escape(t('已记录内容完整保存；分页只控制本页显示。'))}</p>${!descriptor.complete?`<p role="status">${escape(t('仍有范围待核对'))}${Number(descriptor.counts?.pending)>0?' · '+Number(descriptor.counts.pending).toLocaleString(locale):''}</p>`:''}${navigation}<div>${rows.map(row=>{
      const prose=[row.summary,row.formula,row.conditions].filter(Boolean).join('\n\n');
      const body=typeof answerMarkdown!=='undefined'?answerMarkdown.render(prose):`<p>${escape(prose)}</p>`;
      return `<section class="inventory-row answer"><h4>${escape(row.path || row.relative_path)} · ${Number(row.start_line)}–${Number(row.end_line)}</h4>${body}<div class="citations">${(row.citations || []).map((citation,index)=>`<button ${attrs} data-inventory-source="${escape(typeof citation==='string'?citation:citation.id)}">${escape(t('查看源码依据'))} ${index+1}</button>`).join('')}</div></section>`;
    }).join('')}</div><p>${escape(t('当前显示'))} ${rows.length?(offset+1).toLocaleString(locale):'0'}–${(offset+rows.length).toLocaleString(locale)} / ${total.toLocaleString(locale)}</p>${navigation}</details>`;
  }
  function inventoryMessage(id) { return conversation()?.messages?.find(message=>message.id===id); }
  function inventoryAnswerControl(message) {
    if(!message.inventory_answer_preview || !message.inventory_result?.id)return '';
    const expanded=inventoryAnswers.has(inventoryKey(message));
    return `<div class="inventory-answer-control">${expanded?'':`<p>${escape(t('当前显示说明预览，完整内容已保存。'))}</p>`}<button data-inventory-message="${escape(message.id)}" data-inventory-answer>${escape(t(expanded?'收起完整业务说明':'展开完整业务说明'))}</button></div>`;
  }
  function inventoryPath(descriptor,suffix='') {
    return `/inventories/${encodeURIComponent(descriptor.id)}${suffix}?conversation_id=${encodeURIComponent(conversation().id)}`;
  }
  async function loadInventoryPage(messageId,offset) {
    const message=inventoryMessage(messageId),descriptor=message?.inventory_result;
    if(!descriptor || !Number.isSafeInteger(offset) || offset<0)return;
    const conversationId=conversation().id,key=inventoryKey(message),ticket=(inventoryRequests.get(key)||0)+1;
    inventoryRequests.set(key,ticket);
    try {
      const data=await api(inventoryPath(descriptor)+`&offset=${offset}&limit=40`);
      if(conversation()?.id!==conversationId || inventoryMessage(messageId)?.inventory_result?.id!==descriptor.id || inventoryRequests.get(key)!==ticket)return;
      if(data.id!==descriptor.id || data.revision!==descriptor.revision || !Array.isArray(data.findings))throw Error(t('清单版本不一致，请重新读取对话。'));
      inventoryPages.set(key,{...data,offset});
      while(inventoryPages.size>16)inventoryPages.delete(inventoryPages.keys().next().value);
      render();
    } catch(error) { if(conversation()?.id===conversationId && inventoryRequests.get(key)===ticket)showError(error); }
  }
  async function exportInventory(messageId) {
    const descriptor=inventoryMessage(messageId)?.inventory_result;if(!descriptor)return;
    try {
      const result=await api(inventoryPath(descriptor,'/export'));
      if(typeof result.content!=='string')throw Error(t('清单版本不一致，请重新读取对话。'));
      const blob=new Blob([result.content],{type:result.mime_type || 'application/x-ndjson;charset=utf-8'});
      const url=URL.createObjectURL(blob),link=document.createElement('a');
      link.href=url;link.download=result.filename || 'business-formula-inventory.jsonl';link.click();URL.revokeObjectURL(url);
    } catch(error) { showError(error); }
  }
  async function toggleInventoryAnswer(messageId) {
    const message=inventoryMessage(messageId),descriptor=message?.inventory_result;
    if(!message?.inventory_answer_preview || !descriptor)return;
    const key=inventoryKey(message),conversationId=conversation().id;
    if(inventoryAnswers.has(key)){inventoryAnswers.delete(key);render();return;}
    try {
      const data=await api(inventoryPath(descriptor)+'&offset=0&limit=1');
      if(conversation()?.id!==conversationId || inventoryMessage(messageId)?.inventory_result?.id!==descriptor.id)return;
      if(data.id!==descriptor.id || data.revision!==descriptor.revision || typeof data.answer!=='string')throw Error(t('清单版本不一致，请重新读取对话。'));
      inventoryAnswers.set(key,{answer:data.answer,answer_citations:Array.isArray(data.answer_citations)?data.answer_citations:[]});
      while(inventoryAnswers.size>16)inventoryAnswers.delete(inventoryAnswers.keys().next().value);
      render();
    } catch(error) { if(conversation()?.id===conversationId)showError(error); }
  }
  async function openInventoryEvidence(messageId,citationId) {
    const descriptor=inventoryMessage(messageId)?.inventory_result;if(!descriptor || !citationId)return;
    const conversationId=conversation().id;
    await perform(async()=>{
      const result=await api(inventoryPath(descriptor,'/evidence')+`&citation_id=${encodeURIComponent(citationId)}`);
      if(conversation()?.id!==conversationId || inventoryMessage(messageId)?.inventory_result?.id!==descriptor.id)return;
      state.evidence=result;renderEvidence();$('evidence-dialog').showModal();
    });
  }
  function render() {
    const workspace = currentWorkspace(), selected = conversation(), isBusy = busy();
    $('source-title').textContent = workspace?.label || workspace?.repository?.identity || t('从业务问题开始');
    $('source-version').textContent = workspace ? `${workspace.repository?.identity || ''} · ${t('提交')} ${String(workspace.repository?.commit || '').slice(0,12)} · ${t('资料')} ${String(workspace.revision || '').slice(0,12)}${workspace.connected ? '' : ' · ' + t('需重新授权后继续调查')}` : t('接入已发布的在线分析资料，查找流程、规则与源码依据。');
    $('source-basis').textContent = workspace ? sourceBasisText(workspace.source_basis) : '';
    $('source-basis').hidden = !workspace;
    const coverage = workspace?.coverage || {};
    $('coverage').hidden = !workspace;
    $('coverage-title').textContent = t('资料覆盖范围 · 已纳入 {count} 个文件',{count:Number(coverage.indexed_files || 0).toLocaleString(locale)}) + (coverage.input_skip_count ? ' · ' + t('跳过 {count} 个文件',{count:coverage.input_skip_count}) : '');
    $('coverage-summary').textContent = t('纳入源码约 {size} MiB。',{size:(Number(coverage.source_bytes || 0)/1048576).toFixed(2)}) + (coverage.input_coverage_complete === false ? t('存在未成功读取的源码；') : '') + t('以下范围只说明预计算资料，不能代表本次问题已读完整仓库。');
    const boundaryNames = {REPOSITORY_COMPLETENESS_NOT_VERIFIED:t('全仓完整性尚未独立验证'),SPARSE_SEMANTICS_ONLY:t('静态事实不能替代完整语义或运行证明'),RULE_OCCURRENCE_DETAILS_NOT_EXPORTED:t('部分语句出现位置未展开'),UNRESOLVED_DEPENDENCY:t('仍有未确定的调用或 COPY 关系'),SOURCE_SCOPE_BOUNDARY:t('源码范围存在待确认项')};
    $('coverage-boundaries').innerHTML = (coverage.boundaries || []).slice(0,12).map(item=>`<li>${escape(boundaryNames[item.code] || item.code || t('待确认项'))}${item.count != null ? `（${escape(item.count)}）` : ''}${item.relative_path ? `：${escape(item.relative_path)}` : ''}</li>`).join('');
    $('coverage-skips').innerHTML = (coverage.input_skips || []).slice(0,16).map(item=>`<li>${t('未纳入')}：${escape(item.relative_path)}${item.reason_code ? `（${escape(item.reason_code)}）` : ''}</li>`).join('') + (coverage.input_skips_truncated || Number(coverage.input_skip_count || 0)>16 ? `<li>${t('这里只显示部分跳过记录；完整范围保留在所选资料中。')}</li>` : '');
    $('workspace-list').innerHTML = (state.data?.workspaces || []).map(item => `<button class="item ${item.id === workspace?.id ? 'selected' : ''}" data-workspace="${escape(item.id)}" ${isBusy ? 'disabled' : ''}><strong>${escape(item.label || item.repository?.identity)}</strong><small>${escape(String(item.repository?.commit || '').slice(0,12))} · ${item.connected ? t('已授权') : t('历史可查看')}</small></button>`).join('') || `<p class="muted small">${t('尚未接入在线资料')}</p>`;
    $('conversation-list').innerHTML = (state.data?.conversations || []).filter(item => !workspace || item.workspace_id === workspace.id).map(item => `<button class="item ${item.id === selected?.id ? 'selected' : ''}" data-conversation="${escape(item.id)}" data-workspace="${escape(item.workspace_id)}" ${isBusy ? 'disabled' : ''}><strong>${escape(item.title || t('新对话'))}</strong><small>${escape(statusText(item.status))}</small></button>`).join('') || `<p class="muted small">${t('分析记录将在这里保留')}</p>`;
    for (const id of ['update','credentials','disconnect','new-conversation']) $(id).disabled = !workspace || isBusy;
    $('connect').disabled = isBusy;
    $('send').disabled = !workspace?.connected || !state.modelConfigured || isBusy || !$('allow-source-transfer').checked;
    for (const id of ['export-markdown','export-json','delete-conversation']) $(id).disabled = !selected || isBusy;
    $('model-status').textContent = state.modelConfigured ? t('使用现有公司模型配置；只发送本次调查选取的资料。') : t('公司模型尚未配置，请返回原工作台设置模型接口，再刷新本页。');
    $('connection-status').textContent = state.token ? t('本机服务已连接') : t('本机服务未连接');
    const job = activeJob();
    $('progress').hidden = !running(job);
    if (running(job)) {
      $('progress-title').textContent = job.kind === 'connect' || job.kind === 'update' ? t('正在核对分析版本') : t('正在调查业务问题');
      const progress = job.progress || {};
      $('progress-detail').textContent = typeof progress === 'string' ? t(progress) : [t(progress.message || phaseNames[progress.phase] || progress.phase || '正在读取资料'), progress.completed != null ? `${progress.completed}${progress.total != null ? '/' + progress.total : ''}` : ''].filter(Boolean).join(' · ');
      $('cancel').disabled = state.submitting;
    }
    $('model-diagnostic').textContent = diagnosticText(selectedJob()?.diagnostic) || diagnosticText(selected?.pending?.diagnostic);
    $('model-diagnostic').hidden = !$('model-diagnostic').textContent;
    const list = selected?.messages || [];
    if (list.length) {
      $('messages').innerHTML = list.map(message => {
        const full=inventoryAnswers.get(inventoryKey(message));
        const isUser = message.role === 'user', citations = full ? full.answer_citations : (Array.isArray(message.citations) ? message.citations : []);
        const text = full?.answer ?? message.content ?? message.answer ?? '';
        const html = isUser ? escape(text) : (typeof answerMarkdown !== 'undefined' ? answerMarkdown.render(text) : `<p>${escape(text)}</p>`);
        return `<article class="message ${isUser ? 'user' : 'assistant'}"><div class="message-label"><span>${isUser ? t('业务问题') : t('业务分析')}</span><span>${escape(statusText(message.status))}</span></div><div class="answer">${html || `<p class="muted">${t('本次调查还没有形成回答。')}</p>`}</div>${isUser?'':inventoryAnswerControl(message)+inventoryMarkup(message)}${citations.length ? `<div class="citations">${citations.map(item => { const value=item.citation || item; const id=escape(item.key || item.id || item.citation_id || value.id || ''); return `<button ${full?`data-inventory-message="${escape(message.id)}" data-inventory-source="${id}"`:`data-citation="${id}"`}>${escape(value.path || value.relative_path || t('源码依据'))} · ${escape(value.start ?? value.start_line ?? '')}–${escape(value.end ?? value.end_line ?? '')}</button>`; }).join('')}</div>` : ''}${message.coverage ? `<p class="coverage">${t('结论以本次已核对资料为依据；未解析关系与未读取范围仍需确认。')}</p>` : ''}</article>`;
      }).join('');
    } else {
      $('messages').innerHTML = `<div class="empty"><div class="empty-mark">⌕</div><h2>${t('开始一次业务调查')}</h2><p>${t('当前对话固定在所选源码版本。可以描述业务目标，也可以从已知程序或字段开始。')}</p></div>`;
    }
    const last = [...list].reverse().find(item => item.role === 'assistant');
    const status = String(selected?.status || last?.status || '').toLowerCase();
    const resumable = ['needs_continuation','cancelled','interrupted','failed','retry_confirmation_required'].includes(status) && !!selected && !isBusy;
    $('continuation').hidden = !resumable;
    const unknown = status === 'retry_confirmation_required' || !!selected?.pending?.retry_confirmation_required;
    $('resume').hidden = unknown;
    $('retry').hidden = !unknown;
    $('resume').disabled = $('retry').disabled = !workspace?.connected || !$('allow-source-transfer').checked;
    $('continuation-description').textContent = unknown ? t('上次模型请求结果未知。重新调用可能再次消耗额度，请明确确认。') : t('已保留本次问题、已核对依据与待查项，可以从检查点继续。');
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
      else if (selectedJob()?.error && !diagnosticText(selectedJob().diagnostic)) { const job = selectedJob(); const error = new Error(typeof job.error === 'string' ? job.error : job.error.code); error.diagnostic = job.diagnostic; showError(error); }
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
    state.releases = null; state.release = null; renderConnectState();
    $('release-status').textContent = ''; $('source-credentials').hidden = true;
    $('connect-title').textContent = t(update ? '选择新的分析版本' : '接入在线分析资料');
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
      state.releases = releases; state.release = null; renderConnectState();
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
      state.release = result.release; renderConnectState();
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
    const question = $('question').value.trim(), requestLocale = locale; if (!question) return;
    await perform(async () => {
      if (!conversation()) { await api('/conversations',{workspace_id:state.data.current_workspace_id}); state.data = await api('/state'); }
      await api('/question',{conversation_id:state.data.current_conversation_id,question,locale:requestLocale,allow_source_transfer:true});
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
      state.evidence = result; renderEvidence(); $('evidence-dialog').showModal();
    });
  }

  function renderConnectState() {
    const selected = $('release-name').value;
    const releases = state.releases;
    $('release-name').innerHTML = `<option value="">${t(releases ? '选择一个已发布版本' : '请先读取版本列表')}</option>` + (releases || []).map(item => {
      const name = typeof item === 'string' ? item : item.name;
      const label = typeof item === 'string' ? name : [item.created_on?.slice(0,16)?.replace('T',' '),item.label || name].filter(Boolean).join(' · ');
      return `<option value="${escape(name)}">${escape(label)}</option>`;
    }).join('');
    $('release-name').value = selected;
    const release = state.release;
    $('release-status').textContent = release ? `${release.repository.identity} · ${t('源码提交')} ${release.repository.commit.slice(0,12)} · ${sourceBasisText(release.source_basis)}` : releases ? (releases.length ? t('找到 {count} 个版本，请明确选择',{count:releases.length}) : t('尚无已发布版本，请先完成公司环境的资料发布')) : '';
    $('connect-title').textContent = t(state.updateWorkspace ? '选择新的分析版本' : '接入在线分析资料');
  }
  function renderEvidence() {
    const result = state.evidence; if (!result) return;
    const citation = result.citation || result;
    $('evidence-title').textContent = `${citation.path || t('源码依据')} · ${citation.start || ''}–${citation.end || ''}`;
    $('evidence-version').textContent = `${t('提交')} ${citation.commit || ''} · ${t('资料')} ${citation.revision || ''}${result.truncated ? ' · ' + t('当前片段已截断') : ''}`;
    $('evidence-text').textContent = result.text || '';
  }

  // Only pure helpers are public; credentials and session state stay in closure.
  globalThis.OnlineUI = {repositoryIdentity,errorText,escape};
  if (typeof document === 'undefined') return;
  captureStaticLocale(); applyStaticLocale();
  $('online-language')?.addEventListener('change',()=>selectLocale($('online-language').value));
  if (typeof addEventListener === 'function') addEventListener('storage',event=>{if(event.key===LOCALE_KEY && LOCALES.includes(event.newValue))selectLocale(event.newValue,false);});
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
  $('messages').addEventListener('click',event=>{
    const button=event.target.closest('[data-citation],[data-inventory-page],[data-inventory-export],[data-inventory-source],[data-inventory-answer]');if(!button)return;
    if(button.dataset.citation)openEvidence(button.dataset.citation);
    else if(button.dataset.inventoryPage!==undefined)loadInventoryPage(button.dataset.inventoryMessage,Number(button.dataset.inventoryPage));
    else if(button.dataset.inventorySource)openInventoryEvidence(button.dataset.inventoryMessage,button.dataset.inventorySource);
    else if(button.dataset.inventoryExport!==undefined)exportInventory(button.dataset.inventoryMessage);
    else if(button.dataset.inventoryAnswer!==undefined)toggleInventoryAnswer(button.dataset.inventoryMessage);
  });
  $('cancel').addEventListener('click',()=>perform(()=>api('/cancel',{job_id:activeJob().job_id})));
  for (const id of ['resume','retry']) $(id).addEventListener('click',()=>{if($('allow-source-transfer').checked)perform(()=>api('/resume',{conversation_id:state.data.current_conversation_id,allow_source_transfer:true,locale,approve_retry:id==='retry'}));});
  $('disconnect').addEventListener('click',()=>perform(()=>api('/disconnect',{workspace_id:state.data.current_workspace_id})));
  $('credentials').addEventListener('click',()=>{clearCredentials('auth-');clearError('credentials-error');$('credentials-dialog').showModal();});
  $('credentials-form').addEventListener('submit',async event=>{event.preventDefault();const credentials={};for(const name of ['material-token','source-token','material-username','source-username']){const value=$('auth-'+name).value.trim();if(value)credentials[name.replace('-','_')]=value;}clearCredentials('auth-');try{await api('/credentials',{workspace_id:state.data.current_workspace_id,credentials});$('credentials-dialog').close();await refresh();}catch(error){showError(error,'credentials-error');}});
  $('export-markdown').addEventListener('click',()=>exportConversation('markdown'));
  $('export-json').addEventListener('click',()=>exportConversation('json'));
  $('delete-conversation').addEventListener('click',()=>{if(confirm(t('删除当前在线对话及其记录的引用？')))perform(()=>api('/conversations/'+state.data.current_conversation_id,undefined,'DELETE'));});
  document.querySelectorAll('[data-close]').forEach(button=>button.addEventListener('click',()=>button.closest('dialog').close()));
  $('connect-dialog').addEventListener('close',()=>clearCredentials());
  $('credentials-dialog').addEventListener('close',()=>clearCredentials('auth-'));
  (async()=>{try{const bootstrap=await api('/api/state');state.token=bootstrap.session_token;state.modelConfigured=!!bootstrap.api_configured;await refresh();}catch(error){showError(error);}})();
})();
