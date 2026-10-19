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
import codecs
import errno
import json
import math
import os
import queue
import re
import sys
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from .app_paths import config_root
    from .api_diagnostics import APIResponseDiagnostics
    from .api_error_details import MAX_ERROR_BODY_BYTES, TRANSPORT_EXPLANATIONS, build_diagnostic, is_error_envelope, sanitize_diagnostic
except ImportError:
    from app_paths import config_root
    from api_diagnostics import APIResponseDiagnostics
    from api_error_details import MAX_ERROR_BODY_BYTES, TRANSPORT_EXPLANATIONS, build_diagnostic, is_error_envelope, sanitize_diagnostic


PROBE_SCHEMA_VERSION = "company-api-capability-probe/v1"
DEFAULT_API_KEY_ENV = "COMPANY_API_KEY"
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_RESPONSE_BYTES = 1_000_000
# SSE repeats response metadata for each small delta, including hidden reasoning.
# Keep its wire budget separate from retained content and non-stream JSON.
MAX_STREAM_RESPONSE_BYTES = 8_000_000
MAX_REQUEST_BYTES = 256_000
# DNS and TLS setup cannot always be interrupted by urllib. Abandoned native
# requests keep their slot until their worker actually exits.
MAX_PENDING_NATIVE_REQUESTS = 4
_NATIVE_REQUEST_SLOTS = threading.BoundedSemaphore(MAX_PENDING_NATIVE_REQUESTS)
DEFAULT_MAX_OUTPUT_TOKENS = 1_024
SUPPORTED_API_STYLE = "openai_compatible"
PROJECT_ENV_FILE = config_root() / ".env"
MAX_ENV_FILE_BYTES = 65_536
CONFIG_ENV_KEYS = frozenset({
    "COMPANY_API_BASE_URL", "COMPANY_API_KEY", "COMPANY_CHAT_MODEL",
    "COMPANY_EMBEDDING_MODEL", "COMPANY_API_STYLE", "COMPANY_MAX_OUTPUT_TOKENS",
    "FRAMEWORK_REFERENCE_PATH",
})
MODEL_PROFILE_NAMES = frozenset({"adapter", "workbench", "analysis", "custom"})
OUTPUT_LIMIT_SOURCES = frozenset({"profile", "environment", "dotenv", "explicit", "unknown"})

_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


class SafeAPIError(RuntimeError):
    """Base error whose string representation never contains remote data."""

    def __init__(self, code: str, *, http_status: int | None = None, diagnostic=None) -> None:
        safe_code = code if isinstance(code, str) and _SAFE_ERROR_CODE.fullmatch(code) else "API_ERROR"
        self.code = safe_code
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        self.diagnostic = sanitize_diagnostic(diagnostic) or build_diagnostic(safe_code, http_status=self.http_status)
        message = safe_code
        if self.http_status is not None:
            message = f"{safe_code} (HTTP {self.http_status})"
        super().__init__(message)

    def to_safe_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"code": self.code, "diagnostic": sanitize_diagnostic(self.diagnostic)}
        if self.http_status is not None:
            result["http_status"] = self.http_status
        return result


class APIConfigurationError(SafeAPIError):
    """Raised when required local configuration is missing or invalid."""


def _read_local_env(path: Path) -> dict[str, str]:
    """Read literal configuration only; never execute or expand file contents."""

    try:
        with path.open("rb") as handle:
            contents = handle.read(MAX_ENV_FILE_BYTES + 1)
    except FileNotFoundError:
        return {}
    except OSError:
        raise APIConfigurationError("ENV_FILE_UNREADABLE") from None
    if len(contents) > MAX_ENV_FILE_BYTES:
        raise APIConfigurationError("ENV_FILE_TOO_LARGE")
    try:
        text = contents.decode("utf-8-sig")
    except UnicodeError:
        raise APIConfigurationError("ENV_FILE_INVALID") from None
    if "\x00" in text:
        raise APIConfigurationError("ENV_FILE_INVALID")
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if match is None:
            raise APIConfigurationError("ENV_FILE_INVALID")
        name, raw = match.groups()
        if name not in CONFIG_ENV_KEYS:
            continue
        if raw.startswith(("'", '"')):
            end = raw.find(raw[0], 1)
            if end < 0:
                raise APIConfigurationError("ENV_FILE_INVALID")
            tail = raw[end + 1:].strip()
            if tail and not tail.startswith("#"):
                raise APIConfigurationError("ENV_FILE_INVALID")
            value = raw[1:end]
        else:
            value = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
        values[name] = value
    return values


class APIClientError(SafeAPIError):
    """Raised for safe client, transport, HTTP, and response failures."""

    def __init__(
        self,
        code: str,
        *,
        http_status: int | None = None,
        transport_reason: str | None = None,
        diagnostic=None,
    ) -> None:
        super().__init__(code, http_status=http_status, diagnostic=diagnostic)
        self.transport_reason = (
            transport_reason
            if isinstance(transport_reason, str) and transport_reason in TRANSPORT_EXPLANATIONS
            else None
        )
        if self.transport_reason is not None:
            self.diagnostic = sanitize_diagnostic({**self.diagnostic,
                "transport_reason": self.transport_reason})

    def to_safe_dict(self) -> dict[str, object]:
        result = super().to_safe_dict()
        if (
            isinstance(self.transport_reason, str)
            and self.transport_reason in TRANSPORT_EXPLANATIONS
        ):
            result["transport_reason"] = self.transport_reason
        return result


def _transport_failure(
    error: BaseException, *, opening_connection: bool = False
) -> APIClientError:
    """Classify typed network failures without inspecting remote error text."""

    # urllib wraps socket errors in reason, which may also be arbitrary text.
    # A bounded unwrap handles nested wrappers without trusting their messages.
    reason: str | None = None
    for _ in range(8):
        if not isinstance(error, urllib.error.URLError):
            break
        if not isinstance(error.reason, BaseException):
            break
        error = error.reason
    if isinstance(error, TimeoutError):
        reason = "timeout"
    elif isinstance(error, ssl.SSLCertVerificationError):
        reason = "tls_certificate_invalid"
    elif isinstance(error, ssl.SSLError):
        # A TLS read failure after opening is not necessarily a handshake failure.
        reason = "tls_handshake_failed" if opening_connection else None
    elif isinstance(error, (socket.gaierror, socket.herror)):
        reason = "dns_resolution_failed"
    elif isinstance(error, ConnectionRefusedError):
        reason = "connection_refused"
    elif isinstance(error, ConnectionResetError):
        reason = "connection_reset"
    elif isinstance(error, OSError):
        error_numbers = {
            number for number in (error.errno, getattr(error, "winerror", None))
            if isinstance(number, int) and not isinstance(number, bool)
        }
        if error_numbers & {errno.ETIMEDOUT, 10060}:
            reason = "timeout"
        elif error_numbers & {errno.ECONNREFUSED, 10061}:
            reason = "connection_refused"
        elif error_numbers & {errno.ECONNRESET, 10054}:
            reason = "connection_reset"
        elif error_numbers & {errno.ENETUNREACH, errno.EHOSTUNREACH, 10051, 10065}:
            reason = "network_unreachable"
        elif error_numbers & {11001, 11002, 11003, 11004}:
            reason = "dns_resolution_failed"
    return APIClientError(
        "REQUEST_TIMEOUT" if reason == "timeout" else "TRANSPORT_ERROR",
        transport_reason=reason,
    )


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
    api_key_source_hint: str | None = field(
        default=None, repr=False, compare=False
    )
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    allow_insecure_localhost: bool = False
    profile_name: str = field(default="custom", repr=False, compare=False)
    # Direct constructors do not reveal whether their token default was explicit.
    output_limit_source: str = field(default="unknown", repr=False, compare=False)

    def __repr__(self) -> str:
        return (
            "CompanyAPIConfig("
            f"api_style={self.normalized_api_style!r}, "
            f"base_url_configured={bool(self.base_url.strip())}, "
            f"chat_model_configured={bool(self.chat_model.strip())}, "
            f"embedding_model_configured={bool((self.embedding_model or '').strip())}, "
            f"api_key_source={self.api_key_source!r}, "
            f"timeout_seconds={self.timeout_seconds!r}, "
            f"max_output_tokens={self.max_output_tokens!r}, "
            f"profile_name={self.safe_profile_name!r}, "
            f"output_limit_source={self.safe_output_limit_source!r}, "
            f"allow_insecure_localhost={self.allow_insecure_localhost!r})"
        )

    @property
    def normalized_api_style(self) -> str:
        return self.api_style.strip().lower()

    @property
    def safe_profile_name(self) -> str:
        return (self.profile_name if isinstance(self.profile_name, str)
                and self.profile_name in MODEL_PROFILE_NAMES else "unknown")

    @property
    def safe_output_limit_source(self) -> str:
        return (self.output_limit_source if isinstance(self.output_limit_source, str)
                and self.output_limit_source in OUTPUT_LIMIT_SOURCES else "unknown")

    @property
    def api_key_source(self) -> str:
        if self.api_key_source_hint in {"EXPLICIT", "ENVIRONMENT", "LOCAL_FILE", "MISSING"}:
            return self.api_key_source_hint
        if self.api_key is not None and self.api_key.strip():
            return "EXPLICIT"
        if os.environ.get(self.api_key_env, "").strip():
            return "ENVIRONMENT"
        return "MISSING"

    def resolve_api_key(self) -> str | None:
        if self.api_key is not None and self.api_key.strip():
            return self.api_key.strip()
        # ``from_env(environ=...)`` is an isolation boundary.  When it records
        # a source hint, do not silently fall back to the process environment.
        if self.api_key_source_hint is not None:
            return None
        candidate = os.environ.get(self.api_key_env, "")
        return candidate.strip() or None

    def validate(self, *, require_key: bool = True) -> None:
        if self.safe_profile_name == "unknown":
            raise APIConfigurationError("MODEL_PROFILE_INVALID")
        if not isinstance(self.output_limit_source, str) or self.output_limit_source not in OUTPUT_LIMIT_SOURCES:
            raise APIConfigurationError("OUTPUT_LIMIT_SOURCE_INVALID")
        if self.normalized_api_style != SUPPORTED_API_STYLE:
            raise APIConfigurationError("API_STYLE_UNSUPPORTED")
        if not self.base_url.strip():
            raise APIConfigurationError("BASE_URL_MISSING")
        try:
            parsed = urllib.parse.urlsplit(self.base_url.strip())
            hostname = parsed.hostname
        except ValueError:
            raise APIConfigurationError("BASE_URL_INVALID") from None
        localhost_http = (
            parsed.scheme == "http"
            and hostname in {"localhost", "127.0.0.1", "::1"}
            and self.allow_insecure_localhost
        )
        if (
            (parsed.scheme != "https" and not localhost_http)
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
        if (
            not isinstance(self.max_output_tokens, int)
            or isinstance(self.max_output_tokens, bool)
            or not 1 <= self.max_output_tokens <= 8192
        ):
            raise APIConfigurationError("MAX_OUTPUT_TOKENS_INVALID")
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
            "max_output_tokens": self.max_output_tokens,
            "profile_name": self.safe_profile_name,
            "output_limit_source": self.safe_output_limit_source,
            "allow_insecure_localhost": self.allow_insecure_localhost,
        }

    @classmethod
    def from_env(
        cls,
        *,
        environ: Mapping[str, str] | None = None,
        env_file: Path | str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        chat_model: str | None = None,
        embedding_model: str | None = None,
        api_style: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_tokens: int | None = None,
        default_max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        profile_name: str = "adapter",
        allow_insecure_localhost: bool = False,
    ) -> "CompanyAPIConfig":
        """Resolve explicit values, process environment, then the project .env.

        No process variables are changed. An explicitly supplied environment
        is isolated from ambient files unless env_file is also supplied.
        Output limits fall back to the caller's profile default when absent.
        Fixed metadata records the profile and selected output-limit source.
        """

        environment = os.environ if environ is None else environ
        local = _read_local_env(Path(env_file)) if env_file is not None else (
            _read_local_env(PROJECT_ENV_FILE) if environ is None else {}
        )
        source = {**local, **{key: environment[key] for key in CONFIG_ENV_KEYS if key in environment}}
        resolved_output_tokens = default_max_output_tokens
        output_limit_source = "profile"
        if max_output_tokens is not None:
            resolved_output_tokens = max_output_tokens
            output_limit_source = "explicit"
        elif "COMPANY_MAX_OUTPUT_TOKENS" in source:
            output_limit_source = "environment" if "COMPANY_MAX_OUTPUT_TOKENS" in environment else "dotenv"
            raw_tokens = source["COMPANY_MAX_OUTPUT_TOKENS"]
            if not isinstance(raw_tokens, str) or not re.fullmatch(r"[0-9]+", raw_tokens.strip()):
                raise APIConfigurationError("MAX_OUTPUT_TOKENS_INVALID")
            try:
                resolved_output_tokens = int(raw_tokens.strip())
            except ValueError:
                raise APIConfigurationError("MAX_OUTPUT_TOKENS_INVALID") from None
            if not 128 <= resolved_output_tokens <= 8192:
                raise APIConfigurationError("MAX_OUTPUT_TOKENS_INVALID")
        resolved_key = (
            api_key
            if api_key is not None
            else source.get(DEFAULT_API_KEY_ENV, "")
        )
        if api_key is not None:
            key_source = "EXPLICIT" if api_key.strip() else "MISSING"
        else:
            key_source = ("ENVIRONMENT" if DEFAULT_API_KEY_ENV in environment else "LOCAL_FILE") if resolved_key.strip() else "MISSING"
        config = cls(
            base_url=(
                base_url
                if base_url is not None
                else source.get("COMPANY_API_BASE_URL", "")
            ),
            api_key=resolved_key,
            api_key_source_hint=key_source,
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
            max_output_tokens=resolved_output_tokens,
            profile_name=profile_name,
            output_limit_source=output_limit_source,
            allow_insecure_localhost=allow_insecure_localhost,
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
    """Transport response with optional numeric observations since HTTP start.

    Body and headers are never shown in repr. A first response byte means an
    HTTP body byte, not a generated token or a complete business answer.
    """

    status_code: int
    body: bytes | str = field(default=b"", repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    body_omitted_reason: str | None = field(default=None, repr=False)
    timing_seconds: Mapping[str, float] | None = field(default=None, repr=False)
    response_bytes: int | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        body_size = len(self.body) if isinstance(self.body, (bytes, str)) else 0
        return (
            "TransportResponse("
            f"status_code={self.status_code!r}, body_size={body_size})"
        )


# A descriptive alias is convenient for simple injected fake transports.
HTTPResponse = TransportResponse
Transport = Callable[[TransportRequest], TransportResponse]


@dataclass(frozen=True)
class _StreamProgress:
    completed: bool
    progressed: bool


def _safe_request_timing(value, *, elapsed=None):
    """Retain numeric transport observations, never provider text or metadata."""
    result = {"timing_seconds": {}}
    if isinstance(value, Mapping):
        timings = value.get("timing_seconds")
        if isinstance(timings, Mapping):
            for phase in ("headers_received", "first_response_byte", "response_complete"):
                seconds = timings.get(phase)
                if type(seconds) in (int, float) and 0 <= seconds <= 86400:
                    result["timing_seconds"][phase] = round(float(seconds), 6)
        count = value.get("response_bytes")
        if type(count) is int and 0 <= count <= MAX_STREAM_RESPONSE_BYTES + 1:
            result["response_bytes"] = count
    if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0:
        result["timing_seconds"]["request_elapsed"] = round(float(elapsed), 6)
    return result


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects so an Authorization header never crosses origins."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        return None


class UrllibTransport:
    """Network transport created only after explicit caller authorization."""

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    @staticmethod
    def _apply_remaining_socket_timeout(response: object, remaining: float) -> None:
        """Best-effort CPython urllib socket deadline tightening.

        The post-read monotonic check remains authoritative.  Tightening the
        underlying socket prevents a late read from consuming the original
        full timeout after most of the request budget has already elapsed.
        """

        current: object | None = response
        for _ in range(4):
            if current is None:
                return
            candidates = [
                getattr(current, "_sock", None),
                getattr(getattr(current, "raw", None), "_sock", None),
            ]
            for candidate in candidates:
                setter = getattr(candidate, "settimeout", None)
                if callable(setter):
                    try:
                        setter(max(0.001, remaining))
                    except (OSError, ValueError):
                        pass
                    return
            current = getattr(current, "fp", None)

    @staticmethod
    def _abort_socket(response: object) -> None:
        """Interrupt an available local socket without claiming remote cancellation."""
        current: object | None = response
        for _ in range(4):
            if current is None:
                return
            candidates = [getattr(current, "_sock", None),
                          getattr(getattr(current, "raw", None), "_sock", None)]
            for candidate in candidates:
                shutdown = getattr(candidate, "shutdown", None)
                if callable(shutdown):
                    try:
                        shutdown(socket.SHUT_RDWR)
                    except (OSError, ValueError):
                        pass
                    return
            current = getattr(current, "fp", None)

    def __call__(self, request: TransportRequest, *, check_cancel=None, on_response_chunk=None) -> TransportResponse:
        """Bound connection setup and idle generation without cutting off progress.

        A socket timeout is an inactivity timeout and does not bound repeated
        header reads. Connection setup and non-streaming responses therefore use
        one total deadline. SSE body reads use a deadline renewed only by parsed
        generated text/tool activity; comments and heartbeat bytes cannot renew
        it. Hidden reasoning text may indicate activity but is never exposed.
        A timed-out exchange is never repeated and a late result is discarded;
        a provider may still complete or charge for the original request.
        """
        if check_cancel:
            check_cancel()
        deadline = time.monotonic() + request.timeout_seconds
        slots = _NATIVE_REQUEST_SLOTS
        if not slots.acquire(blocking=False):
            raise APIClientError("TRANSPORT_BUSY")
        done, abandoned = threading.Event(), threading.Event()
        exchange: dict[str, object] = {"request_timing": {"timing_seconds": {}, "response_bytes": 0}}
        # Only the waiting caller invokes application callbacks. An abandoned
        # socket worker can never emit late text into another question.
        pending_chunks = queue.SimpleQueue() if on_response_chunk is not None else None
        exchange["pending_chunks"] = pending_chunks
        exchange["chunk_processed"] = threading.Event()
        response_ready = threading.Event()
        exchange["response_ready"] = response_ready

        def active_deadline():
            return exchange.get("stream_deadline", deadline)

        def deliver_chunks():
            if pending_chunks is None:
                return False
            while True:
                try:
                    chunk = pending_chunks.get_nowait()
                except queue.Empty:
                    return False
                if check_cancel:
                    check_cancel()
                progress_at = time.monotonic()
                try:
                    observed = on_response_chunk(chunk)
                    if isinstance(observed, _StreamProgress) and observed.progressed:
                        exchange["stream_deadline"] = progress_at + request.timeout_seconds
                finally:
                    exchange["chunk_processed"].set()
                if isinstance(observed, _StreamProgress) and observed.completed:
                    return True

        def completed_stream():
            # [DONE] terminates a completion even if an SSE connection stays
            # open. Stop this local socket; never repeat the provider request.
            if time.monotonic() >= active_deadline():
                raise APIClientError("REQUEST_TIMEOUT", transport_reason="timeout")
            abandoned.set()
            response = exchange["response"]
            self._abort_socket(response)
            observations = exchange["request_timing"]
            observations["timing_seconds"]["response_complete"] = max(
                0.0, time.monotonic() - (deadline - request.timeout_seconds))
            return TransportResponse(
                int(response.status), b"", headers=dict(response.headers.items()),
                timing_seconds=dict(observations["timing_seconds"]),
                response_bytes=observations["response_bytes"],
            )

        def send():
            try:
                exchange["result"] = self._send(request, deadline, abandoned, exchange)
            except BaseException as error:
                exchange["error"] = error
            finally:
                slots.release()
                done.set()
                response_ready.set()

        try:
            worker = threading.Thread(target=send, name="model-http-request", daemon=True)
            worker.start()
        except BaseException:
            slots.release()
            raise
        try:
            while not done.is_set():
                if check_cancel:
                    check_cancel()
                remaining = active_deadline() - time.monotonic()
                if remaining <= 0:
                    raise APIClientError("REQUEST_TIMEOUT", transport_reason="timeout")
                if deliver_chunks():
                    return completed_stream()
                response_ready.wait(min(0.05, remaining))
                response_ready.clear()
            if time.monotonic() >= active_deadline():
                raise APIClientError("REQUEST_TIMEOUT", transport_reason="timeout")
            if deliver_chunks():
                return completed_stream()
            if "error" in exchange:
                raise exchange["error"]
            if time.monotonic() >= active_deadline():
                raise APIClientError("REQUEST_TIMEOUT", transport_reason="timeout")
            return exchange["result"]
        except BaseException as error:
            abandoned.set()
            self._abort_socket(exchange.get("response"))
            if isinstance(error, APIClientError):
                error.request_timing = _safe_request_timing(exchange["request_timing"])
            raise

    def _send(self, request, deadline, abandoned, exchange):
        started = deadline - request.timeout_seconds
        observations = exchange["request_timing"]
        timings = observations["timing_seconds"]

        def mark(phase):
            timings[phase] = max(0.0, time.monotonic() - started)

        def observe_chunk(chunk):
            if isinstance(chunk, bytes) and chunk:
                if "first_response_byte" not in timings:
                    mark("first_response_byte")
                observations["response_bytes"] += len(chunk)

        raw_request = urllib.request.Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method=request.method,
        )
        opening_connection = True
        try:
            with self._opener.open(  # noqa: S310 - explicit opt-in only
                raw_request, timeout=request.timeout_seconds
            ) as response:
                opening_connection = False
                exchange["response"] = response
                mark("headers_received")
                stream_chunks = exchange.get("pending_chunks")
                if (not 200 <= int(response.status) < 300
                        or not _is_event_stream_headers(dict(response.headers.items()))):
                    stream_chunks = None
                if stream_chunks is not None:
                    headers_at = started + timings["headers_received"]
                    if headers_at >= deadline:
                        raise APIClientError("REQUEST_TIMEOUT", transport_reason="timeout")
                    exchange["stream_deadline"] = headers_at + request.timeout_seconds
                chunks: list[bytes] = []
                body_size = 0
                response_limit = MAX_STREAM_RESPONSE_BYTES if stream_chunks is not None else MAX_RESPONSE_BYTES
                read_chunk = getattr(response, "read1", None)
                if not callable(read_chunk):
                    read_chunk = response.read
                while body_size <= response_limit:
                    remaining = exchange.get("stream_deadline", deadline) - time.monotonic()
                    if remaining <= 0 or abandoned.is_set():
                        raise APIClientError("REQUEST_TIMEOUT", transport_reason="timeout")
                    self._apply_remaining_socket_timeout(response, remaining)
                    chunk = read_chunk(
                        min(65_536, response_limit + 1 - body_size)
                    )
                    observe_chunk(chunk)
                    if time.monotonic() >= exchange.get("stream_deadline", deadline):
                        raise APIClientError("REQUEST_TIMEOUT", transport_reason="timeout")
                    if not chunk:
                        mark("response_complete")
                        break
                    body_size += len(chunk)
                    if stream_chunks is None:
                        chunks.append(chunk)
                    elif body_size > response_limit:
                        raise APIClientError("RESPONSE_TOO_LARGE")
                    else:
                        # Wait for parsing before choosing the next socket read
                        # timeout; a valid delta may have renewed the idle clock.
                        processed = exchange["chunk_processed"]
                        processed.clear()
                        stream_chunks.put(chunk)
                        exchange["response_ready"].set()
                        while not processed.is_set():
                            remaining = exchange["stream_deadline"] - time.monotonic()
                            if remaining <= 0 or abandoned.is_set():
                                raise APIClientError("REQUEST_TIMEOUT", transport_reason="timeout")
                            processed.wait(min(0.05, remaining))
                body = b"".join(chunks)
                return TransportResponse(
                    status_code=int(response.status),
                    body=body,
                    headers=dict(response.headers.items()),
                    timing_seconds=dict(timings),
                    response_bytes=observations["response_bytes"],
                )
        except urllib.error.HTTPError as exc:
            # Bounded private input for classification; the capture path always
            # omits error bodies and no upstream text enters a safe diagnostic.
            mark("headers_received")
            chunks, size = [], 0
            try:
                read_chunk = getattr(exc, "read1", None)
                if not callable(read_chunk):
                    read_chunk = exc.read
                while size <= MAX_ERROR_BODY_BYTES:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or abandoned.is_set():
                        break
                    self._apply_remaining_socket_timeout(exc, remaining)
                    chunk = read_chunk(min(8192, MAX_ERROR_BODY_BYTES + 1 - size))
                    observe_chunk(chunk)
                    if time.monotonic() >= deadline:
                        chunks = []
                        break
                    if not isinstance(chunk, bytes) or not chunk:
                        if isinstance(chunk, bytes):
                            mark("response_complete")
                        break
                    chunks.append(chunk)
                    size += len(chunk)
            except Exception:
                chunks = []
            finally:
                exc.close()
            return TransportResponse(status_code=int(exc.code), body=b"".join(chunks),
                                     headers=dict(exc.headers.items()) if exc.headers is not None else {},
                                     body_omitted_reason="HTTP_ERROR_BODY_NOT_COLLECTED",
                                     timing_seconds=dict(timings), response_bytes=observations["response_bytes"])
        except APIClientError:
            raise
        except (OSError, urllib.error.URLError) as exc:
            raise _transport_failure(exc, opening_connection=opening_connection) from None


# Keep type identity separate from the injectable constructor. Offline callers
# can prohibit native transport construction without changing type inspection.
_NATIVE_TRANSPORT_CLASS = UrllibTransport


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


def _is_event_stream_headers(headers: Mapping[str, str]) -> bool:
    if not isinstance(headers, Mapping):
        return False
    return any(
        isinstance(name, str) and name.lower() == "content-type"
        and isinstance(value, str) and value.split(";", 1)[0].strip().lower() == "text/event-stream"
        for name, value in headers.items()
    )


def _completion_usage(usage):
    if not isinstance(usage, dict):
        return None
    result = {name: value for name, value in usage.items()
              if name in {"prompt_tokens", "completion_tokens", "total_tokens"}
              and type(value) is int and value >= 0}
    for name in ("prompt_tokens_details", "completion_tokens_details"):
        details = usage.get(name)
        if isinstance(details, dict):
            result[name] = {
                key: value for key, value in details.items()
                if key in {"cached_tokens", "audio_tokens", "reasoning_tokens",
                           "accepted_prediction_tokens", "rejected_prediction_tokens"}
                and type(value) is int and value >= 0
            }
    return result


def _stream_json_completion(parsed):
    """Project a JSON fallback through the same public completion boundary as SSE."""
    result = {name: parsed[name] for name in ("id", "model", "object") if isinstance(parsed.get(name), str)}
    if type(parsed.get("created")) is int:
        result["created"] = parsed["created"]
    usage = _completion_usage(parsed.get("usage"))
    if usage is not None:
        result["usage"] = usage
    result["choices"] = []
    choices = parsed.get("choices")
    for choice in choices if isinstance(choices, list) else []:
        if not isinstance(choice, dict):
            continue
        safe_choice = {"finish_reason": choice.get("finish_reason") if isinstance(choice.get("finish_reason"), str) else None}
        if type(choice.get("index")) is int:
            safe_choice["index"] = choice["index"]
        message = choice.get("message")
        if isinstance(message, dict):
            safe_message = {name: message[name] for name in ("role", "content", "refusal")
                            if isinstance(message.get(name), str)}
            content = message.get("content")
            if content is None:
                safe_message["content"] = None
            elif isinstance(content, list):
                safe_message["content"] = [
                    {"type": part["type"], "text": part["text"]} for part in content
                    if isinstance(part, dict) and isinstance(part.get("type"), str)
                    and part.get("type") in {"text", "output_text"}
                    and isinstance(part.get("text"), str)
                ]
            if isinstance(message.get("function_call"), dict):
                safe_message["function_call"] = {
                    name: message["function_call"][name] for name in ("name", "arguments")
                    if isinstance(message["function_call"].get(name), str)}
            if isinstance(message.get("tool_calls"), list):
                safe_message["tool_calls"] = []
                for call in message["tool_calls"]:
                    if not isinstance(call, dict):
                        continue
                    safe_call = {name: call[name] for name in ("id", "type") if isinstance(call.get(name), str)}
                    if isinstance(call.get("function"), dict):
                        safe_call["function"] = {name: call["function"][name] for name in ("name", "arguments")
                                                 if isinstance(call["function"].get(name), str)}
                    safe_message["tool_calls"].append(safe_call)
            safe_choice["message"] = safe_message
        result["choices"].append(safe_choice)
    return result


class _StreamTextEmitter:
    """Emit content only, withholding a possible credential across chunk edges."""

    def __init__(self, callback, secret: str, check_cancel=None):
        self.callback = callback
        self.secret = secret
        self.check_cancel = check_cancel
        self.pending = ""

    def emit(self, value: str, *, final=False, check_cancellation=True):
        if check_cancellation and self.check_cancel:
            self.check_cancel()
        text = (self.pending + value).replace(self.secret, "[REDACTED]") if self.secret else self.pending + value
        retained = 0
        if not final and self.secret:
            for size in range(min(len(self.secret) - 1, len(text)), 0, -1):
                if text.endswith(self.secret[:size]):
                    retained = size
                    break
        self.pending = text[-retained:] if retained else ""
        safe_text = text[:-retained] if retained else text
        if safe_text:
            try:
                self.callback(safe_text)
            except Exception:
                # Consumer failures may contain credentials or provider text.
                raise APIClientError("STREAM_CALLBACK_FAILED") from None

    def completed(self, value=""):
        """A final preview failure must not discard an already complete reply."""
        try:
            self.emit(value, final=True, check_cancellation=False)
        except APIClientError as error:
            if error.code != "STREAM_CALLBACK_FAILED":
                raise


class _ChatCompletionStream:
    """Bounded SSE decoder producing the normal Chat Completions shape.

    Only assistant content from choice zero reaches the text callback. Reasoning,
    provider metadata, tool arguments and refusal text are never progress text.
    """

    def __init__(self, callback, secret: str, check_cancel=None):
        self.emitter = _StreamTextEmitter(callback, secret, check_cancel)
        self.decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
        self.buffer = ""
        self.data_lines: list[str] = []
        self.data_bytes = 0
        self.bytes_received = 0
        self.content_bytes = 0
        self.done = False
        self.progress_count = 0
        self.choices: dict[int, dict[str, object]] = {}
        self.metadata: dict[str, object] = {}

    def feed(self, chunk: bytes):
        before = self.progress_count
        if not isinstance(chunk, bytes):
            raise APIClientError("RESPONSE_BODY_INVALID")
        self.bytes_received += len(chunk)
        if self.bytes_received > MAX_STREAM_RESPONSE_BYTES:
            raise APIClientError("RESPONSE_TOO_LARGE")
        try:
            self.buffer += self.decoder.decode(chunk)
        except UnicodeError:
            raise APIClientError("INVALID_STREAM_RESPONSE") from None
        self._lines()
        return _StreamProgress(self.done, self.progress_count != before)

    def _lines(self, *, final=False):
        consumed = 0
        for boundary in re.finditer(r"\r\n|\r|\n", self.buffer):
            if not final and boundary.group() == "\r" and boundary.end() == len(self.buffer):
                break
            self._line(self.buffer[consumed:boundary.start()])
            consumed = boundary.end()
        self.buffer = self.buffer[consumed:]
        if len(self.buffer.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise APIClientError("RESPONSE_TOO_LARGE")
        if final and self.buffer:
            self._line(self.buffer)
            self.buffer = ""

    def _line(self, line: str):
        if len(line.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise APIClientError("RESPONSE_TOO_LARGE")
        if self.done:
            return
        if not line:
            self._event()
        elif line.startswith("data:"):
            value = line[5:]
            value = value[1:] if value.startswith(" ") else value
            self.data_bytes += len(value.encode("utf-8")) + 1
            if self.data_bytes > MAX_RESPONSE_BYTES:
                raise APIClientError("RESPONSE_TOO_LARGE")
            self.data_lines.append(value)
        elif line == "data":
            self.data_bytes += 1
            if self.data_bytes > MAX_RESPONSE_BYTES:
                raise APIClientError("RESPONSE_TOO_LARGE")
            self.data_lines.append("")

    def _event(self):
        if not self.data_lines:
            return
        data = "\n".join(self.data_lines)
        self.data_lines.clear()
        self.data_bytes = 0
        if data.strip() == "[DONE]":
            self.done = True
            self.progress_count += 1
            return
        try:
            event = json.loads(data)
        except (ValueError, RecursionError):
            raise APIClientError("INVALID_STREAM_RESPONSE") from None
        if not isinstance(event, dict):
            raise APIClientError("INVALID_RESPONSE_SHAPE")
        if event.get("error") is not None:
            raise APIClientError("MODEL_ERROR_RESPONSE")
        for name in ("id", "model"):
            if isinstance(event.get(name), str):
                self.metadata[name] = event[name]
        if type(event.get("created")) is int:
            self.metadata["created"] = event["created"]
        usage = _completion_usage(event.get("usage"))
        if usage is not None:
            self.metadata["usage"] = usage
        choices = event.get("choices", [])
        if not isinstance(choices, list):
            raise APIClientError("INVALID_RESPONSE_SHAPE")
        for choice in choices:
            if not isinstance(choice, dict):
                raise APIClientError("INVALID_RESPONSE_SHAPE")
            index = choice.get("index", 0)
            if type(index) is not int or not 0 <= index < 128:
                raise APIClientError("INVALID_RESPONSE_SHAPE")
            state = self.choices.setdefault(index, {
                "index": index, "message": {"role": "assistant", "content": None},
                "finish_reason": None,
            })
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                raise APIClientError("INVALID_RESPONSE_SHAPE")
            # Some compatible models generate reasoning before their answer.
            # Observe text activity only; never retain or publish that text as
            # content, evidence, diagnostics or business investigation progress.
            if any(isinstance(delta.get(name), str) and delta[name].strip()
                   for name in ("reasoning_content", "reasoning")):
                self.progress_count += 1
            message = state["message"]
            for name in ("content", "refusal"):
                part = delta.get(name)
                if part is not None and not isinstance(part, str):
                    raise APIClientError("INVALID_RESPONSE_SHAPE")
                if isinstance(part, str):
                    self._retain_text(part)
                    message[name] = (message.get(name) or "") + part
                    if part.strip():
                        self.progress_count += 1
                    if name == "content" and index == 0:
                        self.emitter.emit(part)
            if delta.get("tool_calls") is not None:
                calls = delta["tool_calls"]
                if not isinstance(calls, list):
                    raise APIClientError("INVALID_RESPONSE_SHAPE")
                accumulated = message.setdefault("tool_calls", {})
                for call in calls:
                    if not isinstance(call, dict):
                        raise APIClientError("INVALID_RESPONSE_SHAPE")
                    call_index = call.get("index")
                    if type(call_index) is not int or not 0 <= call_index < 128:
                        raise APIClientError("INVALID_RESPONSE_SHAPE")
                    target = accumulated.setdefault(call_index, {"function": {}})
                    for name in ("id", "type"):
                        if isinstance(call.get(name), str):
                            target[name] = call[name]
                    function = call.get("function", {})
                    if not isinstance(function, dict):
                        raise APIClientError("INVALID_RESPONSE_SHAPE")
                    for name in ("name", "arguments"):
                        part = function.get(name)
                        if part is not None and not isinstance(part, str):
                            raise APIClientError("INVALID_RESPONSE_SHAPE")
                        if isinstance(part, str):
                            self._retain_text(part)
                            target["function"][name] = target["function"].get(name, "") + part
                            if part.strip():
                                self.progress_count += 1
            finish = choice.get("finish_reason")
            if finish is not None:
                if not isinstance(finish, str):
                    raise APIClientError("INVALID_RESPONSE_SHAPE")
                if finish and finish != state["finish_reason"]:
                    self.progress_count += 1
                state["finish_reason"] = finish

    def _retain_text(self, part):
        self.content_bytes += len(part.encode("utf-8"))
        if self.content_bytes > MAX_RESPONSE_BYTES:
            raise APIClientError("RESPONSE_TOO_LARGE")

    def finish(self) -> dict[str, object]:
        try:
            self.buffer += self.decoder.decode(b"", final=True)
        except UnicodeError:
            raise APIClientError("INVALID_STREAM_RESPONSE") from None
        self._lines(final=True)
        self._event()
        # A socket EOF is not evidence of a complete generation. A terminal
        # choice is mandatory, including when the gateway supplies [DONE].
        if not self.choices or any(not choice["finish_reason"] for choice in self.choices.values()):
            raise APIClientError("STREAM_INCOMPLETE")
        for choice in self.choices.values():
            message = choice["message"]
            calls = message.get("tool_calls")
            if isinstance(calls, dict):
                message["tool_calls"] = [calls[index] for index in sorted(calls)]
        # The complete response must reach the durable journal before the
        # business workflow observes a newly arrived cancellation.
        self.emitter.completed()
        return {**self.metadata, "object": "chat.completion",
                "choices": [self.choices[index] for index in sorted(self.choices)]}


class OpenAICompatibleChatClient:
    """Small synchronous client for the Chat Completions baseline."""

    def __init__(
        self,
        config: CompanyAPIConfig,
        *,
        transport: Transport | None = None,
        allow_network: bool = False,
        diagnostics: APIResponseDiagnostics | None = None,
        diagnostic_phase: str = "investigation",
        request_observer=None,
    ) -> None:
        self.config = config
        self.allow_network = bool(allow_network)
        self._diagnostics = diagnostics
        self._diagnostic_phase = diagnostic_phase
        self._request_observer = request_observer
        # Numeric observations for the last attempted exchange, including
        # failures. These are response bytes, not generated-token timings.
        self.last_request_timing: dict[str, object] = {}
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
        timeout_seconds: float | None = None,
        check_cancel=None,
        on_text_delta: Callable[[str], None] | None = None,
    ) -> dict[str, object]:
        """Create one chat completion, optionally streaming assistant content.

        Streaming reconstructs the usual response shape and never emits hidden
        reasoning or tool arguments. A gateway returning JSON is accepted on the
        same request; an unsupported stream is never retried automatically.
        Exact echoed API credentials are defensively redacted in both modes.
        """

        if on_text_delta is not None and not callable(on_text_delta):
            raise APIClientError("STREAM_CALLBACK_INVALID")
        if isinstance(messages, (str, bytes)) or not messages:
            raise APIClientError("MESSAGES_INVALID")
        try:
            normalized_messages = [dict(message) for message in messages]
        except (TypeError, ValueError):
            raise APIClientError("MESSAGES_INVALID") from None

        payload: dict[str, object] = {
            "model": self.config.chat_model,
            "messages": normalized_messages,
            "max_tokens": self.config.max_output_tokens,
        }
        if on_text_delta is not None:
            payload["stream"] = True
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

        response, _ = self._request_json("POST", "chat/completions", payload,
                                       timeout_seconds=timeout_seconds, check_cancel=check_cancel,
                                       on_text_delta=on_text_delta)
        return response

    def suppress_error_capture(self, code: str) -> None:
        if self._diagnostics is not None:
            self._diagnostics.suppress_latest_error(outcome_code=code)

    def _request_json(
        self,
        method: str,
        endpoint: str,
        payload: Mapping[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
        check_cancel=None,
        on_text_delta=None,
    ) -> tuple[dict[str, object], int]:
        self.last_request_timing = {}
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
            if len(request_body) > MAX_REQUEST_BYTES:
                raise APIClientError("REQUEST_TOO_LARGE")

        base_url = self.config.base_url.strip().rstrip("/")
        safe_endpoint = endpoint.strip("/")
        effective_timeout = float(self.config.timeout_seconds)
        if timeout_seconds is not None:
            if (type(timeout_seconds) not in (int, float)
                    or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
                raise APIClientError("REQUEST_TIMEOUT")
            effective_timeout = min(effective_timeout, float(timeout_seconds))
        request = TransportRequest(
            method=method.upper(),
            url=f"{base_url}/{safe_endpoint}",
            headers={
                "Accept": "text/event-stream, application/json" if on_text_delta is not None else "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {secret}",
            },
            body=request_body,
            timeout_seconds=effective_timeout,
            endpoint=safe_endpoint,
        )
        if check_cancel:
            check_cancel()
        if self._request_observer is not None and request_body is not None:
            self._request_observer(request_body)
        started = time.monotonic()
        response: object = None
        response_received = False
        exchange: dict[str, object] | None = None
        cancellation = []

        def cancel():
            try:
                if check_cancel:
                    check_cancel()
            except BaseException as error:
                cancellation.append(error)
                raise

        stream = (_ChatCompletionStream(on_text_delta, secret, cancel if check_cancel else None)
                  if on_text_delta is not None else None)
        try:
            try:
                if isinstance(self._transport, _NATIVE_TRANSPORT_CLASS):
                    transport_options = {"check_cancel": cancel if check_cancel else None}
                    if stream is not None:
                        transport_options["on_response_chunk"] = stream.feed
                    response = self._transport(request, **transport_options)
                else:
                    response = self._transport(request)
                # A completed response must reach the caller's durable journal
                # before it observes cancellation. Waiting is interruptible in
                # the native transport; post-response cancellation belongs to
                # the business workflow after record_remote_response.
                received_at = time.monotonic() if safe_endpoint == "chat/completions" else None
                if (not isinstance(self._transport, _NATIVE_TRANSPORT_CLASS)
                        and safe_endpoint == "chat/completions"
                        and received_at - started >= effective_timeout):
                    raise APIClientError("REQUEST_TIMEOUT", transport_reason="timeout")
                response_received = True
                if isinstance(response, TransportResponse):
                    count = response.response_bytes
                    if count is None and isinstance(response.body, (bytes, str)):
                        count = len(response.body) if isinstance(response.body, bytes) else len(response.body.encode("utf-8", errors="replace"))
                    self.last_request_timing = _safe_request_timing({
                        "timing_seconds": response.timing_seconds, "response_bytes": count},
                        elapsed=received_at - started if received_at is not None else None)
            except APIClientError:
                raise
            except Exception as exc:  # injected transports are untrusted at this boundary
                if cancellation and exc is cancellation[-1]:
                    raise
                raise _transport_failure(exc) from None
            streaming_response = (
                stream is not None and isinstance(response, TransportResponse)
                and type(response.status_code) is int and 200 <= response.status_code < 300
                and _is_event_stream_headers(response.headers)
            )
            if streaming_response:
                if not isinstance(response.body, (bytes, str)):
                    raise APIClientError("RESPONSE_BODY_INVALID")
                raw_size = len(response.body.encode("utf-8")) if isinstance(response.body, str) else len(response.body)
                if raw_size > MAX_STREAM_RESPONSE_BYTES:
                    raise APIClientError("RESPONSE_TOO_LARGE")
                if not stream.bytes_received:
                    body = response.body
                    if isinstance(body, str):
                        body = body.encode("utf-8")
                    stream.feed(body)
                parsed_stream = stream.finish()
                # Diagnostics and ordinary response validation see only the
                # assembled completion, never raw SSE reasoning/provider data.
                response = TransportResponse(
                    response.status_code,
                    json.dumps(parsed_stream, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    timing_seconds=response.timing_seconds, response_bytes=response.response_bytes,
                )
            elif stream is not None:
                parsed_json, status = self._validate_response(response, secret,
                    protected_values=(secret, self.config.base_url, self.config.chat_model, self.config.embedding_model),
                    request_body=request_body)
                response = TransportResponse(status,
                    json.dumps(_stream_json_completion(parsed_json), ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    timing_seconds=response.timing_seconds, response_bytes=response.response_bytes)
            # Capture before HTTP status, body shape, or JSON validation. The
            # shared collector redacts first and stores a bounded text preview.
            if self._diagnostics is not None:
                exchange = self._capture_response(
                    response, safe_endpoint, started, "RESPONSE_RECEIVED"
                )
            validated = self._validate_response(response, secret, protected_values=(secret, self.config.base_url,
                self.config.chat_model, self.config.embedding_model), request_body=request_body)
            if stream is not None and not streaming_response:
                message = _first_message(validated[0])
                content = message.get("content") if message is not None else None
                if isinstance(content, str):
                    stream.emitter.completed(content)
            return validated
        except APIClientError as exc:
            if not self.last_request_timing:
                self.last_request_timing = _safe_request_timing(getattr(exc, "request_timing", None),
                    elapsed=time.monotonic() - started if safe_endpoint == "chat/completions" else None)
            if self._diagnostics is not None:
                if not response_received:
                    exchange = self._capture_response(None, safe_endpoint, started, exc.code)
                elif stream is not None and exchange is None:
                    # Decoding failed before an assembled completion existed.
                    # Record the status without retaining raw SSE/error text.
                    safe_response = (TransportResponse(
                        response.status_code, b"", body_omitted_reason="HTTP_ERROR_BODY_NOT_COLLECTED",
                    ) if isinstance(response, TransportResponse) else None)
                    exchange = self._capture_response(safe_response, safe_endpoint, started, exc.code)
                elif exchange is not None:
                    self._diagnostics.suppress_latest_error(outcome_code=exc.code)
                if exchange is not None:
                    exchange["diagnostic"] = sanitize_diagnostic(exc.diagnostic)
                    if exc.transport_reason is not None:
                        exchange["transport_reason"] = exc.transport_reason
            raise

    def _capture_response(
        self, response: object, endpoint: str, started: float, outcome_code: str
    ) -> dict[str, object] | None:
        assert self._diagnostics is not None
        valid_response = isinstance(response, TransportResponse)
        omission = None
        if valid_response:
            # Omission annotations from injected transports are untrusted.
            if (type(response.status_code) is int and not 200 <= response.status_code < 300) or is_error_envelope(response.body):
                omission = "ERROR_RESPONSE_BODY_OMITTED"
            elif response.body_omitted_reason == "HTTP_ERROR_BODY_NOT_COLLECTED":
                omission = "HTTP_ERROR_BODY_NOT_COLLECTED"
            elif not isinstance(response.body, (bytes, str)):
                omission = "RESPONSE_BODY_INVALID"
        else:
            omission = "NO_RESPONSE" if response is None else "TRANSPORT_RESPONSE_INVALID"
        record = self._diagnostics.record(
            phase=self._diagnostic_phase,
            endpoint=endpoint,
            http_status=response.status_code if valid_response else None,
            outcome_code=outcome_code,
            body=response.body if valid_response else None,
            elapsed_ms=(time.monotonic() - started) * 1000,
            body_omitted_reason=omission,
        )
        if record is not None:
            record["request_timing"] = self.last_request_timing
        return record

    @staticmethod
    def _validate_response(
        response: object, secret: str, *, protected_values=(), request_body=None
    ) -> tuple[dict[str, object], int]:
        if not isinstance(response, TransportResponse):
            raise APIClientError("TRANSPORT_RESPONSE_INVALID")
        status = response.status_code
        if not isinstance(status, int) or not 100 <= status <= 599:
            raise APIClientError("HTTP_STATUS_INVALID")
        if not 200 <= status < 300:
            raise APIClientError("HTTP_ERROR", http_status=status, diagnostic=build_diagnostic("HTTP_ERROR",
                http_status=status, body=response.body, headers=response.headers,
                protected_values=protected_values or (secret,), request_body=request_body))

        raw_body = response.body
        if not isinstance(raw_body, (bytes, str)):
            raise APIClientError("RESPONSE_BODY_INVALID", http_status=status)
        body_bytes = len(raw_body.encode("utf-8", errors="replace")) if isinstance(raw_body, str) else len(raw_body)
        if body_bytes > MAX_RESPONSE_BYTES:
            raise APIClientError("RESPONSE_TOO_LARGE", http_status=status)
        try:
            text_body = (
                raw_body.decode("utf-8-sig") if isinstance(raw_body, bytes) else raw_body.removeprefix("\ufeff")
            )
            parsed = json.loads(text_body)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise APIClientError("INVALID_JSON_RESPONSE", http_status=status) from None
        if not isinstance(parsed, dict):
            raise APIClientError("INVALID_RESPONSE_SHAPE", http_status=status)
        if is_error_envelope(raw_body):
            raise APIClientError("MODEL_ERROR_RESPONSE", http_status=status,
                diagnostic=build_diagnostic("MODEL_ERROR_RESPONSE", http_status=status, body=raw_body,
                    headers=response.headers, protected_values=protected_values or (secret,), request_body=request_body))
        try:
            redacted = _redact_secret(parsed, secret)
        except RecursionError:
            raise APIClientError(
                "RESPONSE_NESTING_TOO_DEEP", http_status=status
            ) from None
        return redacted, status


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
        "MAX_OUTPUT_TOKENS_INVALID",
    }:
        return _capability("NOT_RUN", error.code)
    diagnostic = sanitize_diagnostic(error.diagnostic)
    evidence = {"diagnostic": diagnostic}
    if status is not None:
        evidence["http_status"] = status
    category = diagnostic.get("category") if diagnostic else None
    if error.code in {"HTTP_ERROR", "MODEL_ERROR_RESPONSE"}:
        if status in {401, 403}:
            return _capability(
                "UNAVAILABLE", "AUTHORIZATION_FAILED", evidence=evidence
            )
        if status == 429:
            return _capability(
                "UNAVAILABLE", "RATE_LIMITED" if category == "rate_limit" else "QUOTA_EXHAUSTED" if category == "quota_exhausted" else "HTTP_429_UNCONFIRMED", evidence=evidence
            )
        if status is not None and status >= 500:
            return _capability(
                "UNAVAILABLE", "REMOTE_SERVER_ERROR", evidence=evidence
            )
        if feature_request and category == "unsupported_feature":
            return _capability(
                "UNSUPPORTED", "FEATURE_REQUEST_REJECTED", evidence=evidence
            )
        return _capability(
            "UNAVAILABLE", "REQUEST_REJECTED", evidence=evidence
        )
    return _capability("INDETERMINATE", error.code, evidence=evidence)


class CapabilityProbe:
    """Behaviorally probe an OpenAI-compatible service one feature at a time."""

    def __init__(
        self,
        config: CompanyAPIConfig,
        *,
        transport: Transport | None = None,
        allow_network: bool = False,
        probe_embeddings: bool = False,
        diagnostics: APIResponseDiagnostics | None = None,
    ) -> None:
        self.config = config
        self.client = OpenAICompatibleChatClient(
            config, transport=transport, allow_network=allow_network,
            diagnostics=diagnostics, diagnostic_phase="capability_probe",
        )
        self.probe_embeddings = bool(probe_embeddings)
        self._audit: list[dict[str, object]] = []
        self._deadline: float | None = None

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
        event["diagnostic"] = sanitize_diagnostic(error.diagnostic)
        self._audit.append(event)

    def _request(
        self,
        capability: str,
        method: str,
        endpoint: str,
        payload: Mapping[str, object] | None = None,
    ) -> tuple[dict[str, object], int] | APIClientError:
        remaining: float | None = None
        if self._deadline is not None:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                error = APIClientError("PROBE_TIMEOUT")
                self._record_error(capability, method, endpoint, error)
                return error
        try:
            data, status = self.client._request_json(
                method,
                endpoint,
                payload,
                timeout_seconds=remaining,
            )
        except APIClientError as exc:
            self._record_error(capability, method, endpoint, exc)
            return exc
        if self._deadline is not None and time.monotonic() > self._deadline:
            error = APIClientError("PROBE_TIMEOUT")
            self._record_error(capability, method, endpoint, error)
            return error
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

        # The configured timeout is one application-level deadline budget for
        # the complete probe, not a fresh allowance for each request.  Late
        # results are rejected and urllib sockets receive the remaining time;
        # synchronous DNS and injected transports cannot be forcibly cancelled.
        self._deadline = time.monotonic() + float(self.config.timeout_seconds)

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

        readiness_capabilities = [
            capabilities["chat"],
            capabilities["tool_calling"],
            capabilities["strict_json"],
        ]
        if self.probe_embeddings:
            readiness_capabilities.append(capabilities["embeddings"])
        statuses = {item["status"] for item in readiness_capabilities}
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
                "max_tokens": 16,
            },
        )
        if isinstance(outcome, APIClientError):
            return _error_capability(outcome, feature_request=False)
        data, status = outcome
        message = _first_message(data)
        role = message.get("role") if message is not None else None
        content = message.get("content") if message is not None else None
        if role != "assistant" or not isinstance(content, str) or content.strip() != "OK":
            return _capability(
                "INDETERMINATE",
                "EXPECTED_CHAT_CONTENT_NOT_OBSERVED",
                evidence={
                    "http_status": status,
                    "request_accepted": True,
                    "assistant_role_observed": role == "assistant",
                    "nonempty_content_observed": isinstance(content, str)
                    and bool(content.strip()),
                },
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
        user_message = {
            "role": "user",
            "content": "Call capability_probe with ok set to true.",
        }
        tool_definition = {
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
        forced_choice = {
            "type": "function",
            "function": {"name": tool_name},
        }
        outcome = self._request(
            "tool_calling",
            "POST",
            "chat/completions",
            {
                "model": self.config.chat_model,
                "messages": [user_message],
                "tools": [tool_definition],
                "tool_choice": forced_choice,
                "max_tokens": 64,
            },
        )
        if isinstance(outcome, APIClientError):
            return _error_capability(outcome, feature_request=True)
        data, status = outcome
        message = _first_message(data)
        call_id: str | None = None
        calls = message.get("tool_calls") if message is not None else None
        if (
            message is not None
            and message.get("role") == "assistant"
            and isinstance(calls, list)
            and len(calls) == 1
        ):
            for call in calls:
                if not isinstance(call, Mapping):
                    continue
                if call.get("type", "function") != "function":
                    continue
                function = call.get("function")
                if not isinstance(function, Mapping) or function.get("name") != tool_name:
                    continue
                arguments = function.get("arguments")
                if not isinstance(arguments, str):
                    continue
                try:
                    parsed_arguments = json.loads(arguments)
                except (ValueError, RecursionError):
                    continue
                candidate_id = call.get("id")
                if (
                    parsed_arguments == {"ok": True}
                    and isinstance(candidate_id, str)
                    and 1 <= len(candidate_id) <= 256
                ):
                    call_id = candidate_id
                    break
        if call_id is None:
            return _capability(
                "UNSUPPORTED",
                "VALID_TOOL_CALL_NOT_OBSERVED",
                evidence={
                    "http_status": status,
                    "request_accepted": True,
                    "expected_behavior_observed": False,
                },
            )

        canonical_call = {
            "id": call_id,
            "type": "function",
            "function": {"name": tool_name, "arguments": '{"ok":true}'},
        }
        round_trip = self._request(
            "tool_calling",
            "POST",
            "chat/completions",
            {
                "model": self.config.chat_model,
                "messages": [
                    user_message,
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [canonical_call],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": tool_name,
                        "content": '{"ok":true}',
                    },
                ],
                "tools": [tool_definition],
                "tool_choice": "auto",
                "max_tokens": 64,
            },
        )
        if isinstance(round_trip, APIClientError):
            return _error_capability(round_trip, feature_request=True)
        final_data, final_status = round_trip
        final_message = _first_message(final_data)
        final_content = (
            final_message.get("content") if final_message is not None else None
        )
        if (
            final_message is None
            or final_message.get("role") != "assistant"
            or not isinstance(final_content, str)
            or not final_content.strip()
        ):
            return _capability(
                "UNSUPPORTED",
                "TOOL_RESULT_ROUND_TRIP_NOT_OBSERVED",
                evidence={
                    "http_status": final_status,
                    "tool_call_observed": True,
                    "tool_result_accepted": True,
                    "final_assistant_message_observed": False,
                },
            )
        return _capability(
            "SUPPORTED",
            "TOOL_RESULT_ROUND_TRIP_OBSERVED",
            evidence={
                "http_status": final_status,
                "forced_named_tool": True,
                "arguments_valid_json": True,
                "tool_result_round_trip": True,
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
                "max_tokens": 32,
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
            except (ValueError, RecursionError):
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
            and all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and (isinstance(value, int) or math.isfinite(value))
                for value in vector
            )
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
        chat_supported = (
            capabilities.get("chat", {}).get("status") == "SUPPORTED"
        )
        tool_supported = (
            capabilities.get("tool_calling", {}).get("status") == "SUPPORTED"
        )
        strict_json_supported = (
            capabilities.get("strict_json", {}).get("status") == "SUPPORTED"
        )
        if chat_supported and tool_supported:
            readiness_mode = "NATIVE_TOOL_CALLING"
        elif chat_supported and strict_json_supported:
            readiness_mode = "VALIDATED_JSON_FALLBACK"
        else:
            readiness_mode = "UNAVAILABLE"
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
            "agent_readiness": {
                "ready": readiness_mode != "UNAVAILABLE",
                "mode": readiness_mode,
                "provider_strict_json": capabilities.get("strict_json", {}).get(
                    "status"
                )
                == "SUPPORTED",
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
    if not isinstance(choices, list) or len(choices) != 1:
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
    diagnostics: APIResponseDiagnostics | None = None,
) -> dict[str, object]:
    """Convenience wrapper around :class:`CapabilityProbe`."""

    return CapabilityProbe(
        config,
        transport=transport,
        allow_network=allow_network,
        probe_embeddings=probe_embeddings,
        diagnostics=diagnostics,
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
        "agent_readiness": {
            "ready": False,
            "mode": "UNAVAILABLE",
            "provider_strict_json": False,
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
    parser.add_argument(
        "--max-output-tokens", type=int,
        help="Output limit override; otherwise use environment, local file, then the 1024-token adapter default."
    )
    parser.add_argument("--allow-insecure-localhost", action="store_true")
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
            max_output_tokens=args.max_output_tokens,
            allow_insecure_localhost=args.allow_insecure_localhost,
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
    if report["overall_status"] == "COMPLETED" and report["agent_readiness"][
        "ready"
    ]:
        return 0
    return 2 if report["overall_status"] == "NOT_RUN" else 1


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
