"""Print a deterministic, network-free replay of three online answer delivery paths."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch


COMMIT = "97231568836ab4ae5e9e7cc6f58d4be19abfab5f"
QUESTION = "输入金额满足什么条件才会处理，异常分支是什么？"
READS = {"main.cbl": (1, 13), "worker.cbl": (1, 8), "records.cpy": (1, 2)}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def dispatch(app, method, path, payload=None, expected=200):
    status, response = app.dispatch(method, "/api/online/" + path, payload)
    require(status == expected, f"{method} {path} returned {status}: {response.get('error')}")
    return response


def finished(app, job_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = dispatch(app, "GET", "jobs/" + job_id)["job"]
        if job["status"] != "running":
            return job
        time.sleep(.01)
    raise TimeoutError("Fixture job did not finish")


def observe(app, job_id, label):
    state = dispatch(app, "GET", "state")
    job = dispatch(app, "GET", "jobs/" + job_id)["job"]
    require(job["status"] == state["active_job"]["status"], "Public state and job disagree")
    conv = state["conversation"]
    pending = conv.get("pending") or {}
    assistant = [msg for msg in conv["messages"] if msg["role"] == "assistant" and msg.get("content")]
    return {"step": label, "job_status": job["status"], "phase": job["progress"]["phase"],
            "error": job.get("error"), "resume_available": bool(pending.get("can_resume")),
            "retry_confirmation_required": bool(pending.get("retry_confirmation_required")),
            "conversation_status": conv["status"], "message_roles": [m["role"] for m in conv["messages"]],
            "assistant_body_count": len(assistant),
            "cited_files": sorted(c["path"] for msg in assistant for c in msg.get("citations", [])),
            "source_activity": job.get("source_activity") or []}


def replay(mode, directory):
    from company_api import APIClientError
    from online_http_fixture import OnlineFixture, SyntheticModel, run_investigation

    entered, release = threading.Event(), threading.Event()
    requests = []

    class Client(SyntheticModel):
        def complete(self, *, messages, check_cancel=None, **options):
            context = json.loads(messages[-1]["content"])
            paths = sorted(page["citation"]["path"] for page in context["source_context"])
            requests.append(paths)
            if not paths:
                actions = [{"tool": "read", "path": path, "start_line": start, "end_line": end}
                           for path, (start, end) in READS.items()]
                content = json.dumps({"actions": actions, "remaining": []})
                return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}
            require(paths == sorted(READS), "Missing evidence in the answer request")
            if mode != "continuation":
                entered.set()
                require(release.wait(10), "Fixture did not release")
            if mode == "unknown_timeout":
                self.last_request_outcome = "unknown"
                raise APIClientError("REQUEST_TIMEOUT", transport_reason="timeout")
            return super().complete(messages=messages, check_cancel=check_cancel, **options)

    fixture = OnlineFixture(directory)
    app = fixture.controller()
    app.config_provider = lambda: None

    def run(*args, **options):
        # In the continuation case the first request only asks to read sources.
        # Its single-request allowance cannot fund an answer on that same job.
        return run_investigation(*args, **options, client=Client(), max_model_calls=1 if mode == "continuation" else 2)

    app.investigator = run
    samples = []
    try:
        connection = fixture.connect_payload()
        connection.pop("credentials")
        connected = finished(app, dispatch(app, "POST", "connect", connection, 202)["job_id"])
        require(connected["status"] == "completed", "Synthetic workspace connection failed")
        conv = dispatch(app, "POST", "conversations", {"workspace_id": connected["workspace_id"]}, 201)["conversation"]
        base = {"conversation_id": conv["id"], "allow_source_transfer": True}
        job = dispatch(app, "POST", "question", {**base, "question": QUESTION}, 202)["job_id"]
        if mode == "continuation":
            finished(app, job)
            first = observe(app, job, "one_request_budget_exhausted")
            require(first["job_status"] == "needs_continuation" and first["resume_available"]
                    and first["error"] == "ONLINE_MODEL_BUDGET_EXCEEDED", "Continuation was not offered")
            require(not first["retry_confirmation_required"], "Ordinary continuation requested duplicate-call approval")
            first["prior_source_files_read"] = sorted({path for _, path in fixture.source_reads})
            require(first["prior_source_files_read"] == sorted(READS), "Budget expired before all reads")
            samples.append(first)
            job = dispatch(app, "POST", "resume", base, 202)["job_id"]
        else:
            require(entered.wait(10), "Synthetic answer-stage request was not reached")
            first = observe(app, job, "answer_request_held_after_three_reads")
            require(first["job_status"] == "running" and first["phase"] == "model_request_started",
                    "Held request missing from public progress")
            samples.append(first)
            release.set()
        require(first["assistant_body_count"] == 0 and first["message_roles"] == ["user"], "Unexpected early answer")
        finished(app, job)
        last = observe(app, job, "after_resume" if mode == "continuation" else "after_release")
        samples.append(last)
        if mode == "unknown_timeout":
            require(last["job_status"] == "retry_confirmation_required" and last["retry_confirmation_required"]
                    and last["error"] == "REQUEST_TIMEOUT" and last["assistant_body_count"] == 0, "Unexpected timeout state")
        else:
            require(last["job_status"] == "answered" and last["message_roles"] == ["user", "assistant"]
                    and last["assistant_body_count"] == 1 and last["cited_files"] == sorted(READS), "Answer lost or duplicated")
        require(requests == [[], sorted(READS)], "Unexpected request count or evidence in request")
        return {"scenario": mode, "max_requests_per_job": 1 if mode == "continuation" else 2,
                "scripted_client_request_source_files": requests, "observations": samples}
    finally:
        release.set()
        app.close()


def main():
    root = Path(__file__).resolve().parents[3]
    with tempfile.TemporaryDirectory() as tmp:
        checkout = Path(tmp) / "checkout"
        subprocess.run(["git", "clone", "--local", "--shared", "--no-checkout", "--quiet", str(root), str(checkout)],
                       check=True)
        subprocess.run(["git", "sparse-checkout", "set", "poc"], cwd=checkout, check=True)
        subprocess.run(["git", "checkout", "--quiet", "--detach", COMMIT], cwd=checkout, check=True)
        sys.dont_write_bytecode = True
        sys.path[:0] = [str(checkout / "poc"), str(checkout / "poc" / "tests")]
        with patch("socket.socket.connect", side_effect=AssertionError("Network disabled")) as connections:
            scenarios = [replay(mode, Path(tmp) / mode) for mode in ("delayed_answer", "continuation", "unknown_timeout")]
            require(connections.call_count == 0, "Unexpected network attempt")
        print(json.dumps({"application_commit": COMMIT, "question": QUESTION,
                          "fixture": "existing local online_http_fixture; fixed scripted evidence requests and answer",
                          "surface": "OnlineWorkbench public question, state, jobs and resume routes via dispatch",
                          "real_model_calls": 0, "socket_connect_calls": 0, "scenarios": scenarios,
                          "boundary": "Controlled states only; no elapsed-time estimate, browser, device or enterprise validation."},
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
