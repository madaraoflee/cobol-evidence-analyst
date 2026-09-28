#!/usr/bin/env python3
"""Loopback-only, dependency-free web adapter for the source analysis workflow."""

from __future__ import annotations

import argparse
import codecs
import copy
from contextlib import closing
from dataclasses import replace
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Callable, Sequence
from urllib.parse import parse_qs, urlsplit
import webbrowser

from analyze_source import ARTIFACT_NAMES, AnalysisCancelled, _paths, analyze_source
from company_api import APIClientError, APIConfigurationError, CompanyAPIConfig, OpenAICompatibleChatClient, Transport
from investigation_tools import InvestigationTools
from repo_inventory import parse_extensions
from framework_knowledge import framework_status, _resolve_reference
from report_view import DIRECT_REPORT_BYTES, VIEW_REPORT_BYTES, report_sha256
from conversation_store import ConversationStore


WEB_ROOT = Path(__file__).resolve().parent / "web"
FRAMEWORK_DEMO_SOURCE = Path(__file__).resolve().parent / "fixtures" / "framework-workbench" / "source"
FRAMEWORK_DEMO_OUTPUT = Path(__file__).resolve().parents[1] / ".poc-data" / "framework-runs"
STATIC_FILES = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/i18n.js": "i18n.js", "/styles.css": "styles.css",
                "/markdown.js": "markdown.js", "/marked.umd.js": "marked.umd.js"}
MAX_REQUEST_BYTES = 32_768
MAX_GRAPH_EDGES = 200
OUTPUT_NAMES = frozenset((*ARTIFACT_NAMES, "structural-index.sqlite-wal", "structural-index.sqlite-shm", "source-catalog.sqlite-wal", "source-catalog.sqlite-shm", "conversations.sqlite", "conversations.sqlite-wal", "conversations.sqlite-shm", "conversations.sqlite-journal"))
EVIDENCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}")


class RequestError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _project(source: str | None = None, output: str | None = None, run_id: str | None = None) -> dict:
    synthetic = bool(source and Path(source).resolve() == FRAMEWORK_DEMO_SOURCE.resolve())
    return {
        "run_id": run_id, "source": source, "output": output,
        "source_origin": "synthetic_framework" if synthetic else "local_source",
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
    fields = {"source", "output", "encoding", "source_format", "entry", "question", "allow_network", "extensions", "index_mode", "verify_content", "max_source_pages", "reading_strategy", "conversation_id", "framework_reference_path"}
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
    max_source_pages = payload.get("max_source_pages", 4)
    if type(max_source_pages) is not int or not 1 <= max_source_pages <= 128:
        raise RequestError("INVALID_OPTIONS", "阅读段数须为 1 到 128 的整数。")
    reading_strategy = _text(payload, "reading_strategy", maximum=32) or "retrieval"
    if reading_strategy not in {"retrieval", "focused", "full_chain"}:
        raise RequestError("INVALID_OPTIONS", "阅读方式须为 retrieval、focused 或 full_chain。")
    extension_text = _text(payload, "extensions", maximum=256)
    try:
        extensions = parse_extensions(extension_text)
    except ValueError:
        raise RequestError("INVALID_EXTENSIONS", "扩展名格式无效。") from None
    return {
        "source": source, "output": output, "encoding": encoding, "source_format": source_format,
        "entry": _text(payload, "entry"), "question": _text(payload, "question", maximum=8000),
        "allow_network": allow_network, "extensions": extensions, "index_mode": index_mode, "verify_content": verify_content,
        "analysis_mode": "business", "max_source_pages": max_source_pages, "reading_strategy": reading_strategy,
        "conversation_id": _text(payload, "conversation_id", maximum=64),
        "framework_reference_path": _text(payload, "framework_reference_path", maximum=4096),
    }


def _read_json(path: Path) -> object:
    if path.is_symlink() or not path.is_file():
        raise ValueError("invalid report")
    if path.stat().st_size <= DIRECT_REPORT_BYTES:
        return json.loads(path.read_text(encoding="utf-8"))
    view_path = path.with_name(f"{path.stem}-view.json")
    if view_path.is_symlink() or not view_path.is_file() or view_path.stat().st_size > VIEW_REPORT_BYTES:
        raise ValueError("invalid report view")
    view = json.loads(view_path.read_text(encoding="utf-8"))
    projection = view.get("display_projection", {}) if isinstance(view, dict) else {}
    if projection.get("source_size") != path.stat().st_size or projection.get("source_sha256") != report_sha256(path):
        raise ValueError("report view does not match complete report")
    return view


def _unaccepted_agent(agent: dict, reason_code: str) -> dict:
    """Retain response text for diagnostics without granting citation authority."""
    rejected = {"runner_status": "NOT_READY", "reason_code": reason_code, "agent_result": None}
    if isinstance(agent.get("api_diagnostics"), dict):
        rejected["api_diagnostics"] = agent["api_diagnostics"]
    if isinstance(agent.get("display_projection"), dict):
        rejected["display_projection"] = agent["display_projection"]
    response = agent.get("unaccepted_response")
    answer = agent.get("agent_result")
    text = response.get("text") if isinstance(response, dict) else None
    if not isinstance(text, str) or not text.strip():
        if isinstance(answer, dict) and answer.get("model_answer_recorded") is not False:
            narrative = answer.get("narrative")
            text = narrative.get("text") if isinstance(narrative, dict) else None
            if not isinstance(text, str) or not text.strip():
                text = answer.get("answer")
    if isinstance(text, str) and text.strip():
        rejected["unaccepted_response"] = {"reason_code": reason_code, "text": text}
    return rejected


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
    return CompanyAPIConfig.from_env(timeout_seconds=60.0, max_output_tokens=2048)


_FOLDER_DIALOG_SCRIPT = """
import json
import sys
try:
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    selected = filedialog.askdirectory(parent=root, initialdir=sys.argv[1], mustexist=True)
    root.destroy()
    print(json.dumps({'path': selected or None}))
except Exception:
    print(json.dumps({'error': 'FOLDER_PICKER_UNAVAILABLE'}))
"""

_MAC_FOLDER_DIALOG_SCRIPT = """
function run(argv) {
    var app = Application.currentApplication();
    app.includeStandardAdditions = true;
    try {
        return app.chooseFolder({withPrompt: "Choose a folder", defaultLocation: Path(argv[0])}).toString();
    } catch (error) {
        if (error.errorNumber === -128 || String(error).indexOf("(-128)") !== -1) return "__CANCELLED__";
        throw error;
    }
}
"""


def _pick_macos_folder(initial_path: Path) -> str | None:
    try:
        child = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", _MAC_FOLDER_DIALOG_SCRIPT, str(initial_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=300, check=False,
        )
    except subprocess.TimeoutExpired:
        raise RequestError("FOLDER_PICKER_TIMEOUT", "选择文件夹窗口已超时，请重试。", 504) from None
    except OSError:
        raise RequestError("FOLDER_PICKER_UNAVAILABLE", "无法打开本机文件夹选择窗口，请手动填写路径。", 503) from None
    if child.returncode:
        raise RequestError("FOLDER_PICKER_UNAVAILABLE", "无法打开本机文件夹选择窗口，请手动填写路径。", 503)
    selected = child.stdout.strip()
    if selected == "__CANCELLED__":
        return None
    directory = Path(selected).expanduser()
    if not selected or not directory.is_absolute() or not directory.is_dir():
        raise RequestError("FOLDER_PICKER_UNAVAILABLE", "无法打开本机文件夹选择窗口，请手动填写路径。", 503)
    return str(directory.resolve())


def _pick_local_folder(initial_path: Path) -> str | None:
    """Run the native dialog on the child process's main thread."""
    try:
        child = subprocess.run(
            [sys.executable, "-c", _FOLDER_DIALOG_SCRIPT, str(initial_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=300, check=False,
        )
    except subprocess.TimeoutExpired:
        raise RequestError("FOLDER_PICKER_TIMEOUT", "选择文件夹窗口已超时，请重试。", 504) from None
    except OSError:
        if sys.platform == "darwin":
            return _pick_macos_folder(initial_path)
        raise RequestError("FOLDER_PICKER_UNAVAILABLE", "无法打开本机文件夹选择窗口，请手动填写路径。", 503) from None
    try:
        result = json.loads(child.stdout)
        if child.returncode or not isinstance(result, dict) or result.get("error"):
            raise ValueError("dialog unavailable")
        selected = result["path"]
        if selected is None:
            return None
        directory = Path(selected).expanduser()
        if not directory.is_absolute() or not directory.is_dir():
            raise ValueError("invalid selected path")
        return str(directory.resolve())
    except (KeyError, TypeError, ValueError, OSError):
        if sys.platform == "darwin":
            return _pick_macos_folder(initial_path)
        raise RequestError("FOLDER_PICKER_UNAVAILABLE", "无法打开本机文件夹选择窗口，请手动填写路径。", 503) from None


class WorkbenchState:
    """One in-memory browser session, one job, and one current source snapshot."""

    def __init__(self, *, analyzer: Callable = analyze_source, config_provider: Callable = _environment_config,
                 demo_output_root: Path | None = None, state_path: Path | None = None,
                 model_check_transport: Transport | None = None,
                 folder_picker: Callable[[Path], str | None] = _pick_local_folder) -> None:
        self.session_token = secrets.token_urlsafe(32)
        self.analyzer, self.config_provider = analyzer, config_provider
        self.model_check_transport, self.folder_picker = model_check_transport, folder_picker
        self.lock = threading.RLock()
        self.project = _project()
        self.job: dict | None = None
        self.cancel_event = threading.Event()
        self.started_at = self.updated_at = self.phase_started_at = 0.0
        self.phase_start_completed = 0.0
        self.demo_output_root = demo_output_root if demo_output_root is not None else FRAMEWORK_DEMO_OUTPUT
        self.state_path = state_path
        self.framework_reference_path = None
        self.conversation = None
        self.conversation_store = None
        self._restore_workspace()

    def _save_workspace(self, options=None):
        if self.state_path is None:
            return
        options = options or self.project
        data = {"source": str(options.get("source") or ""), "output": str(options.get("output") or ""),
                "framework_reference_path": self.framework_reference_path,
                "conversation_id": (self.conversation or {}).get("id")}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if self.state_path.is_symlink():
            raise ValueError("WORKSPACE_PATH_INVALID")
        temporary = self.state_path.with_suffix(".tmp")
        if temporary.is_symlink():
            raise ValueError("WORKSPACE_PATH_INVALID")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.state_path)

    def _restore_workspace(self):
        if self.state_path is None or not self.state_path.is_file() or self.state_path.is_symlink():
            return
        try:
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.framework_reference_path = saved.get("framework_reference_path")
            if not saved.get("source") or not saved.get("output"):
                return
            source, output = _paths(Path(saved["source"]), Path(saved["output"]))
            report = _read_json(output / "diagnosis.json")
            from repository_discovery import repository_search_overview
            overview = repository_search_overview(output / "structural-index.sqlite", source)
            if not report.get("source_manifest_verified") or report.get("source_root") != str(source):
                return
            if (report.get("build_report") or {}).get("snapshot_id") != overview["snapshot_id"]:
                return
            programs = _read_json(output / "programs.json")
            project = _project(str(source), str(output))
            project.update(diagnosis=report, programs=programs.get("programs", []),
                           snapshot_id=overview["snapshot_id"], agent=_read_json(output / "agent-result.json"),
                           relations=_relationships(output / "structural-index.sqlite", overview["snapshot_id"]))
            self.project = project
            self.conversation_store = ConversationStore(output, source)
            self.conversation_store.recover_interrupted()
            if saved.get("conversation_id"):
                self.conversation = self.conversation_store.get(saved["conversation_id"])
        except (OSError, ValueError, KeyError, sqlite3.Error):
            # The saved path is a convenience, not authority for a stale index.
            self.project = _project()

    def framework(self):
        try:
            resolved = _resolve_reference(self.framework_reference_path)
        except ValueError:
            resolved = self.framework_reference_path
        return {**framework_status(self.framework_reference_path),
                "configured_path": str(resolved) if resolved else ""}

    def configure_framework(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"path"}:
            raise RequestError("INVALID_OPTIONS", "请提供框架资料文件或目录。")
        path = _text(payload, "path", maximum=4096)
        with self.lock:
            if self.job and self.job["status"] == "RUNNING":
                raise RequestError("JOB_ACTIVE", "本次回答完成后可以更新框架资料。", 409)
            self.framework_reference_path = path
            status = self.framework()
            self._save_workspace()
            return {"framework_knowledge": status}

    def check_model(self, payload: object) -> dict:
        if payload != {}:
            raise RequestError("INVALID_OPTIONS", "模型连接测试请求须为空对象。")
        try:
            config = self.config_provider()
            config.validate()
            probe_config = replace(config, timeout_seconds=min(float(config.timeout_seconds), 15.0),
                                   max_output_tokens=min(config.max_output_tokens, 32))
            response = OpenAICompatibleChatClient(
                probe_config, transport=self.model_check_transport, allow_network=True,
            ).complete(messages=[{"role": "user", "content": "Reply with the single word OK."}])
        except (APIConfigurationError, APIClientError) as exc:
            return {"usable": False, "code": exc.code, "http_status": exc.http_status,
                    "model_returned": False}
        except (TypeError, ValueError):
            return {"usable": False, "code": "CONFIGURATION_INVALID", "http_status": None,
                    "model_returned": False}
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            for choice in choices[:1]:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
                    continue
                content = message.get("content")
                if isinstance(content, str):
                    answer = content.strip()
                elif isinstance(content, list):
                    answer = "".join(
                        part.get("text", "") for part in content
                        if isinstance(part, dict) and isinstance(part.get("text"), str)
                    ).strip()
                else:
                    answer = ""
                if answer and not message.get("refusal") and choice.get("finish_reason") != "content_filter":
                    return {"usable": True, "code": "MODEL_REPLY_RECEIVED", "http_status": 200,
                            "model_returned": True}
        return {"usable": False, "code": "MODEL_TEXT_EMPTY", "http_status": 200,
                "model_returned": False}

    def pick_folder(self, payload: object) -> dict:
        if not isinstance(payload, dict) or set(payload) - {"initial_path"}:
            raise RequestError("INVALID_OPTIONS", "文件夹选择请求只接受起始路径。")
        suggested = _text(payload, "initial_path", maximum=4096)
        with self.lock:
            current_source = self.project.get("source")
        candidate = Path(suggested or current_source or Path.home()).expanduser()
        initial_path = candidate if candidate.is_dir() else candidate.parent
        if not initial_path.is_dir():
            initial_path = Path.home()
        try:
            selected = self.folder_picker(initial_path)
        except RequestError:
            raise
        except Exception:
            raise RequestError("FOLDER_PICKER_UNAVAILABLE", "无法打开本机文件夹选择窗口，请手动填写路径。", 503) from None
        if selected is None:
            return {"cancelled": True, "path": None}
        directory = Path(selected).expanduser()
        if not directory.is_absolute() or not directory.is_dir():
            raise RequestError("FOLDER_PICKER_UNAVAILABLE", "所选文件夹不可用，请重新选择或手动填写路径。", 503)
        return {"cancelled": False, "path": str(directory.resolve())}

    def get_conversation(self, identifier):
        with self.lock:
            if self.job and self.job["status"] == "RUNNING":
                raise RequestError("JOB_ACTIVE", "当前回答完成后可以切换对话。", 409)
            try:
                self.conversation = self.conversation_store.get(identifier)
            except (ValueError, AttributeError):
                raise RequestError("CONVERSATION_NOT_FOUND", "找不到这段对话。", 404) from None
            self._save_workspace()
            return {"conversation": copy.deepcopy(self.conversation)}

    def new_conversation(self, payload):
        if payload != {}:
            raise RequestError("INVALID_OPTIONS", "新对话请求须为空对象。")
        with self.lock:
            if self.job and self.job["status"] == "RUNNING":
                raise RequestError("JOB_ACTIVE", "请先完成或停止当前回答。", 409)
            if self.conversation_store is None:
                raise RequestError("SOURCE_REQUIRED", "先接入源码，再开始对话。", 409)
            self.conversation = self.conversation_store.create(self.project.get("snapshot_id"))
            self._save_workspace()
            return {"conversation": copy.deepcopy(self.conversation)}

    def _record_answer(self, options, job_id, project):
        identifier = options.get("conversation_id")
        if not identifier:
            return
        agent = (project.get("agent") or {}).get("agent_result") or {}
        status = "completed" if (project.get("agent") or {}).get("runner_status") == "COMPLETED" else "failed"
        self.conversation_store.complete_user(job_id, status)
        self.conversation_store.append(identifier, "assistant",
            agent.get("answer") or "本次没有取得模型回答。对话已保留，可以重试。", status=status, run_id=job_id,
            details={"evidence_refs": agent.get("evidence_refs", []),
                     "cited_evidence_ids": [r.get("evidence_id") for r in (agent.get("narrative") or {}).get("citations", []) if r.get("evidence_id")],
                     "framework_references": (agent.get("framework_context") or {}).get("references", []),
                     "related_sources": [{key: item[key] for key in ("relative_path", "program_names", "direct_source_match") if key in item}
                         for item in ((agent.get("investigation") or {}).get("business_map") or {}).get("programs", [])],
                     "snapshot_id": agent.get("snapshot_id"), "metrics": agent.get("metrics", {}),
                     "diagnostics": agent.get("diagnostics", [])})
        self.conversation = self.conversation_store.get(identifier)

    def _record_failure(self, options, job_id, status):
        identifier = options.get("conversation_id")
        if identifier and self.conversation_store:
            self.conversation_store.complete_user(job_id, status)
            self.conversation = self.conversation_store.get(identifier)

    def framework_demo(self) -> dict:
        from framework_demo import load_framework_demo
        try:
            return load_framework_demo()
        except (OSError, ValueError):
            raise RequestError("FRAMEWORK_DEMO_UNAVAILABLE", "框架案例资料不完整或已变化，请更新案例文件后重试。", 409) from None

    def prepare_framework_demo(self, payload: object) -> dict:
        from framework_demo import select_framework_demo_case
        if not isinstance(payload, dict) or "case_id" not in payload or set(payload) - {"case_id", "locale"}:
            raise RequestError("INVALID_OPTIONS", "请选择一个框架案例。")
        locale = payload.get("locale", "zh-CN")
        if not isinstance(locale, str) or locale not in {"zh-CN", "zh-HK", "en"}:
            raise RequestError("INVALID_OPTIONS", "界面语言无效。")
        demo = self.framework_demo()
        try:
            case = select_framework_demo_case(demo, payload["case_id"])
        except (ValueError, TypeError):
            raise RequestError("FRAMEWORK_DEMO_CASE_UNKNOWN", "所选框架案例不存在。", 404) from None
        # Cases share one source snapshot; choosing another question does not
        # rebuild the synthetic repository or discard its conversation.
        output = (self.demo_output_root / "business-conversation").resolve()
        question = case["question"]
        if isinstance(question, dict):
            question = question.get(locale) or question.get("zh-CN")
        with self.lock:
            same_project = (self.project.get("source") == demo["source_path"]
                            and self.project.get("output") == str(output))
            diagnosis = self.project.get("diagnosis") or {}
            search = diagnosis.get("repository_search") or {}
            search_ready = (search.get("snapshot_id") == self.project.get("snapshot_id")
                            and search.get("full_text_complete") is True
                            and (diagnosis.get("scope") or {}).get("mode") in {"repository_index", "repository_question"})
            if self.job and self.job["status"] == "RUNNING":
                if same_project and self.project.get("run_id") == self.job["job_id"]:
                    return {"job_id": self.job["job_id"], "status": "RUNNING",
                            "suggested_question": question}
                raise RequestError("JOB_ACTIVE", "当前工作完成后可以切换业务案例。", 409)
            if same_project and self.project.get("snapshot_id") and diagnosis.get("source_manifest_verified") and search_ready:
                if self.conversation_store is None:
                    self.conversation_store = ConversationStore(output, Path(demo["source_path"]))
                if not self.conversation:
                    self.conversation = self.conversation_store.create(self.project["snapshot_id"])
                    self._save_workspace()
                return {"status": "READY", "project": copy.deepcopy(self.project),
                        "conversation": copy.deepcopy(self.conversation),
                        "suggested_question": question}
        return {**self.start({
            "source": demo["source_path"], "output": str(output),
            "encoding": "utf-8", "source_format": "free", "index_mode": "full",
            "allow_network": False, "max_source_pages": 4, "reading_strategy": "retrieval",
        }), "suggested_question": question}

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
                "framework_knowledge": self.framework(),
                "conversation": copy.deepcopy(self.conversation),
                "conversations": self.conversation_store.list() if self.conversation_store else [],
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
            same_project = self.project.get("source") == str(options["source"]) and self.project.get("output") == str(options["output"])
            self.conversation_store = ConversationStore(options["output"], options["source"])
            options["framework_reference_path"] = options.get("framework_reference_path") or self.framework_reference_path
            self.framework_reference_path = options["framework_reference_path"]
            if options["question"] and options["allow_network"]:
                try:
                    conversation = self.conversation_store.get(options["conversation_id"]) if options.get("conversation_id") else self.conversation_store.create(self.project.get("snapshot_id") if same_project else None)
                except ValueError:
                    raise RequestError("CONVERSATION_NOT_FOUND", "找不到这段对话，请新建对话后重试。", 404) from None
                options["conversation_id"] = conversation["id"]
                options["conversation_history"] = conversation["messages"]
                self.conversation_store.append(conversation["id"], "user", options["question"], status="pending", run_id=job_id)
                self.conversation = self.conversation_store.get(conversation["id"])
            elif not same_project:
                self.conversation = None
            if not same_project or not options["question"]:
                self.project = _project(str(options["source"]), str(options["output"]), job_id)
            self._save_workspace(options)
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
            arguments = {key: value for key, value in options.items() if key not in {"source", "output", "conversation_id"}}
            if arguments.get("framework_reference_path") is None:
                arguments.pop("framework_reference_path", None)
            if options["allow_network"] and options["question"]:
                arguments["capture_api_responses"] = True
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
            if program_report.get("display_projection"):
                project["display_projection"] = program_report["display_projection"]
            agent = _read_json(output / "agent-result.json")
            snapshot = (report.get("build_report") or {}).get("snapshot_id")
            # A failed refresh can deliberately preserve an older SQLite file.
            # It must never authorize that old file as this browser's snapshot.
            if report.get("source_manifest_verified") is True and snapshot:
                try:
                    if program_report.get("snapshot_id") != snapshot:
                        raise ValueError("snapshot report mismatch")
                    relations = _relationships(output / "structural-index.sqlite", snapshot)
                except (OSError, ValueError, sqlite3.Error):
                    project["diagnosis"] = {**report, "runner_status": "BLOCKED", "question_status": "NOT_READY",
                                            "source_manifest_verified": False, "catalog_ready": False,
                                            "reason_code": "SOURCE_SNAPSHOT_MISMATCH"}
                    project["agent"] = _unaccepted_agent(agent, "SOURCE_SNAPSHOT_MISMATCH")
                else:
                    agent_snapshot = (agent.get("agent_result") or {}).get("snapshot_id")
                    if agent_snapshot is not None and agent_snapshot != snapshot:
                        agent = _unaccepted_agent(agent, "ANSWER_SNAPSHOT_MISMATCH")
                        project["diagnosis"] = {**report, "question_status": "NOT_READY"}
                    project.update(programs=program_report["programs"], agent=agent,
                                   snapshot_id=snapshot, relations=relations)
            elif report.get("catalog_ready") is True:
                # A usable catalog permits another question but does not verify
                # any model answer or citations from an unverified source scope.
                if agent.get("agent_result"):
                    agent = _unaccepted_agent(agent, "SOURCE_SNAPSHOT_UNVERIFIED")
                    project["diagnosis"] = {**report, "question_status": "NOT_READY"}
                project.update(programs=program_report["programs"], agent=agent)
            else:
                project["agent"] = _unaccepted_agent(agent, report.get("reason_code") or "SOURCE_SNAPSHOT_UNVERIFIED")
            with self.lock:
                if self.job and self.job["job_id"] == job_id:
                    self.project = project
                    self._record_answer(options, job_id, project)
                    project["conversation"] = copy.deepcopy(self.conversation)
                    self._save_workspace(options)
                    self.updated_at = time.monotonic()
                    self.job.update(status="COMPLETED", result=copy.deepcopy(project))
        except AnalysisCancelled:
            with self.lock:
                if self.job and self.job["job_id"] == job_id:
                    self.updated_at = time.monotonic()
                    self._record_failure(options, job_id, "cancelled")
                    self.job.update(status="CANCELLED", result=None, conversation=copy.deepcopy(self.conversation))
        except Exception:
            # Never serialize raw adapter errors, environment values or remote
            # responses. Failed work leaves no current evidence authority.
            with self.lock:
                if self.job and self.job["job_id"] == job_id:
                    self.updated_at = time.monotonic()
                    self._record_failure(options, job_id, "failed")
                    self.job["conversation"] = copy.deepcopy(self.conversation)
                    self.job.update(status="FAILED", error={"code": "ANALYSIS_FAILED", "message": "本次分析未完成。请检查输入路径和源码格式后重试。"})

    def get_job(self, job_id: str) -> dict:
        with self.lock:
            if not self.job or self.job["job_id"] != job_id:
                raise RequestError("JOB_NOT_FOUND", "任务不存在或已由新的源码分析替换。", 404)
            return self._job_snapshot()

    def evidence(self, evidence_id: str, conversation_id=None, message_id=None) -> dict:
        if EVIDENCE_ID.fullmatch(evidence_id) is None:
            raise RequestError("INVALID_EVIDENCE_ID", "证据标识无效。")
        with self.lock:
            if conversation_id:
                try:
                    conversation = self.conversation_store.get(conversation_id)
                    message = next(m for m in conversation["messages"] if m["id"] == message_id)
                    if evidence_id not in {r.get("evidence_id") for r in message.get("evidence_refs", [])}:
                        raise ValueError("missing reference")
                    if message.get("snapshot_id") != self.project.get("snapshot_id"):
                        raise RequestError("HISTORICAL_SOURCE_VERSION", "这条引用属于较早的源码版本；请查看当时的路径和行号，或在当前版本继续提问。", 409)
                except (ValueError, StopIteration, AttributeError):
                    raise RequestError("EVIDENCE_NOT_FOUND", "引用不属于这条对话消息。", 404) from None
            if not self.project["snapshot_id"] or (self.job and self.job["status"] == "RUNNING" and not self.conversation):
                raise RequestError("NO_CURRENT_INDEX", "当前没有完成并验证的源码索引。", 409)
            database = Path(self.project["output"]) / "structural-index.sqlite"
            if database.is_symlink():
                raise RequestError("INDEX_CHANGED", "索引位置已发生变化，请重新分析。", 409)
            try:
                result = InvestigationTools(database).read_evidence([evidence_id])
            except (OSError, ValueError, sqlite3.Error):
                raise RequestError("INDEX_UNAVAILABLE", "当前源码索引不可用，请重新分析。", 409) from None
            if result["snapshot_id"] != self.project["snapshot_id"]:
                raise RequestError("INDEX_CHANGED", "源码索引已更新，请重新分析。", 409)
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
            elif path == "/api/framework-demo" and not route.query:
                self._json(200, self.server.app.framework_demo())
            elif path.startswith("/api/jobs/") and not route.query:
                self._json(200, self.server.app.get_job(path.removeprefix("/api/jobs/")))
            elif re.fullmatch(r"/api/conversations/[a-f0-9]{32}", path) and not route.query:
                self._json(200, self.server.app.get_conversation(path.rsplit("/", 1)[1]))
            elif path == "/api/evidence":
                query = parse_qs(route.query, keep_blank_values=True)
                if "id" not in query or set(query) - {"id", "conversation_id", "message_id"} or any(len(v) != 1 for v in query.values()):
                    raise RequestError("INVALID_QUERY", "请提供一个有效证据标识。")
                self._json(200, self.server.app.evidence(query["id"][0], query.get("conversation_id", [None])[0], query.get("message_id", [None])[0]))
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
            if self.path not in {"/api/analyze", "/api/framework-demo/prepare", "/api/framework", "/api/conversations", "/api/model-check", "/api/pick-folder"} and cancel_match is None:
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
            elif self.path == "/api/framework-demo/prepare":
                self._json(202, self.server.app.prepare_framework_demo(payload))
            elif self.path == "/api/framework":
                self._json(200, self.server.app.configure_framework(payload))
            elif self.path == "/api/model-check":
                self._json(200, self.server.app.check_model(payload))
            elif self.path == "/api/pick-folder":
                self._json(200, self.server.app.pick_folder(payload))
            elif self.path == "/api/conversations":
                self._json(201, self.server.app.new_conversation(payload))
            else:
                self._json(202, self.server.app.start(payload))
        except RequestError as exc:
            self._error(exc)
        except Exception:
            self._error(RequestError("REQUEST_FAILED", "请求未完成。", 500))

    def do_OPTIONS(self) -> None:
        self._error(RequestError("CROSS_ORIGIN_DENIED", "不提供跨站访问。", 403))


def create_server(port: int = 8765, *, app: WorkbenchState | None = None, web_root: Path = WEB_ROOT) -> LocalServer:
    return LocalServer(port, app or WorkbenchState(state_path=Path(__file__).resolve().parents[1] / ".poc-data" / "workbench-state.json"), web_root)


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
