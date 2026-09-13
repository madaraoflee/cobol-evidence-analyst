'use strict';

// Translate authored interface text only. Template values (source, questions,
// model output and evidence) are never sent through the translation catalog.
const UI_MESSAGES = `
請使用完整絕對路徑：源碼目錄須存在；輸出須獨立且為空、新目錄或只含分析產物。兩者不可相互嵌套。|请使用完整绝对路径：源码目录须存在；输出须独立且为空、新目录或只含分析产物。两者不可相互嵌套。|Use absolute paths. Source must exist; output must be separate and new, empty or contain analysis artifacts only. Neither folder may contain the other.
目前已有分析正在執行，請等候完成。|当前已有分析正在执行，请等候完成。|An analysis is already running. Wait for it to finish.
工作階段已失效，請重新整理頁面。|会话已失效，请刷新页面。|Your session has expired. Reload the page.
請檢查文字編碼、源碼格式與副檔名設定。|请检查文本编码、源码格式与扩展名配置。|Check the encoding, source format and file extensions.
請重新接入源碼，再選取當次快照的引用。|请重新接入源码，再选择当前快照的引用。|Reconnect the source, then select a citation from the current snapshot.
請檢查輸入欄位及請求大小。|请检查输入字段及请求大小。|Check the input fields and request size.

讓業務邏輯，清晰可見。|让业务逻辑，清晰可见。|Make business logic clear.
從一個業務問題出發，沿著源碼找到可追溯的答案。|从一个业务问题出发，沿着源码找到可追溯的答案。|Start with a business question. Find answers you can trace to source.
先看清楚，分析了甚麼。|先看清楚，分析了什么。|Know what you are analysing.
程式、來源與版本，都有跡可尋。|程序、来源与版本，都有迹可循。|Identify every program, source file and version.
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
保費計算 · 合成示例|保费计算 · 合成示例|Premium calculation · Sample
示例資料|示例数据|Sample data
資料模式|数据模式|Data mode
示例預覽|示例预览|Sample preview
本機源碼|本机源码|Local source
介面示例 · 非實際分析|界面示例 · 非实际分析|UI sample · Not a live analysis
選擇源碼與結果資料夾，建立自己的分析範圍|选择源码与结果文件夹，建立自己的分析范围|Choose source and output folders to define your scope
尚未接入本機源碼|尚未接入本机源码|No local source connected
尚未建立快照|尚未建立快照|No snapshot yet
尚未建立業務問答|尚未建立业务问答|No business question yet
合成示例 · 介面預覽|合成示例 · 界面预览|Synthetic sample · UI preview
本機源碼 · 本次工作階段|本机源码 · 本次会话|Local source · Current session
接入源碼後開始|接入源码后开始|Connect source to begin
介面示例 · 不代表公司業務結果|界面示例 · 不代表公司业务结果|UI sample · Not a company business finding
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
例如：這個程式如何處理輸入、計算與異常？|例如：这个程序如何处理输入、计算与异常？|For example: how does this program handle inputs, calculations and errors?
調查起點|调查起点|Starting program
從程式目錄探索|从程序目录探索|Explore the program catalog
預覽示例解讀|预览示例解读|Preview sample
開始業務分析|开始业务分析|Start analysis
發起分析將使用已設定的模型接口，傳送問題及有限源碼證據。|发起分析将使用已配置的模型接口，发送问题及有限源码证据。|Starting analysis sends your question and limited source evidence to the configured model endpoint.
請解釋這個程式的主要處理步驟和輸入輸出。|请解释这个程序的主要处理步骤和输入输出。|Explain this program’s main processing steps, inputs and outputs.
哪些程式會被呼叫？請引用對應的源碼。|哪些程序会被调用？请引用对应的源码。|Which programs are called? Cite the relevant source.
發生錯誤時，狀態如何傳回呼叫方？|发生错误时，状态如何返回调用方？|When an error occurs, how is its status returned to the caller?
主要處理流程|主要处理流程|Main process
呼叫與依賴|调用与依赖|Calls and dependencies
異常處理|异常处理|Error handling
分析檢視|分析视图|Analysis views
業務解讀|业务解读|Business findings
程式關係|程序关系|Program relationships
調查記錄|调查记录|Investigation log
查看證據|查看证据|View evidence
分期保費是如何計算出來的？|分期保费是如何计算出来的？|How is the instalment premium calculated?
分期保費計算邏輯|分期保费计算逻辑|Instalment premium calculation
分期保費計算|分期保费计算|Instalment premium formula
年繳保費組成|年缴保费组成|Annual premium components
繳費係數來源|缴费系数来源|Payment factor source
解讀示例 · 未執行模型分析|解读示例 · 未执行模型分析|Sample findings · No model analysis performed
先計算年繳保費，|先计算年缴保费，|Calculate the annual premium,
再套用繳費係數。|再应用缴费系数。|then apply the payment factor.
這個示例展示如何把分散在程式中的計算步驟，整理成可逐項回查的業務解讀。|这个示例展示如何把分散在程序中的计算步骤，整理成可逐项回查的业务解读。|This sample turns calculations spread across programs into business findings that can be checked individually.
計算摘要|计算摘要|Calculation summary
分期保費|分期保费|Instalment premium
年繳保費|年缴保费|Annual premium
繳費係數|缴费系数|Payment factor
彙總年繳保費|汇总年缴保费|Build the annual premium
將基本保費及附加保障保費加總，再扣除適用折扣，形成計算基礎。|将基本保费及附加保障保费相加，再扣除适用折扣，形成计算基础。|Add the base and rider premiums, then subtract applicable discounts to form the calculation base.
取得對應的繳費係數|取得对应的缴费系数|Find the applicable payment factor
以繳費方式查詢係數。源碼顯示查詢來源；實際設定值需要配置資料確認。|按缴费方式查询系数。源码显示查询来源；实际配置值需要配置数据确认。|Look up the factor by payment mode. The source identifies the lookup; configuration data is needed to confirm its value.
計算並回傳分期保費|计算并返回分期保费|Calculate and return the instalment premium
年繳保費乘以繳費係數，使用 ROUNDED，並將結果寫入輸出欄位。|年缴保费乘以缴费系数，使用 ROUNDED，并将结果写入输出字段。|Multiply the annual premium by the payment factor using ROUNDED, then write the result to the output field.
尚待確認|尚待确认|Still to confirm
實際係數、折扣適用條件與完整異常路徑，需要同版本配置及進一步源碼核對。|实际系数、折扣适用条件与完整异常路径，需要同版本配置及进一步源码核对。|Actual factors, discount conditions and complete error paths need matching configuration and further source review.
處理流程概覽|处理流程概览|Process overview
示例路徑|示例路径|Sample path
接收請求|接收请求|Receive request
計算年繳保費|计算年缴保费|Calculate annual premium
取得繳費係數|取得缴费系数|Get payment factor
回傳結果|返回结果|Return result
以上為介面設計示例；本機模式只呈現實際分析結果。|以上为界面设计示例；本机模式只呈现实际分析结果。|This is a UI sample. Local mode displays results from your analysis only.
準備分析新的問題|准备分析新的问题|Ready for a new question
問題或調查起點已更改。發起分析後，這裡會呈現新問題的結果。|问题或调查起点已更改。发起分析后，这里会呈现新问题的结果。|The question or starting program has changed. Start analysis to see the new result here.
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
此處是預先編排的關係示例。|此处是预先编排的关系示例。|These relationships are illustrative samples.
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
問題或調查起點已更改。發起分析後，這裡會顯示新問題的工具記錄。|问题或调查起点已更改。发起分析后，这里会显示新问题的工具记录。|The question or starting program has changed. Start analysis to generate a new investigation log.
每一步，留下可核對的依據。|每一步，留下可核对的依据。|Every step leaves a record.
以下是介面呈現方式示例，並非已執行的模型調查記錄。|以下是界面呈现方式示例，并非已执行的模型调查记录。|This demonstrates the interface; it is not a log of an actual model investigation.
定位分析起點|定位分析起点|Locate the starting point
從程式目錄及名稱，選定本次要調查的範圍。|从程序目录及名称，选定本次要调查的范围。|Use the program catalog and names to select the investigation scope.
核對計算與依賴|核对计算与依赖|Check calculations and dependencies
檢視欄位讀寫及呼叫關係，收集相關源碼引用。|查看字段读写及调用关系，收集相关源码引用。|Inspect field reads, writes and calls, and collect source citations.
集中讀取證據|集中读取证据|Read the evidence
閱讀已發現的源碼片段，逐項綁定結論與未決事項。|阅读已发现的源码片段，逐项绑定结论与未决事项。|Read discovered source spans and link them to findings and open issues.
示例步驟|示例步骤|Sample step
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
示例引用|示例引用|Sample citations
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
介面示例|界面示例|UI sample
快照證據完整性有效|快照证据完整性有效|Snapshot evidence integrity valid
完整性未確認|完整性未确认|Integrity unconfirmed
示例內容|示例内容|Sample content
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
先查看介面示例|先查看界面示例|Explore the sample first
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
合成示例|合成示例|Synthetic sample
目前源碼快照|当前源码快照|Current source snapshot
源碼位置|源码位置|Source location
本次快照|本次快照|Current snapshot
未建立有效快照|未建立有效快照|No valid snapshot
介面示例記錄|界面示例记录|Sample history
非實際分析|非实际分析|Not a live analysis
預先編排的介面內容 · 3 段示例引用|预先编排的界面内容 · 3 段示例引用|Prebuilt UI sample · 3 illustrative citations
查看示例|查看示例|View sample
本次工作階段|本次会话|Current session
源碼接入|源码接入|Source intake
查看摘要|查看摘要|View summary
尚未有分析記錄|尚未有分析记录|No analysis history yet
本次工作階段完成的分析會列在這裡；完整結果同時保存在你指定的資料夾。|本次会话完成的分析会列在这里；完整结果同时保存在你指定的文件夹。|Completed analyses from this session appear here. Full results are also saved in your output folder.
本機服務未回傳有效資料。請重新啟動服務。|本机服务未返回有效数据。请重新启动服务。|The local service returned invalid data. Restart the service.
本機服務暫時無法完成操作。|本机服务暂时无法完成操作。|The local service could not complete the operation.
請先以 python poc/web_app.py 啟動本機服務，再透過服務地址開啟此頁。|请先用 python poc/web_app.py 启动本机服务，再通过服务地址打开此页。|Start the local service with python poc/web_app.py, then open this page using its service address.
接入未完成，請檢查資料範圍及設定。|接入未完成，请检查数据范围及配置。|Source intake did not finish. Check the scope and settings.
源碼快照已更新，請重新選擇本次引用。|源码快照已更新，请重新选择本次引用。|The source snapshot changed. Select a citation from the current result.
這段證據未通過快照完整性核對，暫不展示原文。|这段证据未通过快照完整性核对，暂不显示原文。|This evidence failed the snapshot integrity check. Source text cannot be shown.
這是介面示例。請接入本機源碼，才能調查自己的問題。|这是界面示例。请接入本机源码，才能调查自己的问题。|This is a UI sample. Connect local source to investigate your own questions.
請解釋 |请解释 |Explain
 的主要處理步驟、輸入輸出及呼叫。| 的主要处理步骤、输入输出及调用。|: main processing steps, inputs, outputs and calls.
已匯出該次摘要；目前源碼範圍保持不變。|已导出该次摘要；当前源码范围保持不变。|Summary exported. The current source scope is unchanged.
示例只展示預先編排的保費問題。接入本機源碼後，可分析自己的程式。|示例只展示预先编排的保费问题。接入本机源码后，可分析自己的程序。|The sample covers a predefined premium question. Connect local source to analyse your own programs.
目前為介面示例，未發起模型請求。|当前为界面示例，未发起模型请求。|This is a UI sample. No model request was sent.
請先輸入一個業務問題。|请先输入一个业务问题。|Enter a business question first.
請先成功接入本機源碼。|请先成功接入本机源码。|Connect local source successfully before starting analysis.
接口設定已提供，發起問答時會進行實際能力探測。|接口配置已提供，发起问答时会进行实际能力检测。|Endpoint settings are available. Capabilities will be checked when you start analysis.
源碼索引可離線使用。模型問答需要啟動服務時提供接口設定。|源码索引可离线使用。模型问答需要启动服务时提供接口配置。|Source indexing works offline. Model analysis requires endpoint settings when starting the service.
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
本機源碼服務|本机源码服务|Local source service
模型接口設定|模型接口配置|Model endpoint settings
API Key 儲存位置|API Key 存储位置|API key location
啟動服務的環境變數|启动服务的环境变量|Service environment variables
在啟動服務的終端配置以下變數後重新啟動。頁面不讀取或保存 Key。|在启动服务的终端配置以下变量后重新启动。页面不读取或保存 Key。|Set these variables in the service terminal, then restart. The page does not read or store your key.
已提供設定不代表連線驗證通過。每次發起問答時，系統會檢查接口所需能力。|已提供配置不代表连接验证通过。每次发起问答时，系统会检查接口所需能力。|Configured settings do not mean the connection is verified. Required capabilities are checked for each analysis.
未連線|未连接|Disconnected
未提供|未提供|Not configured
明白|明白|Got it
匯出分析摘要|导出分析摘要|Export analysis summary
摘要會保留資料模式、來源快照、引用和待確認事項。|摘要会保留数据模式、来源快照、引用和待确认事项。|The summary includes data mode, source snapshot, citations and open issues.
可閱讀摘要|可阅读摘要|Readable summary
Markdown · 適合審閱及分享|Markdown · 适合审阅及分享|Markdown · For review and sharing
完整分析資料|完整分析数据|Full analysis data
JSON · 保留本次診斷與分析結果|JSON · 保留本次诊断与分析结果|JSON · Includes diagnostics and results
資料模式：預先編排的合成示例，未執行模型分析。|数据模式：预先编排的合成示例，未执行模型分析。|Data mode: prebuilt synthetic sample; no model analysis performed.
本內容用於展示回答、關係與引用的介面，不代表公司業務結果。|本内容用于展示回答、关系与引用的界面，不代表公司业务结果。|This demonstrates findings, relationships and citations. It is not a company business result.
實際係數、折扣適用條件與完整異常路徑需額外資料核對。|实际系数、折扣适用条件与完整异常路径需额外数据核对。|Actual factors, discount conditions and full error paths need additional evidence.
本機源碼分析|本机源码分析|Local source analysis
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
 段示例證據| 段示例证据| sample evidence spans
 段引用| 段引用| citations
 項| 项| items
快照 |快照 |Snapshot
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
