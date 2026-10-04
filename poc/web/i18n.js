'use strict';

// Translate authored interface text only. Template values (source, questions,
// model output and evidence) are never sent through the translation catalog.
const UI_MESSAGES = `
錯誤詳情|错误详情|Error details
模型服務暫時出錯，請稍後重試。|模型服务暂时出错，请稍后重试。|The model service encountered a temporary error; please retry later.
接口回應逾時，請稍後重試。|接口响应超时，请稍后重试。|The API request timed out; please retry later.
找不到接口地址，請檢查網路或地址。|找不到接口地址，请检查网络或地址。|Endpoint address not found; check the network or address.
連線驗證失敗，請聯絡管理員檢查憑證。|连接验证失败，请联系管理员检查证书。|Connection verification failed; ask an administrator to check the certificate.
無法建立安全連線，請聯絡管理員。|无法建立安全连接，请联系管理员。|Could not establish a secure connection; contact an administrator.
接口拒絕連線，請確認服務已啟動。|接口拒绝连接，请确认服务已启动。|The endpoint refused the connection; check that the service is running.
接口連線中斷，請稍後重試。|接口连接中断，请稍后重试。|The endpoint connection was interrupted; please retry later.
無法連上接口網路，請檢查網路連線。|无法连上接口网络，请检查网络连接。|The endpoint network is unreachable; check your network connection.
接口連線失敗，請稍後重試。|接口连接失败，请稍后重试。|The endpoint connection failed; please retry later.
無法連上本機工作台，請確認工作台已啟動。|无法连上本机工作台，请确认工作台已启动。|Cannot reach the local workbench; check that it is running.
下一次回答保存接口原文|下一次回答保存接口原文|Save the raw API response for the next answer
僅排查問題時開啟，發送一次後自動關閉。原文可能包含業務資料，會保存在本機結果目錄；一般回答和引用仍正常保存。|仅排查问题时开启，发送一次后自动关闭。原文可能包含业务资料，会保存在本机结果目录；一般回答和引用仍正常保存。|Enable only for troubleshooting; it turns off after one submission. The raw response may contain business data and is stored in the local results folder. Normal answers and references are still saved.
分析範圍|分析范围|Scope
從哪個程式開始|从哪个程序开始|Start from a program
不選擇程式時，系統會在整個代碼庫中查找。|不选择程序时，系统会在整个代码库中查找。|Leave this blank to search the whole codebase.
查看相關源碼|查看相关源码|Related source
相關對象|相关对象|Related objects
已索引文字與靜態關係範圍|已索引文本与静态关系范围|Indexed text and static relations
載入更多|加载更多|Load more
匯出完整 JSONL|导出完整 JSONL|Export complete JSONL
合成源碼 · 示例起點|合成源码 · 示例起点|Synthetic source · Example starters
選一個問題，開始對話。|选一个问题，开始对话。|Choose a question to start a conversation.
案例只提供起始問題。你可以改寫問題，接著自由追問；回答由目前源碼與模型生成。|案例只提供起始问题。你可以改写问题，接着自由追问；回答由当前源码与模型生成。|Examples are starting questions. Edit one and keep asking follow-ups; answers are generated from the current source and model.
你的第一個業務問題|你的第一个业务问题|Your first business question
輸入任何業務問題，或修改上方案例的建議問題…|输入任何业务问题，或修改上方案例的建议问题…|Ask any business question, or edit an example above…
首次建立合成源碼索引；之後切換案例和追問會重用索引。|首次建立合成源码索引；之后切换案例和追问会重用索引。|The synthetic source is indexed once. Switching examples and follow-up questions reuse the index.
載入只在本機建立索引。進入對話後按「發送」才會呼叫模型；追問會重用索引。|载入只在本机建立索引。进入对话后按“发送”才会调用模型；追问会重用索引。|Loading builds a local index only. Choose Send in the conversation to call the model; follow-ups reuse the index.
載入案例，前往提問|载入案例，前往提问|Load example and open chat
案例已載入，按「發送」開始提問；之後可繼續追問。|案例已载入，按“发送”开始提问；之后可继续追问。|Example loaded. Choose Send to ask, then continue with follow-up questions.
案例索引暫時不可用，請重試。|案例索引暂时不可用，请重试。|The example index is temporarily unavailable. Please retry.
案例尚未開始對話|案例尚未开始对话|No conversation started for this example
選擇一個示例問題，載入合成源碼後即可提問和追問。|选择一个示例问题，载入合成源码后即可提问和追问。|Choose an example question, load the synthetic source, then ask and follow up.
返回示例問題|返回示例问题|Back to example questions
選擇文件夾|选择文件夹|Choose folder
可點選文件夾，也可手動填入完整路徑；可保留程式、COPYBOOK 的子資料夾。|可选择文件夹，也可手动填入完整路径；可保留程序、COPYBOOK 的子文件夹。|Choose a folder or enter its full path. Keep program and COPYBOOK subfolders.
正在開啟本機文件夾選擇窗口…|正在打开本机文件夹选择窗口…|Opening the folder picker on this computer…
已取消選取；仍可手動填入路徑。|已取消选择；仍可手动填入路径。|Selection cancelled. You can still enter a path manually.
已選取本機文件夾。|已选取本机文件夹。|Local folder selected.
目前無法開啟本機文件夾窗口，請手動填入路徑。|当前无法打开本机文件夹窗口，请手动填入路径。|The folder picker is unavailable on this computer. Enter the path manually.
無法選取文件夾，請手動填入路徑。|无法选择文件夹，请手动填入路径。|The folder could not be selected. Enter the path manually.
測試模型連線|测试模型连接|Test model connection
填好設定後，點選「測試模型連線」，確認模型能返回文字。|填好设置后，点击“测试模型连接”，确认模型能返回文字。|After configuring the endpoint, choose Test model connection to confirm the model returns text.
正在測試模型連線…|正在测试模型连接…|Testing the model connection…
連線成功：模型已返回有效回覆。|连接成功：模型已返回有效回复。|Connected: the model returned a usable reply.
已驗證可用|已验证可用|Verified working
連線測試未通過|连接测试未通过|Connection test failed
接口驗證失敗，請檢查 API Key。|接口验证失败，请检查 API Key。|Authentication failed; check the API key.
沒有模型使用權限，請聯絡管理員。|没有模型使用权限，请联系管理员。|Model access denied; contact an administrator.
模型設定有誤，請檢查設定。|模型设置有误，请检查设置。|The model settings are invalid; check the configuration.
模型未返回可用文字，請重試。|模型未返回可用文字，请重试。|The model returned no usable text; please retry.
模型連線測試失敗，請檢查接口設定。|模型连接测试失败，请检查接口设置。|The model connection test failed; check the API settings.
模型請求過於頻繁，請稍後重試。|模型请求过于频繁，请稍后重试。|Too many model requests; please retry later.
接口回覆格式不相容，請檢查接口設定。|接口回复格式不兼容，请检查接口设置。|The reply format is incompatible; check the API settings.
接口請求無效，請檢查接口設定。|接口请求无效，请检查接口设置。|The API request is invalid; check the API settings.
資料夾內沒有可讀資料；請選擇 .md、.markdown、.txt 文件或包含這些文件的資料夾。|文件夹内没有可读资料；请选择 .md、.markdown、.txt 文件或包含这些文件的文件夹。|No supported references were found. Choose .md, .markdown or .txt files, or a folder containing them.
已找到文件，但無法讀取內容。請檢查檔案權限與文字編碼。|已找到文件，但无法读取内容。请检查文件权限与文字编码。|Files were found but could not be read. Check permissions and text encoding.
目前電腦無法讀取這個位置。請確認文件或資料夾存在，並具有讀取權限。|当前电脑无法读取这个位置。请确认文件或文件夹存在，并具有读取权限。|This computer cannot read that location. Check that the file or folder exists and is readable.
資料不是有效文字。請另存為 UTF-8 或帶 BOM 的 UTF-16 文字。|资料不是有效文本。请另存为 UTF-8 或带 BOM 的 UTF-16 文本。|The reference is not valid text. Save it as UTF-8 or UTF-16 with a BOM.
框架資料設定無法讀取。請重新填寫路徑並儲存。|框架资料设置无法读取。请重新填写路径并保存。|The reference setting cannot be read. Enter the path and save it again.
文件在讀取期間發生變更。請儲存文件後重新讀取。|文件在读取期间发生变更。请保存文件后重新读取。|The document changed while it was being read. Save it, then load it again.
單份資料過大。可按章節拆分到同一資料夾，再指定該資料夾。|单份资料过大。可按章节拆分到同一文件夹，再指定该文件夹。|The document is too large. Split it into chapters in one folder, then select that folder.
已讀取部分文件，其餘文件未納入本次框架資料。|已读取部分文件，其余文件未纳入本次框架资料。|Some documents were loaded; the remaining documents are not included in the current reference.
已讀取的框架文件|已读取的框架文件|Loaded framework documents
找不到這段對話。請從對話記錄選擇，或開始新的對話。|找不到这段对话。请从对话记录选择，或开始新的对话。|This conversation was not found. Choose one from history or start a new conversation.
請先接入源碼，再開始對話。|请先接入源码，再开始对话。|Connect your source before starting a conversation.
繼續對話|继续对话|Continue conversation
刪除對話|删除对话|Delete conversation
刪除這段對話？|删除这段对话？|Delete this conversation?
這會從對話記錄中移除提問與回答，且無法復原。已生成的分析文件仍會保留。|这会从对话记录中移除提问与回答，且无法恢复。已生成的分析文件仍会保留。|This permanently removes the conversation record. Generated analysis files remain available.
對話已刪除。|对话已删除。|Conversation deleted.
編輯後重新發送|编辑后重新发送|Edit and send again
這條問題已無法重試，請在對話末尾繼續提問。|这条问题已无法重试，请在对话末尾继续提问。|This question can no longer be retried. Continue at the end of the conversation.
已停止本次工作。對話已保留，可以繼續提問。|已停止本次工作。对话已保留，可以继续提问。|Work stopped. Your conversation is saved and you can keep asking questions.
使用已建立的代碼庫索引|使用已建立的代码库索引|Using the existing codebase index
查找與問題相關的依據|查找与问题相关的依据|Finding relevant evidence
整理業務回答|整理业务回答|Preparing the business answer
把業務問題，聊清楚。|把业务问题，聊清楚。|Talk through your business questions.
沿用代碼庫索引與對話上下文，持續追問。|沿用代码库索引与对话上下文，持续追问。|Keep exploring with the same codebase index and conversation context.
新的對話|新的对话|New conversation
業務對話|业务对话|Business conversation
提出業務問題，或接著上一個回答繼續追問…|提出业务问题，或接着上一个回答继续追问…|Ask a business question, or follow up on the last answer…
分析選項|分析选项|Analysis options
調查方式|调查方式|Investigation mode
按問題檢索 · 建議|按问题检索 · 建议|Retrieve for this question · Recommended
深入閱讀完整業務鏈 · 較慢|深入阅读完整业务链 · 较慢|Read the full business flow · Slower
一般提問會查找相關片段與呼叫關係；只有深入閱讀才逐批處理完整範圍。|一般提问会查找相关片段与调用关系；只有深入阅读才逐批处理完整范围。|Normal questions retrieve relevant excerpts and call relationships. Deep reading processes the full scope in batches.
Enter 發送 · Shift + Enter 換行|Enter 发送 · Shift + Enter 换行|Enter to send · Shift + Enter for a new line
發送|发送|Send
業務分析助手|业务分析助手|Business assistant
你|你|You
本次未取得回答。可保留對話並重試。|本次未取得回答。可保留对话并重试。|No answer was returned. Your conversation is saved and you can retry.
查看回答依據|查看回答依据|View answer sources
重試這個問題|重试这个问题|Retry this question
停止|停止|Stop
正在讀取引用…|正在读取引用…|Loading source…
想了解哪一項業務？|想了解哪一项业务？|What would you like to understand?
接入代碼庫，開始對話。|接入代码库，开始对话。|Connect your codebase to start a conversation.
直接描述你的問題。可以繼續追問條件、例外、處理過程或影響。|直接描述你的问题。可以继续追问条件、例外、处理过程或影响。|Describe your question, then follow up on conditions, exceptions, processing or impact.
首次建立本機索引，之後的問題會重用索引與對話。|首次建立本机索引，之后的问题会重用索引与对话。|Build the local index once. Later questions reuse the index and conversation.
框架與連線設定|框架与连接设置|Framework and connection settings
本次調查詳情|本次调查详情|Investigation details
源碼已變更，保留原回答的引用位置；請重新提問以取得目前源碼依據。|源码已变更，保留原回答的引用位置；请重新提问以取得目前源码依据。|The source has changed. The original citation location is retained; ask again for current source evidence.
框架文件或資料夾（選填）|框架文件或文件夹（选填）|Framework file or folder (optional)
框架文件或資料夾|框架文件或文件夹|Framework file or folder
可指定 Markdown 文件或包含文件的資料夾；接入後可在設定中查看辨識結果。|可指定 Markdown 文件或包含文件的文件夹；接入后可在设置中查看识别结果。|Choose a Markdown file or a folder containing documents. Check the detected reference in settings after connecting.
支援完整文件路徑或資料夾路徑。儲存後立即重新讀取，無需重新啟動。|支持完整文件路径或文件夹路径。保存后立即重新读取，无需重新启动。|Use a full file or folder path. Saving reloads the reference immediately without restarting.
儲存並讀取框架資料|保存并读取框架资料|Save and load framework reference
其他連線說明|其他连接说明|More connection information
正在讀取框架資料…|正在读取框架资料…|Loading framework reference…
源碼索引已就緒|源码索引已就绪|Source indexes ready
源碼已索引|源码已索引|Source indexed
 個程式| 个程序| programs
程式|程序|Programs
已建立索引，可直接提出業務問題|已建立索引，可直接提出业务问题|Indexed and ready for business questions

選擇源碼資料夾，建立業務查找索引後，即可直接提問。|选择源码文件夹，建立业务查找索引后，即可直接提问。|Choose a source folder, build the business search index, then ask your questions.
建立索引|建立索引|Build the index
提出問題|提出问题|Ask a question
此步驟在本機建立源碼結構與全文查找索引，不呼叫模型。|此步骤在本机建立源码结构与全文查找索引，不调用模型。|This step builds local source structure and full-text search indexes without calling the model.
本機代碼庫 · 業務問答|本机代码库 · 业务问答|Local repository · Business questions
建立業務查找索引|建立业务查找索引|Building business search indexes
進度按目前階段計算。首次接入會建立整個代碼庫的查找索引；提問後再選取相關源碼閱讀。預估會隨檔案大小及處理速度調整。|进度按当前阶段计算。首次接入会建立整个代码库的查找索引；提问后再选取相关源码阅读。预估会随文件大小及处理速度调整。|Progress is measured per stage. Initial setup indexes the repository for search; questions then select relevant source for reading. Estimates adjust to file size and processing speed.

已建立業務查找索引|已建立业务查找索引|Business search index ready
已讀取源碼結構，可直接提出業務問題；系統會按問題選取相關檔案繼續閱讀。|已读取源码结构，可直接提出业务问题；系统会按问题选取相关文件继续阅读。|Source structure is indexed. Ask a business question to select relevant files for further reading.
已索引檔案|已索引文件|Indexed files
呼叫實作未提供|调用实现未提供|Called implementations not supplied
直接提出業務問題，系統會自動查找相關程式與 COPYBOOK。|直接提出业务问题，系统会自动查找相关程序与 COPYBOOK。|Ask a business question to automatically find relevant programs and copybooks.

本次查找|本次查找|Searches for this question
本次選入的檔案|本次选入的文件|Files selected for this question
另有|另有|There are
 個候選檔案尚未納入閱讀。| 个候选文件尚未纳入阅读。| additional candidate files not selected for reading.

整個代碼庫 · 自動查找|整个代码库 · 自动查找|Entire repository · Find relevant source
分析範圍|分析范围|Analysis scope
用業務語言提問，系統會自動查找相關程式、定義與處理規則。|用业务语言提问，系统会自动查找相关程序、定义与处理规则。|Ask a business question. Relevant programs, definitions and rules will be found automatically.
這個代碼庫支援哪些業務功能？請整理主要功能、處理過程與業務結果。|这个代码库支持哪些业务功能？请整理主要功能、处理过程与业务结果。|What business functions does this repository support? Explain the main functions, processes and outcomes.
可辦理的業務|可办理的业务|Business functions
請找出主要業務規則與計算方式，說明輸入、適用條件和例外。|请找出主要业务规则与计算方式，说明输入、适用条件和例外。|Find the main business rules and calculations, including inputs, conditions and exceptions.
規則與計算|规则与计算|Rules and calculations
哪些情況會中止、退回或延後處理？會影響哪些業務結果？|哪些情况会中止、退回或延后处理？会影响哪些业务结果？|What conditions stop, return or defer processing, and which business outcomes are affected?
直接輸入業務問題，系統會從代碼庫自動查找相關實作。也可以選擇程式縮小範圍。|直接输入业务问题，系统会从代码库自动查找相关实现。也可以选择程序缩小范围。|Enter a business question to find relevant implementations automatically. Optionally choose a program to narrow the scope.
本次尚未取得業務解讀，請查看 API 返回中的具體原因後重試。|本次尚未取得业务解读，请查看 API 返回中的具体原因后重试。|No business explanation was returned. Check the API response for the specific cause, then retry.
依據與補充說明|依据与补充说明|Sources and additional details
本次查找 ·|本次查找 ·|Searches for this question ·
 個相關檔案| 个相关文件| relevant files
目錄共有|目录共有|The catalog contains
 個檔案，本次選入| 个文件，本次选入| files; this question selected
 個檔案繼續閱讀。這是問題相關範圍，不表示全庫源碼已讀完。| 个文件继续阅读。这是问题相关范围，不表示全库源码已读完。| files for further reading. This is the relevant scope for this question, not a complete reading of the repository.
匹配檔案|匹配文件|Matching files
大型結果已按頁面展示量整理。完整逐頁解讀、引用與接口記錄仍保存在結果資料夾。|大型结果已按页面展示量整理。完整逐页解读、引用与接口记录仍保存在结果文件夹。|Large results are condensed for display. Full page explanations, citations and API records remain in the output folder.
查找相關業務實作|查找相关业务实现|Finding relevant business implementations
理解問題並展開查找|理解问题并展开查找|Planning the business search

閱讀方式|阅读方式|Reading approach
完整業務鏈 · 自動分批閱讀|完整业务链 · 自动分批阅读|Full business flow · Automatic batches
按問題重點閱讀|按问题重点阅读|Focus on the question
每批閱讀量|每批阅读量|Sections per batch
每批 4 段|每批 4 段|4 sections per batch
每批 12 段|每批 12 段|12 sections per batch
每批 48 段|每批 48 段|48 sections per batch
每批 |每批 |Sections per batch:\u0020
自動分批讀取本次範圍內所有可讀源碼，再整合業務解讀。每批數量不是整次分析的總段數上限。|自动分批读取本次范围内所有可读源码，再整合业务解读。每批数量不是整次分析的总段数上限。|Read all readable source in the current scope in batches, then combine the business explanation. Batch size does not cap the total sections analysed.
完整業務鏈 · 分批閱讀|完整业务链 · 分批阅读|Full business flow · Batched reading
已完成批次 |已完成批次 |Completed batches:\u0020
各來源的業務解讀|各来源的业务解读|Business explanations by source
按來源保留的模型解讀，可補充上方業務摘要；仍需業務覆核。|按来源保留的模型解读，可补充上方业务摘要；仍需业务复核。|These model explanations supplement the business summary with detail from each source. Business review is still needed.
源碼或索引已更新，舊引用暫時不可用。請重新分析目前源碼。|源码或索引已更新，旧引用暂时不可用。请重新分析当前源码。|Source or its index has changed, so earlier citations are unavailable. Analyse the current source again.
已選入相關片段的呼叫：|已选入相关片段的调用：|Calls with related source selected:\u0020
 個已定位目標的呼叫 · | 个已定位目标的调用 · | resolved calls ·\u0020
 個尚未覆蓋| 个尚未覆盖| not covered
 · 另有 | · 另有 | · Plus\u0020
 個呼叫目標未確認| 个调用目标未确认| calls with unresolved targets
這是本次入口閱讀計劃的資料覆蓋，只表示呼叫位置、被呼叫程式入口與介面片段已選入；不代表模型已讀完、整條呼叫鏈完整或業務結果已驗證。|这是本次入口阅读计划的资料覆盖，只表示调用位置、被调用程序入口与接口片段已选入；不代表模型已读完、整条调用链完整或业务结果已验证。|This is source coverage in the current entry’s reading plan: call sites, called program entries and interface excerpts are selected. It does not establish completed model reading, full call-chain coverage or verified business outcomes.
未接受的模型正文|未接受的模型正文|Unaccepted model response
以下文字已保留供診斷，但未通過本次源碼或結果核對，不能作為目前業務結論或引用依據。|以下文字已保留供诊断，但未通过本次源码或结果核对，不能作为当前业务结论或引用依据。|This text is retained for diagnostics. It did not pass this run's source or result checks and cannot support current business findings or citations.
已保留未接受正文，不能作為目前業務結論|已保留未接受正文，不能作为当前业务结论|Unaccepted response retained; not a current business finding
本次業務結果未獲接受|本次业务结果未被接受|Business result not accepted
源碼或結果核對未通過，已保留收到的模型文字供診斷。|源码或结果核对未通过，已保留收到的模型文字供诊断。|Source or result checks did not pass. The received model text is retained for diagnostics.
技術與框架依據|技术与框架依据|Technical and framework references
來源程式|来源程序|Source program
查看程式關係|查看程序关系|View program relationships
請說明 |请说明 |Explain\u0020
 支援的業務、受理條件、狀態變化與異常影響。| 支持的业务、受理条件、状态变化与异常影响。| in terms of its business purpose, eligibility rules, status changes and exception impact.
案例已載入，尚未呼叫模型|案例已载入，尚未调用模型|Case loaded; no model call yet
案例已載入|案例已载入|Case loaded
可修改問題並按「開始業務分析」，從目前源碼生成自己的業務解讀。|可修改问题并点击“开始业务分析”，从当前源码生成自己的业务解读。|Edit the question and choose Start analysis to generate an explanation from the current source.
部分產生式或閉源依賴未提供，仍可先分析已有源碼。|部分生成式或闭源依赖未提供，仍可先分析已有源码。|Some generated or closed-source dependencies are not provided. The available source can still be analysed.
合成源碼 · 本次工作階段|合成源码 · 本次会话|Synthetic source · Current session
合成源碼|合成源码|Synthetic source
傳統聯機|传统联机|Traditional online
控制器分段聯機|控制器分段联机|Controller-based online
批處理|批处理|Batch processing
業務案例工作台|业务案例工作台|Business case workbench
業務案例|业务案例|Business cases
正在讀取業務案例|正在读取业务案例|Loading business cases
業務案例暫時無法讀取，請重試或檢查本機演示文件。|业务案例暂时无法读取，请重试或检查本机演示文件。|Business cases could not be loaded. Retry or check the local demo files.
請先啟動本機服務，才能讀取業務案例及來源證據。|请先启动本机服务，才能读取业务案例及来源证据。|Start the local service to read the business cases and their source evidence.
重新讀取案例|重新读取案例|Reload cases
在項目目錄啟動 poc/run_web.bat，或執行 python poc/web_app.py，再開啟本機服務地址。|在项目目录启动 poc/run_web.bat，或运行 python poc/web_app.py，再打开本机服务地址。|Start poc/run_web.bat or run python poc/web_app.py from the project folder, then open the local service address.
一條業務鏈 · 三個業務場景|一条业务链 · 三个业务场景|One business flow · Three business scenarios
受理、覆核，到夜間生效。|受理、复核，到夜间生效。|Intake, review, then overnight activation.
合成業務案例 · 未呼叫模型|合成业务案例 · 未调用模型|Synthetic business case · No model call
合成業務案例|合成业务案例|Synthetic business case
 · 合成源碼| · 合成源码| · Synthetic source
合成源碼 · 業務解讀仍需覆核|合成源码 · 业务解读仍需复核|Synthetic source · Business interpretation needs review
建議業務問題|建议业务问题|Suggested business question
載入案例後可自由修改問題，再按「開始業務分析」呼叫模型。|载入案例后可自由修改问题，再按“开始业务分析”调用模型。|Load the case to edit the question, then choose Start analysis to call the model.
載入此案例|载入此案例|Load this case
載入只建立本機目錄與源碼索引，不呼叫模型。|载入只建立本机目录与源码索引，不调用模型。|Loading builds the local catalog and source index without calling a model.
業務說明|业务说明|Business explanation
框架類型|框架类型|Framework type
業務處理過程|业务处理过程|Business process
以下說明由合成源碼整理，不代表已執行程式或模型分析。|以下说明由合成源码整理，不代表已执行程序或模型分析。|This explanation is derived from synthetic source, not from program execution or model analysis.
業務規則與影響|业务规则与影响|Business rules and impact
業務設定為案例中的合成聲明，不代表已匯入生產配置或執行記錄。|业务设置为案例中的合成声明，不代表已导入生产配置或运行记录。|Business settings are synthetic declarations for this case. Production settings and execution records have not been imported.
案例範圍與待確認事項|案例范围与待确认事项|Case scope and open questions
點選引用可查看演示文件中的實際源碼；自由問題需載入案例後分析。|点击引用可查看演示文件中的实际源码；自由问题需载入案例后分析。|Open a citation to read actual source from the demo files. Load the case to analyse your own question.
以下關係來自合成案例源碼中的呼叫語句，不代表實際執行順序。|以下关系来自合成案例源码中的调用语句，不代表实际执行顺序。|These relationships come from calls in the synthetic source. They do not establish execution order.
源碼呼叫已讀取|源码调用已读取|Source call observed
尚未發起模型分析|尚未发起模型分析|No model analysis has started
合成業務案例不會生成調查記錄。載入案例並開始分析後，才會顯示本次活動。|合成业务案例不会生成调查记录。载入案例并开始分析后，才会显示本次活动。|A synthetic business case does not generate investigation records. Load the case and start analysis to see actual activity.
案例源碼引用|案例源码引用|Case source citations
合成文件原文|合成文件原文|Synthetic source files
合成源碼 · 文件內容已讀取|合成源码 · 文件内容已读取|Synthetic source · File content read
這些片段來自隨附的合成源碼，行號與雜湊由本機服務讀取，不是執行結果。|这些片段来自随附的合成源码，行号与哈希由本机服务读取，不是运行结果。|These excerpts come from the bundled synthetic source. The local service reads their lines and hashes; they are not execution results.
先查看業務案例|先查看业务案例|Explore business cases
查看業務案例|查看业务案例|View business cases
共用程式|共用程序|Shared program
由合成文件實際讀取|由合成文件实际读取|Read from synthetic files
案例尚未產生分析記錄|案例尚未产生分析记录|No analysis records for this case
選擇業務案例，載入並開始模型分析後，可在本次工作階段查看記錄。|选择业务案例，载入并开始模型分析后，可在本次会话查看记录。|Choose and load a business case, then start model analysis to see records in this session.
業務案例尚未載入。|业务案例尚未载入。|Business cases have not loaded yet.
本機合成源碼|本机合成源码|Local synthetic source
請先載入案例或接入本機源碼，再分析自己的問題。|请先载入案例或接入本机源码，再分析自己的问题。|Load a case or connect local source before analysing your own question.
部分業務解讀|部分业务解读|Partial business explanation
模型輸出尚未完成，已保留取得的解讀。|模型输出尚未完成，已保留取得的解读。|Model output is incomplete; available explanations are preserved.
部分模型回應尚未完整輸出，已保留可用的解讀內容。|部分模型响应尚未完整输出，已保留可用的解读内容。|Some model responses ended before completion. Available explanations are preserved.
已生成業務解讀|已生成业务解读|Business explanation generated
已有模型解讀，業務含義尚待覆核|已有模型解读，业务含义尚待复核|Model explanation available; business meaning needs review
模型解讀 · 需業務覆核|模型解读 · 需业务复核|Model interpretation · Business review needed
部分解讀|部分解读|Partial explanation
閱讀覆蓋|阅读覆盖|Reading coverage
已讀 |已读 |Read\u0020
已解讀 |已解读 |Interpreted\u0020
 頁（本次源碼範圍）| 页（本次源码范围）| pages (current source scope)
已發送 |已发送 |Sent\u0020
本次選取 |本次选取 |Selected for this run:\u0020
本次源碼範圍已讀完；閱讀完整不等於業務結論已驗證。|本次源码范围已读完；阅读完整不等于业务结论已验证。|The current source scope was fully read. Complete reading does not verify the business conclusions.
閱讀覆蓋描述本次讀取範圍，不代表已驗證所有業務路徑。|阅读覆盖描述本次读取范围，不代表已验证所有业务路径。|Reading coverage describes this source scope; it does not verify every business path.
尚有 |尚有 |There are\u0020
 頁未讀，本次解讀先涵蓋已讀內容。| 页未读，本次解读先覆盖已读内容。| unread pages. This explanation covers the material read so far.
部分分析請求未完成，已保留取得的解讀；詳情可查看 API 返回。|部分分析请求未完成，已保留取得的解读；详情可查看 API 返回。|Some analysis requests did not complete. Available explanations are preserved; see API responses for details.
 項依賴尚缺實作，相關內部行為仍需補充源碼確認。| 项依赖尚缺实现，相关内部行为仍需补充源码确认。| dependencies are missing implementations; their internal behaviour needs additional source.
目前呈現已取得的解讀，尚未完成的範圍可繼續分析。|当前呈现已取得的解读，尚未完成的范围可继续分析。|The available explanation is shown. The remaining scope can be analysed further.
閱讀來源 · |阅读来源 · |Source pages ·\u0020
補充上下文|补充上下文|Additional context
逐頁讀取源碼|逐页读取源码|Reading source pages
解讀已讀源碼|解读已读源码|Interpreting source pages
整合業務解讀|整合业务解读|Combining the business explanation
閱讀深度|阅读深度|Reading depth
按問題閱讀 · 最多 12 段|按问题阅读 · 最多 12 段|Question-focused · Up to 12 sections
深入閱讀 · 最多 48 段|深入阅读 · 最多 48 段|In-depth · Up to 48 sections
自訂 · 最多 |自定义 · 最多 |Custom · Up to\u0020
 段| 段| sections
增加閱讀量會使用更多模型請求。|增加阅读量会使用更多模型请求。|Reading more source uses more model requests.
快速閱讀 · 最多 4 段|快速阅读 · 最多 4 段|Quick reading · Up to 4 sections
快速模式只閱讀與問題最相關的少量源碼；需要完整鏈路時請切換上方選項。|快速模式只阅读与问题最相关的少量源码；需要完整链路时请切换上方选项。|Quick mode reads only the most relevant source. Switch the option above for the full chain.
 頁| 页| pages
頁|页|pages
接口返回 HTTP 錯誤；本次傳輸未收集錯誤正文。|接口返回 HTTP 错误；本次传输未收集错误正文。|The API returned an HTTP error; this transport did not collect the error body.
請求未收到回應，因此沒有返回正文。|请求未收到响应，因此没有返回正文。|No response was received, so no response body is available.
收到的返回正文格式無效，無法保存為文字。|收到的返回正文格式无效，无法保存为文本。|The response body was invalid and could not be saved as text.
傳輸返回資料無效，無法取得可展示的正文。|传输返回数据无效，无法取得可展示的正文。|The transport response was invalid; no displayable body could be obtained.
源碼核對或分析失敗，本次業務結果未獲接受。|源码核对或分析失败，本次业务结果未获接受。|Source verification or analysis failed; this business result was not accepted.
已達診斷保存上限，部分返回內容或請求記錄未保存。|已达诊断保存上限，部分返回内容或请求记录未保存。|The diagnostic storage limit was reached. Some response content or request records were not saved.
 未保存的請求記錄：| 未保存的请求记录：| Omitted request records:
業務調查優先；各階段的最新請求優先，序號保留實際呼叫順序。|业务调查优先；各阶段的最新请求优先，序号保留实际调用顺序。|Business investigation responses first; newest requests first in each phase. Numbers preserve the original call order.
API 返回|API 返回|API response
查看 API 返回|查看 API 返回|View API response
分析流程中斷|分析流程中断|Analysis interrupted
API 連線|API 连接|API connection
Agent 流程|Agent 流程|Agent workflow
業務證據|业务证据|Business evidence
模型回應未符合 Agent 動作協議，請先查看 API 返回。|模型响应不符合 Agent 动作协议，请先查看 API 返回。|The model response did not match the Agent action contract. Check the API response.
模型的最終答案未符合答案協議，請先查看 API 返回。|模型的最终答案不符合答案协议，请先查看 API 返回。|The final model answer did not match the answer contract. Check the API response.
模型請求或回應處理失敗，請查看 HTTP 狀態與 API 返回。|模型请求或响应处理失败，请查看 HTTP 状态与 API 返回。|The model request or response processing failed. Check the HTTP status and API response.
已達本次工具呼叫上限，調查尚未完成。|已达本次工具调用上限，调查尚未完成。|The tool-call limit was reached before the investigation finished.
已達本次模型輪次上限，調查尚未完成。|已达本次模型轮次上限，调查尚未完成。|The model-turn limit was reached before the investigation finished.
連續調查未取得新的可用事實，流程已停止。|连续调查未取得新的可用事实，流程已停止。|Repeated investigation calls found no new usable facts, so the workflow stopped.
模型輸出超過本次處理上限。|模型输出超过本次处理上限。|Model output exceeded the processing limit for this run.
模型請求了未開放的工具。|模型请求了未开放的工具。|The model requested an unavailable tool.
模型提供的工具參數未通過核對。|模型提供的工具参数未通过核对。|The model's tool arguments failed validation.
證據讀取階段已結束，模型仍請求繼續讀取。|证据读取阶段已结束，模型仍请求继续读取。|The model requested more evidence after the evidence-reading phase had closed.
本機調查工具執行失敗。|本机调查工具执行失败。|A local investigation tool failed.
本機工具的返回資料未符合工具協議。|本机工具的返回数据不符合工具协议。|A local tool response did not match the tool contract.
本機工具返回了不支援的狀態。|本机工具返回了不支持的状态。|A local tool returned an unsupported status.
工具結果缺少有效的源碼快照。|工具结果缺少有效的源码快照。|A tool result was missing a valid source snapshot.
工具結果與本次源碼快照不一致。|工具结果与本次源码快照不一致。|A tool result did not match the current source snapshot.
源碼證據完整性核對失敗。|源码证据完整性核对失败。|Source evidence failed its integrity check.
工具結果超出本次允許的處理範圍。|工具结果超出本次允许的处理范围。|A tool result exceeded the allowed processing scope.
本次需要的調查工具能力不可用。|本次需要的调查工具能力不可用。|A required investigation tool capability was unavailable.
模型引用超出本次調查範圍的證據。|模型引用超出本次调查范围的证据。|The model cited evidence outside this investigation's scope.
模型引用了尚未核驗的證據。|模型引用了尚未核验的证据。|The model cited evidence that had not been verified.
候選結論與源碼內容未能對應。|候选结论与源码内容未能对应。|A candidate claim could not be matched to source content.
候選結論未獲現有證據支持。|候选结论未获现有证据支持。|A candidate claim was not supported by the available evidence.
本機結論核對程序執行失敗。|本机结论核对程序执行失败。|The local claim checker failed.
分析流程因下列原因停止；請查看診斷與 API 返回。|分析流程因以下原因停止；请查看诊断与 API 返回。|Analysis stopped for the reason below. Check diagnostics and the API response.
聊天回應已收到|聊天响应已收到|Chat response received
已收到 HTTP 回應；聊天回應尚未確認|已收到 HTTP 响应；聊天响应尚未确认|HTTP response received; chat response not confirmed
未收到 HTTP 回應|未收到 HTTP 响应|No HTTP response received
尚無本次請求記錄|尚无本次请求记录|No request records for this run
能力探測未通過，業務調查尚未開始。|能力探测未通过，业务调查尚未开始。|Capability checks failed; the business investigation has not started.
Agent 流程已完成；業務證據需另行核對。|Agent 流程已完成；业务证据需另行核对。|The Agent workflow completed; business evidence requires separate review.
已發起業務調查；完成狀態尚未確認。|已发起业务调查；完成状态尚未确认。|A business investigation was started; completion has not been confirmed.
聊天能力探測已通過；尚無業務調查結果。|聊天能力探测已通过；尚无业务调查结果。|Chat capability checks passed; no business investigation result is available.
尚無 Agent 完成記錄。|尚无 Agent 完成记录。|No Agent completion record is available.
流程中斷，尚未形成業務結論|流程中断，尚未形成业务结论|Workflow interrupted; no business conclusion reached
尚未形成業務結論|尚未形成业务结论|No business conclusion reached
新問題尚未取得 API 返回|新问题尚未取得 API 返回|No API response for the new question
問題、調查起點或閱讀方式已更改。發起分析後，這裡會顯示本次請求的返回資料。|问题、调查起点或阅读方式已更改。发起分析后，这里会显示本次请求的返回数据。|The question, starting program or reading settings have changed. Run analysis to view responses for the new request.
未核驗的 API 返回|未核验的 API 返回|Unverified API response
 次請求| 次请求| requests
先確認接口返回，再核對分析結果。|先确认接口返回，再核对分析结果。|Check the API response, then review the analysis.
這裡展示接口實際返回的文字，用於核對連線與返回格式；不代表已通過業務證據核驗。|这里展示接口实际返回的文本，用于核对连接与返回格式；不代表已通过业务证据核验。|These are actual API responses for checking connections and response formats. They are not verified business evidence.
能力探測成功不代表業務分析已完成。|能力探测成功不代表业务分析已完成。|Successful capability checks do not mean business analysis is complete.
API 請求記錄已遮蔽配置值，內容可能因大小限制而截斷。保留的模型正文可能包含源碼或業務內容，請在分享前核對。|API 请求记录已遮蔽配置值，内容可能因大小限制而截断。保留的模型正文可能包含源码或业务内容，请在分享前核对。|Configuration values are masked in API request records, and size limits may truncate content. Retained model text may contain source or business content; review before sharing.
能力探測|能力探测|Capability check
業務調查|业务调查|Business investigation
未知階段|未知阶段|Unknown phase
返回內容已截斷；未顯示部分不能作為缺失資料判斷。|返回内容已截断；未显示部分不能作为缺失数据判断。|The response was truncated. Omitted content must not be treated as missing data.
模型返回文字 · 未核驗|模型返回文本 · 未核验|Model response text · Unverified
未提取到 message.content；請查看下方原始返回（可能是工具呼叫、錯誤或截斷內容）。|未提取到 message.content；请查看下方原始返回（可能是工具调用、错误或截断内容）。|No message.content was extracted. Check the raw response below for tool calls, errors or truncated content.
原始返回內容 · 已遮蔽配置值|原始返回内容 · 已遮蔽配置值|Raw response body · Configuration values masked
未記錄返回內容。|未记录返回内容。|No response body was recorded.
本次尚未發出接口請求，請先處理接入或配置問題。|本次尚未发出接口请求，请先处理接入或配置问题。|No API requests were made for this run. Resolve source intake or configuration issues first.
這份分析未保存 API 返回原文。重新發起分析後，可在這裡查看。|这份分析未保存 API 返回原文。重新发起分析后，可在这里查看。|This analysis did not save raw API responses. Run analysis again to view them here.
尚未發起模型分析；接入源碼本身不會呼叫模型。|尚未发起模型分析；接入源码本身不会调用模型。|No model analysis has been started. Source intake alone does not call the model.
框架知識|框架知识|Framework knowledge
框架資料已載入|框架资料已载入|Framework reference loaded
已找到相關框架資料|已找到相关框架资料|Relevant framework references found
已載入 · 未確認源碼匹配|已载入 · 未确认源码匹配|Loaded · Source match unconfirmed
框架資料無法讀取|框架资料无法读取|Framework reference unavailable
尚未設定框架資料|尚未配置框架资料|Framework reference not configured
框架資料超過讀取上限。請依使用手冊縮小參考文件，再重新啟動服務。|框架资料超过读取上限。请按使用手册缩小参考文件，再重新启动服务。|The framework reference exceeds the reading limit. Reduce the reference document as described in the manual, then restart the service.
框架資料格式無效。請使用含有章節標題的 UTF-8 Markdown 文件，並核對本機設定。|框架资料格式无效。请使用含有章节标题的 UTF-8 Markdown 文件，并核对本机配置。|The framework reference format is invalid. Use a UTF-8 Markdown document with section headings and check your local settings.
請檢查 FRAMEWORK_REFERENCE_PATH 指向的文件是否存在且可讀，修改設定後重新啟動服務。|请检查 FRAMEWORK_REFERENCE_PATH 指向的文件是否存在且可读，修改配置后重新启动服务。|Check that FRAMEWORK_REFERENCE_PATH points to an existing, readable document. Restart the service after changing settings.
查看框架引用|查看框架引用|View framework reference
框架|框架|Framework
本機參考文件|本机参考文件|Local reference document
文件指紋|文件指纹|Document fingerprint
個資料片段|个资料片段|reference excerpts
將本機框架文件接入後，系統會結合相關章節與源碼證據解讀程式。|接入本机框架文件后，系统会结合相关章节与源码证据解读程序。|Connect a local framework reference to interpret programs using relevant sections and source evidence.
參考文件已更新。請重新分析，讓引用對應目前內容。|参考文件已更新。请重新分析，让引用对应当前内容。|The reference document has changed. Run analysis again to align citations with its current contents.
下列章節與本次問題或源碼相關；名稱及文字匹配僅是線索，需要結合源碼覆核。|下列章节与本次问题或源码相关；名称及文本匹配只是线索，需要结合源码复核。|These sections relate to this question or source. Name and text matches are clues that still require source review.
本次範圍內未確認框架與源碼的對應，仍可分析已有源碼。依問題選取的章節僅供背景參考。|本次范围内未确认框架与源码的对应，仍可分析已有源码。按问题选取的章节仅供背景参考。|No source match was confirmed within this scope. Available source can still be analysed; sections selected for the question are background only.
參考文件已載入，但本次源碼索引未能完成框架匹配。請重新接入或選擇有效入口。|参考文件已载入，但本次源码索引未能完成框架匹配。请重新接入或选择有效入口。|The reference is loaded, but the current source index could not be matched. Reconnect source or select a valid entry.
已載入參考文件。開始業務分析後，可查看本次選用的章節和源碼對應。|已载入参考文件。开始业务分析后，可查看本次选用的章节和源码对应。|The reference is loaded. Start analysis to see selected sections and their source matches.
文件知識不等於現場執行驗證；閉源實作、實際配置與執行結果仍可能未知。|文件知识不等于现场运行验证；闭源实现、实际配置与运行结果仍可能未知。|Document knowledge does not verify runtime behaviour. Closed-source implementations, live settings and execution results may remain unknown.
查看本次框架依據|查看本次框架依据|View framework references used
段框架引用|段框架引用|framework excerpts
處源碼線索|处源码线索|source matches
頁碼|页码|Page
對應源碼線索|对应源码线索|Source matches
依問題選取的背景章節；未確認對應源碼。|按问题选取的背景章节；未确认对应源码。|Background selected for this question; no source match has been confirmed.
已達本次框架檢索上限，未涵蓋的章節或源碼不代表不相關。|已达本次框架检索上限，未覆盖的章节或源码不代表不相关。|This framework retrieval reached its limit. Other sections or source may still be relevant.
查看本機設定|查看本机配置|View local settings
框架條件解讀 · 需源碼與現場覆核|框架条件解读 · 需源码与现场复核|Conditional framework interpretation · Source and runtime review needed
框架解讀結合可見源碼與文件規則；現場配置、生成或閉源實作及執行效果尚未獨立核驗。|框架解读结合可见源码与文件规则；现场配置、生成或闭源实现及运行效果尚未独立核验。|Framework interpretations combine visible source and document rules. Live settings, generated or closed-source implementations and runtime effects have not been independently verified.
檢索相關框架資料|检索相关框架资料|Finding relevant framework references
發起分析將使用已設定的模型接口，傳送問題、有限源碼證據及相關框架文件節錄。|发起分析将使用已配置的模型接口，发送问题、有限源码证据及相关框架文件节录。|Starting analysis sends your question, limited source evidence and relevant framework excerpts to the configured model endpoint.
提問時，向已設定的接口|提问时，向已配置的接口|Questions, limited source and
傳送有限源碼與框架節錄。|发送有限源码与框架节录。|framework excerpts go to your endpoint.
框架資料：將獲准使用的 Markdown 文件複製至項目的 .poc-data/framework/reference.md，再於 .env 加入以下設定。框架文件與 .env 均不會隨 GitHub 下載，換電腦時需要另外複製。|框架资料：将获准使用的 Markdown 文件复制至项目的 .poc-data/framework/reference.md，再于 .env 加入以下配置。框架文件与 .env 均不会随 GitHub 下载，换电脑时需要另外复制。|Copy the approved Markdown reference to .poc-data/framework/reference.md in the project, then add this setting to .env. The private reference and .env are not included in GitHub downloads; copy them separately when changing computers.
目前只核對已索引的源碼；資料庫記錄、執行參數與完整執行狀態未核驗。|目前只核对已索引的源码；数据库记录、运行参数与完整运行状态未核验。|Only indexed source has been checked. Database records, runtime parameters and full execution state remain unverified.
呼叫或 COPY 的實作未提供，相關內部行為仍待確認。|调用或 COPY 的实现未提供，相关内部行为仍待确认。|A call or COPY implementation is unavailable; its internal behaviour remains unknown.
部分依賴或工具結果不完整，本次回答只涵蓋已有證據。|部分依赖或工具结果不完整，本次回答只涵盖已有证据。|Some dependencies or tool results are incomplete; this answer covers available evidence only.
入口超出本次解析範圍|入口超出本次解析范围|Entry exceeds this analysis scope
 項未確認依賴| 项未确认依赖| unresolved dependencies
已達本次範圍限制，部分依賴未納入。可另選相關入口繼續分析。|已达本次范围限制，部分依赖未纳入。可另选相关入口继续分析。|The scope limit was reached and some dependencies were excluded. Select another related entry to continue.
本檔案預計剩餘|本文件预计剩余|Estimated time left for this file
移除過期索引|移除过期索引|Removing stale index entries
整理 COPY 引用|整理 COPY 引用|Resolving COPY references
整理呼叫參數|整理调用参数|Binding call parameters
檔案|文件|files
行|行|lines
項|项|items
目前檔案已解析 |当前文件已解析 |Current file: parsed\u0020
局部解讀 · 未知依賴保留為邊界|局部解读 · 未知依赖保留为边界|Partial findings · Unknown dependencies remain bounded
已取消|已取消|Cancelled
個目錄入口|个目录入口|catalog entries
目錄已就緒 · 詳細分析按需建立|目录已就绪 · 详细分析按需建立|Catalog ready · Details built on demand
搜尋調查入口|搜索调查入口|Search starting programs
搜尋名稱或路徑，顯示前 100 項|搜索名称或路径，显示前 100 项|Search name or path; showing up to 100 entries
檔名入口 · 待詳細解析|文件名入口 · 待详细解析|File entry · Detailed parsing pending
檔名入口|文件名入口|File entry
目錄已識別 · 按需解析|目录已识别 · 按需解析|Catalogued · Parsed on demand
沒有符合的入口|没有符合的入口|No matching entries
上一頁|上一页|Previous
下一頁|下一页|Next
目錄檔案|目录文件|Catalog files
可選入口|可选入口|Available entries
先建立目錄，提問時解析相關源碼|先建立目录，提问时解析相关源码|Catalog first; parse related source when asked
本次重用快取|本次复用缓存|Cached files reused
個檔案已更新|个文件已更新|files updated
已取消。再次接入會重用已完成的目錄快取。|已取消。再次接入会复用已完成的目录缓存。|Cancelled. Reconnect to reuse completed catalog work.
進度連線中斷，正在重試；後台工作可能仍在繼續。|进度连接中断，正在重试；后台工作可能仍在继续。|Progress connection lost. Retrying; work may still be running.
等待開始|等待开始|Waiting to start
掃描檔案目錄|扫描文件目录|Discovering files
更新輕量目錄|更新轻量目录|Updating source catalog
定位相關源碼|定位相关源码|Locating related source
建立本次詳細索引|建立本次详细索引|Building the analysis index
讀取當前檔案|读取当前文件|Reading current file
解析當前檔案|解析当前文件|Parsing current file
寫入索引|写入索引|Writing index
整理程式關係|整理程序关系|Resolving relationships
核對並保存結果|核对并保存结果|Checking and saving results
核對源碼是否更新|核对源码是否更新|Checking for source changes
模型正在調查已有源碼|模型正在调查已有源码|Model investigating available source
已完成|已完成|Completed
準備中|准备中|Preparing
接入進度|接入进度|Source intake progress
本次執行|本次执行|Current job
統計中|统计中|Counting
目前階段進度|当前阶段进度|Current stage progress
已運行|已运行|Elapsed
本階段預計剩餘|本阶段预计剩余|Estimated time left in this stage
本階段已處理資料|本阶段已处理数据|Data processed in this stage
估算中|估算中|Estimating
目前處理|当前处理|Current file
進度按目前階段計算；預估會隨檔案大小及處理速度調整。詳細解析只處理本次問題相關範圍。|进度按当前阶段计算；预估会随文件大小及处理速度调整。详细解析只处理本次问题相关范围。|Progress is measured per stage. Estimates adjust to file sizes and processing speed. Detailed parsing is limited to this question’s scope.
正在停止，等待目前步驟結束|正在停止，等待当前步骤结束|Stopping after the current step
停止本次工作|停止本次工作|Stop this job
停止後可再次接入，重用已完成的目錄快取。|停止后可再次接入，复用已完成的目录缓存。|Reconnect after stopping to reuse completed catalog work.
位元組|字节|bytes
剩餘|剩余|remaining
正在確認總量|正在确认总量|determining total
目前步驟仍在處理，距上次進度更新 |当前步骤仍在处理，距上次进度更新 |This step is still running. Last progress update:\u0020
 秒。| 秒。| seconds ago.
進度連線正常|进度连接正常|Progress connection active
本次問題的局部解析|本次问题的局部解析|Scoped analysis for this question
輕量目錄已就緒|轻量目录已就绪|Source catalog ready
本次詳細索引包含 |本次详细索引包含 |This detailed index contains\u0020
 個檔案。缺失或閉源的依賴會標示邊界，已有源碼仍可分析。| 个文件。缺失或闭源的依赖会标示边界，已有源码仍可分析。| files. Missing or closed-source dependencies are marked as boundaries; available source can still be analysed.
目錄用於定位程式，不代表全庫已完成語句解析。選擇入口後，系統會按需讀取相關源碼。|目录用于定位程序，不代表全库已完成语句解析。选择入口后，系统会按需读取相关源码。|The catalog locates programs; it is not a fully parsed repository. Select an entry to read related source on demand.
強制校驗全部檔案內容|强制校验全部文件内容|Verify all file contents
一般更新會重用未變檔案。強制校驗會完整讀取全部源碼，首次大目錄接入無需勾選。|一般更新会复用未变文件。强制校验会完整读取全部源码，首次大目录接入无需勾选。|Normal updates reuse unchanged files. Forced verification reads all source contents; leave this off for normal intake.
此步驟只在本機更新輕量目錄，不呼叫模型。|此步骤只在本机更新轻量目录，不调用模型。|This step updates a local source catalog without calling a model.
請使用完整絕對路徑：源碼目錄須存在；輸出須獨立且為空、新目錄或只含分析產物。兩者不可相互嵌套。|请使用完整绝对路径：源码目录须存在；输出须独立且为空、新目录或只含分析产物。两者不可相互嵌套。|Use absolute paths. Source must exist; output must be separate and new, empty or contain analysis artifacts only. Neither folder may contain the other.
目前已有分析正在執行，請等候完成。|当前已有分析正在执行，请等候完成。|An analysis is already running. Wait for it to finish.
工作階段已失效，請重新整理頁面。|会话已失效，请刷新页面。|Your session has expired. Reload the page.
請檢查文字編碼、源碼格式與副檔名設定。|请检查文本编码、源码格式与扩展名配置。|Check the encoding, source format and file extensions.
請重新接入源碼，再選取當次快照的引用。|请重新接入源码，再选择当前快照的引用。|Reconnect the source, then select a citation from the current snapshot.
請檢查輸入欄位及請求大小。|请检查输入字段及请求大小。|Check the input fields and request size.
讓業務邏輯，清晰可見。|让业务逻辑，清晰可见。|Make business logic clear.
從一個業務問題出發，沿著源碼找到可追溯的答案。|从一个业务问题出发，沿着源码找到可追溯的答案。|Start with a business question. Find answers you can trace to source.
先看清楚，分析了甚麼。|先看清楚，分析了什么。|Know what you are analysing.
程式、來源與讀取狀態，都有跡可尋。|程序、来源与读取状态，都有迹可循。|Identify each program, source file and reading status.
每一次分析，都有依據。|每一次分析，都有依据。|Keep a record of every analysis.
回看本次工作階段的問題、來源快照與結論。|回看本次会话的问题、来源快照与结论。|Review questions, source snapshots and findings from this session.
業務洞察工作台|业务洞察工作台|Business Insight Workspace
分析工作台|分析工作台|Workbench
源碼資料庫|源码资料库|Source library
分析記錄|分析记录|Analysis history
工作空間|工作空间|Workspace
主導覽|主导航|Main navigation
本次分析|本次分析|Current analysis
業務分析工作空間|业务分析工作空间|Business analysis workspace
本機工作階段|本机会话|Local session
業務洞察|业务洞察|Business insights
連線設定|连接设置|Connection settings
查看模型連線設定|查看模型连接设置|View model connection settings
正在檢查本機服務|正在检查本机服务|Checking local service
本機服務已啟動|本机服务已启动|Local service running
介面預覽 · 本機服務未啟動|界面预览 · 本机服务未启动|UI preview · Local service offline
源碼留在本機|源码保留在本机|Source stays local
僅在發起問答時，向已設定|仅在发起问答时，向已配置|Only questions and limited evidence
的接口傳送有限證據。|的接口传送有限证据。|are sent to your configured endpoint.
匯出摘要|导出摘要|Export summary
接入源碼|接入源码|Connect source
當前源碼範圍|当前源码范围|Current source scope
資料模式|数据模式|Data mode
本機源碼|本机源码|Local source
選擇源碼與結果資料夾，建立自己的分析範圍|选择源码与结果文件夹，建立自己的分析范围|Choose source and output folders to define your scope
尚未接入本機源碼|尚未接入本机源码|No local source connected
尚未建立快照|尚未建立快照|No snapshot yet
尚未建立業務問答|尚未建立业务问答|No business question yet
本機源碼 · 本次工作階段|本机源码 · 本次会话|Local source · Current session
接入源碼後開始|接入源码后开始|Connect source to begin
本機源碼 · 問題完整性仍需覆核|本机源码 · 问题完整性仍需复核|Local source · Question coverage needs review
更新中|更新中|Updating
等待接入|等待接入|Awaiting source
已核驗局部語句 · 保留邊界|已核验局部语句 · 保留边界|Local statements verified · Limits remain
引用已核對 · 解釋待核驗|引用已核对 · 解释待核验|Citations checked · Interpretation needs review
證據不足 · 尚未形成結論|证据不足 · 尚未形成结论|Insufficient evidence · No conclusion
尚未具備分析條件|尚未具备分析条件|Not ready for analysis
調查已停止 · 需要處理|调查已停止 · 需要处理|Investigation stopped · Action needed
尚未啟用模型問答|尚未启用模型问答|Model analysis not enabled
尚未提出業務問題|尚未提出业务问题|No business question yet
接入受阻|接入受阻|Source connection blocked
源碼已就緒|源码已就绪|Source ready
源碼已接入 · 有待核對|源码已接入 · 有待核对|Source connected · Review needed
正在接入源碼|正在接入源码|Connecting source
尚未分析|尚未分析|Not analysed
本次業務問題|本次业务问题|Business question
業務問題|业务问题|Business question
例如：哪些申請可以通過？狀態如何變化，失敗會有甚麼影響？|例如：哪些申请可以通过？状态如何变化，失败会有什么影响？|For example: which requests qualify, how does their status change, and what happens if they fail?
調查起點|调查起点|Starting program
從程式目錄探索|从程序目录探索|Explore the program catalog
開始業務分析|开始业务分析|Start analysis
發起分析將使用已設定的模型接口，傳送問題及有限源碼證據。|发起分析将使用已配置的模型接口，发送问题及有限源码证据。|Starting analysis sends your question and limited source evidence to the configured model endpoint.
請用業務語言說明這項業務從受理到完成的處理過程與結果。|请用业务语言说明这项业务从受理到完成的处理过程与结果。|Explain this business process from intake to completion, including its outcomes, in business terms.
哪些條件會允許或拒絕處理？會產生甚麼狀態與業務結果？|哪些条件会允许或拒绝处理？会产生什么状态与业务结果？|Which conditions allow or reject processing, and what statuses and business outcomes result?
哪些異常會中止、退回或延後處理？對目前業務與後續處理有甚麼影響？|哪些异常会中止、退回或延后处理？对当前业务与后续处理有什么影响？|Which exceptions stop, return or defer processing, and how do they affect the current business activity and subsequent processing?
准入與狀態|准入与状态|Eligibility and status
異常與影響|异常与影响|Exceptions and impact
分析檢視|分析视图|Analysis views
業務解讀|业务解读|Business findings
程式關係|程序关系|Program relationships
調查記錄|调查记录|Investigation log
查看證據|查看证据|View evidence
準備分析新的問題|准备分析新的问题|Ready for a new question
問題、調查起點或閱讀方式已更改。發起分析後，這裡會呈現新問題的結果。|问题、调查起点或阅读方式已更改。发起分析后，这里会呈现新问题的结果。|The question, starting program or reading settings have changed. Start analysis to see the new result here.
上一題摘要保留在分析記錄中。|上一题摘要保留在分析记录中。|The previous summary is kept in analysis history.
保留現有資料與阻斷原因。處理缺口後，可重新發起分析。|保留现有数据与阻断原因。处理缺口后，可重新发起分析。|Available evidence and blocking reasons are retained. Resolve the gaps, then try again.
源碼已就緒。選擇調查起點，輸入業務問題，即可從當前源碼開始。|源码已就绪。选择调查起点，输入业务问题，即可从当前源码开始。|Source is ready. Choose a starting program and enter a business question.
診斷代碼：|诊断代码：|Diagnostic code:
來自當前源碼的分析結果|来自当前源码的分析结果|Findings from the current source
以下陳述保留實際核驗狀態。點選引用，可回到本次快照的源碼位置。|以下陈述保留实际核验状态。点击引用，可回到本次快照的源码位置。|Each statement retains its verification status. Select a citation to inspect the source in this snapshot.
源碼事實|源码事实|Source fact
業務推測 · 未核驗|业务推测 · 未核验|Business inference · Unverified
待確認問題 · 未形成結論|待确认问题 · 未形成结论|Open question · No conclusion
候選解讀|候选解读|Candidate interpretation
局部語句已核驗|局部语句已核验|Local statement verified
引用已核對，語義待核驗|引用已核对，语义待核验|Citation checked; meaning needs review
證據尚不支持|证据尚不支持|Not supported by evidence
語義未核驗|语义未核验|Meaning unverified
問題相關性與完整性尚未核驗；局部語句通過不代表整條業務流程已驗證。|问题相关性与完整性尚未核验；局部语句通过不代表整条业务流程已验证。|Question relevance and coverage are not verified. A checked statement does not validate an entire business process.
待確認與分析邊界|待确认与分析边界|Open issues and limitations
沿著程式，回查關係。|沿着程序，回查关系。|Trace relationships between programs.
以下來自當前源碼的靜態呼叫與 COPY 關係，不表示執行順序或實際可達。|以下来自当前源码的静态调用与 COPY 关系，不表示执行顺序或实际可达。|These static call and COPY relationships come from the current source. They do not establish execution order or reachability.
關係數量已達顯示上限。|关系数量已达显示上限。|The relationship display limit has been reached.
引入定義|引入定义|Included definition
呼叫來源|调用来源|Caller
未解析目標|未解析目标|Unresolved target
源碼關係已確認|源码关系已确认|Source relationship confirmed
候選關係|候选关系|Candidate relationship
未能確認|未能确认|Unconfirmed
示例關係|示例关系|Sample relationship
本次沒有可展示的呼叫或 COPY 關係；不能據此推斷不存在依賴。|本次没有可展示的调用或 COPY 关系；不能据此推断不存在依赖。|No call or COPY relationships are available to display. This does not prove there are no dependencies.
目前顯示前 40 項關係。完整回傳資料可在匯出檔中查看。|目前显示前 40 项关系。完整返回数据可在导出文件中查看。|Showing the first 40 relationships. Export the data to view the full response.
新問題尚未執行調查|新问题尚未执行调查|The new question has not been investigated
問題、調查起點或閱讀方式已更改。發起分析後，這裡會顯示新問題的工具記錄。|问题、调查起点或阅读方式已更改。发起分析后，这里会显示新问题的工具记录。|The question, starting program or reading settings have changed. Start analysis to generate a new investigation log.
每一步，留下可核對的依據。|每一步，留下可核对的依据。|Every step leaves a record.
定位分析起點|定位分析起点|Locate the starting point
從程式目錄及名稱，選定本次要調查的範圍。|从程序目录及名称，选定本次要调查的范围。|Use the program catalog and names to select the investigation scope.
核對計算與依賴|核对计算与依赖|Check calculations and dependencies
檢視欄位讀寫及呼叫關係，收集相關源碼引用。|查看字段读写及调用关系，收集相关源码引用。|Inspect field reads, writes and calls, and collect source citations.
集中讀取證據|集中读取证据|Read the evidence
閱讀已發現的源碼片段，逐項綁定結論與未決事項。|阅读已发现的源码片段，逐项绑定结论与未决事项。|Read discovered source spans and link them to findings and open issues.
定位程式與欄位|定位程序与字段|Locate programs and fields
檢視定義與依賴|查看定义与依赖|Inspect definitions and dependencies
追蹤源碼關係|追踪源码关系|Trace source relationships
讀取源碼證據|读取源码证据|Read source evidence
本次調查記錄|本次调查记录|Current investigation log
只顯示實際工具活動與狀態，不包含模型私有推理。|只显示实际工具活动与状态，不包含模型私有推理。|Shows tool activity and status, without private model reasoning.
已執行|已执行|Executed
執行失敗|执行失败|Execution failed
參數未通過核對|参数未通过核对|Invalid parameters
調查範圍受限|调查范围受限|Scope restricted
快照核對失敗|快照核对失败|Snapshot check failed
已嘗試|已尝试|Attempted
狀態未提供|状态未提供|Status unavailable
工具活動|工具活动|Tool activity
查看工具資料|查看工具数据|View tool details
尚未執行模型調查。建立索引不會產生模型工具記錄。|尚未执行模型调查。建立索引不会产生模型工具记录。|No model investigation has run. Indexing does not generate a model tool log.
源碼證據|源码证据|Source evidence
非實際源碼驗收|非实际源码验收|Not a source acceptance result
本次回答引用|本次回答引用|Citations for this answer
點選檢視原文|点击查看原文|Select to view source
源碼引用|源码引用|Source citation
分析結論的引用|分析结论的引用|Citations supporting findings
將在這裡顯示。|将在这里显示。|will appear here.
尚未選擇證據|尚未选择证据|No evidence selected
源碼原文|源码原文|Original source
選擇一段引用，查看源碼內容。|选择一段引用，查看源码内容。|Select a citation to view its source.
從引用清單選擇|从引用清单选择|Select a citation
快照證據完整性有效|快照证据完整性有效|Snapshot evidence integrity valid
完整性未確認|完整性未确认|Integrity unconfirmed
已截斷|已截断|Truncated
讓結論可被覆核|让结论可被复核|Make findings reviewable
選擇本機源碼後，此面板會展示實際文件、行號與證據。|选择本机源码后，此面板会展示实际文件、行号与证据。|Connect local source to see actual files, line numbers and evidence here.
引用有效只表示證據可核對，不代表業務含義或所有執行路徑已獲證明。|引用有效只表示证据可核对，不代表业务含义或所有执行路径已获证明。|A valid citation makes evidence traceable; it does not prove business meaning or every execution path.
正在調查這次的業務問題|正在调查这次的业务问题|Investigating your question
正在接入你的源碼|正在接入你的源码|Connecting your source
請保持此頁開啟，完成後會自動更新。|请保持此页打开，完成后会自动更新。|Keep this page open. It will update when the analysis finishes.
你可以稍後回到分析記錄查看結果。|你可以稍后回到分析记录查看结果。|You can return to analysis history to review the result.
更新索引 → 檢查接口 → 調查源碼|更新索引 → 检查接口 → 调查源码|Update index → Check endpoint → Investigate source
讀取源碼 → 建立索引 → 核對範圍|读取源码 → 建立索引 → 核对范围|Read source → Build index → Check scope
從自己的源碼，開始。|从自己的源码，开始。|Start with your own source.
接入本機 COBOL 程式與 COPYBOOK，確認分析範圍，|接入本机 COBOL 程序与 COPYBOOK，确认分析范围，|Connect local COBOL programs and COPYBOOKs, confirm the scope,
再用業務語言提出問題。|再用业务语言提出问题。|then ask questions in business terms.
接入本機源碼|接入本机源码|Connect local source
已識別定義|已识别定义|Definition identified
分析程式|分析程序|Analyse program
待確認的程式與 COPY 依賴|待确认的程序与 COPY 依赖|Unconfirmed program and COPY dependencies
目標名稱|目标名称|Target name
來源檔案|来源文件|Source file
動態目標|动态目标|Dynamic target
目前顯示前 20 項；完整清單保留在匯出 JSON。|目前显示前 20 项；完整清单保留在导出 JSON 中。|Showing the first 20 items. The full list is available in the JSON export.
請補齊對應源碼；動態目標可能仍需設定資料確認。|请补齐对应源码；动态目标可能仍需配置数据确认。|Add the missing source. Dynamic targets may also require configuration data.
實際讀取檔案|实际读取文件|Files read
介面展示數據|界面展示数据|Illustrative data
程式定義|程序定义|Program definitions
依 PROGRAM-ID 識別|按 PROGRAM-ID 识别|Identified by PROGRAM-ID
COPYBOOK 定義|COPYBOOK 定义|COPYBOOK definitions
程式目錄|程序目录|Program catalog
搜尋程式名稱或路徑|搜索程序名称或路径|Search program names or paths
來源路徑|来源路径|Source path
定義行|定义行|Definition line
識別狀態|识别状态|Identification status
目前源碼快照|当前源码快照|Current source snapshot
源碼位置|源码位置|Source location
本次快照|本次快照|Current snapshot
未建立有效快照|未建立有效快照|No valid snapshot
非實際分析|非实际分析|Not a live analysis
本次工作階段|本次会话|Current session
源碼接入|源码接入|Source intake
查看摘要|查看摘要|View summary
尚未有分析記錄|尚未有分析记录|No analysis history yet
本次工作階段完成的分析會列在這裡；完整結果同時保存在你指定的資料夾。|本次会话完成的分析会列在这里；完整结果同时保存在你指定的文件夹。|Completed analyses from this session appear here. Full results are also saved in your output folder.
本機服務未回傳有效資料。請重新啟動服務。|本机服务未返回有效数据。请重新启动服务。|The local service returned invalid data. Restart the service.
本機服務暫時無法完成操作。|本机服务暂时无法完成操作。|The local service could not complete the operation.
請先以 python poc/web_app.py 啟動本機服務，再透過服務地址開啟此頁。|请先用 python poc/web_app.py 启动本机服务，再通过服务地址打开此页。|Start the local service with python poc/web_app.py, then open this page using its service address.
接入未完成，請檢查資料範圍及設定。|接入未完成，请检查数据范围及配置。|Source intake did not finish. Check the scope and settings.
源碼已更新，請重新分析後選擇引用。|源码已更新，请重新分析后选择引用。|Source has changed. Run analysis again before selecting a citation.
這段證據未通過快照完整性核對，暫不展示原文。|这段证据未通过快照完整性核对，暂不显示原文。|This evidence failed the snapshot integrity check. Source text cannot be shown.
請解釋 |请解释 |Explain
已匯出該次摘要；目前源碼範圍保持不變。|已导出该次摘要；当前源码范围保持不变。|Summary exported. The current source scope is unchanged.
請先輸入一個業務問題。|请先输入一个业务问题。|Enter a business question first.
請先成功接入本機源碼。|请先成功接入本机源码。|Connect local source successfully before starting analysis.
接口設定已提供，發起分析時會使用已設定的接口生成業務解讀。|接口配置已提供，发起分析时会使用已配置的接口生成业务解读。|Endpoint settings are available. Starting analysis uses the configured endpoint to generate a business explanation.
源碼索引可離線使用。模型問答需要先在項目根目錄的 .env 設定接口。|源码索引可离线使用。模型问答需要先在项目根目录的 .env 配置接口。|Source indexing works offline. Configure the model endpoint in .env at the project root to enable analysis.
已啟動|已启动|Running
尚未啟動|尚未启动|Not running
已提供 · 待實際驗證|已提供 · 待实际验证|Configured · Verification pending
尚未提供|尚未提供|Not configured
已匯出摘要，包含資料模式與分析邊界。|已导出摘要，包含数据模式与分析边界。|Summary exported with its data mode and analysis limitations.
已匯出本次資料。|已导出本次数据。|Current data exported.
選定源碼範圍，先核對讀取結果，再開始業務分析。|选定源码范围，先核对读取结果，再开始业务分析。|Choose your source scope and review the intake results before starting analysis.
指定資料|指定数据|Choose source
核對程式|核对程序|Review programs
開始分析|开始分析|Start analysis
源碼資料夾|源码文件夹|Source folder
在檔案總管複製完整路徑；可保留程式、COPYBOOK 的子資料夾。|在文件管理器复制完整路径；可保留程序、COPYBOOK 的子文件夹。|Copy the full folder path from your file manager. Keep program and COPYBOOK subfolders.
分析結果資料夾|分析结果文件夹|Output folder
與源碼分開儲存，供本次及日後更新使用。|与源码分开存储，供本次及日后更新使用。|Use a separate folder for this analysis and future updates.
文字編碼|文本编码|Text encoding
自動偵測|自动检测|Auto-detect
繁體中文 · CP950|繁体中文 · CP950|Traditional Chinese · CP950
簡體中文 · GB18030|简体中文 · GB18030|Simplified Chinese · GB18030
源碼格式|源码格式|Source format
固定格式 · Fixed|固定格式 · Fixed|Fixed format
自由格式 · Free|自由格式 · Free|Free format
進階讀取選項|高级读取选项|Advanced intake options
自訂副檔名（選填）|自定义扩展名（选填）|Custom extensions (optional)
填寫後取代預設副檔名清單。無副檔名成員預設納入。|填写后替换默认扩展名清单。无扩展名成员默认包含。|Overrides the default extension list. Extensionless members are included by default.
此步驟只在本機建立索引，不呼叫模型。|此步骤只在本机建立索引，不调用模型。|This step builds a local index without calling a model.
建立源碼索引|建立源码索引|Build source index
取消|取消|Cancel
關閉|关闭|Close
模型連線設定|模型连接设置|Model connection settings
設定需修正|配置需修正|Settings need attention
尚未填寫接口地址。請在 .env 設定 COMPANY_API_BASE_URL。|尚未填写接口地址。请在 .env 配置 COMPANY_API_BASE_URL。|The endpoint address is missing. Set COMPANY_API_BASE_URL in .env.
尚未填寫模型。請在 .env 設定 COMPANY_CHAT_MODEL。|尚未填写模型。请在 .env 配置 COMPANY_CHAT_MODEL。|The model is missing. Set COMPANY_CHAT_MODEL in .env.
尚未填寫密鑰。請在 .env 設定 COMPANY_API_KEY。|尚未填写密钥。请在 .env 配置 COMPANY_API_KEY。|The key is missing. Set COMPANY_API_KEY in .env.
接口地址格式無效。請核對 COMPANY_API_BASE_URL 與接口要求的路徑。|接口地址格式无效。请核对 COMPANY_API_BASE_URL 与接口要求的路径。|The endpoint address is invalid. Check COMPANY_API_BASE_URL and the path required by your endpoint.
接口風格不受支援。請核對 COMPANY_API_STYLE；預設為 openai_compatible。|接口风格不受支持。请核对 COMPANY_API_STYLE；默认为 openai_compatible。|The endpoint style is unsupported. Check COMPANY_API_STYLE; the default is openai_compatible.
.env 格式無效。請使用 UTF-8 文字，每行填寫一個「變數名稱=值」，並核對引號。|.env 格式无效。请使用 UTF-8 文本，每行填写一个“变量名称=值”，并核对引号。|The .env format is invalid. Use UTF-8 text with one NAME=value per line, and check the quotes.
本機服務無法讀取 .env。請檢查檔案是否可由目前使用者讀取。|本机服务无法读取 .env。请检查文件是否可由当前用户读取。|The local service cannot read .env. Check that the current user has permission to read the file.
.env 檔案過大。請只保留接口設定，不要把源碼或其他內容貼入檔案。|.env 文件过大。请只保留接口配置，不要把源码或其他内容贴入文件。|The .env file is too large. Keep only endpoint settings; remove any source code or unrelated content.
接口配置無效。請核對 .env 與啟動服務時的環境變數，修改後重新啟動。|接口配置无效。请核对 .env 与启动服务时的环境变量，修改后重新启动。|The endpoint configuration is invalid. Check .env and the service environment variables, then restart.
本機源碼服務|本机源码服务|Local source service
模型接口設定|模型接口配置|Model endpoint settings
API Key 儲存位置|API Key 存储位置|API key location
項目根目錄 .env 或服務環境變數|项目根目录 .env 或服务环境变量|Project-root .env or service environment
首次使用：將項目根目錄的 .env.example 複製為 .env，填入以下三項並儲存。之後雙擊 poc/run_web.bat 即可啟動，無需每次重新輸入。|首次使用：将项目根目录的 .env.example 复制为 .env，填入以下三项并保存。之后双击 poc/run_web.bat 即可启动，无需每次重新输入。|First use: copy .env.example to .env in the project root, fill in these three settings and save. Then double-click poc/run_web.bat to start; no need to enter them again.
修改 .env 後請重新啟動服務。既有環境變數會優先於 .env。Key 只由本機服務讀取，不回傳或儲存於瀏覽器；請保留本機 .env，不要上傳至 GitHub。|修改 .env 后请重新启动服务。已有环境变量会优先于 .env。Key 只由本机服务读取，不返回或存储于浏览器；请保留本机 .env，不要上传至 GitHub。|Restart the service after editing .env. Existing environment variables override .env. Only the local service reads your key; it is never returned to or stored in the browser. Keep .env locally and out of GitHub.
已提供設定不代表連線驗證通過。開始分析後，可在「API 返回」查看實際請求結果。|已提供配置不代表连接验证通过。开始分析后，可在“API 返回”查看实际请求结果。|Configured settings do not mean the connection is verified. After starting analysis, view actual request results in API response.
未連線|未连接|Disconnected
未提供|未提供|Not configured
明白|明白|Got it
匯出分析摘要|导出分析摘要|Export analysis summary
摘要會保留資料模式、來源快照、引用和待確認事項。|摘要会保留数据模式、来源快照、引用和待确认事项。|The summary includes data mode, source snapshot, citations and open issues.
可閱讀摘要|可阅读摘要|Readable summary
Markdown · 適合審閱及分享|Markdown · 适合审阅及分享|Markdown · For review and sharing
完整分析資料|完整分析数据|Full analysis data
JSON · 保留本次診斷與分析結果|JSON · 保留本次诊断与分析结果|JSON · Includes diagnostics and results
本內容用於展示回答、關係與引用的介面，不代表公司業務結果。|本内容用于展示回答、关系与引用的界面，不代表公司业务结果。|This demonstrates findings, relationships and citations. It is not a company business result.
業務分析摘要|业务分析摘要|Business analysis summary
匯出時間：|导出时间：|Exported at:
接入狀態：|接入状态：|Intake status:
問答狀態：|问答状态：|Answer status:
尚未提出問題|尚未提出问题|No question submitted
本次沒有生成業務答案。|本次没有生成业务答案。|No business answer was generated.
本次引用索引|本次引用索引|Citation index
本次沒有回答引用。|本次没有回答引用。|No answer citations are available.
接入診斷|接入诊断|Intake diagnostics
問題相關性與完整性尚未核驗。完整診斷與分析邊界可一併匯出為 JSON。|问题相关性与完整性尚未核验。完整诊断与分析边界可一并导出为 JSON。|Question relevance and coverage are unverified. Export JSON for full diagnostics and limitations.
資料模式：|数据模式：|Data mode:
來源：|来源：|Source:
調查起點：|调查起点：|Starting program:
快照：|快照：|Snapshot:
 個候選檔案未能讀取| 个候选文件未能读取| candidate files could not be read
 個候選檔案| 个候选文件| candidate files
 個程式定義| 个程序定义| program definitions
 個檔案| 个文件| files
 個 COPYBOOK| 个 COPYBOOK| COPYBOOKs
 段引用| 段引用| citations
 項| 项| items
快照 |快照 |Snapshot\u0020
關係|关系|Relationship
來源|来源|Source
待確認|待确认|Open issues
未建立|未建立|Not created
首頁|首页|Home
`.trim().split('\n').filter(Boolean).map(line => line.split('|'));
const SUPPORTED_LOCALES = ['zh-CN', 'zh-HK', 'en'];
const LOCALE_STORAGE_KEY = 'cobol-lens.locale';
let currentLocale = 'zh-HK';
try {
  const saved = localStorage.getItem(LOCALE_STORAGE_KEY);
  if (SUPPORTED_LOCALES.includes(saved)) currentLocale = saved;
} catch { /* Storage can be disabled by the browser. */ }
const messagePattern = new RegExp(UI_MESSAGES.map(row => row[0]).sort((a,b)=>b.length-a.length).map(key=>key.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('|'),'g');
const messageCatalog = new Map(UI_MESSAGES.map(row=>[row[0],row]));
function t(text) {
  if (currentLocale === 'zh-HK') return String(text);
  const column = currentLocale === 'zh-CN' ? 1 : 2;
  return String(text).replace(messagePattern, key => messageCatalog.get(key)[column]);
}
function ui(parts, ...values) {
  return parts.reduce((text, part, index) => text + t(part) + (index < values.length ? values[index] : ''), '');
}
// Demo guide text is authored in the fixture manifest. Source excerpts and
// analysis responses never pass through this locale selector.
function frameworkDemoText(value){
  if(typeof value==='string')return value;
  if(!value || typeof value!=='object')return '';
  return [value[currentLocale],value['zh-HK'],value['zh-CN'],value.en].find(text=>typeof text==='string') || '';
}
// Capture only the initial, authored HTML before the application inserts data.
const staticText = [];
const staticAttributes = [];
const textWalker = document.createTreeWalker(document.documentElement, NodeFilter.SHOW_TEXT);
while (textWalker.nextNode()) {
  const node = textWalker.currentNode;
  if (!node.parentElement?.closest('script,style,[data-language-names]') && /[\u3400-\u9fff]/.test(node.data)) staticText.push([node,node.data]);
}
for (const element of document.querySelectorAll('[aria-label],[placeholder],[title]')) {
  for (const name of ['aria-label','placeholder','title']) {
    const value = element.getAttribute(name);
    if (value && /[\u3400-\u9fff]/.test(value)) staticAttributes.push([element,name,value]);
  }
}
function translateStaticInterface() {
  document.documentElement.lang = currentLocale === 'zh-HK' ? 'zh-Hant-HK' : currentLocale === 'zh-CN' ? 'zh-Hans' : 'en';
  document.title = 'COBOL Lens · ' + t('業務洞察工作台');
  for (const [node,original] of staticText) if (node.isConnected) node.data = t(original);
  for (const [node,attribute,original] of staticAttributes) if (node.isConnected) node.setAttribute(attribute,t(original));
  document.getElementById('language-select').value = currentLocale;
}
function selectInterfaceLocale(locale) {
  if (!SUPPORTED_LOCALES.includes(locale)) return false;
  currentLocale = locale;
  try { localStorage.setItem(LOCALE_STORAGE_KEY,locale); } catch { /* Keep the selection for this page. */ }
  translateStaticInterface();
  return true;
}
translateStaticInterface();
