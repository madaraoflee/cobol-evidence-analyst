"""Prepare and publish immutable analysis data inside an approved environment."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from online_distribution import (DownloadsClient, PublicationUnknown, load_release, publish_material,
                                 make_source_snapshot, validate_sync_receipt)
from online_material_format import MaterialError, decode_json


def _receipt(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(64 * 1024 + 1)
    value = decode_json(raw, maximum=64 * 1024)
    if not isinstance(value, dict):
        raise MaterialError("DISTRIBUTION_INVALID_SYNC_RECEIPT")
    repository = {"identity": value.get("repository"), "commit": value.get("commit"),
                  "subdirectory": value.get("subdirectory")}
    return validate_sync_receipt(value, repository)


def _client(arguments, *, write=False):
    prefix = "ONLINE_PUBLISH_" if write else "ONLINE_MATERIAL_"
    token = os.environ.get(prefix + "TOKEN")
    username = os.environ.get(prefix + "USERNAME")
    if write and not token:
        raise MaterialError("DISTRIBUTION_CREDENTIAL_REQUIRED")
    return DownloadsClient(arguments.workspace, arguments.repository, token=token, username=username,
                           download_hosts=arguments.download_host)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Verify a selected clean Git checkout, then build data")
    prepare.add_argument("--source", required=True)
    prepare.add_argument("--output", required=True)
    basis = prepare.add_mutually_exclusive_group(required=True)
    basis.add_argument("--sync-receipt", help="Existing completed synchronization attestation")
    basis.add_argument("--repository-snapshot", action="store_true",
                       help="Analyze a fixed repository commit; upstream synchronization is not verified")
    prepare.add_argument("--source-repository", help="Workspace/repository identity for a repository snapshot")
    prepare.add_argument("--commit", help="Full immutable source commit for a repository snapshot")
    prepare.add_argument("--subdirectory", default="", help="Selected source directory inside the repository")
    prepare.add_argument("--encoding", default="auto")
    prepare.add_argument("--source-format", default="auto", choices=("auto", "fixed", "free"))
    prepare.add_argument("--include-extensionless", action="store_true")
    for name in ("publish", "list", "inspect"):
        command = commands.add_parser(name)
        command.add_argument("--workspace", required=True)
        command.add_argument("--repository", required=True, help="Repository that stores approved analysis data")
        command.add_argument("--download-host", action="append", help="Explicit approved HTTPS redirect host; replaces defaults")
        if name == "publish":
            command.add_argument("--material", required=True)
            basis = command.add_mutually_exclusive_group(required=True)
            basis.add_argument("--sync-receipt")
            basis.add_argument("--repository-snapshot", action="store_true",
                               help="Publish the Git-checked snapshot without claiming synchronization completeness")
        if name == "inspect":
            command.add_argument("release_name")
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            from online_analysis import build
            receipt = None
            if arguments.repository_snapshot:
                if not arguments.source_repository or not arguments.commit:
                    raise MaterialError("DISTRIBUTION_SNAPSHOT_IDENTITY_REQUIRED")
                arguments.repository = arguments.source_repository
            else:
                if arguments.source_repository is not None or arguments.commit is not None or arguments.subdirectory:
                    raise MaterialError("DISTRIBUTION_INVALID_PUBLICATION_SOURCE")
                receipt = _receipt(arguments.sync_receipt)
                arguments.repository = receipt["repository"]
                arguments.commit = receipt["commit"]
                arguments.subdirectory = receipt["subdirectory"]
            arguments.unverified_source = False
            result = build(arguments)
            if arguments.repository_snapshot:
                result["source_snapshot"] = make_source_snapshot(result["manifest"]["repository"])
            else:
                result["sync_receipt"] = receipt
        elif arguments.command == "publish":
            result = publish_material(Path(arguments.material), _client(arguments, write=True),
                                      _receipt(arguments.sync_receipt) if arguments.sync_receipt else None,
                                      repository_snapshot=arguments.repository_snapshot)
        elif arguments.command == "list":
            result = {"release_names": _client(arguments).list_release_names(),
                      "selection": "explicit_revision_required"}
        else:
            result = load_release(_client(arguments), arguments.release_name)
    except PublicationUnknown as error:
        print(json.dumps({"status": "publication_unknown", "release_name": error.release_name,
                          "error": error.code, "cause": error.cause_code}), file=sys.stderr)
        return 3
    except MaterialError as error:
        print(json.dumps({"error": error.code}), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(json.dumps({"error": "DISTRIBUTION_CANCELLED"}), file=sys.stderr)
        return 130
    except (OSError, ValueError):
        print(json.dumps({"error": "DISTRIBUTION_OPERATION_FAILED"}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
