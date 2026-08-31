"""Safe OpenAI-compatible client and behavioral capability probe.

The module intentionally uses only the Python standard library.  It does not
create a network transport unless ``allow_network=True`` is supplied.  Tests
and approved adapters can inject a transport without opening a real socket.

Capability reports are audit records, not transcripts: they contain endpoint
names, status codes, and response-shape observations, but never the API key,
base URL, model identifiers, prompts, request bodies, or response bodies.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


PROBE_SCHEMA_VERSION = "company-api-capability-probe/v1"
DEFAULT_API_KEY_ENV = "COMPANY_API_KEY"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 1_000_000
SUPPORTED_API_STYLE = "openai_compatible"

_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


class SafeAPIError(RuntimeError):
    """Base error whose string representation never contains remote data."""

    def __init__(self, code: str, *, http_status: int | None = None) -> None:
        safe_code = code if _SAFE_ERROR_CODE.fullmatch(code) else "API_ERROR"
        self.code = safe_code
        self.http_status = http_status if isinstance(http_status, int) else None
        message = safe_code
        if self.http_status is not None:
            message = f"{safe_code} (HTTP {self.http_status})"
        super().__init__(message)

    def to_safe_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"code": self.code}
        if self.http_status is not None:
            result["http_status"] = self.http_status
        return result


class APIConfigurationError(SafeAPIError):
    """Raised when required local configuration is missing or invalid."""


class APIClientError(SafeAPIError):
    """Raised for safe client, transport, HTTP, and response failures."""


@dataclass(frozen=True, repr=False)
class CompanyAPIConfig:
    """Configuration for the approved company API.

    ``api_key`` is excluded from dataclass repr/equality and is resolved before
    the environment only when an explicit value is supplied.  The API root is
    expected to include the OpenAI-compatible version prefix, for example
    ``https://company.example/v1``.
    """

    base_url: str = field(repr=False, compare=False)
    chat_model: str = field(repr=False, compare=False)
    api_key: str | None = field(default=None, repr=False, compare=False)
    embedding_model: str | None = field(default=None, repr=False, compare=False)
    api_style: str = SUPPORTED_API_STYLE
    api_key_env: str = field(default=DEFAULT_API_KEY_ENV, repr=False, compare=False)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def __repr__(self) -> str:
        return (
            "CompanyAPIConfig("
            f"api_style={self.normalized_api_style!r}, "
            f"base_url_configured={bool(self.base_url.strip())}, "
            f"chat_model_configured={bool(self.chat_model.strip())}, "
            f"embedding_model_configured={bool((self.embedding_model or '').strip())}, "
            f"api_key_source={self.api_key_source!r}, "
            f"timeout_seconds={self.timeout_seconds!r})"
        )

    @property
    def normalized_api_style(self) -> str:
        return self.api_style.strip().lower()

    @property
    def api_key_source(self) -> str:
        if self.api_key is not None and self.api_key.strip():
            return "EXPLICIT"
        if os.environ.get(self.api_key_env, "").strip():
            return "ENVIRONMENT"
        return "MISSING"

    def resolve_api_key(self) -> str | None:
        if self.api_key is not None and self.api_key.strip():
            return self.api_key.strip()
        candidate = os.environ.get(self.api_key_env, "")
        return candidate.strip() or None

    def validate(self, *, require_key: bool = True) -> None:
        if self.normalized_api_style != SUPPORTED_API_STYLE:
            raise APIConfigurationError("API_STYLE_UNSUPPORTED")
        if not self.base_url.strip():
            raise APIConfigurationError("BASE_URL_MISSING")
        parsed = urllib.parse.urlsplit(self.base_url.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise APIConfigurationError("BASE_URL_INVALID")
        if not self.chat_model.strip():
            raise APIConfigurationError("CHAT_MODEL_MISSING")
        if not isinstance(self.timeout_seconds, (int, float)) or not (
            0 < float(self.timeout_seconds) <= 300
        ):
            raise APIConfigurationError("TIMEOUT_INVALID")
        if require_key and self.resolve_api_key() is None:
            raise APIConfigurationError("API_KEY_MISSING")

    def safe_summary(self) -> dict[str, object]:
        """Return only non-secret configuration facts for an audit report."""

        return {
            "api_style": self.normalized_api_style,
            "base_url_configured": bool(self.base_url.strip()),
            "chat_model_configured": bool(self.chat_model.strip()),
            "embedding_model_configured": bool(
                (self.embedding_model or "").strip()
            ),
            "api_key_source": self.api_key_source,
            "timeout_seconds": float(self.timeout_seconds),
        }

    @classmethod
    def from_env(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        chat_model: str | None = None,
        embedding_model: str | None = None,
        api_style: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> "CompanyAPIConfig":
        """Build validated configuration from explicit values and environment.

        Explicit values take precedence.  The environment mapping is copied
        only for lookup and is never retained in the returned object.
        """

        source = os.environ if environ is None else environ
        config = cls(
            base_url=(
                base_url
                if base_url is not None
                else source.get("COMPANY_API_BASE_URL", "")
            ),
            api_key=(
                api_key
                if api_key is not None
                else source.get("COMPANY_API_KEY", "")
            ),
            chat_model=(
                chat_model
                if chat_model is not None
                else source.get("COMPANY_CHAT_MODEL", "")
            ),
            embedding_model=(
                embedding_model
                if embedding_model is not None
                else source.get("COMPANY_EMBEDDING_MODEL")
            ),
            api_style=(
                api_style
                if api_style is not None
                else source.get("COMPANY_API_STYLE", SUPPORTED_API_STYLE)
            ),
            timeout_seconds=timeout_seconds,
        )
        config.validate(require_key=True)
        return config


@dataclass(frozen=True, repr=False)
class TransportRequest:
    """Request passed to an injected transport.

    The full URL, authorization header, and body remain accessible to the
    transport but are deliberately excluded from repr.
    """

    method: str
    url: str = field(repr=False)
    headers: Mapping[str, str] = field(repr=False)
    body: bytes | None = field(default=None, repr=False)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    endpoint: str = ""

    def __repr__(self) -> str:
        return (
            "TransportRequest("
            f"method={self.method!r}, endpoint={self.endpoint!r}, "
            f"has_body={self.body is not None}, "
            f"timeout_seconds={self.timeout_seconds!r})"
        )


@dataclass(frozen=True, repr=False)
class TransportResponse:
    """Minimal transport response; body and headers are never shown in repr."""

    status_code: int
    body: bytes | str = field(default=b"", repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __repr__(self) -> str:
        body_size = len(self.body) if isinstance(self.body, (bytes, str)) else 0
        return (
            "TransportResponse("
            f"status_code={self.status_code!r}, body_size={body_size})"
        )


# A descriptive alias is convenient for simple injected fake transports.
HTTPResponse = TransportResponse
Transport = Callable[[TransportRequest], TransportResponse]


class UrllibTransport:
    """Network transport created only after explicit caller authorization."""

    def __call__(self, request: TransportRequest) -> TransportResponse:
        raw_request = urllib.request.Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method=request.method,
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - explicit opt-in only
                raw_request, timeout=request.timeout_seconds
            ) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                return TransportResponse(
                    status_code=int(response.status),
                    body=body,
                    headers=dict(response.headers.items()),
                )
        except urllib.error.HTTPError as exc:
            # Do not read or preserve an error body; corporate gateways often
            # include request details that are unsafe to surface.
            return TransportResponse(status_code=int(exc.code), body=b"")
        except (urllib.error.URLError, TimeoutError, OSError):
            raise APIClientError("TRANSPORT_ERROR") from None


def _redact_secret(value: Any, secret: str | None) -> Any:
    """Recursively remove an echoed credential from parsed remote JSON."""

    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    if isinstance(value, list):
        return [_redact_secret(item, secret) for item in value]
    if isinstance(value, dict):
        return {
            str(_redact_secret(key, secret)): _redact_secret(item, secret)
            for key, item in value.items()
        }
    return value


class OpenAICompatibleChatClient:
    """Small synchronous client for the Chat Completions baseline."""

    def __init__(
        self,
        config: CompanyAPIConfig,
        *,
        transport: Transport | None = None,
        allow_network: bool = False,
    ) -> None:
        self.config = config
        self.allow_network = bool(allow_network)
        self.transport_mode = (
            "INJECTED"
            if transport is not None
            else "URLLIB"
            if self.allow_network
            else "DISABLED"
        )
        self._transport = (
            transport
            if transport is not None
            else UrllibTransport()
            if self.allow_network
            else None
        )

    def complete(
        self,
        *,
        messages: Sequence[Mapping[str, object]],
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
        tool_choice: str | Mapping[str, object] | None = "auto",
    ) -> dict[str, object]:
        """Create one chat completion and return the parsed response unchanged.

        The sole exception is defensive credential redaction if a broken or
        hostile gateway echoes the exact API key in its JSON response.
        """

        if isinstance(messages, (str, bytes)) or not messages:
            raise APIClientError("MESSAGES_INVALID")
        try:
            normalized_messages = [dict(message) for message in messages]
        except (TypeError, ValueError):
            raise APIClientError("MESSAGES_INVALID") from None

        payload: dict[str, object] = {
            "model": self.config.chat_model,
            "messages": normalized_messages,
        }
        if tools is not None:
            try:
                payload["tools"] = [dict(tool) for tool in tools]
            except (TypeError, ValueError):
                raise APIClientError("TOOLS_INVALID") from None
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
        if response_format is not None:
            try:
                payload["response_format"] = dict(response_format)
            except (TypeError, ValueError):
                raise APIClientError("RESPONSE_FORMAT_INVALID") from None

        response, _ = self._request_json("POST", "chat/completions", payload)
        return response

    def _request_json(
        self,
        method: str,
        endpoint: str,
        payload: Mapping[str, object] | None = None,
    ) -> tuple[dict[str, object], int]:
        if self._transport is None:
            raise APIClientError("NETWORK_DISABLED")
        try:
            self.config.validate(require_key=True)
        except APIConfigurationError as exc:
            raise APIClientError(exc.code) from None

        secret = self.config.resolve_api_key()
        if secret is None:  # validate() already guards this; keep invariant local.
            raise APIClientError("API_KEY_MISSING")

        request_body: bytes | None = None
        if payload is not None:
            try:
                request_body = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError, OverflowError):
                raise APIClientError("REQUEST_PAYLOAD_INVALID") from None

        base_url = self.config.base_url.strip().rstrip("/")
        safe_endpoint = endpoint.strip("/")
        request = TransportRequest(
            method=method.upper(),
            url=f"{base_url}/{safe_endpoint}",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {secret}",
            },
            body=request_body,
            timeout_seconds=float(self.config.timeout_seconds),
            endpoint=safe_endpoint,
        )
        try:
            response = self._transport(request)
        except APIClientError:
            raise
        except Exception:  # injected transports are untrusted at this boundary
            raise APIClientError("TRANSPORT_ERROR") from None

        if not isinstance(response, TransportResponse):
            raise APIClientError("TRANSPORT_RESPONSE_INVALID")
        status = response.status_code
        if not isinstance(status, int) or not 100 <= status <= 599:
            raise APIClientError("HTTP_STATUS_INVALID")
        if not 200 <= status < 300:
            raise APIClientError("HTTP_ERROR", http_status=status)

        raw_body = response.body
        if not isinstance(raw_body, (bytes, str)):
            raise APIClientError("RESPONSE_BODY_INVALID", http_status=status)
        if len(raw_body) > MAX_RESPONSE_BYTES:
            raise APIClientError("RESPONSE_TOO_LARGE", http_status=status)
        try:
            text_body = (
                raw_body.decode("utf-8") if isinstance(raw_body, bytes) else raw_body
            )
            parsed = json.loads(text_body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise APIClientError("INVALID_JSON_RESPONSE", http_status=status) from None
        if not isinstance(parsed, dict):
            raise APIClientError("INVALID_RESPONSE_SHAPE", http_status=status)
        return _redact_secret(parsed, secret), status


def _capability(
    status: str,
    reason_code: str,
    *,
    evidence: Mapping[str, object] | None = None,
) -> dict[str, object]:
    supported: bool | None
    if status == "SUPPORTED":
        supported = True
    elif status == "UNSUPPORTED":
        supported = False
    else:
        supported = None
    return {
        "status": status,
        "supported": supported,
        "reason_code": reason_code,
        "evidence": dict(evidence or {}),
    }


def _error_capability(
    error: APIClientError,
    *,
    feature_request: bool,
) -> dict[str, object]:
    status = error.http_status
    if error.code == "NETWORK_DISABLED":
        return _capability("NOT_RUN", "NETWORK_DISABLED")
    if error.code in {
        "API_KEY_MISSING",
        "BASE_URL_MISSING",
        "BASE_URL_INVALID",
        "CHAT_MODEL_MISSING",
        "TIMEOUT_INVALID",
    }:
        return _capability("NOT_RUN", error.code)
    if error.code == "HTTP_ERROR":
        if status in {401, 403}:
            return _capability(
                "UNAVAILABLE", "AUTHORIZATION_FAILED", evidence={"http_status": status}
            )
        if status == 429:
            return _capability(
                "UNAVAILABLE", "RATE_LIMITED", evidence={"http_status": status}
            )
        if status is not None and status >= 500:
            return _capability(
                "UNAVAILABLE", "REMOTE_SERVER_ERROR", evidence={"http_status": status}
            )
        if feature_request and status in {400, 404, 405, 415, 422}:
            return _capability(
                "UNSUPPORTED", "FEATURE_REQUEST_REJECTED", evidence={"http_status": status}
            )
        if status in {404, 405}:
            return _capability(
                "UNSUPPORTED", "ENDPOINT_UNAVAILABLE", evidence={"http_status": status}
            )
        return _capability(
            "UNAVAILABLE", "REQUEST_REJECTED", evidence={"http_status": status}
        )
    return _capability("INDETERMINATE", error.code)


class CapabilityProbe:
    """Behaviorally probe an OpenAI-compatible service one feature at a time."""

    def __init__(
        self,
        config: CompanyAPIConfig,
        *,
        transport: Transport | None = None,
        allow_network: bool = False,
        probe_embeddings: bool = False,
    ) -> None:
        self.config = config
        self.client = OpenAICompatibleChatClient(
            config, transport=transport, allow_network=allow_network
        )
        self.probe_embeddings = bool(probe_embeddings)
        self._audit: list[dict[str, object]] = []

    def _record_success(
        self, capability: str, method: str, endpoint: str, status: int
    ) -> None:
        self._audit.append(
            {
                "sequence": len(self._audit) + 1,
                "capability": capability,
                "method": method,
                "endpoint": endpoint,
                "outcome_code": "RESPONSE_RECEIVED",
                "http_status": status,
            }
        )

    def _record_error(
        self,
        capability: str,
        method: str,
        endpoint: str,
        error: APIClientError,
    ) -> None:
        event: dict[str, object] = {
            "sequence": len(self._audit) + 1,
            "capability": capability,
            "method": method,
            "endpoint": endpoint,
            "outcome_code": error.code,
        }
        if error.http_status is not None:
            event["http_status"] = error.http_status
        self._audit.append(event)

    def _request(
        self,
        capability: str,
        method: str,
        endpoint: str,
        payload: Mapping[str, object] | None = None,
    ) -> tuple[dict[str, object], int] | APIClientError:
        try:
            data, status = self.client._request_json(method, endpoint, payload)
        except APIClientError as exc:
            self._record_error(capability, method, endpoint, exc)
            return exc
        self._record_success(capability, method, endpoint, status)
        return data, status

    def run(self) -> dict[str, object]:
        """Run requested probes and return a JSON-serializable safe report."""

        self._audit = []
        capability_names = ["models", "chat", "tool_calling", "strict_json"]
        if self.probe_embeddings:
            capability_names.append("embeddings")

        if self.config.normalized_api_style != SUPPORTED_API_STYLE:
            capabilities = {
                name: _capability("UNSUPPORTED", "API_STYLE_UNSUPPORTED")
                for name in capability_names
            }
            return self._report("UNSUPPORTED", capabilities)

        try:
            self.config.validate(require_key=True)
        except APIConfigurationError as exc:
            capabilities = {
                name: _capability("NOT_RUN", exc.code) for name in capability_names
            }
            return self._report("NOT_RUN", capabilities)

        if self.client.transport_mode == "DISABLED":
            capabilities = {
                name: _capability("NOT_RUN", "NETWORK_DISABLED")
                for name in capability_names
            }
            return self._report("NOT_RUN", capabilities)

        capabilities: dict[str, dict[str, object]] = {}
        capabilities["models"] = self._probe_models()
        capabilities["chat"] = self._probe_chat()

        if capabilities["chat"]["status"] == "SUPPORTED":
            capabilities["tool_calling"] = self._probe_tool_calling()
            capabilities["strict_json"] = self._probe_strict_json()
        else:
            reason = "CHAT_CAPABILITY_REQUIRED"
            capabilities["tool_calling"] = _capability("NOT_RUN", reason)
            capabilities["strict_json"] = _capability("NOT_RUN", reason)

        if self.probe_embeddings:
            if (self.config.embedding_model or "").strip():
                capabilities["embeddings"] = self._probe_embeddings()
            else:
                capabilities["embeddings"] = _capability(
                    "NOT_RUN", "EMBEDDING_MODEL_MISSING"
                )

        statuses = {item["status"] for item in capabilities.values()}
        overall = "COMPLETED" if statuses <= {"SUPPORTED", "UNSUPPORTED"} else "PARTIAL"
        return self._report(overall, capabilities)

    def _probe_models(self) -> dict[str, object]:
        outcome = self._request("models", "GET", "models")
        if isinstance(outcome, APIClientError):
            return _error_capability(outcome, feature_request=False)
        data, status = outcome
        models = data.get("data")
        if not isinstance(models, list):
            return _capability(
                "INDETERMINATE",
                "MODEL_LIST_SHAPE_INVALID",
                evidence={"http_status": status, "response_body_recorded": False},
            )
        identifiers = [
            model.get("id")
            for model in models
            if isinstance(model, Mapping) and isinstance(model.get("id"), str)
        ]
        return _capability(
            "SUPPORTED",
            "MODEL_LIST_VALID",
            evidence={
                "http_status": status,
                "model_count": len(identifiers),
                "configured_chat_model_visible": self.config.chat_model in identifiers,
                "response_body_recorded": False,
            },
        )

    def _probe_chat(self) -> dict[str, object]:
        outcome = self._request(
            "chat",
            "POST",
            "chat/completions",
            {
                "model": self.config.chat_model,
                "messages": [
                    {"role": "user", "content": "Reply with exactly OK."}
                ],
            },
        )
        if isinstance(outcome, APIClientError):
            return _error_capability(outcome, feature_request=False)
        data, status = outcome
        message = _first_message(data)
        if message is None:
            return _capability(
                "UNSUPPORTED",
                "CHAT_MESSAGE_NOT_OBSERVED",
                evidence={"http_status": status, "request_accepted": True},
            )
        return _capability(
            "SUPPORTED",
            "CHAT_MESSAGE_OBSERVED",
            evidence={
                "http_status": status,
                "assistant_message_present": True,
                "response_body_recorded": False,
            },
        )

    def _probe_tool_calling(self) -> dict[str, object]:
        tool_name = "capability_probe"
        outcome = self._request(
            "tool_calling",
            "POST",
            "chat/completions",
            {
                "model": self.config.chat_model,
                "messages": [
                    {
                        "role": "user",
                        "content": "Call capability_probe with ok set to true.",
                    }
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "description": "Confirm tool calling support.",
                            "parameters": {
                                "type": "object",
                                "properties": {"ok": {"type": "boolean"}},
                                "required": ["ok"],
                                "additionalProperties": False,
                            },
                        },
                    }
                ],
                "tool_choice": {
                    "type": "function",
                    "function": {"name": tool_name},
                },
            },
        )
        if isinstance(outcome, APIClientError):
            return _error_capability(outcome, feature_request=True)
        data, status = outcome
        message = _first_message(data)
        valid_call = False
        if message is not None and isinstance(message.get("tool_calls"), list):
            for call in message["tool_calls"]:
                if not isinstance(call, Mapping):
                    continue
                function = call.get("function")
                if not isinstance(function, Mapping) or function.get("name") != tool_name:
                    continue
                arguments = function.get("arguments")
                if not isinstance(arguments, str):
                    continue
                try:
                    parsed_arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    continue
                if parsed_arguments == {"ok": True}:
                    valid_call = True
                    break
        if not valid_call:
            return _capability(
                "UNSUPPORTED",
                "VALID_TOOL_CALL_NOT_OBSERVED",
                evidence={
                    "http_status": status,
                    "request_accepted": True,
                    "expected_behavior_observed": False,
                },
            )
        return _capability(
            "SUPPORTED",
            "VALID_TOOL_CALL_OBSERVED",
            evidence={
                "http_status": status,
                "forced_named_tool": True,
                "arguments_valid_json": True,
                "response_body_recorded": False,
            },
        )

    def _probe_strict_json(self) -> dict[str, object]:
        outcome = self._request(
            "strict_json",
            "POST",
            "chat/completions",
            {
                "model": self.config.chat_model,
                "messages": [
                    {
                        "role": "user",
                        "content": "Return an object with ok set to true.",
                    }
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "capability_probe",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {"ok": {"type": "boolean"}},
                            "required": ["ok"],
                            "additionalProperties": False,
                        },
                    },
                },
            },
        )
        if isinstance(outcome, APIClientError):
            return _error_capability(outcome, feature_request=True)
        data, status = outcome
        message = _first_message(data)
        content = message.get("content") if message is not None else None
        conforms = False
        if isinstance(content, str):
            try:
                conforms = json.loads(content) == {"ok": True}
            except json.JSONDecodeError:
                conforms = False
        if not conforms:
            return _capability(
                "UNSUPPORTED",
                "STRICT_JSON_NOT_OBSERVED",
                evidence={
                    "http_status": status,
                    "request_accepted": True,
                    "expected_behavior_observed": False,
                },
            )
        return _capability(
            "SUPPORTED",
            "STRICT_JSON_OBSERVED",
            evidence={
                "http_status": status,
                "schema_conformant_response": True,
                "confidence": "BEHAVIORAL_PROBE",
                "response_body_recorded": False,
            },
        )

    def _probe_embeddings(self) -> dict[str, object]:
        outcome = self._request(
            "embeddings",
            "POST",
            "embeddings",
            {
                "model": self.config.embedding_model,
                "input": "capability probe",
            },
        )
        if isinstance(outcome, APIClientError):
            return _error_capability(outcome, feature_request=True)
        data, status = outcome
        items = data.get("data")
        vector: object = None
        if isinstance(items, list) and items and isinstance(items[0], Mapping):
            vector = items[0].get("embedding")
        valid = (
            isinstance(vector, list)
            and bool(vector)
            and all(isinstance(value, (int, float)) for value in vector)
        )
        if not valid:
            return _capability(
                "UNSUPPORTED",
                "EMBEDDING_VECTOR_NOT_OBSERVED",
                evidence={"http_status": status, "request_accepted": True},
            )
        return _capability(
            "SUPPORTED",
            "EMBEDDING_VECTOR_OBSERVED",
            evidence={
                "http_status": status,
                "vector_dimensions": len(vector),
                "response_body_recorded": False,
            },
        )

    def _report(
        self,
        overall_status: str,
        capabilities: Mapping[str, Mapping[str, object]],
    ) -> dict[str, object]:
        return {
            "schema_version": PROBE_SCHEMA_VERSION,
            "overall_status": overall_status,
            "configuration": self.config.safe_summary(),
            "execution": {
                "transport_mode": self.client.transport_mode,
                "urllib_network_allowed": self.client.allow_network,
                "request_count": len(self._audit),
            },
            "capabilities": {
                name: dict(value) for name, value in capabilities.items()
            },
            "audit": list(self._audit),
            "privacy": {
                "api_key_recorded": False,
                "base_url_recorded": False,
                "model_identifiers_recorded": False,
                "request_bodies_recorded": False,
                "response_bodies_recorded": False,
            },
        }


def _first_message(data: Mapping[str, object]) -> Mapping[str, object] | None:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, Mapping):
        return None
    message = first.get("message")
    return message if isinstance(message, Mapping) else None


def probe_capabilities(
    config: CompanyAPIConfig,
    *,
    transport: Transport | None = None,
    allow_network: bool = False,
    probe_embeddings: bool = False,
) -> dict[str, object]:
    """Convenience wrapper around :class:`CapabilityProbe`."""

    return CapabilityProbe(
        config,
        transport=transport,
        allow_network=allow_network,
        probe_embeddings=probe_embeddings,
    ).run()


def _configuration_failure_report(error: APIConfigurationError) -> dict[str, object]:
    capability_names = ("models", "chat", "tool_calling", "strict_json")
    return {
        "schema_version": PROBE_SCHEMA_VERSION,
        "overall_status": (
            "UNSUPPORTED" if error.code == "API_STYLE_UNSUPPORTED" else "NOT_RUN"
        ),
        "configuration": {"status": "INVALID", "reason_code": error.code},
        "execution": {
            "transport_mode": "DISABLED",
            "urllib_network_allowed": False,
            "request_count": 0,
        },
        "capabilities": {
            name: _capability(
                "UNSUPPORTED"
                if error.code == "API_STYLE_UNSUPPORTED"
                else "NOT_RUN",
                error.code,
            )
            for name in capability_names
        },
        "audit": [],
        "privacy": {
            "api_key_recorded": False,
            "base_url_recorded": False,
            "model_identifiers_recorded": False,
            "request_bodies_recorded": False,
            "response_bodies_recorded": False,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Probe an approved OpenAI-compatible company API. No network "
            "request is made unless --allow-network is present."
        )
    )
    parser.add_argument("--base-url")
    parser.add_argument("--chat-model")
    parser.add_argument("--embedding-model")
    parser.add_argument("--api-style")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--probe-embeddings", action="store_true")
    parser.add_argument("--allow-network", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = CompanyAPIConfig.from_env(
            base_url=args.base_url,
            chat_model=args.chat_model,
            embedding_model=args.embedding_model,
            api_style=args.api_style,
            timeout_seconds=args.timeout_seconds,
        )
    except APIConfigurationError as exc:
        report = _configuration_failure_report(exc)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 2

    report = probe_capabilities(
        config,
        allow_network=args.allow_network,
        probe_embeddings=args.probe_embeddings,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["overall_status"] in {"COMPLETED", "NOT_RUN"} else 1


__all__ = [
    "APIClientError",
    "APIConfigurationError",
    "CapabilityProbe",
    "CompanyAPIConfig",
    "HTTPResponse",
    "OpenAICompatibleChatClient",
    "TransportRequest",
    "TransportResponse",
    "UrllibTransport",
    "probe_capabilities",
]


if __name__ == "__main__":
    sys.exit(main())
