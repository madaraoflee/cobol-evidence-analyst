"""Recognize an explicit repository-wide calculation inventory request.

This is deliberately independent of conversation history and domain vocabulary:
an explicit whole-repository request replaces an earlier single-file focus, but
ordinary formula questions retain their existing scope.
"""
from __future__ import annotations

import re
import unicodedata


_CALCULATION = re.compile(
    r"公式|计算|計算|算法|\b(?:formula(?:s)?|calculat\w*|computations?|arithmetic)\b", re.I
)
_REPOSITORY = re.compile(
    r"全(?:部)?(?:系统|系統|库|庫|仓库|倉庫|源码|源碼|代码库|代碼庫)"
    r"|(?:整个|整個|所有)(?:系统|系統|仓库|倉庫|源码库|源碼庫|代码库|代碼庫)"
    r"|(?:系统|系統|仓库|倉庫|源码库|源碼庫|代码库|代碼庫)(?:中|内|內|里|裡|的|范围内|範圍內|\s)*(?:所有|全部)"
    r"|\b(?:entire|whole|full)\s+(?:source\s+)?(?:system|repository|repo|codebase)\b"
    r"|\b(?:system|repository|repo|codebase)[- ]wide\b"
    r"|\b(?:across|throughout)\s+(?:(?:the|this|our)\s+)?(?:system|repository|repo|codebase)\b"
    r"|\b(?:all|every)\b[^\n.!?。！？]{0,120}\b(?:in|of)\s+(?:(?:the|this|our)\s+)?(?:system|repository|repo|codebase)\b",
    re.I,
)
_NEGATED_SCOPE = re.compile(
    r"(?:不要|无需|無需|不用|不是|并非|並非|不必)(?:检查|檢查|分析|扫描|掃描|列出|清点|清點|问|問|做|进行|進行|\s)*$"
    r"|\b(?:not|without|except|exclude|excluding)\s+(?:the\s+)?$"
    r"|\b(?:do\s+not|don't|no\s+need\s+to)\s+(?:scan|check|list|analy[sz]e|inventory)\s+(?:the\s+)?$",
    re.I,
)
_LOCAL_ONLY = re.compile(
    r"(?:只看|仅看|僅看|只分析|仅分析|僅分析|限定(?:在)?|仅限|僅限)\s*[^\n，。；;]{0,80}\.[A-Z0-9]{1,12}\b"
    r"|\b(?:only|limited\s+to|restrict\w*\s+to)\b[^\n,.;]{0,80}\.[A-Z0-9]{1,12}\b"
    r"|(?:只看|仅看|僅看|只分析|仅分析|僅分析|限定(?:在)?|仅限|僅限)\s*[A-Z0-9_$#@-]+\s*(?:程序|文件|段落)"
    r"|\bonly\s+(?:program|file|routine)\s+[A-Z0-9_$#@-]+\b"
    r"|\bonly\s+[A-Z0-9_$#@-]+\s+is\s+in\s+scope\b",
    re.I,
)


def repository_inventory_requested(question: str) -> bool:
    """True only for a current, explicit whole-repository formula inventory.

No premium alias is inferred here. Candidate relevance must be established from
source evidence. Quoted code and filenames do not themselves establish scope.
"""
    text = unicodedata.normalize("NFKC", str(question or ""))
    text = re.sub(r"```[\s\S]*?```", " ", text)
    text = re.sub(r"`([^`]*)`", lambda match: match[1] if re.fullmatch(
        r"[A-Za-z0-9_./\\ -]+\.[A-Za-z0-9]{1,12}", match[1]) else " ", text)
    if not _CALCULATION.search(text):
        return False
    # A negated old restriction ("不要只看 entry.cbl") is not a new restriction.
    local_text = re.sub(r"(?:不要|不用|不必|不再)(?:只看|只分析)\s*[^\n，。；;]*", " ", text)
    local_text = re.sub(r"\b(?:not|no\s+longer)\s+only\b[^\n,;]*", " ", local_text, flags=re.I)
    if _LOCAL_ONLY.search(local_text):
        return False
    for match in _REPOSITORY.finditer(text):
        if not _NEGATED_SCOPE.search(text[max(0, match.start() - 60):match.start()]):
            return True
    return False
