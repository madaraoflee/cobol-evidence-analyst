"""Local, source-aware question plans; model suggestions remain candidates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from statement_facts import data_references
from repository_identity import mask_source_references


PLAN_VERSION = "business-analysis-plan/v10"
_ACCUMULATION = re.compile(
    r"累加|累[计計积積]|加总|加總|"
    r"(?<![A-Za-z0-9_$#@-])(?:accumulat(?:e[ds]?|ing|ion)|running\s+totals?)(?![A-Za-z0-9_$#@-])|"
    r"(?<![A-Za-z0-9_$#@-])add(?:ed|ing)?\s+[^?!。；;\n]{1,80}\s+(?:to|into)\b", re.I)


def accumulation_requested(question):
    """Nominate an explicit operation, never infer it from an identifier."""
    text = str(question)
    explicit_method = re.search(r"(?:如何|怎么|怎麼|怎样|怎樣).{0,30}(?:累加|累[计計积積]|更新|计算|計算)|"
                                r"\bhow\b.{0,60}\b(?:accumulat\w*|add|update[ds]?)\b", text, re.I)
    if (re.search(r"为什么|為什麼|\bwhy\b", text, re.I) and not explicit_method
            and re.search(r"(?:没有|沒有|没|沒|未|不).{0,12}(?:累加|累[计計积積])|"
                          r"\b(?:not|never|doesn't|didn't|isn't)\b.{0,40}\baccumulat\w*", text, re.I)):
        return False
    if (re.search(r"(?<![A-Za-z0-9_$#@-])(?:PIC|PICTURE)(?![A-Za-z0-9_$#@-])|"
                  r"类型|類型|长度|長度|\b(?:data\s+type|length|initial\s+value)\b", text, re.I)
            and not explicit_method):
        return False
    return bool(_ACCUMULATION.search(text))


_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_$#@-])[A-Z0-9_$#@-]+(?:\s+(?:OF|IN)\s+[A-Z0-9_$#@-]+)*(?:\([^()\r\n]*\)(?:\s+(?:OF|IN)\s+[A-Z0-9_$#@-]+)*)?", re.I)
_HINTS = {
    "origin": r"从哪里|從哪裡|来源|來源|哪里来的|哪裡來的|\b(?:origin|source\s+of|where.+from)\b",
    "overwrite": r"最后|最後|变成|變成|清零|归零|歸零|没有生效|沒有生效|覆盖|覆蓋|\b(?:overwrit|reset|final|not.+effect)",
    "impact": r"影响|影響|修改|变更|變更|\b(?:impact|affect|change)\b",
    "condition": r"为什么|為什麼|条件|條件|情况|情況|\b(?:why|condition|case|branch)\b",
    "exception": r"异常|異常|错误|錯誤|失败|失敗|\b(?:exception|error|fail)\b",
    "calculation": r"计算|計算|公式|算式|怎么算|怎麼算|\b(?:calculate|calculates|calculated|calculating|calculation|calculations|compute|computes|computed|computing|formula|formulas)\b",
    "flow": r"流程|逻辑|邏輯|处理|處理|\b(?:flow|logic|process)\b",
    "activation": r"触发|觸發|启动|啟動|调度|調度|批处理|批處理|(?<![A-Za-z0-9_-])(?:trigger(?:ed|ing)?|batch|schedul(?:e|ed|ing))(?![A-Za-z0-9_-])",
}
_UPSTREAM_ACTIVATION = re.compile(
    r"批处理|批處理|调度|調度|启动|啟動|入口|由谁|由誰|谁调用|誰調用|"
    r"(?:怎么|怎麼|如何|怎样|怎樣|由谁|由誰|谁|誰|how).{0,16}(?:触发|觸發|trigger)|"
    r"(?<![A-Za-z0-9_-])(?:batch|schedul(?:e|ed|ing)|launch(?:ed|ing)?|entry[ -]?point)(?![A-Za-z0-9_-])", re.I)
_BATCH_NOUN = re.compile(r"批处理|批處理|(?<![A-Za-z0-9_-])batch(?![A-Za-z0-9_-])", re.I)
_RUNTIME_ACTIVATION = re.compile(
    r"启动|啟動|调度|調度|排程|"
    r"(?:发起者|發起者).{0,6}(?:谁|誰)|"
    r"(?:何时|何時|什么时候|什麼時候|幾點|几点|谁|誰|何人|多久|频率|頻率)"
    r".{0,20}(?:启动|啟動|触发|觸發|执行|執行|运行|運行)|"
    r"(?:实际|實際|运行时|運行時).{0,12}(?:启动|啟動|触发|觸發|执行|執行|运行|運行)|"
    r"(?:怎么|怎麼|如何|怎样|怎樣|由谁|由誰|谁|誰|how).{0,16}(?:触发|觸發|trigger)|"
    r"(?<![A-Za-z0-9_-])(?:schedul\w*|cron|launch\w*)(?![A-Za-z0-9_-])|"
    r"\b(?:when|who|what\s+time|how\s+often)\s+.{0,80}\b(?:run|start|launch|execut|trigger|initiat)\w*\b|"
    r"\bruntime\s+(?:execution|scheduling|launch)\b", re.I)
_CONCEPT_QUESTION = re.compile(
    r"^(?:(?:请问|請問|请|請|帮我|幫我|麻烦|麻煩)\s*)*"
    r"(?:(?:什么|什麼|甚麼)是\s*\S|"
    r"(?:解释|解釋|说明|說明|介绍|介紹|讲解|講解)(?:一下)?\s*\S)|"
    r"^[^\n。？！?！；;]{1,120}(?:是什么|是什麼|是甚麼)(?:意思)?[\s。？！?！]*$|"
    r"^(?:please\s+)?(?:what\s+(?:is|are)\s+\S|explain\s+(?:to\s+me\s+)?\S|"
    r"tell\s+me\s+about\s+\S)", re.I)
_NARROW_CONCEPT = re.compile(
    r"翻译|翻譯|译成|譯成|中文怎么说|中文怎麼說|"
    r"字段|欄位|栏位|变量|變量|参数|參數|返回值|返回码|返回碼|错误码|錯誤碼|"
    r"这(?:一)?(?:行|句)|這(?:一)?(?:行|句)|语句|語句|语法|語法|段落|"
    r"异常|異常|失败|失敗|初值|初始值|默认值|默認值|类型|類型|长度|長度|"
    r"规则|規則|公式|算式|"
    r"(?:哪个|哪個|哪些|哪裡|哪里|是否|为何|為何|为什么|為什麼|如何|怎么|怎麼|怎样|怎樣)|"
    r"(?:只|仅|僅)(?:需|要|想)?(?:解释|解釋|说明|說明|知道|了解)|"
    r"(?:的|之)(?:输入|輸入|输出|輸出|值|数值|數值|定义|定義|声明|聲明|逻辑|邏輯|规则|規則|公式|条件|條件)|"
    r"\b(?:translat\w*|fields?|variables?|parameters?|return\s+(?:values?|codes?)|"
    r"error\s+codes?|exceptions?|rules?|formulas?|statements?|syntax|paragraphs?|this\s+line|"
    r"data\s+types?|type\s+of|length|initial\s+value|value\s+of|"
    r"default\s+value|which|whether|where|why|how|when|only|just|"
    r"input\s+(?:of|to)|output\s+(?:of|from))\b", re.I)
_TECHNICAL_CONCEPT = re.compile(
    r"(?<![A-Za-z0-9_$#@-])(?:PIC(?:TURE)?|COMP(?:-[1-5])?|REDEFINES|OCCURS|"
    r"MOVE|COMPUTE|PERFORM|GOBACK|GO\s+TO|STOP\s+RUN|COPY|SQLCODE|"
    r"CALL|READ|WRITE|REWRITE|OPEN|CLOSE|DISPLAY|IF|EVALUATE|EXEC\s+SQL)"
    r"[\s。？！?！]*$", re.I)
_MANUAL_DEFINITION = re.compile(
    r"(?:手册|手冊|文档|文檔).{0,100}(?:含义|含義|意思|定义|定義|是什么|是什麼)|"
    r"(?:含义|含義|意思|定义|定義).{0,100}(?:手册|手冊|文档|文檔)|"
    r"\b(?:manual|documentation|guide)\b.{0,100}\b(?:meaning|definition)\b|"
    r"\b(?:meaning|definition)\b.{0,100}\b(?:manual|documentation|guide)\b|"
    r"\b(?:in|according\s+to)\s+(?:the\s+)?(?:framework\s+)?"
    r"(?:manual|documentation|guide)\s*[.!?]*$", re.I)


def business_concept_requested(question, *, field_entities=(), source_scopes=()):
    """Recognize a request to understand a subject, without naming domains.

    A bound data reference or exclusive procedure retains its focused investigation. Otherwise
    plain concept questions nominate a source-based explanation, not a claim
    that the subject is implemented or that all program roles are applicable.
    """
    text = str(question).strip()
    exclusive_scope = any(scope.get("exclusive") is not False for scope in source_scopes)
    return bool(not field_entities and not exclusive_scope and _CONCEPT_QUESTION.search(text)
                and not _NARROW_CONCEPT.search(text)
                and not _TECHNICAL_CONCEPT.search(text)
                and not _MANUAL_DEFINITION.search(text))


def runtime_activation_requested(question):
    """Identify an actual launch/time request, independently of program kind."""
    return bool(_RUNTIME_ACTIVATION.search(str(question)))


def program_relation_direction(question):
    """Distinguish program consumers from a program's own dependencies."""
    text = str(question)
    if re.search(r"(?:哪些|哪幾|哪几|什么|甚麼)\s*(?:相关|相關)?(?:程序|程式).{0,12}(?:用到|使用|调用|調用|呼叫)|"
                 r"(?:谁|誰).{0,6}(?:调用|調用|呼叫)|"
                 r"\b(?:callers?|called\s+by|used\s+by|(?:which|what)\s+programs?\s+(?:call|use))\b", text, re.I):
        return "incoming"
    if re.search(r"(?:用到|使用|调用|調用|呼叫)\s*(?:的)?\s*(?:哪些|什么|甚麼|相关|相關|其他|其它|子|关联|關聯)*\s*(?:程序|程式)|"
                 r"\b(?:callees?|called\s+programs?|programs?\s+(?:it\s+)?(?:calls|uses))\b", text, re.I):
        return "outgoing"
    if re.search(r"(?:相关|相關|关联|關聯)(?:的)?(?:程序|程式)|\brelated\s+programs?\b", text, re.I):
        return "related"
    return None


def question_references(question):
    """Use the source DataRef grammar after locating ASCII spans in prose.

    The location rule does not bind a name. Only current source facts or a local
    symbol lookup do that. Qualification, subscripts and reference modification
    remain one reference rather than extra input fields.
    """
    return tuple(dict.fromkeys(reference.identity for text in _IDENTIFIER.findall(mask_source_references(question))
                               for reference in data_references(text)))


def additive_terms(tokens):
    """Return simple positive sum terms; do not evaluate COBOL arithmetic."""
    tokens = tuple(tokens)
    while tokens[:1] == ("(",) and tokens[-1:] == (")",):
        depth, closes = 0, []
        for index, token in enumerate(tokens):
            depth += (token == "(") - (token == ")")
            if depth == 0:
                closes.append(index)
        if closes != [len(tokens) - 1]:
            break
        tokens = tokens[1:-1]
    depth, start, terms = 0, 0, []
    for index, token in enumerate(tokens):
        depth += (token == "(") - (token == ")")
        if not depth and token == "+":
            terms.append(tokens[start:index])
            start = index + 1
        elif not depth and token == "-":
            return None
    terms.append(tokens[start:])
    return tuple(terms) if len(terms) > 1 and all(terms) else None


def select_accumulations(question, facts, *, entities=()):
    """Select additive self-updates within the caller's already bounded facts.

    A GIVING target which is not read is not an accumulator. Field references
    only nominate candidates; bare qualified-name ambiguity remains explicit.
    """
    from claim_support import statement_tokens
    candidates = []
    for fact in facts:
        if fact.get("statement_kind") not in {"ADD", "COMPUTE"} or not fact.get("target"):
            continue
        terms = additive_terms(fact.get("expression", ()))
        target = statement_tokens(fact["target"])
        if not terms or target not in terms:
            continue
        increments = list(terms)
        increments.remove(target)
        candidates.append({**fact, "target_field": fact["target"],
                           "increment_terms": [list(term) for term in increments]})
    if not candidates:
        return []
    references = [reference for text in [*question_references(question), *entities]
                  for reference in data_references(text)]
    all_fields = [reference for fact in facts for text in
                  [fact.get("target", ""), " ".join(fact.get("expression", ()))]
                  for reference in data_references(text)]

    def matches(requested, actual):
        return (requested.name == actual.name
                and (not requested.qualifiers or requested.qualifiers == actual.qualifiers)
                and (not requested.subscripts or requested.subscripts == actual.subscripts)
                and (requested.reference_modification is None
                     or requested.reference_modification == actual.reference_modification))

    located = [reference for reference in references if any(matches(reference, field) for field in all_fields)]
    if located:
        targets = [fact for fact in candidates if any(matches(requested, actual)
            for requested in located for actual in data_references(fact["target"]))]
        if targets:
            return targets
        return [fact for fact in candidates if any(matches(requested, actual)
            for requested in located for text in [fact["target"], " ".join(fact["expression"])]
            for actual in data_references(text))]
    # A procedure can contain incidental counters. Prefer an accumulator fed
    # by its visible computed result; otherwise only a unique candidate binds.
    computed = {fact["target"] for fact in facts if fact.get("statement_kind") == "COMPUTE"
                and fact.get("target") and not any(row.get("occurrence_id") == fact.get("occurrence_id")
                    and row["target"] == fact["target"] for row in candidates)}
    linked = [fact for fact in candidates if any(reference.identity in computed
        for term in fact["increment_terms"] for reference in data_references(" ".join(term)))]
    return linked or (candidates if len(candidates) == 1 else [])
_TOOLS = {
    "origin": ("backward_slice", "explain_assignment", "trace_parameter"),
    "overwrite": ("explain_assignment", "backward_slice", "forward_slice"),
    "impact": ("forward_slice", "explain_external_effect"),
    "condition": ("backward_slice", "explain_assignment"),
    "exception": ("explain_external_effect", "trace_parameter"),
    "calculation": ("explain_assignment", "backward_slice"),
    "accumulation": ("explain_assignment", "backward_slice"),
    "flow": ("backward_slice", "trace_parameter", "explain_external_effect"),
    "business_overview": ("inspect_business_context", "trace_parameter", "explain_external_effect"),
    "explanation": ("explain_assignment",),
    "declaration": ("inspect_business_context",),
    "activation": ("inspect_business_context", "trace_parameter", "explain_external_effect"),
}


@dataclass(frozen=True)
class AnalysisPlan:
    intents: tuple[str, ...]
    entities: tuple[str, ...]
    paths: tuple[str, ...]
    tools: tuple[str, ...]
    required_obligations: tuple[str, ...]
    focus_basis: str
    unresolved: tuple[str, ...] = ()
    source_scopes: tuple[dict, ...] = ()
    file_entities: tuple[dict, ...] = ()
    relation_direction: str | None = None
    relation_root: dict | None = None
    version: str = PLAN_VERSION

    def to_dict(self):
        return asdict(self)


class QuestionPlanner:
    def plan(self, question, business_map, *, source_pages=(), history=(), candidate=None, entity_validator=None,
             source_scopes=(), file_entities=(), file_impact=False):
        """Facts and conversation focus determine depth even without a trigger word."""
        facts = business_map.get("rule_leads", ())
        known = {str(field).upper() for fact in facts for role in ("reads", "writes")
                 for field in fact.get(role, ())}
        references = question_references(question)
        entities = [token for token in references if token in known or
                    (entity_validator and entity_validator(token))]
        paths = list(dict.fromkeys(business_map.get("direct_paths", ()) or
                                  business_map.get("source_identity", {}).get("direct_paths", ())))
        basis = "question_and_source"
        referring = bool(re.search(r"这个|這個|这里|這裡|刚才|剛才|前面|最后|最後|第二种|第二種|\b(?:this|that|previous|second)\b", str(question), re.I))
        subject = business_map.get("session_subject") or {}
        if not entities and subject.get("status") == "resolved":
            entities = [item for item in subject.get("focus_entities", ()) if item in known or
                        (entity_validator and entity_validator(item))]
            if entities:
                basis = "conversation_focus_and_source"
        # Older local plans can supply a validated field, but model prose and
        # arbitrary citation order never establish a referent. A current source
        # selection or an unresolved ordinal must not inherit an older field.
        explicit = business_map.get("source_identity", {}).get("status") not in (None, "none")
        if referring and not entities and not subject and not explicit:
            for message in reversed(list(history)):
                state = message.get("investigation_state") or {}
                prior = state.get("analysis_plan") or (state.get("question_investigation") or {}).get("analysis_plan") or {}
                entities = [item for item in prior.get("entities", ()) if item in known or
                    (entity_validator and entity_validator(item))]
                if entities:
                    basis = "conversation_focus_and_source"
                    break
        if not paths:
            paths = list(dict.fromkeys(page.get("relative_path") for page in source_pages
                         if page.get("relative_path") in business_map.get("selected_paths", ())))
        # A validated procedure name containing COMPUTE/CASE is an identifier,
        # not a request to calculate or inspect a branch in unrelated code.
        intent_question = str(question)
        source_names = [name for program in business_map.get("programs", []) for name in program.get("program_names", [])]
        source_names.extend(scope.get("procedure_name") for scope in source_scopes)
        mentioned_names = set(re.findall(r"[A-Z0-9_$#@-]+", str(question).upper())) | set(references)
        for name in [*source_names, *entities]:
            if name and name.upper() in mentioned_names:
                intent_question = re.sub(r"(?<![A-Za-z0-9_$#@-])" + re.escape(name) +
                                         r"(?![A-Za-z0-9_$#@-])", " ", intent_question, flags=re.I)
        intents = [name for name, pattern in _HINTS.items() if re.search(pattern, intent_question, re.I)]
        # Share the delivery layer's broad purpose/workflow recognition. A
        # multi-part onboarding request can include rules without becoming
        # only a field/rule question.
        from business_synthesis import wants_business_overview
        concept = wants_business_overview(question, field_entities=entities, source_scopes=source_scopes)
        if concept:
            intents.append("business_overview")
        explicit_activation = runtime_activation_requested(intent_question)
        if explicit_activation and "activation" not in intents:
            intents.append("activation")
        relation_direction = program_relation_direction(intent_question)
        if relation_direction:
            intents.append("call_relationships")
        if accumulation_requested(intent_question):
            intents.append("accumulation")
        if "activation" in intents and _BATCH_NOUN.search(intent_question):
            without_batch = _BATCH_NOUN.sub(" ", intent_question)
            if not re.search(_HINTS["activation"], without_batch, re.I):
                entry_requested = (explicit_activation or _UPSTREAM_ACTIVATION.search(without_batch)
                                   or re.search(r"\bentry\b", without_batch, re.I))
                # A batch noun identifies the kind of program, not a request
                # for runtime scheduling. Business overviews still collect
                # related CL source through the separate batch-context reader.
                if not entry_requested:
                    intents.remove("activation")
        # A rule's trigger condition belongs to its local IF/control analysis.
        # Upstream launch or batch wording asks the separate caller/scheduler
        # question; calculation and activation obligations can then coexist.
        if "activation" in intents and not explicit_activation and not _UPSTREAM_ACTIVATION.search(intent_question):
            intents.remove("activation")
            if "condition" not in intents:
                intents.append("condition")
        declaration = bool(re.search(r"类型|類型|长度|長度|初值|初始值|\b(?:data\s+type|type\s+of|length|initial\s+value|default\s+value)\b",
                                     str(question), re.I))
        if declaration and not set(intents) & {"origin", "overwrite", "impact", "condition", "exception", "calculation", "accumulation"}:
            intents = ["declaration"]
        # An identified field with real assignments warrants lineage inspection
        # even if the question is only "this field?" in ordinary chat.
        if entities and not intents:
            intents = ["origin"]
        if not intents:
            intents = ["explanation"]
        if file_impact and "impact" not in intents:
            intents.append("impact")
        if file_entities:
            records = {name for entity in file_entities for name in entity.get("record_names", ())}
            if not set(intents) & {"calculation", "accumulation", "flow", "impact", "overwrite", "condition", "exception"} and set(entities) <= records:
                intents = ["file_mapping"]
            else:
                intents.append("file_mapping")
        tools = list(dict.fromkeys(tool for intent in intents for tool in
                    (_TOOLS[intent] if intent not in {"file_mapping", "call_relationships"}
                     else ("explain_external_effect", "inspect_business_context"))))
        unresolved = []
        unresolved.extend(reason for reason in subject.get("unresolved", ())
                          if not ((source_scopes or file_entities) and not referring and reason == "conversation_field_not_bound"))
        if referring and not entities:
            unresolved.append("conversation_entity_not_resolved")
        if not paths:
            unresolved.append("source_scope_not_resolved")
        # A proposed entity or path is accepted only after local membership checks.
        if isinstance(candidate, dict):
            entities.extend(item for item in candidate.get("entities", ()) if item in known)
            paths.extend(item for item in candidate.get("paths", ()) if item in business_map.get("selected_paths", ()))
        obligations = ["source_identity", "supplied_source"]
        for intent in intents:
            obligations.extend({"origin": ("input_definitions", "parameter_mapping"),
                "overwrite": ("reaching_definitions", "later_writes", "control_conditions"),
                "impact": ("def_use", "external_effects"),
                "condition": ("control_conditions",), "exception": ("error_return",),
                "calculation": ("assignment_expression", "input_definitions", "numeric_layout"),
                "accumulation": ("accumulation_step", "input_definitions"),
                "flow": ("control_conditions", "calls", "external_effects"),
                "business_overview": ("control_conditions", "calls", "external_effects"),
                "activation": ("entry_point", "caller_invocation", "activation_conditions", "scheduler_binding"),
                "call_relationships": ("calls",),
                "declaration": ("declaration_layout",)}.get(intent, ()))
        if file_entities:
            obligations.extend(("file_assignment", "file_record_layout", "copy_layout", "file_io_mapping"))
        relation_root = None
        identity = business_map.get("source_identity") or {}
        roots = identity.get("direct_paths", ())
        programs = subject.get("programs", ())
        if (relation_direction and identity.get("status") == "resolved" and len(roots) == 1
                and subject.get("status") == "resolved" and subject.get("revalidated") is True
                and subject.get("root_paths") == list(roots) and len(programs) == 1):
            program = programs[0]
            if (program.get("relative_path") == roots[0] and program.get("program_name")
                    and re.fullmatch(r"[0-9a-f]{64}", str(program.get("source_sha256", "")))
                    and program.get("source_sha256") == business_map.get("source_versions", {}).get(roots[0])):
                relation_root = {key: program[key] for key in ("relative_path", "source_sha256", "program_name")}
        return AnalysisPlan(tuple(intents), tuple(dict.fromkeys(entities)), tuple(dict.fromkeys(paths)),
            tuple(tools), tuple(dict.fromkeys(obligations)), basis, tuple(unresolved), tuple(source_scopes), tuple(file_entities),
            relation_direction=relation_direction, relation_root=relation_root)


class EvidencePlanner:
    def organize(self, plan, candidates):
        order = {kind: index for index, kind in enumerate(("file_definitions", "file_layout", "copy_layout", "file_io", "procedure_body", "formula", "accumulation_step", "business_steps", "result_adjustments",
                    "conditions", "declarations", "inputs", "dependencies", "dds_definitions"))}
        return sorted(candidates, key=lambda item: (order.get(item.get("kind"), len(order)),
                      item.get("relative_path", ""), item.get("start_line", 0), item.get("statement", "")))
