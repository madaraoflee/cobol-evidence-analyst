"""Local, source-aware question plans; model suggestions remain candidates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from statement_facts import data_references


PLAN_VERSION = "business-analysis-plan/v4"
_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_$#@-])[A-Z0-9_$#@-]+(?:\s+(?:OF|IN)\s+[A-Z0-9_$#@-]+)*(?:\([^()\r\n]*\)(?:\s+(?:OF|IN)\s+[A-Z0-9_$#@-]+)*)?", re.I)
_HINTS = {
    "origin": r"从哪里|從哪裡|来源|來源|哪里来的|哪裡來的|\b(?:origin|source\s+of|where.+from)\b",
    "overwrite": r"最后|最後|变成|變成|清零|归零|歸零|没有生效|沒有生效|覆盖|覆蓋|\b(?:overwrit|reset|final|not.+effect)",
    "impact": r"影响|影響|修改|变更|變更|\b(?:impact|affect|change)\b",
    "condition": r"为什么|為什麼|条件|條件|情况|情況|\b(?:why|condition|case|branch)\b",
    "exception": r"异常|異常|错误|錯誤|失败|失敗|\b(?:exception|error|fail)\b",
    "calculation": r"计算|計算|公式|算式|怎么算|怎麼算|\b(?:calculate|calculates|calculated|calculating|calculation|calculations|compute|computes|computed|computing|formula|formulas)\b",
    "flow": r"流程|逻辑|邏輯|处理|處理|\b(?:flow|logic|process)\b",
}


def question_references(question):
    """Use the source DataRef grammar after locating ASCII spans in prose.

    The location rule does not bind a name. Only current source facts or a local
    symbol lookup do that. Qualification, subscripts and reference modification
    remain one reference rather than extra input fields.
    """
    return tuple(dict.fromkeys(reference.identity for text in _IDENTIFIER.findall(str(question))
                               for reference in data_references(text)))
_TOOLS = {
    "origin": ("backward_slice", "explain_assignment", "trace_parameter"),
    "overwrite": ("explain_assignment", "backward_slice", "forward_slice"),
    "impact": ("forward_slice", "explain_external_effect"),
    "condition": ("backward_slice", "explain_assignment"),
    "exception": ("explain_external_effect", "trace_parameter"),
    "calculation": ("explain_assignment", "backward_slice"),
    "flow": ("backward_slice", "trace_parameter", "explain_external_effect"),
    "explanation": ("explain_assignment",),
    "declaration": ("inspect_business_context",),
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
    version: str = PLAN_VERSION

    def to_dict(self):
        return asdict(self)


class QuestionPlanner:
    def plan(self, question, business_map, *, source_pages=(), history=(), candidate=None, entity_validator=None,
             source_scopes=()):
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
        declaration = bool(re.search(r"类型|類型|长度|長度|初值|初始值|\b(?:data\s+type|type\s+of|length|initial\s+value|default\s+value)\b",
                                     str(question), re.I))
        if declaration and not set(intents) & {"origin", "overwrite", "impact", "condition", "exception", "calculation"}:
            intents = ["declaration"]
        # An identified field with real assignments warrants lineage inspection
        # even if the question is only "this field?" in ordinary chat.
        if entities and not intents:
            intents = ["origin"]
        if not intents:
            intents = ["explanation"]
        tools = list(dict.fromkeys(tool for intent in intents for tool in _TOOLS[intent]))
        unresolved = []
        unresolved.extend(reason for reason in subject.get("unresolved", ())
                          if not (source_scopes and not referring and reason == "conversation_field_not_bound"))
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
                "flow": ("control_conditions", "calls", "external_effects"),
                "declaration": ("declaration_layout",)}.get(intent, ()))
        return AnalysisPlan(tuple(intents), tuple(dict.fromkeys(entities)), tuple(dict.fromkeys(paths)),
            tuple(tools), tuple(dict.fromkeys(obligations)), basis, tuple(unresolved), tuple(source_scopes))


class EvidencePlanner:
    def organize(self, plan, candidates):
        order = {kind: index for index, kind in enumerate(("procedure_body", "formula", "business_steps", "result_adjustments",
                    "conditions", "declarations", "inputs", "dependencies", "file_io", "file_definitions", "dds_definitions"))}
        return sorted(candidates, key=lambda item: (order.get(item.get("kind"), len(order)),
                      item.get("relative_path", ""), item.get("start_line", 0), item.get("statement", "")))
