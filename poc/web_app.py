#!/usr/bin/env python3
"""Loopback-only, dependency-free web adapter for the source analysis workflow."""

from __future__ import annotations

import argparse
import codecs
import copy
from contextlib import closing
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from typing import Callable, Sequence
from urllib.parse import parse_qs, urlsplit
import webbrowser

from analyze_source import ARTIFACT_NAMES, AnalysisCancelled, _paths, analyze_source
from company_api import APIConfigurationError, CompanyAPIConfig
from investigation_tools import InvestigationTools
from repo_inventory import parse_extensions


WEB_ROOT = Path(__file__).resolve().parent / "web"
STATIC_FILES = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/i18n.js": "i18n.js", "/styles.css": "styles.css"}
MAX_REQUEST_BYTES = 32_768
MAX_GRAPH_EDGES = 200
OUTPUT_NAMES = frozenset((*ARTIFACT_NAMES, "structural-index.sqlite-wal", "structural-index.sqlite-shm", "source-catalog.sqlite-wal", "source-catalog.sqlite-shm"))
EVIDENCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}")


class RequestError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _project(source: str | None = None, output: str | None = None, run_id: str | None = None) -> dict:
    return {
        "run_id": run_id, "source": source, "output": output,
        "diagnosis": None, "programs": [], "agent": None, "snapshot_id": None,
        "relations": {"edges": [], "truncated": False}, "catalog_snapshot_id": None,
    }


def _text(payload: dict, key: str, *, required: bool = False, maximum: int = 1024) -> str | None:
    value = payload.get(key)
    if not required and (value is None or (isinstance(value, str) and not value.strip())):
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise RequestError("INVALID_OPTIONS", f"{key} 必须是有效的非空文本。")
    return value.strip()


def _validate_options(payload: object) -> dict:
    fields = {"source", "output", "encoding", "source_format", "entry", "question", "allow_network", "extensions", "index_mode", "verify_content"}
    if not isinstance(payload, dict) or set(payload) - fields:
        raise RequestError("INVALID_OPTIONS", "请求包含未知设置。")
    source_text, output_text = _text(payload, "source", required=True), _text(payload, "output", required=True)
    source_input, output_input = Path(source_text).expanduser(), Path(output_text).expanduser()
    if not source_input.is_absolute() or not output_input.is_absolute():
        raise RequestError("INVALID_PATH", "源码和输出位置必须使用本机绝对路径。")
    try:
        source, output = _paths(source_input, output_input)
        if not source.is_dir():
            raise ValueError("missing source")
        # Only a dedicated analysis directory may be reused. This avoids
        # overwriting unrelated documents with reserved report filenames.
        if output.exists() and any(path.name not in OUTPUT_NAMES for path in output.iterdir()):
            raise ValueError("unrelated output contents")
    except (OSError, ValueError):
        raise RequestError(
            "INVALID_PATH",
            "请使用存在的源码目录和独立的输出目录。输出须为空、新目录或只含分析产物，且不能与源码相互嵌套或使用符号链接产物。",
        ) from None
    encoding = _text(payload, "encoding", maximum=64) or "auto"
    if encoding != "auto":
        try:
            codecs.lookup(encoding)
        except LookupError:
            raise RequestError("INVALID_ENCODING", "无法识别所选源码编码。") from None
    source_format = _text(payload, "source_format", maximum=16) or "auto"
    if source_format not in {"auto", "fixed", "free"}:
        raise RequestError("INVALID_SOURCE_FORMAT", "源码格式须为 auto、fixed 或 free。")
    allow_network = payload.get("allow_network", False)
    if type(allow_network) is not bool:
        raise RequestError("INVALID_OPTIONS", "allow_network 必须为布尔值。")
    index_mode = _text(payload, "index_mode", maximum=16) or "catalog"
    if index_mode not in {"catalog", "full"}:
        raise RequestError("INVALID_OPTIONS", "index_mode 必须是 catalog 或 full。")
    verify_content = payload.get("verify_content", False)
    if type(verify_content) is not bool:
        raise RequestError("INVALID_OPTIONS", "verify_content 必须为布尔值。")
    extension_text = _text(payload, "extensions", maximum=256)
    try:
        extensions = parse_extensions(extension_text)
    except ValueError:
        raise RequestError("INVALID_EXTENSIONS", "扩展名格式无效。") from None
    return {
        "source": source, "output": output, "encoding": encoding, "source_format": source_format,
        "entry": _text(payload, "entry"), "question": _text(payload, "question", maximum=8000),
        "allow_network": allow_network, "extensions": extensions, "index_mode": index_mode, "verify_content": verify_content,
    }


def _read_json(path: Path) -> object:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16_000_000:
        raise ValueError("invalid report")
    return json.loads(path.read_text(encoding="utf-8"))


def _relationships(database: Path, snapshot_id: str) -> dict:
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        stored_snapshot = connection.execute("SELECT value FROM metadata WHERE key='snapshot_id'").fetchone()
        if stored_snapshot is None or stored_snapshot[0] != snapshot_id:
            raise ValueError("relationship snapshot mismatch")
        rows = [dict(row) for row in connection.execute(
            """SELECT r.relation_id, u.program_name AS source_program,
                      r.target_name, r.relation_type, r.status, r.target_entity_id,
                      e.evidence_id, e.relative_path, e.start_line, e.end_line
                 FROM relations r
                 JOIN code_units u ON u.unit_id = r.from_entity_id
                 JOIN evidence_spans e ON e.evidence_id = r.evidence_id
                WHERE r.relation_type IN ('CALLS', 'CALL_TARGET_FROM', 'INCLUDES_COPY')
                ORDER BY e.relative_path, e.start_line, r.relation_type
                LIMIT ?""", (MAX_GRAPH_EDGES + 1,),
        )]
    return {"edges": rows[:MAX_GRAPH_EDGES], "truncated": len(rows) > MAX_GRAPH_EDGES, "snapshot_id": snapshot_id}


def _environment_config() -> CompanyAPIConfig:
    return CompanyAPIConfig.from_env(timeout_seconds=60.0, max_output_tokens=4096)


class WorkbenchState:
    """One in-memory browser session, one job, and one current source snapshot."""

    def __init__(self, *, analyzer: Callable = analyze_source, config_provider: Callable = _environment_config) -> None:
        self.session_token = secrets.token_urlsafe(32)
        self.analyzer, self.config_provider = analyzer, config_provider
        self.lock = threading.RLock()
        self.project = _project()
        self.job: dict | None = None
        self.cancel_event = threading.Event()
        self.started_at = self.updated_at = self.phase_started_at = 0.0
        self.phase_start_completed = 0.0

    def state(self) -> dict:
        configuration_error = None
        try:
            config = self.config_provider()
            config.validate()
            configured = True
        except APIConfigurationError as exc:
            configured = False
            configuration_error = exc.code
        except (TypeError, ValueError):
            configured = False
            configuration_error = "CONFIGURATION_INVALID"
        with self.lock:
            return {
                "session_token": self.session_token,
                "api_configured": configured,
                "api_configuration_error": configuration_error,
                "active_job_id": self.job["job_id"] if self.job and self.job["status"] == "RUNNING" else None,
                "project": copy.deepcopy(self.project),
                "job": self._job_snapshot() if self.job else None,
            }

    def start(self, payload: object) -> dict:
        with self.lock:
            if self.job and self.job["status"] == "RUNNING":
                raise RequestError("JOB_ACTIVE", "当前分析尚未结束，请等待完成。", 409)
        try:
            options = _validate_options(payload)
        except RequestError:
            with self.lock:
                if not self.job or self.job["status"] != "RUNNING":
                    self.project = _project()
                    self.job = None
            raise
        with self.lock:
            if self.job and self.job["status"] == "RUNNING":
                raise RequestError("JOB_ACTIVE", "当前分析尚未结束，请等待完成。", 409)
            job_id = secrets.token_hex(16)
            # Clear answers, graph and evidence authority before the worker can
            # read an older output directory or fail while refreshing it.
            self.project = _project(str(options["source"]), str(options["output"]), job_id)
            self.cancel_event = threading.Event()
            self.started_at = self.updated_at = self.phase_started_at = time.monotonic()
            self.phase_start_completed = 0.0
            self.job = {"job_id": job_id, "status": "RUNNING", "result": None, "error": None,
                        "cancel_requested": False, "progress": {"phase": "preparing", "completed": 0,
                        "total": None, "unit": "files", "current_file": None,
                        "bytes_completed": 0, "bytes_total": None}}
            threading.Thread(target=self._run, args=(job_id, options), daemon=True).start()
            return {"job_id": job_id, "status": "RUNNING"}

    def _job_snapshot(self) -> dict:
        result = copy.deepcopy(self.job)
        now = time.monotonic()
        progress = result["progress"]
        progress["elapsed_seconds"] = round((now if result["status"] == "RUNNING" else self.updated_at) - self.started_at, 2)
        progress["last_update_seconds"] = round(max(0.0, now - self.updated_at), 2)
        phase_elapsed = max(0.0, self.updated_at - self.phase_started_at)
        completed, total = progress.get("completed", 0), progress.get("total")
        delta = completed - self.phase_start_completed
        progress["eta_seconds"] = (round(max(0.0, (total - completed) * phase_elapsed / delta), 1)
            if total and delta > 0 and phase_elapsed >= 0.25 and result["status"] == "RUNNING" else None)
        progress["eta_scope"] = "current_phase"
        file_completed, file_total = progress.get("file_completed", 0), progress.get("file_total")
        if file_total and file_completed > 0 and phase_elapsed >= 0.25 and result["status"] == "RUNNING":
            progress["eta_seconds"] = round(max(0.0, (file_total - file_completed) * phase_elapsed / file_completed), 1)
            progress["eta_scope"] = "current_file"
        return result

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise AnalysisCancelled("Cancelled by the user.")

    def _progress(self, job_id: str, event: dict) -> None:
        self._check_cancel()
        with self.lock:
            if not self.job or self.job["job_id"] != job_id:
                raise AnalysisCancelled("The current job was replaced.")
            previous = self.job["progress"]
            if event.get("phase") != previous.get("phase") or event.get("total") != previous.get("total"):
                self.phase_started_at = time.monotonic()
                self.phase_start_completed = event.get("completed", 0)
            # Stage-specific counters are replaced, never inherited from a prior
            # stage with a different denominator or measurement unit.
            self.job["progress"] = {"phase": event.get("phase", "working"), "completed": event.get("completed", 0),
                "total": event.get("total"), "unit": event.get("unit", "files"), "current_file": event.get("current_file"),
                "bytes_completed": event.get("bytes_completed", 0), "bytes_total": event.get("bytes_total"),
                **{key: value for key, value in event.items() if key in {"file_completed", "file_total", "file_unit"}}}
            self.updated_at = time.monotonic()

    def cancel(self, job_id: str) -> dict:
        with self.lock:
            if not self.job or self.job["job_id"] != job_id:
                raise RequestError("JOB_NOT_FOUND", "任务不存在或已被替换。", 404)
            if self.job["status"] == "RUNNING":
                self.job["cancel_requested"] = True
                self.cancel_event.set()
            return self._job_snapshot()

    def _run(self, job_id: str, options: dict) -> None:
        try:
            source, output = options["source"], options["output"]
            arguments = {key: value for key, value in options.items() if key not in {"source", "output"}}
            if options["allow_network"] and options["question"]:
                try:
                    arguments["config"] = self.config_provider()
                except APIConfigurationError:
                    # The core workflow records a safe configuration reason.
                    pass
            arguments["progress"] = lambda event: self._progress(job_id, event)
            arguments["check_cancel"] = self._check_cancel
            report = self.analyzer(source, output, **arguments)
            self._check_cancel()
            project = _project(str(source), str(output), job_id)
            project["diagnosis"] = report
            project["catalog_snapshot_id"] = report.get("catalog_snapshot_id")
            program_report = _read_json(output / "programs.json")
            agent = _read_json(output / "agent-result.json")
            snapshot = (report.get("build_report") or {}).get("snapshot_id")
            # A failed refresh can deliberately preserve an older SQLite file.
            # It must never authorize that old file as this browser's snapshot.
            if report.get("source_manifest_verified") is True and snapshot:
                if program_report.get("snapshot_id") != snapshot:
                    raise ValueError("snapshot report mismatch")
                agent_snapshot = (agent.get("agent_result") or {}).get("snapshot_id")
                if agent_snapshot is not None and agent_snapshot != snapshot:
                    raise ValueError("answer snapshot mismatch")
                project.update(
                    programs=program_report["programs"], agent=agent, snapshot_id=snapshot,
                    relations=_relationships(output / "structural-index.sqlite", snapshot),
                )
            elif report.get("catalog_ready") is True:
                project.update(programs=program_report["programs"], agent=agent)
            else:
                project["agent"] = {"runner_status": "NOT_READY", "reason_code": report.get("reason_code"), "agent_result": None}
            with self.lock:
                if self.job and self.job["job_id"] == job_id:
                    self.project = project
                    self.updated_at = time.monotonic()
                    self.job.update(status="COMPLETED", result=copy.deepcopy(project))
        except AnalysisCancelled:
            with self.lock:
                if self.job and self.job["job_id"] == job_id:
                    self.updated_at = time.monotonic()
                    self.project = _project(str(options["source"]), str(options["output"]), job_id)
                    self.job.update(status="CANCELLED", result=None)
        except Exception:
            # Never serialize raw adapter errors, environment values or remote
            # responses. Failed work leaves no current evidence authority.
            with self.lock:
                if self.job and self.job["job_id"] == job_id:
                    self.updated_at = time.monotonic()
                    self.job.update(status="FAILED", error={"code": "ANALYSIS_FAILED", "message": "本次分析未完成。请检查输入路径和源码格式后重试。"})

    def get_job(self, job_id: str) -> dict:
        with self.lock:
            if not self.job or self.job["job_id"] != job_id:
                raise RequestError("JOB_NOT_FOUND", "任务不存在或已由新的源码分析替换。", 404)
            return self._job_snapshot()

    def evidence(self, evidence_id: str) -> dict:
        if EVIDENCE_ID.fullmatch(evidence_id) is None:
            raise RequestError("INVALID_EVIDENCE_ID", "证据标识无效。")
        with self.lock:
            if not self.project["snapshot_id"] or (self.job and self.job["status"] == "RUNNING"):
                raise RequestError("NO_CURRENT_INDEX", "当前没有完成并验证的源码索引。", 409)
            database = Path(self.project["output"]) / "structural-index.sqlite"
            if database.is_symlink():
                raise RequestError("INDEX_CHANGED", "索引位置已发生变化，请重新分析。", 409)
            try:
                result = InvestigationTools(database).read_evidence([evidence_id])
            except (OSError, ValueError, sqlite3.Error):
                raise RequestError("INDEX_UNAVAILABLE", "当前源码索引不可用，请重新分析。", 409) from None
            if result["snapshot_id"] != self.project["snapshot_id"]:
                raise RequestError("INDEX_CHANGED", "索引快照已变化，请重新分析。", 409)
            if not result["spans"]:
                raise RequestError("EVIDENCE_NOT_FOUND", "证据不属于当前源码索引。", 404)
            if result["status"] == "INTEGRITY_ERROR":
                raise RequestError("EVIDENCE_INTEGRITY_ERROR", "证据完整性检查失败，请重新分析。", 409)
            return {**result, "run_id": self.project["run_id"]}


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int, app: WorkbenchState, web_root: Path) -> None:
        self.app, self.web_root = app, web_root.resolve()
        super().__init__(("127.0.0.1", port), RequestHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.allowed_host = f"127.0.0.1:{self.server_port}"


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "LocalWorkbench"
    sys_version = ""

    def log_message(self, *_: object) -> None:
        pass

    def _guard(self, *, token: bool = False) -> None:
        if self.headers.get_all("Host", []) != [self.server.allowed_host]:
            raise RequestError("INVALID_HOST", "只接受本机页面请求。", 403)
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            raise RequestError("CROSS_ORIGIN_DENIED", "不接受其他网站的请求。", 403)
        if self.headers.get("Sec-Fetch-Site") not in {None, "none", "same-origin"}:
            raise RequestError("CROSS_ORIGIN_DENIED", "不接受其他网站的请求。", 403)
        if token:
            supplied = self.headers.get("X-Session-Token", "")
            if not supplied.isascii() or not hmac.compare_digest(supplied, self.server.app.session_token):
                raise RequestError("INVALID_SESSION", "会话已失效，请刷新本机页面。", 403)

    def _send(self, status: int, data: bytes, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def _json(self, status: int, value: object) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def _error(self, exc: RequestError) -> None:
        self._json(exc.status, {"error": {"code": exc.code, "message": exc.message}})

    def do_GET(self) -> None:
        try:
            route = urlsplit(self.path)
            if route.scheme or route.netloc or route.fragment:
                raise RequestError("INVALID_ROUTE", "请求路径无效。")
            path = route.path
            self._guard(token=path.startswith("/api/") and path != "/api/state")
            if path == "/api/state" and not route.query:
                self._json(200, self.server.app.state())
            elif path.startswith("/api/jobs/") and not route.query:
                self._json(200, self.server.app.get_job(path.removeprefix("/api/jobs/")))
            elif path == "/api/evidence":
                query = parse_qs(route.query, keep_blank_values=True)
                if set(query) != {"id"} or len(query["id"]) != 1:
                    raise RequestError("INVALID_QUERY", "请提供一个有效证据标识。")
                self._json(200, self.server.app.evidence(query["id"][0]))
            elif path in STATIC_FILES:
                file = self.server.web_root / STATIC_FILES[path]
                if file.is_symlink() or not file.is_file():
                    raise RequestError("NOT_FOUND", "页面文件尚未就绪。", 404)
                content_type = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}[file.suffix]
                self._send(200, file.read_bytes(), content_type + "; charset=utf-8")
            else:
                raise RequestError("NOT_FOUND", "请求的页面不存在。", 404)
        except RequestError as exc:
            self._error(exc)
        except Exception:
            self._error(RequestError("REQUEST_FAILED", "请求未完成。", 500))

    def do_POST(self) -> None:
        try:
            self._guard(token=True)
            cancel_match = re.fullmatch(r"/api/jobs/([a-f0-9]{32})/cancel", self.path)
            if self.path != "/api/analyze" and cancel_match is None:
                raise RequestError("NOT_FOUND", "请求的操作不存在。", 404)
            if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
                raise RequestError("INVALID_CONTENT_TYPE", "只接受 JSON 请求。", 415)
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not lengths[0].isdigit():
                raise RequestError("INVALID_LENGTH", "请求长度无效。")
            length = int(lengths[0])
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise RequestError("REQUEST_TOO_LARGE", "请求内容过大。", 413)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise RequestError("INVALID_JSON", "请求内容不是有效 JSON。") from None
            if cancel_match:
                if payload != {}:
                    raise RequestError("INVALID_OPTIONS", "取消任务请求须为空对象。")
                self._json(202, self.server.app.cancel(cancel_match.group(1)))
            else:
                self._json(202, self.server.app.start(payload))
        except RequestError as exc:
            self._error(exc)
        except Exception:
            self._error(RequestError("REQUEST_FAILED", "请求未完成。", 500))

    def do_OPTIONS(self) -> None:
        self._error(RequestError("CROSS_ORIGIN_DENIED", "不提供跨站访问。", 403))


def create_server(port: int = 8765, *, app: WorkbenchState | None = None, web_root: Path = WEB_ROOT) -> LocalServer:
    return LocalServer(port, app or WorkbenchState(), web_root)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open a local COBOL source analysis workbench.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        server = create_server(args.port)
    except OSError:
        print("本机端口不可用，请用 --port 指定其他端口。")
        return 2
    print(f"本机工作台：{server.origin}；按 Ctrl+C 关闭。", flush=True)
    if not args.no_browser:
        timer = threading.Timer(0.2, webbrowser.open, args=(server.origin,))
        timer.daemon = True
        timer.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
