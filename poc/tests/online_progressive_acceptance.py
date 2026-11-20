"""Exercise online answer delivery using the existing neutral two-program question."""

from contextlib import ExitStack
import hashlib
from threading import Event
import time

QUESTION = "保费收取时 reserve 和 refund 的扣减顺序及适用条件是什么？"
DRAFT = "先使用准备金，剩余应收大于零时调用退款处理。"


def require(value, message):
    if not value:
        raise AssertionError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def observation(conversation, request, files):
    pending = conversation.get("pending") or {}
    preview = pending.get("progressive_answer") or {}
    answers = [m for m in conversation["messages"] if m["role"] == "assistant"]
    shown = preview or (answers[-1] if answers else {})
    citations = []
    for cite in shown.get("citations", []):
        key = cite.get("key") or cite["id"]
        result = request("GET", "evidence", query={"conversation_id": conversation["id"], "citation_id": key})
        actual = result["citation"]
        require(all(actual[k] == cite[k] for k in ("path", "hash", "start", "end", "revision", "commit")),
                "visible citation resolved to a different source")
        raw = files[actual["path"]]
        lines = raw.decode("utf-8").splitlines()[actual["start"] - 1:actual["end"]]
        require(digest(raw) == actual["hash"] and result["text"] == "\n".join(lines),
                "citation does not resolve to the expected physical lines")
        citations.append({k: actual[k] for k in ("path", "hash", "start", "end")})
    return {"status": conversation["status"], "assistant_messages": len(answers),
            "has_pending_preview": bool(preview),
            "answer": shown.get("answer") or shown.get("content") or "", "sources": citations}


def replay(variant, paused, root):
    import test_online_automatic_source_search as original
    from online_workspace import OnlineWorkbench
    from online_investigation import run_investigation
    from online_material_export import export_material
    from online_material_reader import DirectoryObjectStore, MaterialReader
    from business_index import build_business_index

    with ExitStack() as scope:
        case = original.AutomaticSourceSearchTests()
        scope.callback(case.doCleanups)
        case.setUp()
        require(case.question == QUESTION, "historical question changed")
        files = case.case.source_bytes
        if variant:
            names = {"cash.cbl": "bill.cbl", "return.cbl": "rebate.cbl", "notice.cbl": "notice.cbl"}
            replacements = [(b"NET-DUE", b"OPEN-SUM"), (b"CREDIT-AMT", b"RESERVE-UNITS"),
                            (b"REFUND-BALANCE", b"RETURN-POOL"), (b"DUE", b"NET-LEFT"),
                            (b"ITEM-TYPE", b"CLASS-CODE"), (b"1-RESERVECREDIT", b"7-RESERVE"),
                            (b"APPLYREFUND", b"REDEEMREFUND"), (b"CASHENTRY", b"BILLENTRY"),
                            (b" > 0", b" > 12"), (b" < ", b" <= ")]
            changed = {}
            for name, content in files.items():
                for old, new in replacements:
                    content = content.replace(old, new)
                changed[names[name]] = content
            files = changed

        source = root / "src"
        source.mkdir()
        for name, content in files.items():
            (source / name).write_bytes(content)
        database = root / "facts.sqlite"
        material = root / "material"
        report = build_business_index(source, database, source_format="free",
                                      verify_content=True, framework_reference_path="", quiet=True)
        manifest = export_material(database, source, material, repository_id="example/source",
                                   commit="b" * 40, build_report=report)
        first_page = Event()
        release_first = Event()
        has_final_request = Event()
        release_final = Event()
        requested_pages = []

        def respond(context):
            pages = context["source_context"]
            requested_pages.append(sorted(page["citation"]["path"] for page in pages))
            if not pages:
                return original.missing(context)
            has_final_request.set()
            require(release_final.wait(10), "final reply not released")
            return original.scenarios.answer(context, DRAFT)

        def investigate(*args, **options):
            journal = options["progress"]
            def record(event):
                journal(event)
                if event["phase"] == "source_action_completed" and event["checkpoint"]["evidence"] and not first_page.is_set():
                    first_page.set()
                    require(release_first.wait(10), "first source not released")
            options["progress"] = record
            if paused:
                options["max_model_calls"] = 1
            return run_investigation(*args, **options, client=case.case.client([respond] * 12))

        def provide(_, credentials, expected, check_cancel):
            return MaterialReader(DirectoryObjectStore(material), expected_repository=expected.get("repository"),
                                  expected_commit=expected.get("commit"), expected_revision=expected.get("revision"),
                                  check_cancel=check_cancel)

        app = OnlineWorkbench(root / "profile", material_factory=provide,
                              source_factory=lambda *_: lambda name, *, max_bytes: files[name],
                              config_provider=lambda: case.case.config, investigator=investigate)
        scope.callback(app.close)
        scope.callback(release_first.set)
        scope.callback(release_final.set)

        def request(method, path, body=None, *, status=200, query=None):
            actual, response = app.dispatch(method, "/api/online/" + path, body, query)
            require(actual == status, f"{path} returned {actual}: {response}")
            return response

        def wait(job):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                status = request("GET", "jobs/" + job)["job"]["status"]
                if status != "running":
                    return status
                time.sleep(.005)
            raise TimeoutError(job)

        def snapshot():
            return observation(request("GET", "state")["conversation"], request, files)

        connected = request("POST", "connect", {
            "location": {"kind": "https", "base_url": "https://materials.example.invalid/pilot/"},
            "repository": "example/source", "commit": "b" * 40, "revision": manifest["revision"]}, status=202)
        require(wait(connected["job_id"]) == "completed", "connection failed")
        workspace = request("GET", "state")["current_workspace_id"]
        conversation = request("POST", "conversations", {"workspace_id": workspace}, status=201)["conversation"]["id"]
        payload = {"conversation_id": conversation, "allow_source_transfer": True}
        job = request("POST", "question", dict(payload, question=QUESTION), status=202)["job_id"]
        require(first_page.wait(10), "never read an original source")
        stages = {"after_first_verified_read": snapshot()}
        release_first.set()
        if paused:
            require(wait(job) == "needs_continuation", "first job did not retain unfinished work")
            stages["budget_reached"] = snapshot()
            job = request("POST", "resume", payload, status=202)["job_id"]
        require(has_final_request.wait(10), "no request with evidence")
        stages["waiting_for_final_reply"] = snapshot()
        release_final.set()
        status = wait(job)
        require(status == "answered", f"failed to deliver the final reply: {status}, requests={requested_pages}, {snapshot()}")
        stages["delivered"] = snapshot()
        require(stages["delivered"]["assistant_messages"] == 1, "duplicate terminal answer")
        return {"continued_after_budget": paused, "files_sha256": {k: digest(v) for k, v in files.items()},
                "revision": manifest["revision"], "source_files_in_each_request": requested_pages,
                "states": stages}
