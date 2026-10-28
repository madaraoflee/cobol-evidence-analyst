"""Bounded local topic labels for conversations; never request another model call."""

from __future__ import annotations

import re


TITLE_INPUT_HEAD = 4096
TITLE_INPUT_TAIL = 2048
MAX_TITLE_CHARACTERS = 64
_CJK = re.compile(r"[\u3400-\u9fff]")
_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_-])(?:[A-Za-z][A-Za-z0-9_-]*\d[A-Za-z0-9_-]*|[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)+)(?![A-Za-z0-9_-])")
_CODE_LINE = re.compile(
    r"^(?:\d{6}\s|(?:IDENTIFICATION|DATA|PROCEDURE|ENVIRONMENT)\s+DIVISION\b|"
    r"PROGRAM-ID\s*\.|(?:WORKING-STORAGE|LINKAGE|FILE)\s+SECTION\b|"
    r"\d{1,2}\s+\S+\s+(?:PIC|PICTURE|VALUE|OCCURS)\b|"
    r"(?:MOVE|PERFORM|DISPLAY|CALL|COMPUTE|IF|END-IF|STOP RUN)\b|"
    r"[A-Z][A-Z0-9_]*(?:KEY|TOKEN|PASSWORD)\s*=)", re.I)
_FORMAT_LINE = re.compile(
    r"^(?:(?:请|請)?(?:用|使用|以|按|按照|輸出|输出|回答|不要|不需要)|"
    r"(?:please\s+)?(?:use|format|respond|answer|return|do not)\b).{0,100}"
    r"(?:markdown|json|表格|條列|条列|要點|要点|格式|繁體|繁体|简体|簡體|中文|英文|"
    r"字以[内內]|bullet|format|english|chinese)", re.I)
_GENERIC = re.compile(
    r"^(?:你好|您好|謝謝|谢谢|繼續|继续|繼續說明|继续说明|詳細[一點点些]*|详细[一點点些]*|"
    r"為什麼|为什么|然後呢|然后呢|這是什麼|这是什么|分析一下|解釋一下|解释一下|"
    r"hi|hello|thanks|thank you|continue|go on|why|how|what|explain|more details|tell me more)$", re.I)


def _bounded(text):
    if len(text) <= TITLE_INPUT_HEAD:
        return text
    return text[:TITLE_INPUT_HEAD] + "\n" + text[-TITLE_INPUT_TAIL:]


def _topic_line(question):
    text = _bounded(question)
    identifiers = re.findall(r"PROGRAM-ID\s*\.\s*([A-Za-z0-9_-]+)", text, re.I)
    text = re.sub(r"```[^\n]*\n[\s\S]*?(?:```|$)|~~~[^\n]*\n[\s\S]*?(?:~~~|$)", "\n", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    candidates = []
    for position, raw in enumerate(text.splitlines()):
        line = re.sub(r"^\s*(?:(?:#{1,6}|>|[-*])\s+|\d+[.)、]\s*)", "", raw).strip()
        line = re.sub(r"^(?:实际问题|實際問題|问题|問題|question|request)\s*[:：]\s*", "", line, flags=re.I)
        if not line or _CODE_LINE.match(line) and not re.search(r"[?？]|为什么|為什麼|如何|\b(?:why|how|what|where)\b", line, re.I):
            continue
        if _FORMAT_LINE.match(line) and not re.search(r"[?？]", line):
            continue
        if re.fullmatch(r"(?:请|請)?(?:按(?:照)?以下要求回答|先看以下(?:代码|代碼|源码|源碼)|要求|背景|说明|說明)[:：]?", line):
            continue
        line = re.sub(r"`|(?<!\w)[*_]+|[*_]+(?!\w)", "", line).strip()
        line = re.sub(r"\s+", " ", line)
        if _GENERIC.fullmatch(line.rstrip(" .!?。！？")):
            continue
        score = (5 if re.search(r"[?？]", line) else 0)
        score += 3 if re.search(r"为什么|為什麼|如何|怎么|怎麼|什么|什麼|哪里|哪裡|分析|解释|解釋|\b(?:why|how|what|where|explain|analyse|analyze|compare)\b", line, re.I) else 0
        score += 1 if _IDENTIFIER.search(line) else 0
        candidates.append((score, -position, line))
    if not candidates:
        return (identifiers[0] + " source analysis") if identifiers else None
    return max(candidates)[2]


def _fit(title, limit):
    title = re.sub(r"\s+", " ", title).strip(" ,，。.!?！？:：;；-·")
    if len(title) <= limit:
        return title
    # Keep identifiers intact and prefer phrase/word boundaries over cutting
    # a program name or a partially written word into a misleading new name.
    identifiers = _IDENTIFIER.findall(title)
    end = limit - 1
    for match in _IDENTIFIER.finditer(title):
        if match.start() < end < match.end():
            end = match.start()
    prefix = title[:end].rstrip()
    if not _CJK.search(prefix) and " " in prefix:
        prefix = prefix.rsplit(" ", 1)[0]
    missing = next((item for item in identifiers if item not in prefix), None)
    if missing and len(missing) < limit // 2:
        available = limit - len(missing) - 4
        for match in _IDENTIFIER.finditer(prefix):
            if match.start() < available < match.end():
                available = match.start()
                break
        prefix = prefix[:available].rstrip(" ,，:：·")
        if not _CJK.search(prefix) and " " in prefix:
            prefix = prefix.rsplit(" ", 1)[0]
        return f"{missing} · {prefix}…"
    return (prefix or title[:end]).rstrip(" ,，:：·") + "…"


def summarize_question(question):
    """Name the user's inquiry, without claiming a cause or inventing an answer.

    Returns None for a greeting or contextless follow-up. This lets the store
    wait for a substantive topic instead of pinning a conversation to 'why?'.
    """
    if not isinstance(question, str) or not question.strip():
        return None
    line = _topic_line(question)
    if not line:
        return None
    if _CJK.search(line):
        subject = re.sub(r"^(?:我是|作为|作為)(?:一个|一個)?(?:新人|新手|初学者|初學者)[，,：:\s]+", "", line)
        subject = re.sub(r"^(?:(?:你好|您好)[，,！! ]*|(?:请|請)(?:你|您)?(?:帮我|幫我|帮助我|幫助我)?|(?:帮我|幫我))", "", subject)
        subject = re.sub(r"^(?:详细|詳細|简单|簡單|具体|具體)?(?:分析|解释|解釋|说明|說明|讲解|講解|看|看看|梳理)(?:一下)?[，,：: ]*", "", subject)
        subject = re.sub(r"^(?:关于|關於)[：: ]*", "", subject)
        # Separate same-line presentation instructions from the business topic.
        subject = re.split(r"[，,；;]\s*(?:请|請)?(?:先(?:讲|講|说|說)(?:清楚)?(?:业务|業務)|再(?:按|用)\s*(?:Batch job|表格|分类|分類))", subject, maxsplit=1)[0]
        subject = re.sub(r"^(?:这份|這份)(源码|源碼)(?:实现|實現)的", r"\1", subject)
        subject = re.sub(r"\s*(?:这个|這個)(?=[\u3400-\u9fff])", " ", subject)
        label = ""
        for pattern, name in (
            (r"(?:有何|有什么|有什麼|哪些|什麼|什么)?(?:区别|區別|差异|差異)|比較|比较|對比|对比", "差异"),
            (r"(?:有何|有什么|有什麼|哪些|什麼|什么)?影響|(?:有何|有什么|有什麼|哪些|什麼|什么)?影响", "影响"),
            (r"从哪里来|從哪裡來|从哪来|從哪來|来自哪里|來自哪裡|来源是什么|來源是什麼", "来源"),
            (r"为什么(?:会|會)?|為什麼(?:会|會)?|为何|為何|什么原因|什麼原因", "原因"),
            (r"如何|怎么(?:样|樣)?|怎麼(?:样|樣)?", "流程"),
            (r"(?:是)?(?:干什么的|幹什麼的|做什么用的|做什麼用的|做什么的|做什麼的)|(?:的)?(?:作用|用途)(?:是什么|是什麼)", "用途"),
            (r"(?:是)?什么意思|(?:是)?什麼意思|(?:的)?含义是什么|(?:的)?含義是什麼", "含义"),
        ):
            if re.search(pattern, subject):
                subject = re.sub(pattern, "", subject)
                label = name
                break
        subject = re.sub(r"(?:可以吗|可以嗎|好吗|好嗎|吗|嗎|呢|吧|一下)[?？。.!！\s]*$", "", subject)
        subject = re.sub(r"(?:的)?(?:业务逻辑|業務邏輯|逻辑|邏輯|流程)(?:是什麼|是什么|有哪些)", lambda m: re.sub(r"(?:是什麼|是什么|有哪些)$", "", m[0]), subject)
        subject = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", subject)
        subject = subject.strip(" ?？。.!！:：，,")
        if not subject or _GENERIC.fullmatch(subject):
            return None
        # Traditional wording follows the question rather than interface locale.
        if re.search(r"[請為麼會來裡義異響從這個與執處]", line):
            label = {"差异": "差異", "影响": "影響", "来源": "來源", "含义": "含義"}.get(label, label)
        return _fit(subject + (f" · {label}" if label and not subject.endswith(label) else ""), 40)
    subject = re.sub(r"^(?:(?:hi|hello)[,! ]+)?(?:(?:please\s+)?(?:can|could|would)\s+you\s+)?(?:please\s+)?(?:help\s+me\s+)?", "", line, flags=re.I)
    subject = re.sub(r"^(?:explain|analy[sz]e|describe|summari[sz]e|show\s+me|tell\s+me|walk\s+me\s+through)\s+", "", subject, flags=re.I)
    label = ""
    for pattern, name in ((r"^why\s+(?:(?:does|do|is|are|did|can|would)\s+)?", "Cause"),
                          (r"^how\s+(?:does|do|did|can)\s+", "Flow"),
                          (r"^where\s+(?:does|do|is|are)\s+", "Source"),
                          (r"^what\s+(?:is|are)\s+", "Overview")):
        if re.search(pattern, subject, re.I):
            subject = re.sub(pattern, "", subject, flags=re.I)
            label = name
            break
    if re.match(r"^(?:the\s+)?differences?\s+between\s+", subject, re.I):
        subject = re.sub(r"^(?:the\s+)?differences?\s+between\s+", "", subject, flags=re.I)
        label = "Comparison"
    subject = subject.strip(" .!?;:")
    if not subject or _GENERIC.fullmatch(subject):
        return None
    if subject and subject[0].islower():
        subject = subject[0].upper() + subject[1:]
    return _fit((label + ": " if label else "") + subject, MAX_TITLE_CHARACTERS)


def fallback_title(question):
    return "业务分析" if _CJK.search(question or "") else "Source analysis"
