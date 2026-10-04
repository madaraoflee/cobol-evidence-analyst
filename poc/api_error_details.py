"""Allowlisted API failure explanations; upstream text is never returned."""
from __future__ import annotations

import errno
import json
import re
import sqlite3
import uuid
from collections.abc import Mapping

SCHEMA_VERSION = "safe-api-error/v1"
MAX_ERROR_BODY_BYTES = 65_536
_LOCAL_ID = re.compile(r"local-[0-9a-f]{32}\Z")
_UPSTREAM_ID = re.compile(r"(?:(?:req|request)[_-][0-9a-fA-F]{8,64}|[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})\Z")
_EXPLANATIONS = {
    "request_too_large": ("单次请求正文超过限制。", "缩小本次供应的源码或资料范围后再试。"),
    "context_too_large": ("本次请求超过模型上下文限制。", "减少历史消息和供应资料，分段分析后再试。"),
    "model_unavailable": ("所选模型不可用或当前账号无法访问。", "检查模型配置和账号可访问的模型。"),
    "unsupported_feature": ("当前接口明确不支持所请求的功能或参数。", "使用接口支持的功能或调整请求参数。"),
    "output_limit": ("单次输出额度或输出参数超过限制。", "降低单次最大输出额度后再试。"),
    "quota_exhausted": ("账号配额或累计使用额度不足。", "检查账号配额或账单状态，恢复额度后再试。"),
    "rate_limit": ("接口明确报告请求速率受限。", "等待限流窗口结束后再手动重试。"),
    "authentication": ("接口认证失败。", "检查 API 密钥及认证配置。"),
    "permission": ("接口拒绝当前账号的访问权限。", "检查账号权限和资源访问授权。"),
    "timeout": ("请求超过等待时间。", "检查网络和服务状态，再手动重试。"),
    "connection": ("未能连接到接口。", "检查网络、接口地址和服务连通性。"),
    "http_429_unknown": ("接口返回 HTTP 429，但未提供可确认的原因。", "检查账号额度和服务限流说明，再决定是否重试。"),
    "http_5xx_unknown": ("接口返回服务端错误，具体原因尚未确认。", "凭关联编号检查服务端日志和服务状态。"),
    "request_rejected": ("接口拒绝本次请求，具体原因尚未确认。", "凭关联编号检查接口支持的请求格式和服务日志。"),
    "invalid_response": ("接口返回的响应格式无效。", "检查接口兼容性和服务端响应日志。"),
    "system_error": ("请求处理发生系统错误，具体原因尚未确认。", "凭关联编号检查应用和服务端日志。"),
    "storage_full": ("本机存储空间不足。", "释放结果目录与临时目录磁盘空间后重试。"),
    "storage_permission": ("无法写入本机分析文件。", "检查结果目录和工作台配置目录的写权限。"),
    "storage_locked": ("本机数据库或文件被占用。", "停止其它使用同一结果目录的工作台后重试。"),
    "storage_corrupt": ("本机分析数据库损坏或格式无效。", "保留原结果目录并用新的独立结果目录重新建立索引。"),
    "report_invalid": ("本机分析报告无法读取或格式无效。", "保留原文件，检查导入详情并重新生成分析产物。"),
    "source_snapshot_mismatch": ("本次导入的程序清单与索引快照不一致。", "保留原资料库并重新执行源码更新。"),
    "source_encoding_invalid": ("本次导入遇到无法按所选编码读取的文件。", "核对导入进度显示的文件，确认它是文本源码，并选择正确编码后重试。"),
    "source_changed": ("导入期间源码发生了变化，无法建立一致索引。", "暂停源码编辑或同步，待文件稳定后重新导入。"),
    "source_empty": ("所选目录没有符合设置的可用源码。", "核对源码目录、扩展名和无扩展名文件设置。"),
    "fts_unavailable": ("当前 Python 的 SQLite 不支持全文索引。", "使用包含 FTS5 的 Python 3.10 或更高版本后重新启动工作台。"),
}
TRANSPORT_EXPLANATIONS = {
    "dns_resolution_failed": "找不到接口地址，请检查网络或地址。",
    "tls_certificate_invalid": "连接验证失败，请联系管理员检查证书。",
    "tls_handshake_failed": "无法建立安全连接，请联系管理员。",
    "connection_refused": "接口拒绝连接，请确认服务已启动。",
    "connection_reset": "接口连接中断，请稍后重试。",
    "network_unreachable": "无法连上接口网络，请检查网络连接。",
    "timeout": "接口响应超时，请稍后重试。",
}
_CODE_CATEGORY = {
    "request_too_large": "request_too_large", "payload_too_large": "request_too_large",
    "context_length_exceeded": "context_too_large", "context_window_exceeded": "context_too_large",
    "model_not_found": "model_unavailable", "model_not_available": "model_unavailable",
    "model_unavailable": "model_unavailable", "unsupported_parameter": "unsupported_feature",
    "unsupported_value": "unsupported_feature", "unsupported_feature": "unsupported_feature",
    "max_tokens_exceeded": "output_limit", "max_output_tokens_exceeded": "output_limit",
    "output_token_limit_exceeded": "output_limit", "insufficient_quota": "quota_exhausted",
    "quota_exceeded": "quota_exhausted", "billing_hard_limit_reached": "quota_exhausted",
    "rate_limit_exceeded": "rate_limit", "invalid_api_key": "authentication",
    "authentication_error": "authentication", "permission_denied": "permission",
    "access_denied": "permission", "internal_error": "http_5xx_unknown", "server_error": "http_5xx_unknown",
}
_LOCAL_CATEGORY = {
    "REQUEST_TOO_LARGE": "request_too_large", "BUSINESS_CONTEXT_TOO_LARGE": "request_too_large", "REQUEST_TIMEOUT": "timeout", "PROBE_TIMEOUT": "timeout",
    "TRANSPORT_ERROR": "connection", "API_KEY_MISSING": "authentication",
    "INVALID_JSON_RESPONSE": "invalid_response", "INVALID_RESPONSE_SHAPE": "invalid_response",
    "RESPONSE_BODY_INVALID": "invalid_response", "RESPONSE_TOO_LARGE": "invalid_response",
    "RESPONSE_NESTING_TOO_DEEP": "invalid_response", "TRANSPORT_RESPONSE_INVALID": "invalid_response",
    "HTTP_STATUS_INVALID": "invalid_response", "MODEL_ERROR_RESPONSE": "invalid_response",
    "MODEL_TEXT_EMPTY": "invalid_response", "MODEL_PROTOCOL_ERROR": "invalid_response",
    "MODEL_MESSAGE_MISSING": "invalid_response", "MODEL_TEXT_WRAPPER_UNSUPPORTED": "invalid_response",
    "REQUEST_FAILED": "system_error", "ANALYSIS_FAILED": "system_error", "INTERNAL_ERROR": "system_error",
    "CONFIGURATION_INVALID": "system_error",
    "LOCAL_STORAGE_FULL": "storage_full", "LOCAL_STORAGE_PERMISSION": "storage_permission",
    "LOCAL_STORAGE_LOCKED": "storage_locked", "LOCAL_STORAGE_CORRUPT": "storage_corrupt",
    "LOCAL_REPORT_INVALID": "report_invalid", "SOURCE_SNAPSHOT_MISMATCH": "source_snapshot_mismatch",
    "SOURCE_ENCODING_INVALID": "source_encoding_invalid", "SOURCE_CHANGED_DURING_READ": "source_changed",
    "SOURCE_HASH_MISMATCH": "source_changed", "SOURCE_INDEX_EMPTY": "source_empty",
    "LOCAL_FTS_UNAVAILABLE": "fts_unavailable",
}
_MESSAGE_RULES = (
    (r"(?:request (?:body |payload )?(?:is )?too large|payload too large|request entity too large)[.!]?", "request_too_large"),
    (r"(?:maximum context length exceeded|context length exceeded|context window exceeded)[.!]?", "context_too_large"),
    (r"this model's maximum context length is \d+ tokens\.? however, (?:your messages resulted in|you requested) \d+ tokens(?: \([^\r\n]{1,120}\))?\.? (?:please reduce the length of the messages(?: or completion)?\.?|please reduce your prompt; or completion length\.?)", "context_too_large"),
    (r"(?:model not found|model unavailable|model is not available)[.!]?", "model_unavailable"),
    (r"(?:tools|tool calling|response_format|json_schema|structured outputs?) (?:is |are )?(?:not supported|unsupported)(?: (?:by|for|with) this model)?[.!]?", "unsupported_feature"),
    (r"(?:max_tokens|max_output_tokens) (?:exceeds? (?:the )?(?:maximum|output token) limit|is too large)[.!]?", "output_limit"),
    (r"(?:insufficient quota|quota exceeded|you exceeded your current quota, please check your plan and billing details)[.!]?", "quota_exhausted"),
    (r"(?:rate limit exceeded|too many requests per (?:minute|second|hour))[.!]?", "rate_limit"),
    (r"(?:invalid api key|authentication failed)[.!]?", "authentication"),
    (r"(?:permission denied|access denied)[.!]?", "permission"),
)


def build_local_diagnostic(exc, fallback_code="INTERNAL_ERROR", *, http_status=None):
    """Explain typed local failures without returning exception text or paths."""
    code = fallback_code
    if isinstance(exc, sqlite3.Error):
        number = getattr(exc, "sqlite_errorcode", None)
        primary = number & 255 if type(number) is int else None
        sqlite_codes = {13: "LOCAL_STORAGE_FULL", 8: "LOCAL_STORAGE_PERMISSION",
                        5: "LOCAL_STORAGE_LOCKED", 6: "LOCAL_STORAGE_LOCKED",
                        11: "LOCAL_STORAGE_CORRUPT", 26: "LOCAL_STORAGE_CORRUPT"}
        # Python 3.10 does not attach sqlite_errorcode. Only exact standard
        # messages are recognized; arbitrary database text is never reflected.
        sqlite_messages = {"database or disk is full": "LOCAL_STORAGE_FULL",
                           "attempt to write a readonly database": "LOCAL_STORAGE_PERMISSION",
                           "database is locked": "LOCAL_STORAGE_LOCKED", "database table is locked": "LOCAL_STORAGE_LOCKED",
                           "database schema is locked": "LOCAL_STORAGE_LOCKED",
                           "database disk image is malformed": "LOCAL_STORAGE_CORRUPT", "file is not a database": "LOCAL_STORAGE_CORRUPT"}
        code = sqlite_codes.get(primary, code)
        if primary is None and len(exc.args) == 1 and isinstance(exc.args[0], str):
            code = sqlite_messages.get(exc.args[0], code)
    elif isinstance(exc, OSError):
        windows_error = getattr(exc, "winerror", None)
        if windows_error in {32, 33}:
            code = "LOCAL_STORAGE_LOCKED"
        elif exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)} or windows_error in {39, 112}:
            code = "LOCAL_STORAGE_FULL"
        elif isinstance(exc, PermissionError) or exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
            code = "LOCAL_STORAGE_PERMISSION"
    elif isinstance(exc, json.JSONDecodeError):
        code = "LOCAL_REPORT_INVALID"
    elif isinstance(exc, ValueError) and len(exc.args) == 1 and isinstance(exc.args[0], str):
        known = {"SOURCE_ENCODING_INVALID", "SOURCE_CHANGED_DURING_READ", "SOURCE_HASH_MISMATCH", "SOURCE_INDEX_EMPTY", "SOURCE_SNAPSHOT_MISMATCH"}
        if exc.args[0] in known:
            code = exc.args[0]
        elif exc.args[0] == "SOURCE_UPDATE_BACKUP_BUSY":
            code = "LOCAL_STORAGE_LOCKED"
        elif exc.args[0] in {"invalid report", "invalid report view", "report view does not match complete report"}:
            code = "LOCAL_REPORT_INVALID"
    elif type(exc) is RuntimeError and exc.args in (
            ("This SQLite build does not include FTS5 support.",),
            ("This Python SQLite build does not include FTS5 support.",)):
        code = "LOCAL_FTS_UNAVAILABLE"
    return build_diagnostic(code, http_status=http_status)


def sanitize_diagnostic(value):
    """Rebuild trusted fields; invalid input never gets a newly generated ID."""
    if not isinstance(value, Mapping) or value.get("schema_version") != SCHEMA_VERSION:
        return None
    category, request_id = value.get("category"), value.get("request_id")
    if not isinstance(category, str) or category not in _EXPLANATIONS or not isinstance(request_id, str) or not _LOCAL_ID.fullmatch(request_id):
        return None
    reason, next_step = _EXPLANATIONS[category]
    source = value.get("evidence_source")
    result = {"schema_version": SCHEMA_VERSION, "category": category, "reason": reason,
              "next_step": next_step, "evidence_source": source if isinstance(source, str) and source in {"local", "provider_code", "provider_message", "http_status", "unknown"} else "unknown",
              "request_id": request_id, "request_id_source": "local"}
    transport_reason = value.get("transport_reason")
    if (source == "local" and category in {"connection", "timeout"}
            and isinstance(transport_reason, str) and transport_reason in TRANSPORT_EXPLANATIONS
            and (category != "timeout" or transport_reason == "timeout")):
        result["transport_reason"] = transport_reason
        result["reason"] = TRANSPORT_EXPLANATIONS[transport_reason]
    status = value.get("http_status")
    if type(status) is int and 100 <= status <= 599:
        result["http_status"] = status
    provider_code = value.get("provider_code")
    if isinstance(provider_code, str) and provider_code in _CODE_CATEGORY:
        result["provider_code"] = provider_code
    retry = value.get("retry_after_seconds")
    if type(retry) is int and 0 <= retry <= 86400:
        result["retry_after_seconds"] = retry
    upstream, upstream_source = value.get("upstream_request_id"), value.get("upstream_request_id_source")
    if isinstance(upstream, str) and _UPSTREAM_ID.fullmatch(upstream) and isinstance(upstream_source, str) and upstream_source in {"header", "body"}:
        result["upstream_request_id"], result["upstream_request_id_source"] = upstream, upstream_source
    return result


def format_diagnostic(value):
    safe = sanitize_diagnostic(value)
    if safe is None:
        return ""
    text = safe["reason"] + " " + safe["next_step"]
    if "http_status" in safe:
        text += f" HTTP {safe['http_status']}。"
    text += f" 本地关联编号：{safe['request_id']}。"
    if "upstream_request_id" in safe:
        text += f" 上游关联编号：{safe['upstream_request_id']}。"
    return text


def _json_error(body, limit=MAX_ERROR_BODY_BYTES):
    if not isinstance(body, (bytes, str)) or len(body) > limit:
        return None
    try:
        parsed = json.loads(body.decode("utf-8-sig") if isinstance(body, bytes) else body.removeprefix("\ufeff"))
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(parsed, dict):
        return None
    # A standard error envelope may include gateway metadata. Read only the
    # explicit error field; unrelated metadata never enters the projection.
    if isinstance(parsed.get("error"), (dict, str)) and parsed["error"]:
        return parsed
    error_keys = {"error", "errors", "code", "status", "message", "detail", "details", "type", "request_id"}
    if not set(parsed) <= error_keys:
        return None
    errors = parsed.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], (dict, str)):
        return {**parsed, "error": errors[0]}
    if isinstance(errors, dict) and errors:
        return {**parsed, "error": errors}
    status = parsed.get("status")
    code = parsed.get("code")
    if ((isinstance(status, str) and status in {"error", "failed", "failure"})
            or (type(code) is int and 400 <= code <= 599)):
        return {**parsed, "error": parsed}
    return None


def is_error_envelope(body):
    return _json_error(body, 1_000_000) is not None


def build_diagnostic(code, *, http_status=None, body=None, headers=None, protected_values=(), request_body=None):
    """Classify bounded private input, returning only fixed labels and safe IDs."""
    parsed = _json_error(body)
    error = parsed.get("error") if parsed else None
    category, source, provider_code = None, "unknown", None
    protected = [item for item in protected_values if isinstance(item, str) and item]
    request_text = request_body.decode("utf8", errors="ignore") if isinstance(request_body, bytes) else request_body if isinstance(request_body, str) else ""

    def reflected(value):
        return any(item.casefold() in value.casefold() for item in protected) or bool(
            request_text and value.casefold() in request_text.casefold())

    if isinstance(error, dict):
        for key in ("code", "type"):
            candidate = error.get(key)
            if isinstance(candidate, str) and candidate in _CODE_CATEGORY and not reflected(candidate):
                provider_code, category, source = candidate, _CODE_CATEGORY[candidate], "provider_code"
                break
    message = error.get("message") if isinstance(error, dict) else error
    if category is None and isinstance(message, str) and len(message) <= 1024 and not reflected(message):
        param = error.get("param") if isinstance(error, dict) else None
        if isinstance(param, str) and param in {"max_tokens", "max_output_tokens", "max_completion_tokens"} and re.fullmatch(
            r"(?:max_tokens|max_output_tokens|max_completion_tokens) (?:is too large|exceeds? (?:the )?(?:maximum|output token) limit)(?::? \d+)?[.!]?(?: this model supports at most \d+ (?:completion|output) tokens[.!]?)?", message.strip(), re.I):
            category, source = "output_limit", "provider_message"
        elif isinstance(param, str) and param in {"tools", "response_format", "tool_choice", "json_schema"} and re.fullmatch(
            r"unsupported parameter: ['\"]?(?:tools|response_format|tool_choice|json_schema)['\"]? is not supported (?:with|by) this model[.!]?", message.strip(), re.I):
            category, source = "unsupported_feature", "provider_message"
        elif param == "max_tokens" and re.fullmatch(
            r"unsupported parameter: ['\"]max_tokens['\"] is not supported with this model\. use ['\"]max_completion_tokens['\"] instead\.", message.strip(), re.I):
            category, source = "unsupported_feature", "provider_message"
        for pattern, matched_category in _MESSAGE_RULES:
            if category is None and re.fullmatch(pattern, message.strip(), re.I):
                category, source = matched_category, "provider_message"
                break
    if category is None:
        if isinstance(code, str) and code in _LOCAL_CATEGORY:
            category, source = _LOCAL_CATEGORY[code], "local"
        elif http_status == 401:
            category, source = "authentication", "http_status"
        elif http_status == 403:
            category, source = "permission", "http_status"
        elif http_status == 413:
            category, source = "request_too_large", "http_status"
        elif http_status == 429:
            category, source = "http_429_unknown", "http_status"
        elif type(http_status) is int and 500 <= http_status <= 599:
            category, source = "http_5xx_unknown", "http_status"
        elif type(http_status) is int and 300 <= http_status <= 499:
            category, source = "request_rejected", "http_status"
        else:
            category, source = "system_error", "local"
    reason, next_step = _EXPLANATIONS[category]
    result = {"schema_version": SCHEMA_VERSION, "category": category, "reason": reason, "next_step": next_step,
              "evidence_source": source, "request_id": "local-" + uuid.uuid4().hex, "request_id_source": "local",
              "http_status": http_status}
    if provider_code:
        result["provider_code"] = provider_code
    if isinstance(headers, Mapping):
        normalized = {key.lower(): value for key, value in headers.items() if isinstance(key, str)}
        retry = normalized.get("retry-after")
        if isinstance(retry, str) and re.fullmatch(r"\d{1,5}", retry) and int(retry) <= 86400:
            result["retry_after_seconds"] = int(retry)
        for key in ("x-request-id", "request-id"):
            candidate = normalized.get(key)
            if isinstance(candidate, str) and _UPSTREAM_ID.fullmatch(candidate) and not reflected(candidate):
                result["upstream_request_id"], result["upstream_request_id_source"] = candidate, "header"
                break
    if "upstream_request_id" not in result and parsed:
        candidate = parsed.get("request_id")
        if isinstance(candidate, str) and _UPSTREAM_ID.fullmatch(candidate) and not reflected(candidate):
            result["upstream_request_id"], result["upstream_request_id_source"] = candidate, "body"
    return sanitize_diagnostic(result)
