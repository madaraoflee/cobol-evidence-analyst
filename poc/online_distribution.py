"""Immutable analysis releases in repository Downloads, with bounded transport.

Credentials go only to the exact API origin. Download redirects are followed
manually to an explicit set of HTTPS hosts, without Authorization. Published
names are content-derived; no mutable latest pointer or overwrite is used.
"""

from __future__ import annotations

from collections import OrderedDict
import copy
from datetime import datetime
from http.client import HTTPException
import re
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener

from online_material_format import (MAX_MANIFEST_BYTES, MAX_OBJECT_BYTES, MaterialError,
    canonical_json, decode_json, digest, relative_path, valid_commit, valid_hash,
    validate_manifest, validate_reference)
from online_material_reader import DirectoryObjectStore
from online_source_client import _NoRedirect, _authorization


API_ORIGIN = "https://api.bitbucket.org"
DOWNLOAD_HOSTS = frozenset({"bbuseruploads.s3.amazonaws.com", "cdn.bitbucket.org"})
RELEASE_SCHEMA = "online-release/v1"
SNAPSHOT_RELEASE_SCHEMA = "online-release/v2"
SYNC_SCHEMA = "source-sync/v1"
SNAPSHOT_SCHEMA = "repository-snapshot/v1"
BUNDLE_SCHEMA = "online-bundle/v1"
MAX_RELEASE_BYTES = 8 * 1024 * 1024
MAX_BUNDLE_BYTES = 8 * 1024 * 1024
TARGET_BUNDLE_BYTES = 1024 * 1024
MAX_BUNDLE_ROUTES = 32768
MAX_PUBLISH_OBJECTS = 1_000_000
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}\Z")
_NAME = re.compile(r"analysis-(?:object|bundle|material|release)-[a-f0-9]{64}\.json\Z")
_RELEASE_NAME = re.compile(r"analysis-release-([a-f0-9]{64})\.json\Z")
_PREFIX = re.compile(r"[a-f0-9]{2,64}\Z")
_ERROR_CODES = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 429: "RATE_LIMIT"}


class PublicationUnknown(MaterialError):
    """The release may already exist; reconcile its deterministic name."""

    def __init__(self, release_name, cause_code):
        super().__init__("DISTRIBUTION_PUBLICATION_UNKNOWN")
        self.release_name = release_name
        self.cause_code = (cause_code if isinstance(cause_code, str)
                           and re.fullmatch(r"(?:DISTRIBUTION|MATERIAL)_[A-Z0-9_]{1,80}", cause_code)
                           else "DISTRIBUTION_OPERATION_FAILED")


def _error(status):
    return MaterialError("DISTRIBUTION_" + _ERROR_CODES.get(status, "HTTP_ERROR"))


def _filename(name):
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise MaterialError("DISTRIBUTION_INVALID_NAME")
    return name


def _safe_https(url, *, hosts, origin_path=None):
    try:
        if (not isinstance(url, str) or len(url) > 16384 or any(ord(c) < 33 or ord(c) == 127 for c in url)
                or "\\" in url):
            raise ValueError()
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username is not None
                or parsed.password is not None or parsed.port not in {None, 443} or parsed.fragment):
            raise ValueError()
        if origin_path is not None and (parsed.netloc != "api.bitbucket.org" or parsed.path != origin_path):
            raise ValueError()
        return url
    except (ValueError, UnicodeError):
        raise MaterialError("DISTRIBUTION_UNSAFE_URL") from None


def _utc(value):
    try:
        if not isinstance(value, str) or len(value) > 40 or not value.endswith("Z"):
            raise ValueError()
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        if parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise MaterialError("DISTRIBUTION_INVALID_SYNC_RECEIPT") from None


def validate_sync_receipt(value, repository):
    if (not isinstance(value, dict)
            or set(value) != {"schema_version", "repository", "commit", "subdirectory", "complete", "completed_at", "sync_id"}
            or value["schema_version"] != SYNC_SCHEMA or value["complete"] is not True
            or value["repository"] != repository["identity"]
            or value["commit"] != repository["commit"]
            or value["subdirectory"] != repository["subdirectory"]):
        raise MaterialError("DISTRIBUTION_INVALID_SYNC_RECEIPT")
    valid_commit(value["commit"])
    relative_path(value["subdirectory"], allow_empty=True)
    _utc(value["completed_at"])
    sync_id = value["sync_id"]
    if (not isinstance(sync_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", sync_id)):
        raise MaterialError("DISTRIBUTION_INVALID_SYNC_RECEIPT")
    return value


def validate_source_snapshot(value, repository):
    if (not isinstance(value, dict)
            or set(value) != {"schema_version", "repository", "commit", "subdirectory", "sync_completeness"}
            or value["schema_version"] != SNAPSHOT_SCHEMA
            or value["sync_completeness"] != "not_verified"
            or value["repository"] != repository["identity"]
            or value["commit"] != repository["commit"]
            or value["subdirectory"] != repository["subdirectory"]):
        raise MaterialError("DISTRIBUTION_INVALID_SOURCE_SNAPSHOT")
    valid_commit(value["commit"])
    relative_path(value["subdirectory"], allow_empty=True)
    return value


def make_source_snapshot(repository):
    """Describe the selected commit without asserting upstream synchronization."""
    return validate_source_snapshot({"schema_version": SNAPSHOT_SCHEMA,
        "repository": repository["identity"], "commit": repository["commit"],
        "subdirectory": repository["subdirectory"], "sync_completeness": "not_verified"}, repository)


def _artifact_ref(value, kind, maximum):
    if not isinstance(value, dict) or set(value) != {"name", "sha256", "size"}:
        raise MaterialError("DISTRIBUTION_INVALID_RELEASE")
    sha = valid_hash(value["sha256"])
    if value["name"] != "analysis-" + kind + "-" + sha + ".json":
        raise MaterialError("DISTRIBUTION_INVALID_RELEASE")
    if type(value["size"]) is not int or not 0 < value["size"] <= maximum:
        raise MaterialError("DISTRIBUTION_SIZE_LIMIT")


def validate_release(value, *, name=None):
    if not isinstance(value, dict):
        raise MaterialError("DISTRIBUTION_INVALID_RELEASE")
    schema = value.get("schema_version")
    basis_key = ("sync_receipt" if schema == RELEASE_SCHEMA else
                 "source_snapshot" if schema == SNAPSHOT_RELEASE_SCHEMA else None)
    if (basis_key is None or set(value) != {"schema_version", "repository", "revision", "manifest", "bundle_routes", basis_key}):
        raise MaterialError("DISTRIBUTION_INVALID_RELEASE")
    repository = value["repository"]
    # Reuse the exact repository contract without trusting a supplied revision.
    from online_material_format import seal_manifest, SCHEMA_VERSION
    seal_manifest({"schema_version": SCHEMA_VERSION, "repository": repository,
                   "analysis": {}, "coverage": {}, "routes": {"files": {}, "terms": {}, "references": {}}})
    valid_hash(value["revision"])
    manifest = value["manifest"]
    if (not isinstance(manifest, dict) or set(manifest) != {"name", "sha256", "size"}
            or manifest["name"] != "analysis-material-" + value["revision"] + ".json"):
        raise MaterialError("DISTRIBUTION_INVALID_RELEASE")
    valid_hash(manifest["sha256"])
    if type(manifest["size"]) is not int or not 0 < manifest["size"] <= MAX_MANIFEST_BYTES:
        raise MaterialError("DISTRIBUTION_SIZE_LIMIT")
    routes = value["bundle_routes"]
    if not isinstance(routes, dict) or not routes or len(routes) > MAX_BUNDLE_ROUTES:
        raise MaterialError("DISTRIBUTION_INVALID_RELEASE")
    for prefix, reference in routes.items():
        if not isinstance(prefix, str) or not _PREFIX.fullmatch(prefix):
            raise MaterialError("DISTRIBUTION_INVALID_RELEASE")
        _artifact_ref(reference, "bundle", MAX_BUNDLE_BYTES)
        if any(prefix[:length] in routes for length in range(2, len(prefix))):
            raise MaterialError("DISTRIBUTION_OVERLAPPING_ROUTES")
    if schema == RELEASE_SCHEMA:
        validate_sync_receipt(value["sync_receipt"], repository)
    else:
        validate_source_snapshot(value["source_snapshot"], repository)
    encoded = canonical_json(value)
    if len(encoded) > MAX_RELEASE_BYTES:
        raise MaterialError("DISTRIBUTION_SIZE_LIMIT")
    if name is not None and name != "analysis-release-" + digest(encoded) + ".json":
        raise MaterialError("DISTRIBUTION_RELEASE_HASH_MISMATCH")
    return value


class DownloadsClient:
    """Small JSON artifacts; GET retries are bounded, uploads are never retried."""

    def __init__(self, workspace, repository, token=None, username=None, opener=None,
                 check_cancel=None, *, download_hosts=None, attempts=3, wait=None):
        if any(not isinstance(value, str) or not _SLUG.fullmatch(value) for value in (workspace, repository)):
            raise MaterialError("DISTRIBUTION_INVALID_REPOSITORY")
        if type(attempts) is not int or not 1 <= attempts <= 3:
            raise MaterialError("DISTRIBUTION_INVALID_RETRY_LIMIT")
        hosts = DOWNLOAD_HOSTS if download_hosts is None else frozenset(download_hosts)
        if not hosts or any(not isinstance(host, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", host)
                            or host in {"localhost", "api.bitbucket.org"} or "." not in host for host in hosts):
            raise MaterialError("DISTRIBUTION_INVALID_DOWNLOAD_HOST")
        self._headers = {"Accept": "application/json"}
        authorization = _authorization(token, username, "DISTRIBUTION")
        if authorization:
            self._headers["Authorization"] = authorization
        self._path = "/2.0/repositories/" + quote(workspace, safe="") + "/" + quote(repository, safe="") + "/downloads"
        self._base = API_ORIGIN + self._path
        self._opener = opener if opener is not None else build_opener(_NoRedirect())
        self._check = check_cancel
        self._hosts, self._attempts, self._wait = hosts, attempts, wait or time.sleep
        self.network_fetched_bytes, self.network_requests = 0, 0

    def _tick(self):
        if self._check:
            self._check()

    def _delay(self, seconds):
        remaining = min(5.0, max(0.0, seconds))
        while remaining:
            self._tick()
            chunk = min(0.1, remaining)
            self._wait(chunk)
            remaining -= chunk

    def _read_response(self, response, maximum):
        length = response.headers.get("Content-Length")
        if length is not None:
            if not isinstance(length, str) or len(length) > 20 or not length.isascii() or not length.isdecimal():
                raise MaterialError("DISTRIBUTION_INVALID_RESPONSE")
            length = int(length)
            if length > maximum:
                raise MaterialError("DISTRIBUTION_SIZE_LIMIT")
        if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
            raise MaterialError("DISTRIBUTION_ENCODING_UNSUPPORTED")
        chunks, size = [], 0
        while True:
            self._tick()
            part = getattr(response, "read1", response.read)(min(65536, maximum + 1 - size))
            if not isinstance(part, bytes):
                raise MaterialError("DISTRIBUTION_INVALID_RESPONSE")
            if not part:
                break
            size += len(part)
            self.network_fetched_bytes += len(part)
            if size > maximum:
                raise MaterialError("DISTRIBUTION_SIZE_LIMIT")
            chunks.append(part)
        if length is not None and size != length:
            raise MaterialError("DISTRIBUTION_LENGTH_MISMATCH")
        return b"".join(chunks)

    def _request(self, url, *, maximum, body=None, headers=None, redirect=False):
        if type(maximum) is not int or not 0 <= maximum <= MAX_BUNDLE_BYTES:
            raise MaterialError("DISTRIBUTION_SIZE_LIMIT")
        method = "POST" if body is not None else "GET"
        attempts = 1 if body is not None else self._attempts
        for attempt in range(attempts):
            self._tick()
            request = Request(url, data=body, headers=headers or self._headers, method=method)
            self.network_requests += 1
            response = None
            try:
                try:
                    response = self._opener.open(request, timeout=30)
                except HTTPError as error:
                    response = error
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                if type(status) is not int:
                    raise MaterialError("DISTRIBUTION_INVALID_RESPONSE")
                final = response.geturl() if hasattr(response, "geturl") else url
                if final != url:
                    raise MaterialError("DISTRIBUTION_UNEXPECTED_REDIRECT")
                if status == 302 and redirect:
                    location = _safe_https(response.headers.get("Location"), hosts=self._hosts)
                    response.close()
                    response = None
                    return self._request(location, maximum=maximum, headers={"Accept": "application/json"}, redirect=False)
                if status in {429, 502, 503, 504} and attempt + 1 < attempts:
                    retry = response.headers.get("Retry-After", "")
                    delay = min(5, int(retry)) if isinstance(retry, str) and retry.isdecimal() and len(retry) <= 3 else 2 ** attempt
                    response.close()
                    response = None
                    self._delay(delay)
                    continue
                if status != (201 if body is not None else 200):
                    if type(status) is int and 300 <= status < 400:
                        raise MaterialError("DISTRIBUTION_REDIRECT_REJECTED")
                    raise _error(status)
                return self._read_response(response, maximum)
            except (URLError, OSError, HTTPException, TimeoutError, socket.timeout):
                if attempt + 1 == attempts:
                    raise MaterialError("DISTRIBUTION_CONNECTION_FAILED") from None
                self._delay(2 ** attempt)
            finally:
                if response is not None:
                    response.close()
        raise MaterialError("DISTRIBUTION_CONNECTION_FAILED")

    def read(self, name, max_bytes=MAX_BUNDLE_BYTES):
        return self._request(self._base + "/" + quote(_filename(name), safe=""), maximum=max_bytes, redirect=True)

    def _check_content_name(self, name, data):
        _filename(name)
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BUNDLE_BYTES:
            raise MaterialError("DISTRIBUTION_SIZE_LIMIT")
        if name.startswith("analysis-material-"):
            manifest = validate_manifest(decode_json(data, maximum=MAX_MANIFEST_BYTES))
            if name != "analysis-material-" + manifest["revision"] + ".json":
                raise MaterialError("DISTRIBUTION_CONTENT_NAME_MISMATCH")
            if data != canonical_json(manifest):
                raise MaterialError("DISTRIBUTION_NONCANONICAL_CONTENT")
        elif name.rsplit("-", 1)[1][:-5] != digest(data):
            raise MaterialError("DISTRIBUTION_CONTENT_NAME_MISMATCH")

    def put_immutable(self, name, data):
        """Content names make concurrent same-name writers converge on same bytes."""
        return self.put_immutable_many([(name, data)])[0]

    def put_immutable_many(self, items):
        """One bounded multipart upload, followed by verification of every file."""
        if not isinstance(items, (list, tuple)) or not 1 <= len(items) <= 16:
            raise MaterialError("DISTRIBUTION_UPLOAD_BATCH_LIMIT")
        pending, reports, names, total = [], {}, [], 0
        for item in items:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise MaterialError("DISTRIBUTION_UPLOAD_BATCH_LIMIT")
            name, data = item
            self._check_content_name(name, data)
            if name in names:
                raise MaterialError("DISTRIBUTION_DUPLICATE_UPLOAD")
            names.append(name)
            total += len(data)
        if total > MAX_BUNDLE_BYTES:
            raise MaterialError("DISTRIBUTION_UPLOAD_BATCH_LIMIT")
        for name, data in items:
            try:
                existing = self.read(name, len(data))
            except MaterialError as error:
                if error.code != "DISTRIBUTION_NOT_FOUND":
                    raise
                pending.append((name, data))
            else:
                if existing != data:
                    raise MaterialError("DISTRIBUTION_CONTENT_CONFLICT")
                reports[name] = {"name": name, "uploaded": False, "verified": True}
        if pending:
            boundary = "analysis-" + digest(canonical_json([(name, digest(data)) for name, data in pending]))
            fragments = []
            for name, data in pending:
                fragments.extend([("--" + boundary + '\r\nContent-Disposition: form-data; name="files"; filename="' + name +
                                   '"\r\nContent-Type: application/json\r\n\r\n').encode("ascii"), data, b"\r\n"])
            fragments.append(("--" + boundary + "--\r\n").encode("ascii"))
            headers = {**self._headers, "Content-Type": "multipart/form-data; boundary=" + boundary}
            self._request(self._base, maximum=1024 * 1024, body=b"".join(fragments), headers=headers)
            for name, data in pending:
                if self.read(name, len(data)) != data:
                    raise MaterialError("DISTRIBUTION_UPLOAD_VERIFICATION_FAILED")
                reports[name] = {"name": name, "uploaded": True, "verified": True}
        return [reports[name] for name in names]

    def list_release_names(self, *, max_pages=1000, max_entries=100000):
        return [item["name"] for item in self.list_release_entries(max_pages=max_pages, max_entries=max_entries)]

    def list_release_entries(self, *, max_pages=1000, max_entries=100000):
        if type(max_pages) is not int or not 1 <= max_pages <= 1000 or type(max_entries) is not int or not 1 <= max_entries <= 100000:
            raise MaterialError("DISTRIBUTION_DISCOVERY_LIMIT")
        url, seen, entries, count = self._base + "?pagelen=100", set(), {}, 0
        for _ in range(max_pages):
            _safe_https(url, hosts={"api.bitbucket.org"}, origin_path=self._path)
            if url in seen:
                raise MaterialError("DISTRIBUTION_PAGINATION_LOOP")
            seen.add(url)
            page = decode_json(self._request(url, maximum=1024 * 1024), maximum=1024 * 1024)
            if not isinstance(page, dict) or not isinstance(page.get("values"), list):
                raise MaterialError("DISTRIBUTION_INVALID_RESPONSE")
            count += len(page["values"])
            if count > max_entries:
                raise MaterialError("DISTRIBUTION_DISCOVERY_LIMIT")
            for item in page["values"]:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    raise MaterialError("DISTRIBUTION_INVALID_RESPONSE")
                if _RELEASE_NAME.fullmatch(item["name"]):
                    created, size = item.get("created_on"), item.get("size")
                    if created is not None and (not isinstance(created, str) or len(created) > 64 or any(ord(c) < 32 for c in created)):
                        raise MaterialError("DISTRIBUTION_INVALID_RESPONSE")
                    if size is not None and (type(size) is not int or not 0 <= size <= MAX_RELEASE_BYTES):
                        raise MaterialError("DISTRIBUTION_INVALID_RESPONSE")
                    entries[item["name"]] = {"name": item["name"], "created_on": created, "size": size}
            url = page.get("next")
            if url is None:
                return [entries[name] for name in sorted(entries)]
        raise MaterialError("DISTRIBUTION_DISCOVERY_LIMIT")


def load_release(client, release_name):
    if not isinstance(release_name, str) or not _RELEASE_NAME.fullmatch(release_name):
        raise MaterialError("DISTRIBUTION_INVALID_NAME")
    raw = client.read(release_name, MAX_RELEASE_BYTES)
    value = validate_release(decode_json(raw, maximum=MAX_RELEASE_BYTES), name=release_name)
    if canonical_json(value) != raw:
        raise MaterialError("DISTRIBUTION_NONCANONICAL_CONTENT")
    return value


class DownloadsObjectStore:
    """Resolve logical objects from verified, bounded bundles on demand."""

    def __init__(self, client, revision, *, release=None, cache_bytes=32 * 1024 * 1024,
                 max_network_bytes=128 * 1024 * 1024):
        valid_hash(revision)
        if type(cache_bytes) is not int or cache_bytes < 0 or type(max_network_bytes) is not int or max_network_bytes < 0:
            raise MaterialError("DISTRIBUTION_INVALID_BUDGET")
        self.client, self.revision = client, revision
        self.release = copy.deepcopy(validate_release(release)) if release is not None else None
        if self.release is not None and self.release["revision"] != revision:
            raise MaterialError("DISTRIBUTION_REVISION_MISMATCH")
        self._cache, self._cache_size, self._cache_limit = OrderedDict(), 0, cache_bytes
        self._network_limit = max_network_bytes
        self.reset_network_budget()

    @property
    def network_fetched_bytes(self):
        return self.client.network_fetched_bytes

    @property
    def network_requests(self):
        return self.client.network_requests

    def reset_network_budget(self):
        self._network_start = self.client.network_fetched_bytes

    def _read(self, name, size):
        if self.network_fetched_bytes - self._network_start + size > self._network_limit:
            raise MaterialError("DISTRIBUTION_NETWORK_BUDGET_EXCEEDED")
        raw = self.client.read(name, size)
        if self.network_fetched_bytes - self._network_start > self._network_limit:
            raise MaterialError("DISTRIBUTION_NETWORK_BUDGET_EXCEEDED")
        return raw

    def read(self, path, max_bytes):
        relative_path(path)
        if type(max_bytes) is not int or not 0 <= max_bytes <= MAX_OBJECT_BYTES:
            raise MaterialError("DISTRIBUTION_SIZE_LIMIT")
        if path == "material.json":
            reference = self.release["manifest"] if self.release else None
            raw = self._read("analysis-material-" + self.revision + ".json", reference["size"] if reference else max_bytes)
            if reference and (len(raw) != reference["size"] or digest(raw) != reference["sha256"]):
                raise MaterialError("DISTRIBUTION_MANIFEST_MISMATCH")
        else:
            match = re.fullmatch(r"objects/([a-f0-9]{64})\.json", path)
            if not match:
                raise MaterialError("DISTRIBUTION_INVALID_PATH")
            sha = match[1]
            if self.release is None:
                raw = self._read("analysis-object-" + sha + ".json", max_bytes)
            else:
                routes = self.release["bundle_routes"]
                prefix = next((sha[:length] for length in range(2, 65) if sha[:length] in routes), None)
                if prefix is None:
                    raise MaterialError("DISTRIBUTION_OBJECT_NOT_PUBLISHED")
                ref = routes[prefix]
                encoded = self._cache.get(ref["sha256"])
                if encoded is None:
                    encoded = self._read(ref["name"], ref["size"])
                    if len(encoded) != ref["size"] or digest(encoded) != ref["sha256"]:
                        raise MaterialError("DISTRIBUTION_BUNDLE_MISMATCH")
                bundle = decode_json(encoded, maximum=MAX_BUNDLE_BYTES)
                if (not isinstance(bundle, dict) or set(bundle) != {"schema_version", "prefix", "objects"}
                        or bundle["schema_version"] != BUNDLE_SCHEMA or bundle["prefix"] != prefix
                        or not isinstance(bundle["objects"], dict) or not bundle["objects"]):
                    raise MaterialError("DISTRIBUTION_INVALID_BUNDLE")
                for key, value in bundle["objects"].items():
                    valid_hash(key)
                    if not key.startswith(prefix) or digest(canonical_json(value)) != key:
                        raise MaterialError("DISTRIBUTION_BUNDLE_OBJECT_MISMATCH")
                if sha not in bundle["objects"]:
                    raise MaterialError("DISTRIBUTION_OBJECT_NOT_PUBLISHED")
                raw = canonical_json(bundle["objects"][sha])
                if ref["sha256"] in self._cache:
                    self._cache.move_to_end(ref["sha256"])
                elif len(encoded) <= self._cache_limit:
                    while self._cache and self._cache_size + len(encoded) > self._cache_limit:
                        _, old = self._cache.popitem(last=False)
                        self._cache_size -= len(old)
                    self._cache[ref["sha256"]] = encoded
                    self._cache_size += len(encoded)
            if digest(raw) != sha:
                raise MaterialError("DISTRIBUTION_OBJECT_HASH_MISMATCH")
        if len(raw) > max_bytes:
            raise MaterialError("DISTRIBUTION_SIZE_LIMIT")
        return raw


def _reachable(store, manifest, check_cancel):
    pending = [reference for table in manifest["routes"].values() for reference in table.values()]
    references = {}
    while pending:
        if check_cancel:
            check_cancel()
        reference = pending.pop()
        validate_reference(reference)
        sha = reference["sha256"]
        if sha in references:
            if reference != references[sha]:
                raise MaterialError("DISTRIBUTION_REFERENCE_CONFLICT")
            continue
        if len(references) >= MAX_PUBLISH_OBJECTS:
            raise MaterialError("DISTRIBUTION_OBJECT_LIMIT")
        raw = store.read(reference["path"], reference["size"])
        if len(raw) != reference["size"] or digest(raw) != sha:
            raise MaterialError("DISTRIBUTION_OBJECT_HASH_MISMATCH")
        value = decode_json(raw)
        if canonical_json(value) != raw:
            raise MaterialError("DISTRIBUTION_NONCANONICAL_CONTENT")
        references[sha] = reference
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                if set(item) == {"path", "sha256", "size"}:
                    validate_reference(item)
                    pending.append(item)
                else:
                    stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
    return references


def _bundles(store, references, *, target_bytes, check_cancel):
    groups = {}
    for sha in sorted(references):
        groups.setdefault(sha[:2], []).append(sha)
    pending = sorted(groups.items(), reverse=True)
    while pending:
        if check_cancel:
            check_cancel()
        prefix, shas = pending.pop()
        estimated = sum(references[sha]["size"] + 70 for sha in shas) + 128
        if estimated > target_bytes and len(shas) > 1 and len(prefix) < 64:
            children = {}
            for sha in shas:
                children.setdefault(sha[:len(prefix) + 1], []).append(sha)
            pending.extend(sorted(children.items(), reverse=True))
            continue
        objects = {}
        for sha in shas:
            reference = references[sha]
            raw = store.read(reference["path"], reference["size"])
            if len(raw) != reference["size"] or digest(raw) != sha:
                raise MaterialError("DISTRIBUTION_OBJECT_HASH_MISMATCH")
            objects[sha] = decode_json(raw)
        raw = canonical_json({"schema_version": BUNDLE_SCHEMA, "prefix": prefix, "objects": objects})
        if len(raw) > MAX_BUNDLE_BYTES:
            raise MaterialError("DISTRIBUTION_BUNDLE_SIZE_LIMIT")
        sha = digest(raw)
        yield prefix, {"name": "analysis-bundle-" + sha + ".json", "sha256": sha, "size": len(raw)}, raw


def publish_material(material_dir, client, sync_receipt=None, *, repository_snapshot=False, check_cancel=None,
                     target_bundle_bytes=TARGET_BUNDLE_BYTES, progress=None):
    """Publish verified bundles, manifest, then an immutable release descriptor.

    An explicit repository snapshot leaves upstream synchronization unverified.
    A supplied sync receipt remains an external attestation, never a timer claim.
    Before the final upload, failures leave candidates;
    once that upload starts, any unknown outcome must be reconciled by name.
    """
    if type(target_bundle_bytes) is not int or not 4096 <= target_bundle_bytes <= MAX_BUNDLE_BYTES:
        raise MaterialError("DISTRIBUTION_INVALID_BUNDLE_LIMIT")
    if (type(repository_snapshot) is not bool
            or repository_snapshot == (sync_receipt is not None)):
        raise MaterialError("DISTRIBUTION_INVALID_PUBLICATION_SOURCE")
    store = DirectoryObjectStore(material_dir)
    raw_manifest = store.read("material.json", MAX_MANIFEST_BYTES)
    manifest = validate_manifest(decode_json(raw_manifest, maximum=MAX_MANIFEST_BYTES))
    if manifest["analysis"].get("commit_verification") != "git_checked":
        raise MaterialError("DISTRIBUTION_UNVERIFIED_COMMIT")
    if repository_snapshot:
        basis = {"source_snapshot": make_source_snapshot(manifest["repository"])}
        release_schema = SNAPSHOT_RELEASE_SCHEMA
    else:
        validate_sync_receipt(sync_receipt, manifest["repository"])
        basis = {"sync_receipt": copy.deepcopy(sync_receipt)}
        release_schema = RELEASE_SCHEMA
    if canonical_json(manifest) != raw_manifest:
        raise MaterialError("DISTRIBUTION_NONCANONICAL_CONTENT")
    references = _reachable(store, manifest, check_cancel)
    routes, uploaded, reused = {}, 0, 0
    pending, pending_bytes = [], 0

    def flush():
        nonlocal uploaded, reused, pending_bytes
        if not pending:
            return
        if check_cancel:
            check_cancel()
        items = [(ref["name"], raw) for _, ref, raw in pending]
        upload_many = getattr(client, "put_immutable_many", None)
        reports = (upload_many(items) if upload_many else [client.put_immutable(name, raw) for name, raw in items])
        if not isinstance(reports, list) or len(reports) != len(pending):
            raise MaterialError("DISTRIBUTION_UPLOAD_VERIFICATION_FAILED")
        for (prefix, reference, _), result in zip(pending, reports):
            if result.get("verified") is not True or result.get("name") != reference["name"]:
                raise MaterialError("DISTRIBUTION_UPLOAD_VERIFICATION_FAILED")
            uploaded += bool(result.get("uploaded"))
            reused += not bool(result.get("uploaded"))
            routes[prefix] = reference
        pending.clear()
        pending_bytes = 0
        if progress:
            progress({"phase": "publish_bundles", "completed": len(routes), "uploaded": uploaded, "reused": reused})

    for prefix, reference, raw in _bundles(store, references, target_bytes=target_bundle_bytes, check_cancel=check_cancel):
        if len(routes) + len(pending) >= MAX_BUNDLE_ROUTES:
            raise MaterialError("DISTRIBUTION_BUNDLE_ROUTE_LIMIT")
        if len(pending) >= 16 or pending_bytes + len(raw) > MAX_BUNDLE_BYTES:
            flush()
        pending.append((prefix, reference, raw))
        pending_bytes += len(raw)
    flush()
    manifest_ref = {"name": "analysis-material-" + manifest["revision"] + ".json",
                    "sha256": digest(raw_manifest), "size": len(raw_manifest)}
    if check_cancel:
        check_cancel()
    if client.put_immutable(manifest_ref["name"], raw_manifest).get("verified") is not True:
        raise MaterialError("DISTRIBUTION_UPLOAD_VERIFICATION_FAILED")
    release = validate_release({"schema_version": release_schema, "repository": manifest["repository"],
        "revision": manifest["revision"], "manifest": manifest_ref, "bundle_routes": routes,
        **basis})
    raw_release = canonical_json(release)
    name = "analysis-release-" + digest(raw_release) + ".json"
    if check_cancel:
        check_cancel()
    try:
        if client.put_immutable(name, raw_release).get("verified") is not True:
            raise MaterialError("DISTRIBUTION_UPLOAD_VERIFICATION_FAILED")
    except (Exception, KeyboardInterrupt) as error:
        cause = ("DISTRIBUTION_CANCELLED" if isinstance(error, KeyboardInterrupt)
                 else getattr(error, "code", "DISTRIBUTION_OPERATION_FAILED"))
        raise PublicationUnknown(name, cause) from None
    return {"release_name": name, "release": release, "objects": len(references),
            "bundles": len(routes), "uploaded_bundles": uploaded, "reused_bundles": reused,
            "publication": "published"}
