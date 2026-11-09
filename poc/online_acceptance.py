"""Verify a deployed material revision inside its approved network environment."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

from online_material_format import MaterialError, valid_commit, valid_hash
from online_material_query import MaterialQuery


def verify(reader, source_reader, path, start_line, end_line, *, question=None,
           config=None, allow_source_transfer=False, client=None):
    """Verify real inputs; output identities and metrics, never source excerpts."""
    started = time.monotonic()
    query = MaterialQuery(reader)
    source_stats = {"requests": 0, "bytes": 0}

    def read(path, *, max_bytes):
        data = source_reader(path, max_bytes=max_bytes)
        source_stats["requests"] += 1
        source_stats["bytes"] += len(data)
        return data

    evidence = query.evidence(path, start_line, end_line, read)
    result = {"schema_version": "online-deployment-check/v1", "status": "source_verified",
              "repository": reader.manifest["repository"], "revision": reader.manifest["revision"],
              "coverage": reader.manifest["coverage"], "citation": evidence["citation"],
              "checks": {"material_identity": True, "sample_source_hash": True,
                         "sample_source_lines": True, "business_answer_quality": "not_assessed"},
              "model": {"called": False}, "source": source_stats}
    if question is not None:
        if not allow_source_transfer:
            raise MaterialError("ONLINE_SOURCE_TRANSFER_REQUIRED")
        from online_investigation import run_investigation
        answer = run_investigation(query, read, question, config=config, client=client,
                                    allow_source_transfer=True)
        result["model"] = {"called": answer["model_calls"] > 0, "status": answer["status"],
                           "calls": answer["model_calls"], "citation_count": len(answer["citations"]),
                           "error": answer.get("error")}
        result["status"] = "mechanism_verified" if answer["status"] == "answered" else "incomplete"
    result["material"] = {"logical_bytes": reader.fetched_bytes,
                           "network_bytes": getattr(reader, "network_fetched_bytes", reader.fetched_bytes),
                           "objects": reader.fetched_objects}
    result["seconds"] = round(time.monotonic() - started, 6)
    return result


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    location = root.add_mutually_exclusive_group(required=True)
    location.add_argument("--material-url")
    location.add_argument("--release-name")
    root.add_argument("--material-repository", help="Downloads workspace/repository")
    root.add_argument("--repository", required=True, help="Expected source workspace/repository")
    root.add_argument("--commit", required=True)
    root.add_argument("--revision", required=True)
    root.add_argument("--path", required=True, help="Indexed source path relative to the selected scope")
    root.add_argument("--start-line", type=int, default=1)
    root.add_argument("--end-line", type=int, default=1)
    root.add_argument("--question", help="Optional model check; explicit source-transfer authorization is required")
    root.add_argument("--allow-source-transfer", action="store_true")
    root.add_argument("--output", required=True, help="New local report file; existing files are never overwritten")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    output = Path(args.output)
    if output.exists() or output.is_symlink():
        print(json.dumps({"error": "ONLINE_REPORT_EXISTS"}))
        return 2
    try:
        valid_commit(args.commit)
        valid_hash(args.revision)
        if args.question and not args.allow_source_transfer:
            raise MaterialError("ONLINE_SOURCE_TRANSFER_REQUIRED")
        from online_material_reader import HTTPSObjectStore, MaterialReader
        from online_source_client import PinnedSourceClient
        material_auth = {"token": os.environ.get("ONLINE_MATERIAL_TOKEN"),
                         "username": os.environ.get("ONLINE_MATERIAL_USERNAME")}
        if args.material_url:
            store = HTTPSObjectStore(args.material_url, **material_auth)
        else:
            from online_distribution import DownloadsClient, DownloadsObjectStore, load_release
            parts = (args.material_repository or "").split("/")
            if len(parts) != 2:
                raise MaterialError("ONLINE_INVALID_REPOSITORY")
            distribution = DownloadsClient(*parts, **material_auth)
            release = load_release(distribution, args.release_name)
            if release["repository"]["identity"] != args.repository or release["repository"]["commit"] != args.commit or release["revision"] != args.revision:
                raise MaterialError("ONLINE_RELEASE_MISMATCH")
            store = DownloadsObjectStore(distribution, args.revision, release=release)
        reader = MaterialReader(store, expected_repository=args.repository, expected_commit=args.commit,
                                expected_revision=args.revision)
        parts = args.repository.split("/")
        if len(parts) != 2:
            raise MaterialError("ONLINE_INVALID_REPOSITORY")
        source = PinnedSourceClient(*parts, args.commit, token=os.environ.get("ONLINE_SOURCE_TOKEN"),
                                    username=os.environ.get("ONLINE_SOURCE_USERNAME"))
        prefix = reader.manifest["repository"]["subdirectory"]
        def read(path, *, max_bytes):
            return source.read_file((prefix + "/" if prefix else "") + path, max_bytes=max_bytes)
        config = None
        if args.question:
            from model_profiles import model_config
            config = model_config("workbench")
        result = verify(reader, read, args.path, args.start_line, args.end_line,
                        question=args.question, config=config, allow_source_transfer=args.allow_source_transfer)
        code = 0 if result["status"] != "incomplete" else 3
    except MaterialError as error:
        result, code = {"status": "failed", "error": error.code}, 2
    except (Exception, KeyboardInterrupt):
        result, code = {"status": "failed", "error": "ONLINE_CHECK_FAILED"}, 2
    try:
        with output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except OSError:
        print(json.dumps({"error": "ONLINE_REPORT_WRITE_FAILED"}))
        return 2
    print(json.dumps({"status": result["status"], "error": result.get("error")}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
