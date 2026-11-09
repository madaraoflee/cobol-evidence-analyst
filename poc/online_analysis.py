"""Standalone material build and query entry point; no workbench state is used."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from online_material_format import MaterialError, relative_path, valid_commit


def _git(source, *arguments):
    try:
        result = subprocess.run(["git", "-C", str(source), "--literal-pathspecs", *arguments],
                                capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise MaterialError("MATERIAL_CHECKOUT_UNAVAILABLE") from None
    if result.returncode:
        raise MaterialError("MATERIAL_CHECKOUT_MISMATCH")
    return result.stdout


def _verify_checkout(source, commit, subdirectory, paths=None):
    valid_commit(commit)
    relative_path(subdirectory, allow_empty=True)
    root = Path(os.fsdecode(_git(source, "rev-parse", "--show-toplevel")).strip()).resolve()
    if (root / subdirectory).resolve() != source.resolve():
        raise MaterialError("MATERIAL_CHECKOUT_SCOPE_MISMATCH")
    if _git(source, "rev-parse", "HEAD").decode("ascii").strip() != commit:
        raise MaterialError("MATERIAL_CHECKOUT_MISMATCH")
    # Diff against HEAD checks staged and unstaged edits in the selected scope.
    _git(root, "diff", "--no-ext-diff", "--no-textconv", "--quiet", "HEAD", "--", subdirectory or ".")
    if paths is not None:
        tracked = {os.fsdecode(item) for item in
                   _git(root, "ls-files", "-z", "--", subdirectory or ".").split(b"\0") if item}
        prefix = subdirectory + "/" if subdirectory else ""
        if any(prefix + relative_path(path) not in tracked for path in paths):
            raise MaterialError("MATERIAL_UNTRACKED_SOURCE")


def build(args):
    from business_index import build_business_index
    from online_material_export import export_material

    source = Path(args.source)
    destination = Path(args.output)
    valid_commit(args.commit)
    relative_path(args.subdirectory, allow_empty=True)
    if destination.exists() or destination.is_symlink():
        raise MaterialError("MATERIAL_DESTINATION_EXISTS")
    if destination.resolve() == source.resolve() or source.resolve() in destination.resolve().parents:
        raise MaterialError("MATERIAL_OUTPUT_INSIDE_SOURCE")
    if not args.unverified_source:
        _verify_checkout(source, args.commit, args.subdirectory)
    with tempfile.TemporaryDirectory(prefix="online-material-") as temporary:
        database = Path(temporary) / "facts.sqlite"
        report = build_business_index(
            source, database, encoding=args.encoding, source_format=args.source_format,
            include_extensionless=args.include_extensionless, verify_content=True,
            framework_reference_path="", quiet=True,
        )
        if not args.unverified_source:
            _verify_checkout(source, args.commit, args.subdirectory, report["relative_paths"])
        manifest = export_material(
            database, source, destination, repository_id=args.repository,
            commit=args.commit, subdirectory=args.subdirectory, build_report=report,
            commit_verification="caller_declared" if args.unverified_source else "git_checked",
        )
    return {"manifest": manifest, "publication": "not_published"}


def query(args):
    from online_material_query import MaterialQuery
    from online_material_reader import DirectoryObjectStore, HTTPSObjectStore, MaterialReader

    if args.material_url:
        store = HTTPSObjectStore(args.material_url, token=os.environ.get("ONLINE_MATERIAL_TOKEN"),
                                 username=os.environ.get("ONLINE_MATERIAL_USERNAME"))
    else:
        store = DirectoryObjectStore(Path(args.material))
    reader = MaterialReader(store, expected_repository=args.expected_repository,
                            expected_commit=args.expected_commit,
                            expected_revision=args.expected_revision)
    service = MaterialQuery(reader)
    source_bytes = 0
    source_requests = 0
    if args.command == "inspect":
        result = {"manifest": reader.manifest}
    elif args.command == "search":
        result = service.search(args.text, limit=args.limit, offset=args.offset)
    elif args.command == "relations":
        result = service.relations(args.path, relation_type=args.relation_type)
    elif args.command == "incoming":
        result = service.incoming(args.target, relation_type=args.relation_type, limit=args.limit, offset=args.offset)
    else:
        from online_source_client import PinnedSourceClient
        repository = reader.manifest["repository"]
        parts = repository["identity"].split("/")
        if len(parts) != 2 or not all(parts):
            raise MaterialError("SOURCE_INVALID_REPOSITORY")
        client = PinnedSourceClient(*parts, repository["commit"],
                                    token=os.environ.get("ONLINE_SOURCE_TOKEN"),
                                    username=os.environ.get("ONLINE_SOURCE_USERNAME"))
        prefix = repository["subdirectory"] + "/" if repository["subdirectory"] else ""

        def read_source(path, *, max_bytes):
            nonlocal source_bytes, source_requests
            data = client.read_file(prefix + relative_path(path), max_bytes=max_bytes)
            source_bytes += len(data)
            source_requests += 1
            return data

        result = service.evidence(args.path, args.start_line, args.end_line, read_source,
                                  max_characters=args.max_characters)
    return {**result, "transfer": {"fetched_bytes": reader.fetched_bytes,
                                   "fetched_objects": reader.fetched_objects,
                                   "source_bytes": source_bytes,
                                   "source_requests": source_requests}}


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    make = commands.add_parser("build", help="Build immutable materials on a source producer")
    make.add_argument("--source", required=True)
    make.add_argument("--output", required=True)
    make.add_argument("--repository", required=True, help="Workspace/repository identity")
    make.add_argument("--commit", required=True, help="Full immutable commit hash")
    make.add_argument("--subdirectory", default="")
    make.add_argument("--encoding", default="auto")
    make.add_argument("--source-format", default="auto", choices=("auto", "fixed", "free"))
    make.add_argument("--include-extensionless", action="store_true")
    make.add_argument("--unverified-source", action="store_true",
                      help="For synthetic validation; does not verify a Git checkout")
    for name in ("inspect", "search", "relations", "incoming", "evidence"):
        command = commands.add_parser(name)
        location = command.add_mutually_exclusive_group(required=True)
        location.add_argument("--material", help="Exported materials only; no source directory needed")
        location.add_argument("--material-url", help="HTTPS directory serving immutable files without redirects")
        command.add_argument("--expected-repository")
        command.add_argument("--expected-commit")
        command.add_argument("--expected-revision")
        if name == "search":
            command.add_argument("text")
            command.add_argument("--limit", type=int, default=20)
            command.add_argument("--offset", type=int, default=0)
        elif name in ("relations", "evidence"):
            command.add_argument("path")
        if name in ("relations", "incoming"):
            command.add_argument("--relation-type", default="CALLS" if name == "incoming" else None)
        if name == "incoming":
            command.add_argument("target")
            command.add_argument("--limit", type=int, default=100)
            command.add_argument("--offset", type=int, default=0)
        if name == "evidence":
            command.add_argument("start_line", type=int)
            command.add_argument("end_line", type=int)
            command.add_argument("--max-characters", type=int, default=12000)
    return root


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        result = build(arguments) if arguments.command == "build" else query(arguments)
    except MaterialError as error:
        print(json.dumps({"error": error.code}), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"error": "MATERIAL_CANCELLED"}), file=sys.stderr)
        return 130
    except (OSError, ValueError):
        # Never echo remote responses, source paths or credential-bearing values.
        print(json.dumps({"error": "MATERIAL_OPERATION_FAILED"}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
