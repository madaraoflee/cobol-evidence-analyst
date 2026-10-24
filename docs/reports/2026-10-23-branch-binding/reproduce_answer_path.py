"""Exercise the production answer path with a fixed wrong reply, offline only."""
import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import time
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "poc"))
from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search

SOURCE = """IDENTIFICATION DIVISION.
PROGRAM-ID. BRANCH-RULE.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 IN-AMOUNT PIC S99.
01 OUT-AMOUNT PIC S999.
PROCEDURE DIVISION.
MAIN.
IF IN-AMOUNT > ZERO
COMPUTE OUT-AMOUNT = IN-AMOUNT * 2
ELSE
COMPUTE OUT-AMOUNT = IN-AMOUNT * 3
END-IF.
GOBACK.
"""
question = "BRANCH-RULE 的 OUT-AMOUNT 如何计算？"
requests = []
def transport(request):
    wire = json.loads(request.body)
    payload = json.loads(wire["messages"][-1]["content"])
    pages = [page for bundle in payload["source_context"] for page in bundle.get("pages", [])]
    page = next(page for page in pages if "COMPUTE OUT-AMOUNT = IN-AMOUNT * 3" in page["source_text"])
    answer = ("在 IN-AMOUNT > ZERO 的条件下，OUT-AMOUNT = IN-AMOUNT * 3；"
              "否则 OUT-AMOUNT = IN-AMOUNT * 2。[" + page["evidence_id"] + "]")
    requests.append({"source_pages": pages, "fixed_response": answer,
                     "request_bytes": len(request.body)})
    return TransportResponse(200, json.dumps({"choices": [{"message": {"content": answer},
                                                          "finish_reason": "stop"}]}))
with tempfile.TemporaryDirectory() as directory, patch.object(socket, "create_connection", side_effect=AssertionError("Offline only")), patch.object(socket.socket, "connect", side_effect=AssertionError("Offline only")):
    root = Path(directory)
    source = root / "source"
    source.mkdir()
    (source / "rule.cbl").write_text(SOURCE)
    database = root / "index.sqlite"
    build_business_index(source, database, source_format="free", verify_content=True, framework_reference_path="")
    ensure_repository_search(database, source)
    started = time.perf_counter()
    output = run_business_chat(question, database, source,
        CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model", api_key="offline-only"),
        transport=transport, allow_network=False, framework_reference_path="",
        policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0))
    elapsed = time.perf_counter() - started
    result = output["agent_result"]
    print(json.dumps({"question": question, "synthetic_only": True, "external_model_requests": 0,
        "paid_cost": 0, "scripted_transport_calls": len(requests), "local_elapsed_seconds": elapsed,
        "timing_scope": "local source mechanism plus scripted transport; not model latency",
        "source_sha256": hashlib.sha256(SOURCE.encode()).hexdigest(),
        "requests": requests, "answer": result["answer"], "status": result["status"],
        "stop_reason": result.get("stop_reason"), "evidence_refs": result.get("evidence_refs"),
        "claim_ledger": result.get("claim_ledger"), "claims_semantically_verified": result.get("claims_semantically_verified")},
        ensure_ascii=False, indent=2))
