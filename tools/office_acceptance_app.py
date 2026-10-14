"""Local Windows acceptance assistant for an independently frozen workbench ZIP.

The default action plans without starting the workbench or making model calls.
The helper never publishes, uploads reports, displays credentials or retries a
remote request. Human scores remain explicit, per answer and per office check.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import subprocess
import threading
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "poc"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from desktop_live_acceptance import evaluate_native, validate_gold
from desktop_acceptance import digest, inspect_package, require, BINARY_MEMBER
from desktop_acceptance_runtime import isolated_environment
from live_business_eval import _cases, _safe_write, _turns, _source_excerpt
from release_acceptance import evaluate_release, OFFICE_BASELINE_CHECKS, OFFICE_REGRESSION_BASELINE, _live_case_scope


REQUIRED_INPUTS = ("archive", "cases", "policy", "compiled_report", "manifest")
OFFICE_LABELS = {
    "desktop_start": "桌面窗口可正常打开和操作",
    "source_import": "本机源码导入正常，索引范围与实际材料一致",
    "conversation_recovery": "重开应用后对话与追问历史恢复正常",
    "cancellation_recovery": "取消调查后可以继续提问，没有重复不确定的请求",
    "historical_evidence": "历史答案仍对应原版本证据，源码更新不误改历史",
    "company_api_ca": "实际公司接口与 CA 验证正常，未关闭 TLS 校验",
}


def read_object(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    require(isinstance(value, dict), "OFFICE_DOCUMENT_MUST_BE_OBJECT")
    return value


def kit_file(root, relative):
    require(isinstance(relative, str) and bool(relative), "OFFICE_KIT_PATH_INVALID")
    path = PurePosixPath(relative)
    require(not path.is_absolute() and ".." not in path.parts and "\\" not in relative
            and ":" not in relative, "OFFICE_KIT_PATH_INVALID")
    result = root.joinpath(*path.parts).resolve(strict=True)
    require(result.is_relative_to(root), "OFFICE_KIT_PATH_ESCAPES_ROOT")
    return result


def load_kit(path):
    path = Path(path).expanduser().resolve(strict=True)
    root, document = path.parent, read_object(path)
    require(document.get("schema_version") == "office-acceptance-kit/v1", "OFFICE_KIT_SCHEMA_INVALID")
    require(re.fullmatch(r"[0-9a-f]{40}", document.get("source_commit", "")), "OFFICE_COMMIT_INVALID")
    files = document.get("files")
    require(isinstance(files, dict) and bool(files), "OFFICE_FILE_MANIFEST_REQUIRED")
    require(len({name.casefold() for name in files}) == len(files), "OFFICE_FILE_MANIFEST_DUPLICATE")
    for name, expected in files.items():
        require(isinstance(expected, str) and re.fullmatch(r"[0-9a-f]{64}", expected),
                "OFFICE_FILE_HASH_INVALID")
        actual = kit_file(root, name)
        require(actual.is_file() and digest(actual) == expected, "OFFICE_KIT_FILE_CHANGED")
    inputs = {}
    for key in REQUIRED_INPUTS:
        relative = document.get(key)
        require(relative in files, "OFFICE_INPUT_NOT_FROZEN")
        inputs[key] = kit_file(root, relative)
    helper_entry = document.get("helper_entry")
    require(helper_entry in files, "OFFICE_HELPER_NOT_FROZEN")
    helper = kit_file(root, helper_entry)
    helper_root = kit_file(root, "helper")
    require(helper.is_file() and helper.name == "OfficeAcceptance.exe" and helper.is_relative_to(helper_root),
            "OFFICE_HELPER_ENTRY_INVALID")
    require(all(item.relative_to(root).as_posix() in files for item in helper_root.rglob("*") if item.is_file()),
            "OFFICE_HELPER_NOT_FROZEN")
    source = kit_file(root, document.get("source_root", "sources"))
    require(source.is_dir(), "OFFICE_SOURCE_ROOT_INVALID")
    source_files = [item for item in source.rglob("*") if item.is_file()]
    require(source_files and all(item.relative_to(root).as_posix() in files for item in source_files),
            "OFFICE_SOURCE_NOT_FROZEN")
    artifact = inspect_package(inputs["archive"], document["source_commit"])
    policy = read_object(inputs["policy"])
    require(policy.get("source_commit") == document["source_commit"]
            and policy.get("live_cases_sha256") == digest(inputs["cases"]), "OFFICE_POLICY_BINDING_INVALID")
    require(policy.get("manifest_sha256") == digest(inputs["manifest"]), "OFFICE_MANIFEST_BINDING_INVALID")
    execution = policy.get("live_execution") or {}
    require(isinstance(execution, dict), "OFFICE_EXECUTION_POLICY_INVALID")
    per_turn = execution.get("max_model_requests", 3)
    seconds = execution.get("max_question_seconds", 120)
    require(type(per_turn) is int and 1 <= per_turn <= 10
            and type(seconds) is int and 1 <= seconds <= 600, "OFFICE_EXECUTION_POLICY_INVALID")
    items = _cases(inputs["cases"])
    scope_errors = []
    require(_live_case_scope(items, scope_errors), "OFFICE_CASE_SCOPE_INCOMPLETE")
    coverage = {tag for item in items for tag in (item.get("coverage") or []) if isinstance(tag, str)}
    require(set(OFFICE_REGRESSION_BASELINE) <= coverage, "OFFICE_PRIOR_REGRESSIONS_MISSING")
    validate_gold(source, items, "")
    turns = sum(len(_turns(item)) for item in items)
    return {"path": path, "root": root, "document": document, "inputs": inputs,
            "source": source, "policy": policy, "artifact": artifact,
            "kit_sha256": digest(path), "max_model_requests": per_turn,
            "max_question_seconds": seconds, "turns": turns, "request_budget": turns * per_turn}


def run_kit(*, kit_path, output, source=None, profile_config=None, ca_file=None,
            compilation_context=None, allow_network=False, request_budget=None):
    """Both plan and execution revalidate bytes; approval is never inferred."""
    kit = load_kit(kit_path)
    source = Path(source).expanduser().resolve(strict=True) if source else kit["source"]
    output = Path(output).expanduser().resolve()
    require(not output.exists(), "OFFICE_OUTPUT_MUST_BE_NEW")
    require(not output.is_relative_to(kit["root"]) and not kit["root"].is_relative_to(output),
            "OFFICE_OUTPUT_KIT_OVERLAP")
    if allow_network:
        require(type(request_budget) is int and request_budget == kit["request_budget"],
                "OFFICE_EXPLICIT_BUDGET_REQUIRED")
        require(profile_config is not None, "OFFICE_EXPLICIT_PROFILE_REQUIRED")
    try:
        report = evaluate_native(archive=kit["inputs"]["archive"], source_commit=kit["document"]["source_commit"],
            source=source, cases=kit["inputs"]["cases"], output=output, profile_config=profile_config,
            allow_network=allow_network, plan=not allow_network, request_budget=request_budget,
            max_model_requests=kit["max_model_requests"], max_question_seconds=kit["max_question_seconds"],
            source_format=kit["policy"].get("source_format", "auto"), encoding=kit["policy"].get("encoding", "auto"),
            framework="", ca_file=ca_file, compilation_context=compilation_context,
            office_checks=kit["policy"].get("office_required_checks", OFFICE_BASELINE_CHECKS))
    except Exception:
        if output.is_dir():
            _safe_write(output / "ATTENTION-REQUIRED.json", {"schema_version": "office-attention/v1",
                "kit_sha256": kit["kit_sha256"], "automatic_retry_allowed": False,
                "remote_outcome_may_be_unknown": bool(allow_network),
                "message": "本次未完成。查看本地报告和请求记录；远程结果不确定时不要直接重复运行。"})
        raise
    require(digest(kit["path"]) == kit["kit_sha256"], "OFFICE_KIT_CHANGED_DURING_RUN")
    load_kit(kit_path)
    _safe_write(output / "KIT-RUN.json", {"schema_version": "office-kit-run/v1",
        "kit_sha256": kit["kit_sha256"], "source_commit": kit["document"]["source_commit"],
        "artifact": kit["artifact"], "live_policy_sha256": digest(kit["inputs"]["policy"]),
        "evaluation_run_id": report["evaluation_run_id"], "allow_network": bool(allow_network),
        "declared_request_budget": request_budget if allow_network else 0,
        "status": report["status"], "publication_performed": False})
    return report


def check_review(*, kit_path, evaluation_dir, reviews, office_report=None):
    kit = load_kit(kit_path)
    root = Path(evaluation_dir).resolve(strict=True)
    result = evaluate_release(package=kit["inputs"]["archive"], compiled_report=kit["inputs"]["compiled_report"],
        manifest=kit["inputs"]["manifest"], policy=kit["inputs"]["policy"],
        live_report=root / "LIVE-ACCEPTANCE.json", live_cases=kit["inputs"]["cases"], reviews=Path(reviews),
        **({"office_report": Path(office_report)} if office_report else {}))
    result["publication_performed"] = False
    _safe_write(root / "LOCAL-RELEASE-GATE.json", result)
    return result


def open_workbench(*, kit_path, evaluation_dir, ca_file=None):
    """Open the already measured profile; opening alone makes no model request."""
    require(sys.platform == "win32", "OFFICE_NATIVE_WINDOWS_REQUIRED")
    kit = load_kit(kit_path)
    root = Path(evaluation_dir).resolve(strict=True)
    report = read_object(root / "LIVE-ACCEPTANCE.json")
    runtime = report.get("runtime_provenance") or {}
    require(all(runtime.get(key) == kit["artifact"].get(key)
                for key in ("source_commit", "archive_sha256", "binary_sha256")), "OFFICE_GUI_REPORT_ARTIFACT_MISMATCH")
    extracted = (root / "extracted").resolve(strict=True)
    binary = (extracted / BINARY_MEMBER).resolve(strict=True)
    require(binary.is_relative_to(extracted) and digest(binary) == kit["artifact"]["binary_sha256"],
            "OFFICE_GUI_BINARY_CHANGED")
    profile = (root / "private-profile").resolve(strict=True)
    require(profile.is_dir() and profile.is_relative_to(root) and (profile / ".env").is_file(),
            "OFFICE_GUI_PROFILE_UNAVAILABLE")
    environment = isolated_environment()
    settings = profile / "agent-settings.json"
    if settings.is_file():
        environment["AGENT_SETTINGS_PATH"] = str(settings)
    if ca_file:
        environment["SSL_CERT_FILE"] = str(Path(ca_file).resolve(strict=True))
    log = (root / "office-gui-process.log").open("ab")
    try:
        process = subprocess.Popen([str(binary), "--data-dir", str(profile)], cwd=root,
                                   env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    finally:
        log.close()
    return process


def save_review(path, template, scores, *, reviewer, office_checks, office_note="", office_report=None):
    """Save only explicit choices, keeping unanswered items unscored."""
    require(isinstance(reviewer, str) and bool(reviewer.strip()), "OFFICE_REVIEWER_REQUIRED")
    value = json.loads(json.dumps(template))
    now = datetime.now(timezone.utc).isoformat()
    for row in value["reviews"]:
        key = (row["case_id"], row["turn_id"])
        score = scores.get(key, {})
        row.update(reviewer=reviewer.strip(), reviewed_at=now, comments=score.get("comments", ""))
        for finding in row["required_findings"]:
            selected = score.get("required_findings", {}).get(finding["id"])
            require(selected in (None, "met", "omitted", "incorrect"), "OFFICE_FINDING_SCORE_INVALID")
            finding["result"] = selected
        for claim in row["forbidden_claims"]:
            selected = score.get("forbidden_claims", {}).get(claim["id"])
            require(selected is None or type(selected) is bool, "OFFICE_FORBIDDEN_SCORE_INVALID")
            claim["present"] = selected
        row["usefulness"] = score.get("usefulness")
        row["followup_continuity"] = score.get("followup_continuity")
        require(row["usefulness"] in (None, "usable", "unusable"), "OFFICE_USEFULNESS_INVALID")
        require(row["followup_continuity"] in (None, "preserved", "broken", "not_applicable"),
                "OFFICE_CONTINUITY_INVALID")
        if "partial_review" in row:
            partial = score.get("partial_review") or {}
            classification = partial.get("classification")
            require(classification in (None, "scope_mismatch", "answer_omission", "evidence_gap",
                    "explicit_unknown", "unsupported_semantics"), "OFFICE_PARTIAL_CLASSIFICATION_INVALID")
            resolved = partial.get("resolved")
            require(resolved is None or type(resolved) is bool, "OFFICE_PARTIAL_RESOLUTION_INVALID")
            affected = partial.get("affected_findings", [])
            require(isinstance(affected, list) and all(isinstance(value, str) for value in affected),
                    "OFFICE_PARTIAL_FINDINGS_INVALID")
            row["partial_review"] = {"classification": classification, "reason": partial.get("reason", ""),
                                     "affected_findings": affected, "resolved": resolved}
    office = value.get("office_acceptance")
    require(isinstance(office, dict), "OFFICE_TEMPLATE_BINDING_REQUIRED")
    for key in office["checks"]:
        selected = office_checks.get(key)
        require(selected is None or type(selected) is bool, "OFFICE_CHECK_SCORE_INVALID")
        office["checks"][key] = selected
    office.update(reviewer=reviewer.strip(), reviewed_at=now, comments=office_note)
    office["office_report_sha256"] = digest(Path(office_report).resolve(strict=True)) if office_report else None
    _safe_write(Path(path), value)
    return value


def restore_review(template, path):
    """Resume human choices only for the exact frozen answers and source report."""
    restored = read_object(path)
    expected = {(row["case_id"], row["turn_id"], row.get("branch", "automatic")): row
                for row in template["reviews"]}
    rows = restored.get("reviews")
    require(isinstance(rows, list) and len(rows) == len(expected), "OFFICE_REVIEW_RESUME_STALE")
    seen = set()
    for row in rows:
        key = (row.get("case_id"), row.get("turn_id"), row.get("branch", "automatic"))
        original = expected.get(key)
        require(original is not None and key not in seen and row.get("identity") == original.get("identity")
                and row.get("run_id") == original.get("run_id"), "OFFICE_REVIEW_RESUME_STALE")
        seen.add(key)
    office, original = restored.get("office_acceptance") or {}, template.get("office_acceptance") or {}
    require(all(office.get(key) == original.get(key) for key in ("source_commit", "archive_sha256", "binary_sha256",
                "evaluation_run_id", "live_report_sha256")), "OFFICE_REVIEW_RESUME_STALE")
    return restored


def default_kit_path():
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) or "__compiled__" in globals() else Path(__file__).parent
    for candidate in (base / "OFFICE-KIT.json", base.parent / "OFFICE-KIT.json"):
        if candidate.is_file():
            return candidate
    return base / "OFFICE-KIT.json"


def self_test(*, show_gui=True):
    require(sys.platform == "win32", "OFFICE_NATIVE_WINDOWS_REQUIRED")
    if show_gui:
        import tkinter as tk
        window = tk.Tk()
        window.withdraw()
        window.update_idletasks()
        window.destroy()
    return {"schema_version": "office-helper-self-test/v1", "ok": True,
            "native_platform": "Windows", "gui_started": bool(show_gui),
            "model_calls": 0, "publication_performed": False}


class AcceptanceWindow:
    def __init__(self, kit_path):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.root = tk.Tk()
        self.root.title("Windows EXE 联合验收")
        self.root.geometry(f"{min(940, self.root.winfo_screenwidth() - 40)}x{min(760, self.root.winfo_screenheight() - 100)}")
        self.kit_path = tk.StringVar(value=str(kit_path))
        self.source = tk.StringVar()
        self.profile = tk.StringVar()
        self.ca = tk.StringVar()
        self.compilation = tk.StringVar()
        self.office_report = tk.StringVar()
        self.output_parent = tk.StringVar(value=str(Path.home() / "Desktop" / "WorkbenchAcceptance"))
        self.authorized = tk.BooleanVar(value=False)
        self.budget = tk.StringVar()
        self.status = tk.StringVar(value="先生成计划；计划不调用模型。真实执行可能发送题目和源码，须在既有授权范围内。")
        self.kit = None
        self.last_output = None
        self.busy = False
        self.widgets = []
        self.gui_process = None
        self.last_live_output = None
        frame = ttk.Frame(self.root, padding=14)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        row = 0
        for title, variable, directory, extensions in (
            ("验收包 OFFICE-KIT.json", self.kit_path, False, [("验收合同", "*.json")]),
            ("源码目录（空白使用包内公开/合成源码）", self.source, True, None),
            ("本机公司接口 .env（值不会显示或上传）", self.profile, False, [("配置", "*.env"), ("全部", "*")]),
            ("公司 CA（按需要选择）", self.ca, False, [("证书", "*.pem *.crt *.cer"), ("全部", "*")]),
            ("编译上下文 JSON（按需要选择）", self.compilation, False, [("编译配置", "*.json")]),
            ("本机离线回归报告 COMPILED-ACCEPTANCE.json", self.office_report, False, [("本机回归", "*.json")]),
            ("报告父目录（应位于验收包之外）", self.output_parent, True, None)):
            ttk.Label(frame, text=title).grid(row=row, column=0, sticky="w", pady=5)
            entry = ttk.Entry(frame, textvariable=variable)
            entry.grid(row=row, column=1, sticky="ew", padx=8)
            button = ttk.Button(frame, text="选择", command=lambda v=variable, d=directory, e=extensions: self.choose(v, d, e))
            button.grid(row=row, column=2)
            self.widgets.extend((entry, button))
            row += 1
        ttk.Label(frame, text="本次授权总请求上限（填入计划显示的数值）").grid(row=row, column=0, sticky="w", pady=5)
        entry = ttk.Entry(frame, textvariable=self.budget)
        entry.grid(row=row, column=1, sticky="ew", padx=8)
        self.widgets.append(entry)
        row += 1
        approved = ttk.Checkbutton(frame, text="我已获准使用所选模型并发送本次源码；明确授权按计划预算执行", variable=self.authorized)
        approved.grid(row=row, column=0, columnspan=3, sticky="w", pady=10)
        self.widgets.append(approved)
        row += 1
        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=3, sticky="w")
        for index, (text, command) in enumerate((("1. 校验并生成计划（不联网）", lambda: self.start(False)),
                              ("本机离线回归（零远程调用）", self.offline),
                              ("2. 执行真实 EXE 问答", lambda: self.start(True)),
                              ("3. 人工评审及本机验收", self.review),
                              ("打开本地报告目录", self.open_output))):
            button = ttk.Button(buttons, text=text, command=command)
            button.grid(row=index // 3, column=index % 3, sticky="w", padx=3, pady=6)
            self.widgets.append(button)
        button = ttk.Button(frame, text="打开本次被测工作台（隔离验收配置，不自动提问）", command=self.open_measured_workbench)
        button.grid(row=row + 1, column=0, columnspan=3, sticky="w", pady=4)
        self.widgets.append(button)
        row += 1
        row += 1
        ttk.Label(frame, textvariable=self.status, wraplength=880).grid(row=row, column=0, columnspan=3, sticky="w", pady=12)
        row += 1
        self.log = tk.Text(frame, wrap="word", state="disabled", height=15)
        self.log.grid(row=row, column=0, columnspan=3, sticky="nsew")
        frame.rowconfigure(row, weight=1)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def choose(self, variable, directory, extensions):
        from tkinter import filedialog
        value = filedialog.askdirectory(parent=self.root) if directory else filedialog.askopenfilename(
            parent=self.root, filetypes=extensions)
        if value:
            variable.set(value)

    def append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self, live):
        from tkinter import messagebox
        if self.busy:
            return
        try:
            kit = load_kit(self.kit_path.get())
            selected_source = Path(self.source.get()).expanduser().resolve(strict=True) if self.source.get() else kit["source"]
            validate_gold(selected_source, _cases(kit["inputs"]["cases"]), "")
            if live:
                require(self.gui_process is None or self.gui_process.poll() is not None, "OFFICE_GUI_CLOSE_BEFORE_RUN")
                require(self.authorized.get(), "OFFICE_LOCAL_AUTHORIZATION_REQUIRED")
                require(re.fullmatch(r"[0-9]+", self.budget.get().strip()), "OFFICE_EXPLICIT_BUDGET_REQUIRED")
                require(int(self.budget.get()) == kit["request_budget"], "OFFICE_EXPLICIT_BUDGET_REQUIRED")
                require(bool(self.profile.get()), "OFFICE_EXPLICIT_PROFILE_REQUIRED")
                require(sys.platform == "win32", "OFFICE_NATIVE_WINDOWS_REQUIRED")
                if not messagebox.askyesno("明确执行真实模型", f"将启动受测 Windows EXE，发送 {kit['turns']} 轮问答及必要源码。\n"
                        f"最多 {kit['request_budget']} 次请求；不自动重试。确认既有授权覆盖本次操作？", parent=self.root):
                    return
            self.kit = kit
            parent = Path(self.output_parent.get()).expanduser().resolve()
            output = parent / (("live-" if live else "plan-") + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
            options = {"kit_path": self.kit_path.get(), "output": output, "source": self.source.get() or None,
                "profile_config": self.profile.get() or None, "ca_file": self.ca.get() or None,
                "compilation_context": self.compilation.get() or None, "allow_network": live,
                "request_budget": int(self.budget.get()) if live else None}
        except Exception as error:
            self.status.set("校验未通过：" + safe_error(error))
            return
        self.busy = True
        for widget in self.widgets:
            widget.configure(state="disabled")
        self.status.set("真实问答运行中，冷导入大库可能较久。完成记录保留本地；不确定的请求不自动重试。" if live else "正在本地校验和生成计划……")
        self.append(f"受测 ZIP SHA256：{kit['artifact']['archive_sha256']}\n受测 EXE SHA256：{kit['artifact']['binary_sha256']}")
        # Tcl is accessed exclusively by the main thread. A queue is polled by
        # Tk; workers never call after() or mutate GUI variables themselves.
        import queue
        responses = queue.Queue()
        def work():
            try:
                responses.put((run_kit(**options), None))
            except Exception as error:
                responses.put((None, safe_error(error)))
        threading.Thread(target=work, daemon=False).start()
        def poll():
            if responses.empty():
                self.root.after(200, poll)
                return
            report, error = responses.get_nowait()
            self.busy = False
            for widget in self.widgets:
                widget.configure(state="normal")
            self.last_output = output
            if live:
                self.last_live_output = output
            self.authorized.set(False)
            if error:
                self.status.set("验收未完成；未自动重试，正式发布仍被阻断。")
                self.append("错误：" + error + "\n请保留本次目录，先确认请求日志与远程结果，不要直接重跑。")
            else:
                self.status.set("计划完成：没有模型请求。" if not live else "真实问答已留档，尚未人工评分或完成办公验收；正式发布仍被阻断。")
                self.append(f"状态：{report['status']}\n题目轮数：{kit['turns']}，总请求上限：{kit['request_budget']}\n本地目录：{output}")
                if live:
                    self.append(f"实际请求：{report.get('observed_model_requests')}，耗时：{report.get('latency_seconds')}\n请逐题审阅答案和证据；PARTIAL 不自动等于错误或通过。")
        self.root.after(200, poll)

    def review(self):
        from tkinter import filedialog
        root = self.last_live_output
        if root is None or not (root / "HUMAN-REVIEW-TEMPLATE.json").is_file():
            selected = filedialog.askdirectory(parent=self.root, title="选择含真实报告与未评分模板的目录")
            if not selected:
                return
            root = Path(selected)
        try:
            kit = load_kit(self.kit_path.get())
            ReviewWindow(self, kit, root, Path(self.source.get()).resolve() if self.source.get() else kit["source"])
        except Exception as error:
            self.status.set("无法载入评审：" + safe_error(error))

    def open_measured_workbench(self):
        from tkinter import filedialog
        if self.gui_process is not None and self.gui_process.poll() is None:
            self.status.set("本次被测工作台已经打开。请在该窗口核验答案、证据和历史恢复。")
            return
        root = self.last_live_output
        if root is None:
            selected = filedialog.askdirectory(parent=self.root, title="选择真实验收报告目录（含 extracted 与 private-profile）")
            if not selected:
                return
            root = Path(selected)
        try:
            self.gui_process = open_workbench(kit_path=self.kit_path.get(), evaluation_dir=root,
                                             ca_file=self.ca.get() or None)
            self.last_live_output = root
            self.status.set("被测工作台已打开：使用本次隔离配置，不碰日常配置；单纯打开不发请求。请核对真实答案、证据、重启恢复及 CA。")
            self.append("打开的是本次实测的 EXE 和原 private-profile。工作台中主动提问仍会产生收费请求，须在既有授权内。")
        except Exception as error:
            self.status.set("无法打开本次工作台：" + safe_error(error))

    def offline(self):
        if self.busy:
            return
        try:
            kit = load_kit(self.kit_path.get())
            require(sys.platform == "win32", "OFFICE_NATIVE_WINDOWS_REQUIRED")
            from office_acceptance_checks import run_office_native
            output = Path(self.output_parent.get()).expanduser().resolve() / (
                "office-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
            require(not output.is_relative_to(kit["root"]), "OFFICE_OUTPUT_KIT_OVERLAP")
        except Exception as error:
            self.status.set("无法开始本机回归：" + safe_error(error))
            return
        import queue
        responses = queue.Queue()
        self.busy = True
        for widget in self.widgets:
            widget.configure(state="disabled")
        self.status.set("本机离线回归运行中：受测 EXE 的完整 17 案例/55 样本，零远程模型调用。大库冷导入可能约十分钟。")
        def work():
            try:
                responses.put((run_office_native(kit["root"], output), None))
            except Exception as error:
                responses.put((None, safe_error(error)))
        threading.Thread(target=work, daemon=False).start()
        def poll():
            if responses.empty():
                self.root.after(200, poll)
                return
            report, error = responses.get_nowait()
            self.busy = False
            for widget in self.widgets:
                widget.configure(state="normal")
            self.last_output = output
            if error:
                self.status.set("本机回归未完成；发布仍被阻断。")
                self.append("错误：" + error)
            else:
                self.office_report.set(str(output / "COMPILED-ACCEPTANCE.json"))
                self.status.set("本机操作记录已保存；仍需逐题真实模型评审及人眼 GUI/公司 CA 确认。")
                self.append(f"本机报告状态：{report.get('status')}\n目录：{output}\n不得把本机合成模型当作真实准确率。")
        self.root.after(200, poll)

    def open_output(self):
        if self.last_output and self.last_output.is_dir() and sys.platform == "win32":
            import os
            os.startfile(str(self.last_output))

    def close(self):
        from tkinter import messagebox
        if self.busy:
            messagebox.showinfo("验收仍在执行", "正在执行真实请求时不直接关闭。请等待预算结束；如进程异常被终止，远程结果须人工核对。", parent=self.root)
            return
        self.root.destroy()

    def run(self):
        self.root.mainloop()


class ReviewWindow:
    def __init__(self, owner, kit, evaluation_dir, source):
        from tkinter import ttk
        tk = owner.tk
        self.owner, self.kit, self.directory, self.source = owner, kit, evaluation_dir, source
        self.template = read_object(evaluation_dir / "HUMAN-REVIEW-TEMPLATE.json")
        self.report = read_object(evaluation_dir / "LIVE-ACCEPTANCE.json")
        require(self.report.get("status") == "completed_not_reviewed", "OFFICE_COMPLETED_REPORT_REQUIRED")
        require(self.template.get("office_acceptance", {}).get("live_report_sha256") == digest(evaluation_dir / "LIVE-ACCEPTANCE.json"),
                "OFFICE_REVIEW_TEMPLATE_STALE")
        self.rows = {(row["id"], row["turn_id"]): row for row in self.report["cases"]}
        self.items = {(item["id"], turn["id"]): {**item, **turn}
                      for item in _cases(kit["inputs"]["cases"]) for turn in _turns(item)}
        self.keys = list(self.rows)
        require(set(self.keys) == set(self.items), "OFFICE_REVIEW_CASES_INCOMPLETE")
        self.scores = {}
        restored = None
        if (evaluation_dir / "HUMAN-REVIEW.json").is_file():
            restored = restore_review(self.template, evaluation_dir / "HUMAN-REVIEW.json")
            for row in restored["reviews"]:
                self.scores[(row["case_id"], row["turn_id"])] = {
                    "required_findings": {item["id"]: item.get("result") for item in row["required_findings"]},
                    "forbidden_claims": {item["id"]: item.get("present") for item in row["forbidden_claims"]},
                    "usefulness": row.get("usefulness"), "followup_continuity": row.get("followup_continuity"),
                    "comments": row.get("comments", ""), "partial_review": row.get("partial_review", {})}
        self.current = None
        self.variables = {}
        self.window = tk.Toplevel(owner.root)
        self.window.title("逐题评审 · 分数初始为空")
        self.window.geometry(f"{min(1100, self.window.winfo_screenwidth() - 40)}x{min(900, self.window.winfo_screenheight() - 100)}")
        self.reviewer = tk.StringVar(value=(restored or {}).get("office_acceptance", {}).get("reviewer", ""))
        self.selected = tk.StringVar()
        top = ttk.Frame(self.window, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="评审人（仅留本地）").pack(side="left")
        ttk.Entry(top, textvariable=self.reviewer, width=25).pack(side="left", padx=8)
        self.selector = ttk.Combobox(top, textvariable=self.selected, values=[" / ".join(key) for key in self.keys], state="readonly", width=65)
        self.selector.pack(side="left")
        self.selector.bind("<<ComboboxSelected>>", self.select)
        self.details = tk.Text(self.window, height=8, wrap="word")
        self.details.pack(fill="both", expand=True, padx=8)
        canvas = tk.Canvas(self.window, height=180)
        scrollbar = ttk.Scrollbar(self.window, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True, padx=8)
        self.form = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=self.form, anchor="nw")
        self.form.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        office = ttk.LabelFrame(self.window, text="实际办公设备人工确认（自动操作记录不能代替人眼 GUI 与公司 CA 核验）", padding=8)
        office.pack(fill="x", padx=8, pady=6)
        self.office_variables = {}
        checks = self.template["office_acceptance"]["checks"]
        for index, name in enumerate(checks):
            selected = (restored or {}).get("office_acceptance", {}).get("checks", {}).get(name)
            variable = tk.StringVar(value={None: "未确认", True: "通过", False: "失败"}.get(selected, "未确认"))
            self.office_variables[name] = variable
            ttk.Label(office, text=OFFICE_LABELS.get(name, name)).grid(row=index, column=0, sticky="w")
            ttk.Combobox(office, textvariable=variable, values=("未确认", "通过", "失败"), state="readonly", width=12).grid(row=index, column=1, padx=8, pady=2)
        self.note = tk.StringVar(value=(restored or {}).get("office_acceptance", {}).get("comments", ""))
        ttk.Label(office, text="设备/版本/操作现象及问题说明").grid(row=len(checks), column=0, sticky="w")
        ttk.Entry(office, textvariable=self.note, width=85).grid(row=len(checks), column=1, sticky="ew", pady=3)
        ttk.Button(self.window, text="保存当前选择，并离线核验发布门禁（不发布）", command=self.save).pack(pady=7)
        self.summary = tk.StringVar(value="不得批量默认通过；未评分、错误、遗漏或设备未确认都会保留阻断。")
        ttk.Label(self.window, textvariable=self.summary, wraplength=1000).pack(padx=8, pady=5)
        self.selector.current(0)
        self.select()

    def collect(self):
        if self.current is None:
            return
        findings = {name: variable.get() or None for name, variable in self.variables.get("required_findings", {}).items()}
        claims = {name: {"未确认": None, "未出现": False, "出现": True}[variable.get()]
                  for name, variable in self.variables.get("forbidden_claims", {}).items()}
        self.scores[self.current] = {"required_findings": findings, "forbidden_claims": claims,
            "usefulness": self.variables["usefulness"].get() or None,
            "followup_continuity": self.variables["followup_continuity"].get() or None,
            "comments": self.variables["comments"].get()}
        if "partial_review" in self.variables:
            partial = self.variables["partial_review"]
            self.scores[self.current]["partial_review"] = {
                "classification": partial["classification"].get() or None,
                "reason": partial["reason"].get(),
                "affected_findings": [name.strip() for name in partial["affected_findings"].get().split(",") if name.strip()],
                "resolved": {"未确认": None, "已查清": True, "未查清": False}[partial["resolved"].get()]}

    def select(self, _event=None):
        from tkinter import ttk
        self.collect()
        self.current = self.keys[self.selector.current()]
        row, item = self.rows[self.current], self.items[self.current]
        chunks = ["问题：" + row["question"], "实际答案：\n" + row["answer"],
                  f"实际状态：{row.get('agent_status')}，原因：{row.get('reason_code') or row.get('stop_reason')}，耗时：{row.get('elapsed_seconds')} 秒",
                  "本次实际供证身份：\n" + json.dumps(row.get("supplied_evidence", {}), ensure_ascii=False, indent=2),
                  "实际调查诊断（义务/缺口/停止原因）：\n" + json.dumps(row.get("acceptance_diagnostics", {}), ensure_ascii=False, indent=2),
                  "冻结关键结论与禁增结论：\n" + json.dumps(item.get("gold", {}), ensure_ascii=False, indent=2)]
        for span in item.get("reviewed_context", {}).get("source_ranges", []):
            excerpt = _source_excerpt(self.source, span)
            chunks.append(f"冻结源码 {excerpt['path']}:{excerpt['start_line']}-{excerpt['end_line']}\n{excerpt['excerpt']}")
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", "\n\n".join(chunks))
        self.details.configure(state="disabled")
        for widget in self.form.winfo_children():
            widget.destroy()
        self.variables = {"required_findings": {}, "forbidden_claims": {}}
        score = self.scores.get(self.current, {})
        ttk.Label(self.form, text="met=准确覆盖，omitted=遗漏，incorrect=错误；usable=可用；preserved=追问连续，not_applicable=首轮。").grid(
            row=0, column=0, columnspan=2, sticky="w", padx=8, pady=4)
        index = 1
        for group, entries in (("required_findings", (item.get("gold") or {}).get("required_findings", [])),
                               ("forbidden_claims", (item.get("gold") or {}).get("forbidden_claims", []))):
            for number, finding in enumerate(entries, 1):
                name = finding.get("id", str(number)) if isinstance(finding, dict) else str(number)
                value = score.get(group, {}).get(name)
                values = ("", "met", "omitted", "incorrect") if group == "required_findings" else ("未确认", "未出现", "出现")
                selected = (value or "") if group == "required_findings" else {None: "未确认", False: "未出现", True: "出现"}[value]
                variable = self.owner.tk.StringVar(value=selected)
                self.variables[group][name] = variable
                label = "关键结论" if group == "required_findings" else "禁止新增结论"
                ttk.Label(self.form, text=f"{label} {name}").grid(row=index, column=0, sticky="w", padx=8, pady=3)
                ttk.Combobox(self.form, textvariable=variable, values=values, state="readonly", width=18).grid(row=index, column=1, sticky="w")
                index += 1
        for name, label, choices in (("usefulness", "业务可用性", ("", "usable", "unusable")),
                                     ("followup_continuity", "多轮连续性", ("", "preserved", "broken", "not_applicable"))):
            variable = self.owner.tk.StringVar(value=score.get(name) or "")
            self.variables[name] = variable
            ttk.Label(self.form, text=label).grid(row=index, column=0, sticky="w", padx=8, pady=3)
            ttk.Combobox(self.form, textvariable=variable, values=choices, state="readonly", width=18).grid(row=index, column=1, sticky="w")
            index += 1
        self.variables["comments"] = self.owner.tk.StringVar(value=score.get("comments", ""))
        ttk.Label(self.form, text="评审说明（注明错误/遗漏/未知边界）").grid(row=index, column=0, sticky="w", padx=8, pady=3)
        ttk.Entry(self.form, textvariable=self.variables["comments"], width=70).grid(row=index, column=1, sticky="w")
        index += 1
        template_row = next(part for part in self.template["reviews"]
                            if (part["case_id"], part["turn_id"]) == self.current)
        if "partial_review" in template_row:
            partial = score.get("partial_review", {})
            self.variables["partial_review"] = {}
            for name, label, choices in (
                ("classification", "PARTIAL 具体分类", ("", "scope_mismatch", "answer_omission", "evidence_gap", "explicit_unknown", "unsupported_semantics")),
                ("resolved", "是否逐条查清，并保留必要未知边界", ("未确认", "已查清", "未查清"))):
                selected = (partial.get(name) or "") if name == "classification" else {None: "未确认", True: "已查清", False: "未查清"}[partial.get(name)]
                variable = self.owner.tk.StringVar(value=selected)
                self.variables["partial_review"][name] = variable
                ttk.Label(self.form, text=label).grid(row=index, column=0, sticky="w", padx=8, pady=3)
                ttk.Combobox(self.form, textvariable=variable, values=choices, state="readonly", width=28).grid(row=index, column=1, sticky="w")
                index += 1
            for name, label in (("reason", "具体原因与答案中独立成立/未知的内容"),
                                ("affected_findings", "受影响关键结论 ID（逗号分隔，必须填写）")):
                value = partial.get(name, "")
                if isinstance(value, list):
                    value = ",".join(value)
                variable = self.owner.tk.StringVar(value=value)
                self.variables["partial_review"][name] = variable
                ttk.Label(self.form, text=label).grid(row=index, column=0, sticky="w", padx=8, pady=3)
                ttk.Entry(self.form, textvariable=variable, width=70).grid(row=index, column=1, sticky="w")
                index += 1

    def save(self):
        try:
            self.collect()
            checks = {name: {"未确认": None, "通过": True, "失败": False}[variable.get()]
                      for name, variable in self.office_variables.items()}
            path = self.directory / "HUMAN-REVIEW.json"
            save_review(path, self.template, self.scores, reviewer=self.reviewer.get(), office_checks=checks,
                        office_note=self.note.get(), office_report=self.owner.office_report.get() or None)
            result = check_review(kit_path=self.kit["path"], evaluation_dir=self.directory, reviews=path,
                                  office_report=self.owner.office_report.get() or None)
            if result["publish_allowed"]:
                self.summary.set("本地同包门禁通过。助手未发布；维护者须经唯一发布入口核对草稿与资产后发布。")
            else:
                codes = sorted({row["code"] for row in [*result["candidate_errors"], *result["publish_errors"]]})
                self.summary.set("已保存；正式发布仍被阻断：" + ", ".join(codes))
        except Exception as error:
            self.summary.set("评审未完成：" + safe_error(error))


def safe_error(error):
    """Only local validation codes may be shown; arbitrary exceptions stay private."""
    text = str(error)
    return text if re.fullmatch(r"[A-Z][A-Z0-9_]{2,100}", text) else type(error).__name__


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kit", type=Path, default=default_kit_path())
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--plan", action="store_true")
    modes.add_argument("--run", action="store_true")
    modes.add_argument("--self-test", action="store_true")
    modes.add_argument("--offline", action="store_true")
    parser.add_argument("--self-test-result", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--profile-config", type=Path)
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--compilation-context", type=Path)
    parser.add_argument("--authorize-model-and-source", action="store_true")
    parser.add_argument("--request-budget", type=int)
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            result = self_test()
            if args.self_test_result:
                _safe_write(args.self_test_result.resolve(), result)
            print(json.dumps(result))
            return 0
        if args.offline:
            require(args.output is not None, "OFFICE_OUTPUT_REQUIRED")
            require(sys.platform == "win32", "OFFICE_NATIVE_WINDOWS_REQUIRED")
            kit = load_kit(args.kit)
            from office_acceptance_checks import run_office_native
            report = run_office_native(kit["root"], args.output)
            print(json.dumps({"status": report.get("status"), "model_calls": 0, "publication_performed": False}))
            return 0 if report.get("status") == "passed" else 1
        if args.plan or args.run:
            require(args.output is not None, "OFFICE_OUTPUT_REQUIRED")
            require(not args.run or args.authorize_model_and_source, "OFFICE_LOCAL_AUTHORIZATION_REQUIRED")
            report = run_kit(kit_path=args.kit, output=args.output, source=args.source,
                profile_config=args.profile_config, ca_file=args.ca_file, compilation_context=args.compilation_context,
                allow_network=args.run, request_budget=args.request_budget)
            print(json.dumps({"status": report["status"], "turns": report["plan"]["user_turn_count"],
                "maximum_model_requests": report["plan"]["max_model_requests"], "publication_performed": False}))
            return 0
        AcceptanceWindow(args.kit).run()
        return 0
    except Exception as error:
        print("Office acceptance stopped: " + safe_error(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
