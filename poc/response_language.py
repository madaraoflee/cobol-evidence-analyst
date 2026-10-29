"""Resolve each answer's language independently of source and interface text."""

from __future__ import annotations

import re


SUPPORTED_LANGUAGES = {"en", "zh-CN", "zh-HK"}
_NAMES = {"en": "English", "zh-CN": "Simplified Chinese", "zh-HK": "Traditional Chinese"}
_QUOTED = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`|\"[^\"\n]*\"|“[^”\n]*”|「[^」\n]*」|(?<!\w)'[^'\n]*'(?!\w)")
_LANGUAGE_NAME = (r"English|(?:Simplified|Traditional)\s+Chinese|Chinese|"
                  r"en(?:[-_]US|[-_]GB)?|zh[-_]CN|zh[-_](?:HK|TW)|"
                  r"英文|英语|英語|简体中文|簡體中文|繁体中文|繁體中文|简体|簡體|繁体|繁體|中文")
_REQUEST = re.compile(
    r"\b(?:answer|reply|respond|explain|write)(?:\s+(?:to\s+me|this|it|only|entirely|please|always))*\s+in\s+"
    r"(?P<english>" + _LANGUAGE_NAME + r")(?![A-Za-z0-9_-])"
    r"|(?:请|請)?(?:用|以|使用)\s*(?P<chinese>" + _LANGUAGE_NAME + r")"
    r"(?:(?:来|來)?\s*(?:回答|回复|回覆|解释|解釋|说明|說明|作答|分析)|(?=\s*(?:$|[，。！？：；,.!?;:])))"
    r"|(?<![A-Za-z0-9_-])(?P<short>" + _LANGUAGE_NAME + r")\s*(?:回答|回复|回覆|作答)"
    r"|(?:回答|回复|回覆|输出|輸出)\s*(?:必须|必須|应|應|需要|一律|统一|統一|始终|始終|都|只|仅|僅)*"
    r"\s*(?:使用|用|为|為|是)\s*(?P<postfix>" + _LANGUAGE_NAME + r")(?![A-Za-z0-9_-])"
    r"|(?:\b(?:(?:answer|response|output)[_ -]?)?language|(?:(?:回答|回复|回覆|输出|輸出))?语言|(?:(?:回答|回覆|輸出))?語言)"
    r"\s*[:：=]\s*(?P<label>" + _LANGUAGE_NAME + r")(?![A-Za-z0-9_-])"
    r"|\buse\s+(?P<use>" + _LANGUAGE_NAME + r")\s+(?:for|in)\s+(?:(?:all|every|each)\s+)?(?:answers?|responses?|replies)\b"
    r"|(?:^|[。\n;；.!?])\s*(?:(?:please|always|only)\s+)*use\s+(?P<imperative>" + _LANGUAGE_NAME + r")(?=\s*(?:$|[。;；.!?]))", re.I)


def _explicit(text):
    selected = None
    for match in _REQUEST.finditer(text):
        prefix = re.split(r"[\n。；;!?！？]", text[:match.start()])[-1]
        if re.search(r"(?:\b(?:not|never|don't|don’t|do not|avoid)\s*(?:(?:always|only|just|ever|usually)\s+)*"
                     r"|(?:不要|不用|禁止|避免|切勿|无需|無需|不需要|不|别|別)(?:默认|默認|始终|始終|总是|總是|只|仅|僅)*)\s*$",
                     prefix, re.I):
            continue
        if (re.search(r"(?:例如|示例|例子|範例|范例|舉例|举例|\b(?:for example|examples?|e\.g\.))\s*[:：,，]?\s*$",
                      prefix, re.I)
                or re.match(r"\s*(?:示例|例子|範例|范例|\bexamples?\b)", text[match.end():], re.I)):
            continue
        name = next(value for value in match.groups() if value).lower()
        selected = ("en" if name in {"english", "英文", "英语", "英語", "en", "en-us", "en_us", "en-gb", "en_gb"} else
                    "zh-HK" if name in {"traditional chinese", "繁体中文", "繁體中文", "繁体", "繁體",
                                            "zh-hk", "zh_hk", "zh-tw", "zh_tw"} else "zh-CN")
    return selected


def _saved_language_requirement(instructions):
    if not isinstance(instructions, str):
        return None
    # Quoted example instructions are data. A quoted language value following
    # an explicit language selector is still a direct preference declaration.
    def unquote_language(match):
        quoted = match[0]
        inner = quoted[1:-1]
        if (re.fullmatch(_LANGUAGE_NAME, inner, re.I)
                and re.search(r"(?:\b(?:in|language)|语言|語言|用|使用|为|為)\s*[:：=]?\s*$",
                              instructions[:match.start()], re.I)):
            return " " + inner + " "
        return " "
    text = _QUOTED.sub(unquote_language, instructions)
    lines, example = [], False
    for line in text.splitlines():
        if not line.strip():
            example = False
        if re.match(r"\s*(?:#{1,6}\s*)?(?:示例|例子|範例|范例|例如|examples?)\s*(?:[:：]|$)", line, re.I):
            example = True
        lines.append("" if example else line)
    return _explicit("\n".join(lines))


def _prose_language(text):
    # Paths, citations, code and identifiers are data, not a language preference.
    prose = _QUOTED.sub(" ", text)
    prose = re.sub(r"\[[^\]\n]+\]|\b[\w./\\-]+\.(?:cbl|cob|cpy|txt|md)\b", " ", prose, flags=re.I)
    english_words = {"what", "how", "why", "when", "where", "which", "who", "does", "do", "is", "are",
                     "can", "could", "would", "will", "this", "that", "the", "a", "an", "it", "in", "of",
                     "to", "and", "or", "please", "explain", "show", "describe", "list", "summarize",
                     "continue", "yes", "no", "tell", "me", "program", "code", "logic", "rule"}
    prose = re.sub(r"\b[A-Z][A-Z0-9_-]*\b|\b\w+[-_]\w[\w-]*\b",
                   lambda match: match[0].lower() if match[0].lower() in english_words else " ", prose)
    han = re.findall(r"[\u3400-\u9fff]", prose)
    latin = re.findall(r"[A-Za-z]+", prose)
    chinese_question = bool(re.search(r"怎样|怎樣|如何|怎么|怎麼|为什么|為什麼|请|請|是否|吗|嗎|哪些|什么|什麼|"
                                      r"解释|解釋|说明|說明|分析|讲解|講解|讲讲|講講", prose))
    english_question = bool(re.match(r"\s*(?:please\s+)?(?:what|how|why|when|where|which|does|do|is|are|can|could|"
                                     r"explain|show|describe|list|summarize|tell)\b", prose, re.I))
    if english_question and not chinese_question:
        return "en"
    if han and chinese_question and not english_question:
        latin = []
    if latin and sum(map(len, latin)) > len(han) * 2:
        return "en"
    if han:
        return "zh-HK" if re.search(r"[這請問為麼體復邏輯觸發條處輸數據說與後從應該會讓釋講]", prose) else "zh-CN"
    if re.search(r"[。；，：！？]", prose):
        return "zh-CN"
    return None


def resolve_answer_language(question, *, locale=None, history=None, answer_instructions=""):
    """Current explicit request, saved explicit requirement, then prior defaults.

    Saved prose and English section names do not imply an output language.
    Only an affirmative language directive can override the question's prose.
    """
    text = _QUOTED.sub(" ", str(question or ""))
    selected = _explicit(text) or _saved_language_requirement(answer_instructions) or _prose_language(text)
    if selected:
        return selected
    if locale in SUPPORTED_LANGUAGES:
        return locale
    for message in reversed(history or []):
        if isinstance(message, dict) and message.get("role") == "user":
            previous = _QUOTED.sub(" ", str(message.get("content") or ""))
            selected = _explicit(previous) or _prose_language(previous)
            if selected:
                return selected
    return "zh-CN"


def localized_text(language, zh_cn, en, zh_hk=None):
    return en if language == "en" else zh_hk if language == "zh-HK" and zh_hk is not None else zh_cn


def answer_language_instruction(language, *, compact=False, saved_requirements=False):
    name = _NAMES.get(language, _NAMES["zh-CN"])
    if saved_requirements:
        return (f"Fallback answer language for this turn: {name} ({language}). "
                "Use an explicit language request in the current question first, then an explicit language "
                "requirement in the saved system instructions, otherwise use this fallback. "
                "The payload's answer_language and fallback_answer_language are inferred defaults, not overrides "
                "of those explicit requests. Source and earlier drafts do not set the answer language. "
                "Keep source identifiers, literal values, quoted source and citation IDs unchanged; "
                "investigation action keys and paths retain their protocol format.")
    if compact:
        return f"Answer in {name} ({language}); keep source literals/citations intact."
    return (f"Answer language for this turn: {name} ({language}). Write all user-facing prose, "
            "headings, table labels, explanations and continuation text in this language. "
            "The current question's language or explicit language request takes precedence over "
            "earlier conversation and interface language. Instructions, source comments, reference "
            "documents and earlier drafts may be in other languages; they do not change the answer language. "
            "Keep source identifiers, literal values, quoted source and citation IDs unchanged. "
            "Investigation action keys and paths retain their protocol format.")
