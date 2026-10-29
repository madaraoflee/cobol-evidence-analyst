"""Separate a growing answer from investigation protocol without buffering prose."""

import re


def visible_answer_prefix(text, protected_values=()):
    """Return already received prose, holding protocol and partial credentials.

    Brackets in citations or links and inline braces are normal Markdown.
    Standalone JSON objects remain protocol candidates until terminal parsing.
    This neither generates text nor marks a candidate as reviewed.
    """
    candidate = text.lstrip()
    if candidate.startswith("{"):
        return ""
    numeric_markdown = re.match(r"^\[[0-9][0-9 ,.:/-]*\](?:\(|\s*[^\s,\]}])", candidate)
    if not numeric_markdown and re.match(r'^\[\s*(?:$|[\[{"\d-]|true\b|false\b|null\b)', candidate):
        return ""
    if candidate.casefold() in ("`", "``", "```", "```j", "```js", "```jso"):
        return ""
    if re.match(r"^```(?:json)?\s*(?:\n|$)", candidate, re.I):
        return ""
    holdback = 0
    for protected in protected_values:
        for size in range(min(len(protected) - 1, len(text)), holdback, -1):
            if text.endswith(protected[:size]):
                holdback = size
                break
    visible = text[:-holdback] if holdback else text
    # A real action may follow an introductory sentence on a separate line.
    # Inline set notation or a placeholder must not freeze the whole answer.
    position, fence = 0, None
    for line in visible.splitlines(keepends=True):
        marker = re.match(r"^[ \t]*(`{3,}|~{3,})(.*)", line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
        elif marker:
            if marker[2].strip().casefold() in {"", "json"}:
                return visible[:position].rstrip()
            fence = marker[1]
        else:
            protocol = re.search(r'\{(?=\s*(?:"|$))', line)
            if protocol:
                return visible[:position + protocol.start()].rstrip()
        position += len(line)
    return visible.rstrip()
