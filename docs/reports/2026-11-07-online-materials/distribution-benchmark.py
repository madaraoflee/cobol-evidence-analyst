#!/usr/bin/env python3
"""Measure inline postings and bundled distribution using simulated HTTPS only."""

from __future__ import annotations

import argparse
from email.parser import BytesParser
from email.policy import default
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "poc"))
from online_analysis import build
from online_distribution import DownloadsClient, DownloadsObjectStore, SYNC_SCHEMA, load_release, publish_material
from online_material_format import canonical_json, digest
from online_material_query import MaterialQuery
from online_material_reader import MaterialReader


class Response(io.BytesIO):
    def __init__(self, body, status, url, headers=None):
        super().__init__(body)
        self.status, self.url = status, url
        self.headers = {"Content-Length": str(len(body)), **(headers or {})}

    def geturl(self):
        return self.url


class SimulatedDownloads:
    def __init__(self):
        self.artifacts, self.requests = {}, []

    def open(self, request, timeout):
        self.requests.append({"method": request.method, "host": urlsplit(request.full_url).hostname})
        path = urlsplit(request.full_url).path
        name = unquote(path.rsplit("/", 1)[-1])
        if request.method == "POST":
            content_type = request.get_header("Content-type")
            message = BytesParser(policy=default).parsebytes(("Content-Type: " + content_type + "\r\n\r\n").encode() + request.data)
            for part in message.iter_parts():
                self.artifacts[part.get_filename()] = part.get_payload(decode=True)
            return Response(b"{}", 201, request.full_url)
        if name not in self.artifacts:
            return Response(b"", 404, request.full_url)
        if urlsplit(request.full_url).hostname == "api.bitbucket.org":
            return Response(b"", 302, request.full_url,
                            {"Location": "https://bbuseruploads.s3.amazonaws.com/" + name})
        assert request.get_header("Authorization") is None
        return Response(self.artifacts[name], 200, request.full_url)


def run(output, programs, copybooks):
    spec = importlib.util.spec_from_file_location("material_benchmark_fixture", Path(__file__).with_name("benchmark.py"))
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    with tempfile.TemporaryDirectory(prefix="distribution-benchmark-") as temporary:
        root = Path(temporary)
        source, exported = root / "source", root / "materials"
        source_stats = fixture.generate(source, programs, copybooks)
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=Synthetic User", "-c", "user.email=synthetic@example.invalid",
                        "-c", "commit.gpgsign=false", "commit", "-qm", "synthetic fixture"], check=True)
        commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"]).decode().strip()
        started = time.perf_counter()
        manifest = build(argparse.Namespace(source=str(source), output=str(exported), repository="sample/source",
            commit=commit, subdirectory="", encoding="auto", source_format="free", include_extensionless=False,
            unverified_source=False))["manifest"]
        build_seconds = time.perf_counter() - started
        objects = list((exported / "objects").iterdir())
        kinds = {}
        for path in objects:
            kind = json.loads(path.read_bytes())["kind"]
            kinds[kind] = kinds.get(kind, 0) + 1
        receipt = {"schema_version": SYNC_SCHEMA, "repository": "sample/source", "commit": commit,
                   "subdirectory": "", "complete": True, "completed_at": "2026-11-07T00:00:00Z", "sync_id": "synthetic-001"}
        transport = SimulatedDownloads()
        publisher = DownloadsClient("sample", "materials", token="synthetic-publisher", opener=transport)
        started = time.perf_counter()
        publication = publish_material(exported, publisher, receipt)
        publish_seconds = time.perf_counter() - started
        publish_requests = len(transport.requests)
        artifact_bytes = sum(len(data) for data in transport.artifacts.values())
        bundle_sizes = [len(data) for name, data in transport.artifacts.items() if name.startswith("analysis-bundle-")]
        shutil.rmtree(source)
        shutil.rmtree(exported)
        client = DownloadsClient("sample", "materials", token="synthetic-reader", opener=transport)
        release = load_release(client, publication["release_name"])
        reader = MaterialReader(DownloadsObjectStore(client, release["revision"], release=release),
                                expected_repository="sample/source", expected_commit=commit,
                                expected_revision=manifest["revision"])
        result = MaterialQuery(reader).search("ENTRY-0199", limit=3)
        assert "entry-0199.cbl" in [row["relative_path"] for row in result["matches"]]
        cold = {"logical_material_bytes": reader.fetched_bytes, "actual_protocol_body_bytes": client.network_fetched_bytes,
                "protocol_requests_including_redirects": client.network_requests,
                "total_matches": result["total_matches"], "truncated": result["truncated"],
                "returned_paths": [row["relative_path"] for row in result["matches"]]}
        before = client.network_fetched_bytes
        MaterialQuery(reader).search("ENTRY-0199", limit=3)
        warm_bytes = client.network_fetched_bytes - before
        result = {"schema_version": "online-distribution-benchmark/v1", "source": source_stats,
                  "network_calls": 0, "model_calls": 0, "transport": "in_process_protocol_simulation",
                  "source_and_material_directory_removed_before_query": True,
                  "build_and_export_seconds": round(build_seconds, 6),
                  "publication_seconds_simulated": round(publish_seconds, 6),
                  "logical_objects": len(objects), "object_kinds": kinds,
                  "published_bundles": publication["bundles"], "published_artifacts": len(transport.artifacts),
                  "publication_protocol_requests": publish_requests, "published_bytes": artifact_bytes,
                  "largest_bundle_bytes": max(bundle_sizes), "cold_query": cold,
                  "warm_query_additional_bytes": warm_bytes,
                  "script_sha256": digest(Path(__file__).read_bytes()),
                  "limitations": ["No real HTTP latency, authentication, rate limits or cloud quota were measured.",
                                  "Synthetic source contains repeated comments and is not a GB-scale performance result.",
                                  "The synchronization receipt is a synthetic attestation for this temporary Git checkout.",
                                  "Logical object bytes and actual bundled body bytes intentionally differ."]}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(json.dumps(result, ensure_ascii=False, indent=2).encode() + b"\n")
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--programs", type=int, default=2000)
    parser.add_argument("--copybooks", type=int, default=200)
    arguments = parser.parse_args()
    if not 200 <= arguments.programs <= 10000 or not 1 <= arguments.copybooks <= arguments.programs:
        parser.error("Require 200..10000 programs and 1..programs copybooks.")
    run(arguments.output, arguments.programs, arguments.copybooks)
