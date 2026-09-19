"""Opt-in, bounded response diagnostics kept separate from capability audits."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence


MAX_CAPTURED_EXCHANGES = 32
MAX_BODY_CHARACTERS = 16_000
MAX_TOTAL_BODY_BYTES = 256_000
_REDACTED = "[REDACTED]"
_JSON_KEY = re.compile(r'"(?:\\.|[^"\\])*"\s*:')
_JSON_STRING = re.compile(r'"(?:\\.|[^"\\])*"')
_CREDENTIAL_NAMES = frozenset({
    "apikey", "accesstoken", "refreshtoken", "idtoken", "token", "secret",
    "clientsecret", "password", "passwd", "authorization", "proxyauthorization",
    "credential", "credentials", "cookie", "setcookie", "subscriptionkey",
    "privatekey", "signingkey", "securitytoken", "sessiontoken",
    "xapikey", "xauthtoken", "authtoken", "auth", "authentication",
    "apitoken", "secretkey", "secretaccesskey", "accesskey", "accesskeyid",
    "bearertoken", "authorizationtoken", "sessionid",
})


def _credential_key(value: str) -> bool:
    return re.sub(r"[^a-z0-9]", "", value.lower()) in _CREDENTIAL_NAMES


def _redact_credentials(
    text: str, protected_values: Sequence[str], depth: int = 0
) -> str:
    """Redact JSON credential fields even in otherwise malformed responses.

    Scan fields without requiring the complete envelope to be valid JSON.  A
    nested JSON string, such as assistant content, is inspected after unescaping.
    Unfinished credential values are removed through the end of the response.
    """

    for value in protected_values:
        text = text.replace(value, _REDACTED)
    decoder = json.JSONDecoder()
    changes: list[tuple[int, int, str]] = []
    consumed = 0
    for match in _JSON_KEY.finditer(text):
        if match.start() < consumed:
            continue
        token = match.group().rsplit(":", 1)[0].rstrip()
        try:
            key = json.loads(token)
        except (ValueError, RecursionError):
            continue
        if not _credential_key(key):
            continue
        start = match.end()
        while start < len(text) and text[start].isspace():
            start += 1
        try:
            _, end = decoder.raw_decode(text, start)
        except (ValueError, RecursionError):
            # Do not retain a partial credential from a malformed JSON value.
            end = len(text)
        changes.append((start, end, json.dumps(_REDACTED)))
        consumed = end
    for start, end, replacement in reversed(changes):
        text = text[:start] + replacement + text[end:]

    if depth >= 8:
        # Deeply nested serialized JSON cannot safely be displayed verbatim.
        return _REDACTED if '\\"' in text else text

    def inspect_string(match: re.Match[str]) -> str:
        token = match.group()
        if "\\" not in token:
            return token
        try:
            value = json.loads(token)
        except (ValueError, RecursionError):
            return token
        redacted = _redact_credentials(value, protected_values, depth + 1)
        return json.dumps(redacted, ensure_ascii=False) if redacted != value else token

    text = _JSON_STRING.sub(inspect_string, text)
    # Also cover common non-JSON gateway echoes of credentials.
    return re.sub(
        r'(?i)\b(authorization|proxy-authorization|api[-_ ]?key|access[-_ ]?token|'
        r'refresh[-_ ]?token|client[-_ ]?secret|password)\s*[:=]\s*[^\r\n,;]+',
        lambda match: match.group(1) + ": " + _REDACTED,
        text,
    )


class APIResponseDiagnostics:
    """Collect only actual response attempts; never accept headers or prompts."""

    def __init__(self, *, protected_values: Sequence[str | None]) -> None:
        replacements: set[str] = set()
        for value in protected_values:
            if not value or not value.strip():
                continue
            value = value.strip()
            for candidate in (value, value.rstrip("/")):
                if not candidate:
                    continue
                replacements.add(candidate)
                replacements.add(json.dumps(candidate, ensure_ascii=True)[1:-1])
                replacements.add(json.dumps(candidate, ensure_ascii=False)[1:-1])
                replacements.add(candidate.replace("/", "\\/"))
        self._protected_values = sorted(replacements, key=len, reverse=True)
        self._exchanges: list[dict[str, object]] = []
        self._request_count = 0
        self._body_bytes = 0

    def _sanitize(self, text: str) -> str:
        for value in self._protected_values:
            text = text.replace(value, _REDACTED)
        text = _redact_credentials(text, self._protected_values)
        # Redaction can unescape an embedded JSON string; apply exact values
        # again before truncation so a reflected credential is never clipped.
        for value in self._protected_values:
            text = text.replace(value, _REDACTED)
        return text

    def record(
        self,
        *,
        phase: str,
        endpoint: str,
        http_status: int | None,
        outcome_code: str,
        body: bytes | str | None,
        elapsed_ms: float,
        body_omitted_reason: str | None = None,
    ) -> dict[str, object] | None:
        self._request_count += 1
        if len(self._exchanges) >= MAX_CAPTURED_EXCHANGES:
            return None
        if isinstance(body, bytes):
            body_bytes = len(body)
            raw_text = body.decode("utf-8", errors="replace")
        elif isinstance(body, str):
            body_bytes = len(body.encode("utf-8", errors="replace"))
            raw_text = body
        else:
            body_bytes, raw_text = 0, ""
        sanitized = self._sanitize(raw_text)
        retained = sanitized[:MAX_BODY_CHARACTERS]
        remaining = max(0, MAX_TOTAL_BODY_BYTES - self._body_bytes)
        retained = retained.encode("utf-8", errors="replace")[:remaining].decode(
            "utf-8", errors="ignore"
        )
        self._body_bytes += len(retained.encode("utf-8"))
        exchange: dict[str, object] = {
            "sequence": self._request_count,
            "phase": phase if phase in {"capability_probe", "investigation"} else "investigation",
            "endpoint": endpoint if endpoint in {"models", "chat/completions", "embeddings"} else "other",
            "http_status": http_status if isinstance(http_status, int) and 100 <= http_status <= 599 else None,
            "outcome_code": outcome_code,
            "body_text": retained,
            "body_truncated": retained != sanitized,
            "body_bytes": body_bytes,
            "body_redacted": sanitized != raw_text,
            "elapsed_ms": round(max(0, elapsed_ms), 2),
        }
        if body_omitted_reason is not None:
            exchange["body_omitted_reason"] = body_omitted_reason
        self._exchanges.append(exchange)
        return exchange

    def to_dict(self) -> dict[str, object]:
        return {
            "captured": True,
            "exchanges": [dict(exchange) for exchange in self._exchanges],
            "request_count": self._request_count,
            "omitted_exchange_count": self._request_count - len(self._exchanges),
            "truncated": self._request_count > len(self._exchanges) or any(
                exchange["body_truncated"] for exchange in self._exchanges
            ),
            "limits": {
                "max_exchanges": MAX_CAPTURED_EXCHANGES,
                "max_body_characters": MAX_BODY_CHARACTERS,
                "max_total_body_bytes": MAX_TOTAL_BODY_BYTES,
            },
            "privacy": {
                "configured_values_redacted": True,
                "recognized_credential_fields_redacted": True,
                "request_headers_recorded": False,
                "request_bodies_recorded": False,
                "response_content_unverified": True,
            },
        }
