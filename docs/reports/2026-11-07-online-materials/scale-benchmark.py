#!/usr/bin/env python3
"""Reproduce a physical GiB-scale synthetic run without real HTTP or models.

Source, Git objects, SQLite and simulated Downloads live in one temporary
directory. Only the measurement JSON remains. The comment-padding ratio is
reported explicitly; source bytes are not a proxy for semantic complexity.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default
import hashlib
import io
import json
from pathlib import Path
import platform
import resource
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "poc"))
from business_index import build_business_index
from online_analysis import _verify_checkout
from online_distribution import DownloadsClient, DownloadsObjectStore, SYNC_SCHEMA, load_release, publish_material
from online_material_export import export_material
from online_material_format import MaterialError, digest
from online_material_query import MaterialQuery
from online_material_reader import MaterialReader


GIB = 1024 ** 3
COMMENT = b"*> Synthetic scale padding for account balance processing status and reference review.\n"


def emit(phase, **values):
    print(json.dumps({"phase": phase, **values}, sort_keys=True), flush=True)


def padded(body, target):
    remaining = target - len(body)
    if remaining < len(COMMENT):
        raise ValueError("BENCHMARK_FILE_TARGET_TOO_SMALL")
    whole, tail = divmod(remaining, len(COMMENT))
    if 0 < tail < 3:
        whole -= 1
        tail += len(COMMENT)
    padding = COMMENT * whole + ((b"*>" + b"p" * (tail - 3) + b"\n") if tail else b"")
    assert len(body) + len(padding) == target
    return body + padding, len(padding)


def program(number, programs, copybooks):
    lines = ["IDENTIFICATION DIVISION.", f"PROGRAM-ID. ENTRY-{number:05}.",
             "DATA DIVISION.", "WORKING-STORAGE SECTION.", f"COPY RECORD-{number % copybooks:04}.",
             "01 RESULT-AMOUNT PIC 9(9)V99.", "01 REVIEW-STATUS PIC X(8).",
             "PROCEDURE DIVISION.", "MAIN-PARA."]
    for rule in range(8):
        lines.extend([f"IF INPUT-AMOUNT > {rule * 100 + number % 97}",
                      f"COMPUTE RESULT-AMOUNT = INPUT-AMOUNT * {100 + rule} / 100",
                      "ELSE", "MOVE ZERO TO RESULT-AMOUNT", "END-IF."])
    lines.extend([f"CALL 'ENTRY-{(number + 1) % programs:05}' USING INPUT-AMOUNT RESULT-AMOUNT.",
                  "PERFORM FORMAT-RESULT.", "GOBACK.", "FORMAT-RESULT.",
                  'IF RESULT-AMOUNT > ZERO MOVE "READY" TO REVIEW-STATUS',
                  'ELSE MOVE "REVIEW" TO REVIEW-STATUS END-IF.',
                  f"*> Unique lookup ledgeranchor{number:05}."])
    return ("\n".join(lines) + "\n").encode()


def generate(source, programs, copybooks, program_bytes, copybook_bytes):
    source.mkdir()
    total, padding, body_bytes = 0, 0, 0
    fingerprint = hashlib.sha256()
    for number in range(programs + copybooks):
        if number < programs:
            name = f"entry-{number:05}.cbl"
            body = program(number, programs, copybooks)
            data, fill = padded(body, program_bytes)
        else:
            copy = number - programs
            name = f"RECORD-{copy:04}.cpy"
            body = ("01 INPUT-AMOUNT PIC 9(9)V99.\n" + "".join(
                f"01 SHARED-FIELD-{copy:04}-{field:03} PIC X(24).\n" for field in range(64))).encode()
            data, fill = padded(body, copybook_bytes)
        (source / name).write_bytes(data)
        fingerprint.update(name.encode() + b"\0" + hashlib.sha256(data).digest() + b"\n")
        total += len(data)
        padding += fill
        body_bytes += len(body)
        if (number + 1) % 500 == 0 or number + 1 == programs + copybooks:
            emit("generate", files=number + 1, total_files=programs + copybooks, bytes=total)
    return {"programs": programs, "copybooks": copybooks, "files": programs + copybooks,
            "bytes": total, "gib": round(total / GIB, 6), "program_bytes_each": program_bytes,
            "copybook_bytes_each": copybook_bytes, "body_bytes": body_bytes,
            "comment_padding_bytes": padding, "comment_padding_fraction": round(padding / total, 8),
            "source_fingerprint": fingerprint.hexdigest(),
            "shape": "Each program has a static CALL ring, shared COPY, eight IF/COMPUTE rules and a result paragraph; repeated comments fill target bytes."}


class Response(io.BytesIO):
    def __init__(self, body, status, url, headers=None):
        super().__init__(body)
        self.status, self.url = status, url
        self.headers = {"Content-Length": str(len(body)), **(headers or {})}

    def geturl(self):
        return self.url


class DiskDownloads:
    """Simulate API/redirect protocol, retaining one artifact body at a time."""
    def __init__(self, root):
        root.mkdir()
        self.root, self.requests = root, 0
        self.api_requests, self.cdn_requests, self.upload_requests = 0, 0, 0

    def open(self, request, timeout):
        self.requests += 1
        parsed = urlsplit(request.full_url)
        is_api = parsed.hostname == "api.bitbucket.org"
        self.api_requests += is_api
        self.cdn_requests += not is_api
        name = unquote(parsed.path.rsplit("/", 1)[-1])
        if request.method == "POST":
            self.upload_requests += 1
            header = ("Content-Type: " + request.get_header("Content-type") + "\r\n\r\n").encode()
            message = BytesParser(policy=default).parsebytes(header + request.data)
            for part in message.iter_parts():
                name = part.get_filename()
                assert "/" not in name and "\\" not in name
                (self.root / name).write_bytes(part.get_payload(decode=True))
            return Response(b"{}", 201, request.full_url)
        path = self.root / name
        if not path.is_file():
            return Response(b"", 404, request.full_url)
        if is_api:
            return Response(b"", 302, request.full_url,
                            {"Location": "https://bbuseruploads.s3.amazonaws.com/" + name})
        assert request.get_header("Authorization") is None
        return Response(path.read_bytes(), 200, request.full_url)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--programs", type=int, default=10000)
    parser.add_argument("--copybooks", type=int, default=1000)
    parser.add_argument("--program-kib", type=int, default=110)
    parser.add_argument("--copybook-kib", type=int, default=8)
    parser.add_argument("--temp-directory", type=Path, default=Path(tempfile.gettempdir()))
    arguments = parser.parse_args()
    if not (200 <= arguments.programs <= 20000 and 1 <= arguments.copybooks <= arguments.programs
            and 4 <= arguments.program_kib <= 1024 and 4 <= arguments.copybook_kib <= 1024):
        parser.error("Invalid bounded synthetic scale parameters.")
    expected_source = (arguments.programs * arguments.program_kib + arguments.copybooks * arguments.copybook_kib) * 1024
    required_free = max(4 * GIB, expected_source * 5 + GIB)
    initial_free = shutil.disk_usage(arguments.temp_directory).free
    result = {"schema_version": "online-scale-benchmark/v1", "started_at": datetime.now(timezone.utc).isoformat(),
              "network_calls": 0, "model_calls": 0, "business_programs_executed": False,
              "transport": "disk_backed_in_process_protocol_simulation", "status": "running", "failed_limits": [],
              "disk": {"initial_free_bytes": initial_free, "required_free_bytes": required_free},
              "environment": {"python": platform.python_version(), "platform": sys.platform,
                              "machine": platform.machine(), "sqlite": sqlite3.sqlite_version},
              "script_sha256": digest(Path(__file__).read_bytes()),
              "module_sha256": {path.name: digest(path.read_bytes()) for path in sorted((ROOT / "poc").glob("online_*.py"))},
              "limitations": ["Comment padding is reported and does not reproduce production semantic density.",
                              "Protocol requests are simulated; no real cloud latency, quota, network or enterprise authentication is measured.",
                              "This run tests physical GiB and file count, not business answer accuracy or production runtime behavior.",
                              "Machine load is not controlled; elapsed times are observations, not service guarantees."]}
    phase, total_start = "disk_check", time.perf_counter()
    output = arguments.output
    output.parent.mkdir(parents=True, exist_ok=True)
    last_ping = [0.0]
    artifact_root = [None]

    def heartbeat():
        now = time.perf_counter()
        if now - last_ping[0] >= 30:
            free = shutil.disk_usage(arguments.temp_directory).free
            if free < 512 * 1024 * 1024:
                raise MaterialError("BENCHMARK_DISK_RESERVE_EXHAUSTED")
            fields = {"elapsed_seconds": round(now - total_start, 1), "free_bytes": free}
            if artifact_root[0] is not None and artifact_root[0].is_dir():
                fields["created_object_files"] = sum(1 for _ in artifact_root[0].iterdir())
            emit(phase, **fields)
            last_ping[0] = now

    try:
        if initial_free < required_free:
            raise MaterialError("BENCHMARK_INSUFFICIENT_DISK")
        emit(phase, free_bytes=initial_free, required_bytes=required_free, source_bytes=expected_source)
        with tempfile.TemporaryDirectory(prefix="online-scale-", dir=arguments.temp_directory) as temporary:
            root = Path(temporary)
            source, database, exported = root / "source", root / "index.sqlite", root / "material"
            phase, started = "generate", time.perf_counter()
            result["source"] = generate(source, arguments.programs, arguments.copybooks,
                                         arguments.program_kib * 1024, arguments.copybook_kib * 1024)
            result["generation_seconds"] = round(time.perf_counter() - started, 6)
            phase, started = "git_snapshot", time.perf_counter()
            emit(phase, status="starting")
            subprocess.run(["git", "init", "-q", str(source)], check=True, timeout=60)
            subprocess.run(["git", "-C", str(source), "add", "."], check=True, timeout=600)
            subprocess.run(["git", "-C", str(source), "-c", "user.name=Synthetic User", "-c", "user.email=synthetic@example.invalid",
                            "-c", "commit.gpgsign=false", "commit", "-qm", "synthetic scale fixture"], check=True, timeout=300)
            commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"]).decode().strip()
            _verify_checkout(source, commit, "")
            result["git_snapshot_seconds"] = round(time.perf_counter() - started, 6)
            phase, started = "build", time.perf_counter()
            emit(phase, status="starting")
            last_report = {}
            def progress(item):
                stage, completed = item.get("phase"), item.get("completed")
                if type(completed) is int and completed // 500 > last_report.get(stage, -1):
                    last_report[stage] = completed // 500
                    emit(phase, stage=stage, completed=completed, total=item.get("total"))
                heartbeat()
            report = build_business_index(source, database, source_format="free", verify_content=True,
                framework_reference_path="", quiet=True, progress=progress, check_cancel=heartbeat)
            _verify_checkout(source, commit, "", report["relative_paths"])
            result["build"] = {"seconds": round(time.perf_counter() - started, 6),
                "database_bytes": sum(path.stat().st_size for path in root.glob("index.sqlite*")),
                "database_counts": report["database_counts"],
                "coverage": {key: value for key, value in report["scope"].items() if key != "dependency_scan"},
                "input_skips": report["input_skips"]}
            phase, started = "export", time.perf_counter()
            artifact_root[0] = exported / "objects"
            emit(phase, status="starting")
            manifest = export_material(database, source, exported, repository_id="sample/source", commit=commit,
                build_report=report, commit_verification="git_checked", check_cancel=heartbeat)
            export_seconds = time.perf_counter() - started
            kinds, objects, object_bytes = {}, 0, 0
            for path in (exported / "objects").iterdir():
                raw = path.read_bytes()
                kind = json.loads(raw)["kind"]
                entry = kinds.setdefault(kind, {"objects": 0, "bytes": 0})
                entry["objects"] += 1
                entry["bytes"] += len(raw)
                objects += 1
                object_bytes += len(raw)
            result["export"] = {"seconds": round(export_seconds, 6), "objects": objects,
                "object_bytes": object_bytes, "manifest_bytes": (exported / "material.json").stat().st_size,
                "object_kinds": kinds, "revision": manifest["revision"]}
            phase, started = "publish_simulated", time.perf_counter()
            artifact_root[0] = root / "downloads"
            transport = DiskDownloads(artifact_root[0])
            publisher = DownloadsClient("sample", "materials", token="synthetic-publisher", opener=transport, check_cancel=heartbeat)
            receipt = {"schema_version": SYNC_SCHEMA, "repository": "sample/source", "commit": commit,
                       "subdirectory": "", "complete": True, "completed_at": "2026-11-07T00:00:00Z", "sync_id": "synthetic-scale-001"}
            publication = publish_material(exported, publisher, receipt, check_cancel=heartbeat,
                progress=lambda item: emit(phase, **{key: value for key, value in item.items() if key != "phase"}))
            sizes = [path.stat().st_size for path in transport.root.iterdir()]
            bundle_sizes = [path.stat().st_size for path in transport.root.glob("analysis-bundle-*.json")]
            result["publication"] = {"seconds_simulated": round(time.perf_counter() - started, 6),
                "bundles": publication["bundles"], "artifacts": len(sizes), "artifact_bytes": sum(sizes),
                "largest_bundle_bytes": max(bundle_sizes), "protocol_requests": transport.requests,
                "api_requests": transport.api_requests, "cdn_requests": transport.cdn_requests,
                "upload_requests": transport.upload_requests}
            target = "entry-00199.cbl"
            evidence_bytes = (source / target).read_bytes()
            shutil.rmtree(source)
            shutil.rmtree(exported)
            for path in root.glob("index.sqlite*"):
                path.unlink()
            result["producer_source_database_and_material_removed_before_query"] = True
            phase, started = "cold_query", time.perf_counter()
            client = DownloadsClient("sample", "materials", token="synthetic-reader", opener=transport)
            release = load_release(client, publication["release_name"])
            store = DownloadsObjectStore(client, release["revision"], release=release)
            reader = MaterialReader(store, expected_repository="sample/source", expected_commit=commit, expected_revision=manifest["revision"])
            query = MaterialQuery(reader)
            matches = query.search("ledgeranchor00199", limit=10)
            paths = [row["relative_path"] for row in matches["matches"]]
            assert paths == [target]
            result["cold_query"] = {"seconds_simulated": round(time.perf_counter() - started, 6),
                "query": "ledgeranchor00199", "returned_paths": paths, "total_matches": matches["total_matches"],
                "logical_material_bytes": reader.fetched_bytes, "actual_protocol_body_bytes": client.network_fetched_bytes,
                "protocol_requests": client.network_requests}
            before = client.network_fetched_bytes
            assert query.search("ledgeranchor00199", limit=10) == matches
            result["warm_query_additional_bytes"] = client.network_fetched_bytes - before
            incoming = query.incoming("ENTRY-00199")
            assert [item["relative_path"] for item in incoming["matches"]] == ["entry-00198.cbl"]
            copy_facts = query.relations(target, "INCLUDES_COPY")
            assert copy_facts["relations"][0]["status"] == "confirmed"
            reads = []
            def source_reader(path, *, max_bytes):
                assert path == target and len(evidence_bytes) <= max_bytes
                reads.append(path)
                return evidence_bytes
            evidence = query.evidence(target, 1, 5, source_reader)
            assert evidence["citation"]["hash"] == digest(evidence_bytes)
            assert evidence["citation"]["commit"] == commit
            result["relationship_and_evidence_checks"] = {"incoming_call_path": "entry-00198.cbl", "copy_status": "confirmed",
                "source_reads": len(reads), "source_bytes": len(evidence_bytes), "citation_verified": True,
                "cumulative_protocol_body_bytes": client.network_fetched_bytes}
            result["status"] = "passed"
    except (Exception, KeyboardInterrupt) as error:
        code = "BENCHMARK_CANCELLED" if isinstance(error, KeyboardInterrupt) else getattr(error, "code", None)
        if code is None:
            code = type(error).__name__
        result["status"], result["failed_phase"] = "failed", phase
        result["failed_limits"].append(code)
        emit("failed", stage=phase, code=code)
    finally:
        result["total_seconds"] = round(time.perf_counter() - total_start, 6)
        result["peak_process_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
        result["disk"]["final_free_bytes"] = shutil.disk_usage(arguments.temp_directory).free
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        emit("complete", status=result["status"], seconds=result["total_seconds"], peak_rss_bytes=result["peak_process_rss_bytes"])
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
