"""Replay the two-program reserve deduction evidence probe without network access.

Run with Python 3.11+ and the two pinned Git objects locally available.
The default prints JSON. --verify compares it with the checked-in record.
"""

import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[3]
BASE = "81b91c76e8f657de871cb6a7f675dcd628d0a3ad"
CANDIDATE = "97231568836ab4ae5e9e7cc6f58d4be19abfab5f"
FIXTURE = "poc/tests/test_online_automatic_source_search.py"
FIXTURE_HASH = "75b22fe3fec74477467de596bd7a44244db3278b71e04f33f6e41b966b620b17"
SOURCE_HASHES = {
    "cash.cbl": "29e47638142e07afcc71bdd0d4e7d5b57edc23e0379886be7a9aa4a3ad132aae",
    "return.cbl": "f1add39ab8cc1fa01bca5bd6321ad575682b6086de3859e9775fcc53cce6ce24",
}
QUESTION = "保费收取时 reserve 和 refund 的扣减顺序及适用条件是什么？"
# The independent comparison retains the close of each IF and the callee's
# GOBACK so that fall-through, call order and the lack of an ELSE are visible.
REVIEWED = [("cash.cbl", 4, 9), ("return.cbl", 3, 7)]
CHECKS = {
    "准备金扣减须满足指定类型，才从应收中减去该金额": [("cash.cbl", 4, 6)],
    "准备金分支结束后，独立检查应收是否大于零，再决定是否调用退款程序":
        [("cash.cbl", 4, 9)],
    "传给退款程序的是处理到此时的应收，调用实参与子程序入口对应":
        [("cash.cbl", 7, 9), ("return.cbl", 3, 3)],
    "退款余额严格小于应收才扣减，相等或更大时直接返回":
        [("return.cbl", 3, 7)],
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def load_version(commit, directory):
    names = git("ls-tree", "--name-only", commit + ":poc").decode().splitlines()
    paths = ["poc/" + name for name in names if name.endswith(".py")]
    paths.append("poc/tests/test_online_investigation.py")
    archive = git("archive", "--format=zip", commit, "--", *paths)
    with ZipFile(io.BytesIO(archive)) as files:
        for name in paths:
            target = directory / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(files.read(name))
    # The newer fixture is input/harness only. Each run imports the selected
    # revision's production modules and its own version of the transport.
    source = git("show", CANDIDATE + ":" + FIXTURE)
    require(digest(source) == FIXTURE_HASH, "fixture hash differs")
    (directory / FIXTURE).write_bytes(source)


def page(raw_files, path, start, end):
    raw = raw_files[path]
    lines = raw.decode("utf-8").splitlines()
    require(1 <= start <= end <= len(lines), "invalid range")
    return {"path": path, "start": start, "end": end, "sha256": digest(raw),
            "text": "\n".join(lines[start - 1:end])}


def coverage(pages):
    locations = {(item["path"], n) for item in pages for n in range(item["start"], item["end"] + 1)}
    result = {}
    for finding, ranges in CHECKS.items():
        needed = {(path, n) for path, start, end in ranges for n in range(start, end + 1)}
        result[finding] = needed <= locations
    return result


def json_digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return digest(raw.encode("utf-8"))


def prepare_pair(automatic, reviewed):
    from live_business_eval import SYNTHESIS_PROMPT_VERSION, synthesis_messages

    branches = {}
    shared_inputs = []
    for name, pages in (("automatic", automatic), ("reviewed", reviewed)):
        # Equalize file order and omit navigation metadata on both branches.
        sources = [{"path": p["path"], "source_sha256": p["sha256"],
                    "start_line": p["start"], "end_line": p["end"], "excerpt": p["text"],
                    "excerpt_sha256": digest(p["text"].encode("utf-8"))}
                   for p in sorted(pages, key=lambda p: (p["path"], p["start"]))]
        messages = synthesis_messages(QUESTION, [], {"sources": sources, "framework": []})
        payload = json.loads(messages[-1]["content"])
        require(payload.pop("source_evidence") == sources, "source preparation mismatch")
        shared_inputs.append([messages[:-1], payload])
        branches[name] = {
            "ranges": [[p["path"], p["start_line"], p["end_line"]] for p in sources],
            "messages_sha256": json_digest(messages),
        }
    require(shared_inputs[0] == shared_inputs[1], "branches differ beyond supplied source")
    system_messages, payload = shared_inputs[0]
    require(len(system_messages) == 1 and system_messages[0]["role"] == "system"
            and payload == {"question": QUESTION, "framework_evidence": []}, "unexpected inputs")
    return {"status": "prepared_not_sent", "synthesis_prompt_version": SYNTHESIS_PROMPT_VERSION,
            "shared_system_prompt_sha256": digest(system_messages[0]["content"].encode("utf-8")),
            "shared_question_sha256": digest(QUESTION.encode("utf-8")), "history": [],
            "source_order": "path_then_start_line", "branches": branches}


def run_version(directory, commit):
    sys.path.insert(0, str(directory / "poc/tests"))
    import test_online_automatic_source_search as probe

    case = probe.AutomaticSourceSearchTests()
    try:
        case.setUp()
        run = case.case
        require(case.question == QUESTION, "different historical question")
        for name, expected in SOURCE_HASHES.items():
            require(digest(run.source_bytes[name]) == expected, "source changed: " + name)

        def respond(context):
            if context["source_context"]:
                return case.grounded(context)
            return probe.missing(context)

        # The identical stand-in supplies no search terms, reads or tool calls.
        # It returns the original fixture's fixed answer once evidence arrives.
        result = run.run_question([respond] * 6, question=case.question)
        requests = []
        for index, body in enumerate(run.requests, 1):
            context = json.loads(body["messages"][-1]["content"])
            require(context["question"] == QUESTION, "different outgoing question")
            pages = []
            for item in context["source_context"]:
                cite = item["citation"]
                observed = page(run.source_bytes, cite["path"], cite["start"], cite["end"])
                require(observed["text"] == item["text"] and observed["sha256"] == cite["hash"],
                        "outgoing evidence differs from source")
                require(cite["commit"] == "b" * 40, "fixture commit mismatch")
                pages.append({**observed, "evidence_id": item["id"]})
            requests.append({"round": index, "source_context": pages, "covered": coverage(pages)})
        require(len(requests) == result["model_calls"] and not requests[0]["source_context"],
                "missing first request or unexpected initial evidence")
        observed_paths = [p["path"] for p in requests[-1]["source_context"]]
        if commit == BASE:
            require(result["status"] == "failed" and result["error"] == "ONLINE_MODEL_PROTOCOL_FAILED"
                    and len(requests) == 3 and not run.source_calls
                    and all(not r["source_context"] for r in requests), "baseline result changed")
        else:
            require(result["status"] == "answered" and len(requests) == 2
                    and sorted(observed_paths) == sorted(SOURCE_HASHES)
                    and sorted(run.source_calls) == sorted(SOURCE_HASHES), "candidate result changed")
        manifest = run.query.reader.manifest
        reviewed = [page(run.source_bytes, *span) for span in REVIEWED]
        recorded = {
            "tool_commit": commit, "material_revision": manifest["revision"],
            "status": result["status"], "error": result["error"], "delivered_answer": result["answer"],
            "completed_actions": [o["action"]["tool"] for o in result["checkpoint"]["observations"]],
            "source_reads": run.source_calls, "outgoing_requests": requests,
            "reviewed": {"source_context": reviewed, "covered": coverage(reviewed)},
        }
        if commit == CANDIDATE:
            recorded["synthesis_pair"] = prepare_pair(requests[-1]["source_context"], reviewed)
        return recorded
    finally:
        case.doCleanups()


def replay():
    results = []
    for commit in (BASE, CANDIDATE):
        with tempfile.TemporaryDirectory() as directory:
            load_version(commit, Path(directory))
            output = subprocess.check_output([sys.executable, "-I", "-B", str(Path(__file__).resolve()),
                                              "--worker", directory, "--commit", commit])
            results.append(json.loads(output))
    require(results[0]["reviewed"] == results[1]["reviewed"], "comparison material differs by version")
    reviewed = results[0].pop("reviewed")
    results[1].pop("reviewed")
    return {
        "question": QUESTION, "fixture_commit": CANDIDATE, "fixture_file": FIXTURE,
        "fixture_sha256": FIXTURE_HASH, "source_snapshot": "fixture-declared " + "b" * 40,
        "selection": "自动组从零原文启动。内存接口在未见原文时返回原测试的无引用答复，见到原文后使用同一固定回复。"
                     "人工对照组独立从应收扣减语句确定下列行段，仅比较原文覆盖。",
        "criteria": {key: [list(span) for span in spans] for key, spans in CHECKS.items()},
        "automatic_runs": results, "reviewed_evidence": reviewed,
        "limits": "覆盖表示实际请求中有足以查验该条件的原文行。每个程序只用中性合成夹具，两个版本均使用同一内存接口。"
                  "未调用真实模型、未执行 COBOL、未向人工对照组生成模型答复。"
                  "固定答复无法衡量真实模型水平、公司扣费顺序、实际记账或最终应用质量。",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--commit", choices=(BASE, CANDIDATE), help=argparse.SUPPRESS)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(run_version(args.worker, args.commit), ensure_ascii=False))
    else:
        report = replay()
        if args.verify:
            expected = Path(__file__).with_name("reserve-evidence.json")
            require(report == json.loads(expected.read_text(encoding="utf-8")), "record differs from replay")
            print("Verified both revisions and all four source-coverage checks.")
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2))
