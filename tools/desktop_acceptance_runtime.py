"""Portable standard-library harness for the real compiled desktop HTTP path.

Only synthetic loopback providers and explicitly created profiles are supported.
The fixture key is public test material and must never protect a real service.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import ssl
import subprocess
import threading
import time
from urllib.parse import urlsplit


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def supplied_pages(payload):
    return [page for bundle in payload.get("source_context", []) for page in bundle.get("pages", [])]


def decoded_navigation(payload):
    """Independently decode the wire contract before checking supplied facts.

    Keep the original request bytes and payload in provider records. This
    consumer deliberately does not import the application's projection code.
    """
    from copy import deepcopy
    marker = payload.get("prompt_projection")
    columns_marker = payload.get("navigation_encoding")
    if marker is not None:
        require(isinstance(marker, dict) and marker.get("format") == "shared-navigation/v1", "Unknown navigation contract")
    if columns_marker is not None:
        require(isinstance(columns_marker, dict) and columns_marker.get("format") == "column-values/v1",
                "Unknown navigation column contract")
    records = payload.get("shared_navigation_facts", {})
    require(marker is None or isinstance(records, dict), "Invalid navigation records")
    protected = {"pages", "working_set", "source_text", "framework_references", "framework_facts", "history"}
    column_lists = {"links", "units", "relations", "slice_statements", "statements"}

    def expand(value, references=()):
        if isinstance(value, list):
            return [expand(row, references) for row in value]
        if not isinstance(value, dict):
            return value
        if marker is not None and "navigation_fact_ref" in value:
            reference = value["navigation_fact_ref"]
            require(reference in records, "Missing navigation record")
            require(reference not in references, "Cyclic navigation record")
            require(isinstance(records[reference], dict), "Invalid navigation record")
            references = (*references, reference)
            value = {**deepcopy(records[reference]), **{key: item for key, item in value.items()
                                                     if key != "navigation_fact_ref"}}
        result = {key: item for key, item in value.items()
                  if marker is None or not key.endswith("_shared_fields")}
        if columns_marker is not None:
            for key in column_lists:
                columns = result.pop(key + "_columns", None)
                if columns is None:
                    continue
                require(isinstance(columns, list) and all(isinstance(field, str) for field in columns)
                    and len(columns) == len(set(columns)) and isinstance(result.get(key), list),
                    "Invalid navigation columns")
                rows = []
                for row in result[key]:
                    require(isinstance(row, dict) and isinstance(row.get("_values"), list)
                        and len(row["_values"]) == len(columns)
                        and not (set(columns) & (set(row) - {"_values"})), "Invalid navigation column row")
                    rows.append({**{field: item for field, item in row.items() if field != "_values"},
                                 **dict(zip(columns, row["_values"]))})
                result[key] = rows
        for key in list(result):
            if key in protected:
                continue
            shared = value.get(key + "_shared_fields") if marker is not None else None
            if shared is not None:
                require(isinstance(shared, dict) and isinstance(result[key], list)
                    and all(isinstance(row, dict) for row in result[key]), "Shared navigation fields need rows")
                result[key] = [{**deepcopy(shared), **row} for row in result[key]]
            result[key] = expand(result[key], references)
        return result

    output = deepcopy(payload)
    for key in ("business_map", "evidence_groups", "business_analysis_brief", "question_investigation"):
        if key in output:
            wrapper = {key: output[key]}
            shared_key = key + "_shared_fields"
            if shared_key in output:
                wrapper[shared_key] = output.pop(shared_key)
            output[key] = expand(wrapper)[key]
    if "source_context" in output:
        output["source_context"] = [expand(context) for context in output["source_context"]]
    for key in ("prompt_projection", "navigation_encoding", "shared_navigation_facts"):
        output.pop(key, None)
    return output


@dataclass
class SlowHeaders:
    """Keep the real HTTP header parser waiting while sending small headers."""
    release: threading.Event = field(default_factory=threading.Event)
    started: threading.Event = field(default_factory=threading.Event)
    chunks: int = 0


class LoopbackProvider:
    def __init__(self, root, responder):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.responder = responder
        self.requests, self.errors, self.slow_responses = [], [], []
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def do_POST(self):
                slow = False
                try:
                    require(self.path == "/v1/chat/completions", "Unexpected provider route")
                    length = int(self.headers["Content-Length"])
                    require(0 < length <= 4 * 1024 * 1024, "Provider request exceeds bound")
                    body = self.rfile.read(length)
                    envelope = json.loads(body)
                    payload = json.loads(envelope["messages"][-1]["content"])
                    with owner._lock:
                        index = len(owner.requests)
                        record = {"index": index, "received_at": time.monotonic(), "request_bytes": length,
                            "request_sha256": hashlib.sha256(body).hexdigest(), "envelope": envelope,
                            "payload": payload, "source_pages": supplied_pages(payload)}
                        owner.requests.append(record)
                    (owner.root / f"request-{index + 1:03d}.json").write_bytes(body)
                    response = owner.responder(envelope, payload, index)
                    if isinstance(response, SlowHeaders):
                        slow = True
                        owner.slow_responses.append(response)
                        self.connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n")
                        response.started.set()
                        while not response.release.wait(0.05):
                            if response.chunks < 85:
                                self.connection.sendall(b"X-Progress: pending\r\n")
                                response.chunks += 1
                        self.connection.sendall(b"Content-Length: 2\r\n\r\n{}")
                    else:
                        require(isinstance(response, str), "Provider must return answer text or SlowHeaders")
                        data = json.dumps({"choices": [{"message": {"role": "assistant", "content": response},
                            "finish_reason": "stop"}]}, ensure_ascii=False).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                    record["response_finished_at"] = time.monotonic()
                except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError) as error:
                    if not slow:
                        owner.errors.append(type(error).__name__)
                except Exception as error:
                    owner.errors.append(type(error).__name__ + ": " + str(error))
                    try:
                        self.send_error(500)
                    except OSError:
                        pass
                finally:
                    self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(FIXTURES / "loopback-server.crt", FIXTURES / "loopback-server.key")
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.base_url = f"https://127.0.0.1:{self.server.server_port}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        for response in self.slow_responses:
            response.release.set()
        if self.thread.is_alive():
            self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def isolated_environment(overrides=None):
    environment = {name: value for name, value in os.environ.items()
        if not name.upper().startswith(("COMPANY_", "WORKBENCH_", "PYTHON", "FRAMEWORK_"))
        and name.upper() not in {"AGENT_SETTINGS_PATH", "BROWSER", "SSL_CERT_FILE", "SSL_CERT_DIR",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"}}
    environment.update(overrides or {})
    environment["NO_PROXY"] = "127.0.0.1,localhost"
    return environment


def write_profile(profile, provider, agent_settings=None):
    profile = Path(profile).resolve()
    profile.mkdir(parents=True, exist_ok=False)
    require(urlsplit(provider.base_url).hostname == "127.0.0.1", "Only loopback provider is allowed")
    (profile / ".env").write_text("# Synthetic local acceptance only.\n"
        f"COMPANY_API_BASE_URL={provider.base_url}\nCOMPANY_API_KEY=offline-synthetic-credential\n"
        "COMPANY_CHAT_MODEL=synthetic-acceptance\nCOMPANY_API_STYLE=openai_compatible\n", encoding="utf-8")
    settings = {"max_model_requests": 1, "max_answer_revisions": 0, "max_question_seconds": 120}
    settings.update(agent_settings or {})
    (profile / "agent-settings.json").write_text(json.dumps({"version": 1, "agent": settings}), encoding="utf-8")
    return isolated_environment({"SSL_CERT_FILE": str(FIXTURES / "loopback-server.crt"),
        "AGENT_SETTINGS_PATH": str(profile / "agent-settings.json")})


class NativeWorkbench:
    def __init__(self, command, profile, environment, root, *, capture_context=False):
        self.command = [str(item) for item in command]
        self.profile, self.root = Path(profile).resolve(), Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.environment = dict(environment)
        self.capture_context = bool(capture_context)
        self.info_path = self.profile / "acceptance-server.json"
        self.process = self.process_log = None
        self.origin = self.token = self.address = None
        self.progress_samples = []

    def start(self):
        require(self.process is None or self.process.poll() is not None, "Workbench already running")
        self.info_path.unlink(missing_ok=True)
        self.token = None
        self.process_log = (self.root / "desktop-process.log").open("ab")
        self.process = subprocess.Popen([*self.command, "--data-dir", str(self.profile),
            "--acceptance-server-info", str(self.info_path),
            *(["--acceptance-capture-context"] if self.capture_context else [])], env=self.environment, cwd=self.root,
            stdin=subprocess.DEVNULL, stdout=self.process_log, stderr=self.process_log)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            require(self.process.poll() is None, "Workbench exited before startup; inspect desktop-process.log")
            try:
                info = json.loads(self.info_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.05)
                continue
            self.origin = info["origin"]
            self.address = urlsplit(self.origin)
            require(self.address.scheme == "http" and self.address.hostname == "127.0.0.1"
                and not self.address.username and not self.address.path.strip("/"), "Invalid desktop origin")
            require(info["pid"] == self.process.pid, "Desktop startup PID mismatch")
            state = self.request("GET", "/api/state")
            self.token = state["session_token"]
            require(state.get("api_configured") is True, "Synthetic profile was not loaded")
            return state
        raise TimeoutError("Workbench startup timed out")

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.process_log is not None:
            self.process_log.close()
            self.process_log = None

    def __enter__(self):
        try:
            self.start()
        except BaseException:
            self.stop()
            raise
        return self

    def __exit__(self, *_):
        self.stop()

    def request(self, method, path, payload=None):
        require(path.startswith("/") and not path.startswith("//"), "Local API path required")
        headers = {"Origin": self.origin}
        if self.token:
            headers["X-Session-Token"] = self.token
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.address.port, timeout=20)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            data = json.loads(response.read())
            require(response.status in {200, 202}, "Workbench HTTP rejected: " + str(response.status))
            return data
        finally:
            connection.close()

    def submit(self, source, output, question="", conversation_id=None, **options):
        payload = {"source": str(Path(source).resolve()), "output": str(Path(output).resolve()), "question": question,
            "source_format": "free", "answer_detail": "brief", "allow_network": bool(question),
            "index_mode": "catalog", "reading_strategy": "retrieval", "framework_reference_path": ""}
        payload.update(options)
        if conversation_id:
            payload["conversation_id"] = conversation_id
        return self.request("POST", "/api/analyze", payload)["job_id"]

    def wait_job(self, job_id, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.request("GET", "/api/jobs/" + job_id)
            self.progress_samples.append({"observed_at": time.monotonic(), "status": job["status"],
                "progress": job.get("progress", {})})
            if job["status"] != "RUNNING":
                return job
            time.sleep(0.05)
        raise TimeoutError("Workbench job exceeded acceptance bound")

    def analyze(self, source, output, question="", conversation_id=None, timeout=120, **options):
        start = time.monotonic()
        sample_start = len(self.progress_samples)
        job = self.wait_job(self.submit(source, output, question, conversation_id, **options), timeout)
        elapsed = time.monotonic() - start
        state = self.request("GET", "/api/state")
        result_path = Path(output) / "agent-result.json"
        result = json.loads(result_path.read_text(encoding="utf-8")) if question and result_path.is_file() else None
        return {"job": job, "state": state, "result": result, "started_at": start, "elapsed_seconds": elapsed,
            "progress_samples": self.progress_samples[sample_start:]}


def read_journal(output):
    path = Path(output).resolve() / "investigation-runs.sqlite"
    require(path.is_file(), "RunStore missing")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as database:
        database.row_factory = sqlite3.Row
        return {"runs": [dict(row) for row in database.execute("SELECT run_id,state FROM investigation_runs")],
            "requests": [dict(row) for row in database.execute(
                "SELECT run_id,request_id,state,request_hash,response_json FROM investigation_requests")]}
