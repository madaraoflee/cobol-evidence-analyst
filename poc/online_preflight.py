"""Read-only deployment checks for one fixed cloud repository revision."""

from __future__ import annotations

import argparse
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener

from online_material_format import MaterialError, decode_json, valid_commit
from online_source_client import _authorization, _NoRedirect


API_ORIGIN = "https://api.bitbucket.org"
MAX_METADATA_BYTES = 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30
_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}/[A-Za-z0-9][A-Za-z0-9_.-]{0,255}\Z")
_ERROR_CODE = re.compile(r"PREFLIGHT_[A-Z0-9_]{1,72}\Z")
_RETRY_STATUSES = {429, 502, 503, 504}
_UNKNOWN_CODES = {"PREFLIGHT_NOT_FOUND", "PREFLIGHT_RATE_LIMIT", "PREFLIGHT_CONNECTION_FAILED",
                  "PREFLIGHT_HTTP_ERROR"}
PENDING_GATES = (
    "hosted_runner_connectivity", "plan_quota_and_storage_retention",
    "publishing_write_and_readback", "execution_resources",
    "actual_source_acceptance", "model_and_business_acceptance",
)


def _repository(value):
    if not isinstance(value, str) or not _REPOSITORY.fullmatch(value):
        raise MaterialError("PREFLIGHT_INVALID_REPOSITORY")
    return value


def _commit(value):
    try:
        return valid_commit(value)
    except MaterialError:
        raise MaterialError("PREFLIGHT_INVALID_COMMIT") from None


def _safe_code(error):
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and _ERROR_CODE.fullmatch(code) else "PREFLIGHT_OPERATION_FAILED"


def _status_error(status):
    if 300 <= status < 400:
        return MaterialError("PREFLIGHT_REDIRECT_REJECTED")
    return MaterialError("PREFLIGHT_" + {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND",
                                        429: "RATE_LIMIT"}.get(status, "HTTP_ERROR"))


class CloudMetadataClient:
    """Only fixed metadata routes; no source, writes, pagination or redirects."""

    def __init__(self, repository, *, token=None, username=None, opener=None,
                 attempts=3, wait=None):
        self.repository = _repository(repository)
        if type(attempts) is not int or not 1 <= attempts <= 3:
            raise MaterialError("PREFLIGHT_INVALID_RETRY_LIMIT")
        workspace, name = repository.split("/")
        self._base = (API_ORIGIN + "/2.0/repositories/" + quote(workspace, safe="")
                      + "/" + quote(name, safe=""))
        self._headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        authorization = _authorization(token, username, "PREFLIGHT")
        if authorization:
            self._headers["Authorization"] = authorization
        self._opener = opener if opener is not None else build_opener(_NoRedirect())
        self._attempts, self._wait = attempts, wait or time.sleep
        self.requests, self.received_bytes = 0, 0

    def _read(self, response):
        length = response.headers.get("Content-Length")
        if length is not None:
            if not isinstance(length, str) or len(length) > 20 or not length.isascii() or not length.isdecimal():
                raise MaterialError("PREFLIGHT_INVALID_RESPONSE")
            length = int(length)
            if length > MAX_METADATA_BYTES:
                raise MaterialError("PREFLIGHT_SIZE_LIMIT")
        if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
            raise MaterialError("PREFLIGHT_ENCODING_UNSUPPORTED")
        chunks, size = [], 0
        while True:
            part = getattr(response, "read1", response.read)(min(65536, MAX_METADATA_BYTES + 1 - size))
            if not isinstance(part, bytes):
                raise MaterialError("PREFLIGHT_INVALID_RESPONSE")
            if not part:
                break
            size += len(part)
            self.received_bytes += len(part)
            if size > MAX_METADATA_BYTES:
                raise MaterialError("PREFLIGHT_SIZE_LIMIT")
            chunks.append(part)
        if length is not None and size != length:
            raise MaterialError("PREFLIGHT_LENGTH_MISMATCH")
        try:
            value = decode_json(b"".join(chunks), maximum=MAX_METADATA_BYTES)
        except MaterialError:
            raise MaterialError("PREFLIGHT_INVALID_JSON") from None
        if not isinstance(value, dict):
            raise MaterialError("PREFLIGHT_INVALID_RESPONSE")
        return value

    def _get(self, suffix):
        # Only the fixed public methods below supply suffixes.
        url = self._base + suffix
        for attempt in range(self._attempts):
            request = Request(url, headers=self._headers, method="GET")
            self.requests += 1
            response = None
            try:
                try:
                    response = self._opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS)
                except HTTPError as error:
                    response = error
                status = getattr(response, "status", None)
                if status is None and hasattr(response, "getcode"):
                    status = response.getcode()
                if type(status) is not int:
                    raise MaterialError("PREFLIGHT_INVALID_RESPONSE")
                final = response.geturl() if hasattr(response, "geturl") else url
                if final != url:
                    raise MaterialError("PREFLIGHT_REDIRECT_REJECTED")
                if status in _RETRY_STATUSES and attempt + 1 < self._attempts:
                    retry = response.headers.get("Retry-After", "")
                    delay = min(5, int(retry)) if isinstance(retry, str) and len(retry) <= 3 and retry.isdecimal() else 2 ** attempt
                    response.close()
                    response = None
                    self._wait(delay)
                    continue
                if status != 200:
                    raise _status_error(status)
                return self._read(response)
            except (URLError, OSError, HTTPException, TimeoutError, socket.timeout):
                if attempt + 1 == self._attempts:
                    raise MaterialError("PREFLIGHT_CONNECTION_FAILED") from None
                self._wait(2 ** attempt)
            finally:
                if response is not None:
                    response.close()
        raise MaterialError("PREFLIGHT_CONNECTION_FAILED")

    def repository_metadata(self):
        value = self._get("?fields=full_name")
        if value.get("full_name") != self.repository:
            raise MaterialError("PREFLIGHT_REPOSITORY_MISMATCH")
        return {"identity": self.repository}

    def commit_metadata(self, commit):
        commit = _commit(commit)
        value = self._get("/commit/" + commit + "?fields=hash")
        if value.get("hash") != commit:
            raise MaterialError("PREFLIGHT_COMMIT_MISMATCH")
        return {"commit": commit}

    def downloads_probe(self):
        value = self._get("/downloads?pagelen=1")
        if not isinstance(value.get("values"), list) or len(value["values"]) > 1:
            raise MaterialError("PREFLIGHT_INVALID_RESPONSE")
        # The first page is sufficient to check read access. Do not follow next
        # or retain filenames, content links, or other repository metadata.
        return {"readable": True}

    def pipeline_config(self):
        value = self._get("/pipelines_config")
        if type(value.get("enabled")) is not bool:
            raise MaterialError("PREFLIGHT_INVALID_RESPONSE")
        return {"enabled": value["enabled"]}


def run_checks(source_repository, commit, material_repository, *, source_auth=None,
               material_auth=None, check_pipelines=False, source_opener=None,
               material_opener=None, attempts=3, wait=None):
    """Check metadata only; successful reads never establish deployment readiness."""
    source_repository, material_repository = _repository(source_repository), _repository(material_repository)
    commit = _commit(commit)
    if type(check_pipelines) is not bool:
        raise MaterialError("PREFLIGHT_INVALID_OPTION")
    source = CloudMetadataClient(source_repository, **(source_auth or {}), opener=source_opener,
                                 attempts=attempts, wait=wait)
    material = CloudMetadataClient(material_repository, **(material_auth or {}), opener=material_opener,
                                   attempts=attempts, wait=wait)
    checks = []

    def perform(name, operation, *, pipeline=False):
        try:
            result = operation()
            if pipeline and result["enabled"] is False:
                checks.append({"check": name, "status": "fail", "code": "PREFLIGHT_PIPELINES_DISABLED"})
            else:
                checks.append({"check": name, "status": "pass", "code": "PREFLIGHT_READ_CONFIRMED"})
        except MaterialError as error:
            code = _safe_code(error)
            unknown = code in _UNKNOWN_CODES or (pipeline and code == "PREFLIGHT_FORBIDDEN")
            checks.append({"check": name, "status": "unknown" if unknown else "fail", "code": code})

    perform("source_repository", source.repository_metadata)
    perform("source_commit", lambda: source.commit_metadata(commit))
    perform("material_repository", material.repository_metadata)
    perform("downloads_read", material.downloads_probe)
    pending = [{"check": name, "status": "not_assessed"} for name in PENDING_GATES]
    if check_pipelines:
        perform("pipelines_enabled", source.pipeline_config, pipeline=True)
    else:
        pending.insert(0, {"check": "pipelines_enabled", "status": "not_assessed"})
    statuses = {item["status"] for item in checks}
    status = "blocked" if "fail" in statuses else "incomplete" if "unknown" in statuses else "read_checks_passed"
    return {"schema_version": "online-deployment-preflight/v1", "status": status,
            "source_repository": source_repository, "commit": commit,
            "material_repository": material_repository, "checks": checks, "pending_gates": pending,
            "metrics": {"requests": source.requests + material.requests,
                        "metadata_bytes": source.received_bytes + material.received_bytes},
            "source_content_read": False, "write_performed": False, "model_called": False}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="Read metadata only; do not publish, clone or call a model")
    check.add_argument("--source-repository", required=True)
    check.add_argument("--commit", required=True)
    check.add_argument("--material-repository", required=True)
    check.add_argument("--check-pipelines", action="store_true",
                       help="Check enabled state if this identity has repository administration access")
    check.add_argument("--output", required=True, help="New report file; existing files are never overwritten")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        _repository(args.source_repository)
        _repository(args.material_repository)
        _commit(args.commit)
        output = Path(args.output)
        # Some platforms follow a dangling leaf link even in exclusive mode.
        # Reject that link before opening a report or issuing metadata reads.
        if output.is_symlink():
            raise MaterialError("PREFLIGHT_REPORT_SYMLINK")
        # Reserve the report before any network operation, including detecting
        # inaccessible parents and concurrent attempts to use the same path.
        with output.open("x", encoding="utf-8") as stream:
            try:
                result = run_checks(args.source_repository, args.commit, args.material_repository,
                    source_auth={"token": os.environ.get("ONLINE_SOURCE_TOKEN"),
                                 "username": os.environ.get("ONLINE_SOURCE_USERNAME")},
                    material_auth={"token": os.environ.get("ONLINE_MATERIAL_TOKEN"),
                                   "username": os.environ.get("ONLINE_MATERIAL_USERNAME")},
                    check_pipelines=args.check_pipelines)
                code = {"read_checks_passed": 0, "blocked": 2, "incomplete": 3}[result["status"]]
            except MaterialError as error:
                result, code = {"status": "blocked", "error": _safe_code(error)}, 2
            except (Exception, KeyboardInterrupt):
                result, code = {"status": "incomplete", "error": "PREFLIGHT_OPERATION_FAILED"}, 3
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except MaterialError as error:
        print(json.dumps({"status": "blocked", "error": _safe_code(error)}))
        return 2
    except FileExistsError:
        print(json.dumps({"status": "blocked", "error": "PREFLIGHT_REPORT_EXISTS"}))
        return 2
    except OSError:
        print(json.dumps({"status": "blocked", "error": "PREFLIGHT_REPORT_UNAVAILABLE"}))
        return 2
    print(json.dumps({"status": result["status"], "error": result.get("error")}))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
