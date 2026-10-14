#!/usr/bin/env python3
"""Promote an existing draft only after exact-package joint acceptance.

The default is a local dry run with no GitHub calls. --publish authorizes the
network mutations; a failed gate never calls GitHub. Raw answers and human
review forms stay local. Repository administrators can still bypass this
entry point manually; it is not a server-side access-control mechanism.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "poc"))
from release_acceptance import evaluate_release, file_sha256  # noqa: E402
from live_business_eval import _safe_write  # noqa: E402


class PublicationError(RuntimeError):
    pass


def _require(condition, code):
    if not condition:
        raise PublicationError(code)


def _gh(arguments, *, missing_ok=False):
    """Never expose authentication output, remote response bodies, or credentials."""
    try:
        result = subprocess.run(["gh", *arguments], capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PublicationError("GITHUB_COMMAND_UNCONFIRMED") from exc
    if result.returncode:
        if missing_ok and b"HTTP 404" in result.stderr:
            return None
        raise PublicationError("GITHUB_COMMAND_FAILED")
    return result.stdout


def _api(path, *, missing_ok=False):
    raw = _gh(["api", path], missing_ok=missing_ok)
    if raw is None:
        return None
    try:
        result = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise PublicationError("GITHUB_RESPONSE_INVALID") from exc
    _require(isinstance(result, dict), "GITHUB_RESPONSE_INVALID")
    return result


def _tag_commit(repo, tag):
    reference = _api(f"repos/{repo}/git/ref/tags/{quote(tag, safe='')}", missing_ok=True)
    if reference is None:
        return None
    target = reference.get("object") or {}
    for _ in range(5):
        sha = target.get("sha")
        _require(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha), "RELEASE_TAG_INVALID")
        if target.get("type") == "commit":
            return sha
        _require(target.get("type") == "tag", "RELEASE_TAG_INVALID")
        target = (_api(f"repos/{repo}/git/tags/{sha}").get("object") or {})
    raise PublicationError("RELEASE_TAG_DEPTH_EXCEEDED")


def _release_for_tag(repo, tag):
    """Resolve drafts through gh; REST's release-by-tag route excludes drafts."""
    raw = _gh(["release", "view", tag, "--repo", repo, "--json", "databaseId"])
    try:
        reference = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise PublicationError("GITHUB_RESPONSE_INVALID") from exc
    release_id = reference.get("databaseId") if isinstance(reference, dict) else None
    _require(type(release_id) is int and release_id > 0, "DRAFT_RELEASE_ID_INVALID")
    release = _api(f"repos/{repo}/releases/{release_id}")
    _require(release.get("id") == release_id and release.get("tag_name") == tag,
             "DRAFT_RELEASE_ID_MISMATCH")
    return release


def _verified_draft(repo, tag, package, artifact):
    release = _release_for_tag(repo, tag)
    _require(release.get("draft") is True and release.get("tag_name") == tag, "RELEASE_IS_NOT_MATCHING_DRAFT")
    _require(release.get("target_commitish") == artifact["source_commit"], "DRAFT_SOURCE_COMMIT_MISMATCH")
    tagged = _tag_commit(repo, tag)
    _require(tagged is None or tagged == artifact["source_commit"], "RELEASE_TAG_COMMIT_MISMATCH")
    assets = release.get("assets")
    _require(isinstance(assets, list) and all(isinstance(item, dict) for item in assets), "DRAFT_ASSETS_INVALID")
    matched = [item for item in assets if item.get("name") == Path(package).name]
    _require(len(matched) == 1, "DRAFT_PACKAGE_ASSET_MISSING_OR_DUPLICATED")
    # This invocation verifies one native package. Additional platform archives
    # need their own acceptance; do not silently publish an unverified binary.
    _require(not any(str(item.get("name", "")).lower().endswith((".zip", ".exe", ".msi", ".dmg", ".app", ".7z"))
                     and item is not matched[0] for item in assets), "DRAFT_HAS_UNVERIFIED_BINARY_ASSETS")
    asset = matched[0]
    _require(type(asset.get("id")) is int and asset["id"] > 0, "DRAFT_ASSET_ID_INVALID")
    _require(asset.get("size") == Path(package).stat().st_size, "DRAFT_PACKAGE_SIZE_MISMATCH")
    digest = asset.get("digest")
    if digest is None:
        raw = _gh(["api", f"repos/{repo}/releases/assets/{asset['id']}",
                   "--header", "Accept: application/octet-stream"])
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    _require(digest == "sha256:" + artifact["archive_sha256"], "DRAFT_PACKAGE_HASH_MISMATCH")
    _require(type(release.get("id")) is int and release["id"] > 0, "DRAFT_RELEASE_ID_INVALID")
    return release["id"], asset["id"]


def public_receipt(decision):
    """Allowlisted hashes only: no case IDs, source paths, answers, or reviewers."""
    artifact = decision.get("artifact") or {}
    return {"schema_version": "desktop-release-receipt/v1",
            "candidate_allowed": decision.get("candidate_allowed") is True,
            "publish_allowed": decision.get("publish_allowed") is True,
            "artifact": {key: artifact.get(key) for key in
                         ("source_commit", "archive_sha256", "binary_sha256", "application_sha256")},
            "input_sha256": {key: (decision.get("input_sha256") or {}).get(key) for key in
                             ("policy", "manifest", "compiled_report", "live_report", "live_cases", "reviews", "office_report")}}


def _unchanged(inputs, decision):
    _require(file_sha256(inputs["package"]) == decision["artifact"]["archive_sha256"], "PACKAGE_CHANGED_AFTER_GATE")
    for name, digest in decision["input_sha256"].items():
        _require(file_sha256(inputs[name]) == digest, "INPUT_CHANGED_AFTER_GATE")


def publish_release(*, package, compiled_report, manifest, policy, live_report, live_cases, reviews,
                    repo, tag, output, receipt=None, publish=False, office_report=None):
    """Return a local decision; only explicit publish permits GitHub writes."""
    inputs = dict(package=package, compiled_report=compiled_report, manifest=manifest, policy=policy,
                  live_report=live_report, live_cases=live_cases, reviews=reviews)
    if office_report is not None:
        inputs["office_report"] = office_report
    decision = evaluate_release(**inputs)
    result = {"schema_version": "desktop-publication/v1", "requested_publish": publish,
              "published": False, "status": "gate_rejected", "acceptance": decision}
    output = Path(output)
    # Reject output/input collisions before any local artifact could be overwritten.
    receipt = (Path(receipt) if receipt else output.with_name("release-acceptance-receipt.json")).resolve()
    # gh release upload interprets '#' as a label separator, even inside a path.
    _require("#" not in str(receipt), "PUBLICATION_RECEIPT_PATH_INVALID")
    protected = {Path(value).resolve() for value in inputs.values()}
    _require(output.resolve() not in protected and receipt.resolve() not in protected
             and receipt.resolve() != output.resolve(), "PUBLICATION_OUTPUT_OVERLAPS_INPUT")
    _safe_write(output, result)
    if not decision.get("publish_allowed"):
        return result
    _require(isinstance(repo, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repo), "REPOSITORY_INVALID")
    _require(isinstance(tag, str) and bool(tag) and not tag.startswith("-")
             and not any(character.isspace() or ord(character) < 32 for character in tag), "RELEASE_TAG_INVALID")
    _safe_write(receipt, public_receipt(decision))
    if not publish:
        result["status"] = "local_acceptance_passed_dry_run"
        _safe_write(output, result)
        return result
    try:
        _unchanged(inputs, decision)
        release_id, asset_id = _verified_draft(repo, tag, package, decision["artifact"])
        _unchanged(inputs, decision)
        _gh(["release", "upload", tag, str(receipt), "--repo", repo, "--clobber"])
        # Recheck the draft before attempting publication.
        current_id, current_asset_id = _verified_draft(repo, tag, package, decision["artifact"])
        _require((current_id, current_asset_id) == (release_id, asset_id), "DRAFT_CHANGED_DURING_PUBLICATION")
        _unchanged(inputs, decision)
        _gh(["release", "edit", tag, "--repo", repo, "--draft=false", "--prerelease=false", "--latest"])
        confirmed = _api(f"repos/{repo}/releases/{release_id}")
        _require(confirmed.get("id") == release_id and confirmed.get("tag_name") == tag
                 and confirmed.get("draft") is False and confirmed.get("prerelease") is False
                 and confirmed.get("target_commitish") == decision["artifact"]["source_commit"],
                 "PUBLICATION_OUTCOME_UNCONFIRMED")
        result.update(status="published", published=True)
    except (OSError, PublicationError) as exc:
        result.update(status="publication_not_confirmed", error=str(exc) if isinstance(exc, PublicationError)
                      else "LOCAL_INPUT_UNAVAILABLE")
    _safe_write(output, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("package", "compiled-report", "manifest", "policy", "live-report", "live-cases", "reviews", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--office-report", type=Path)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = publish_release(**vars(args))
    except (OSError, PublicationError) as exc:
        print(json.dumps({"status": "rejected", "error": str(exc) if isinstance(exc, PublicationError)
                          else "LOCAL_INPUT_UNAVAILABLE"}))
        return 1
    print(json.dumps({"status": result["status"], "published": result["published"],
                      "publish_allowed": result["acceptance"].get("publish_allowed", False)}))
    return 0 if result["published"] or result["status"] == "local_acceptance_passed_dry_run" else 1


if __name__ == "__main__":
    sys.exit(main())
