"""Lazy, revision-bound readers for portable static analysis objects."""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
import copy
from functools import wraps
import os
from pathlib import Path
import stat
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from online_material_format import (MAX_MANIFEST_BYTES, MAX_OBJECT_BYTES, MaterialError,
    INLINE_POSTING_BYTES, INLINE_POSTING_PATHS, canonical_json,
    bucket, decode_json, digest, relative_path, valid_commit, valid_hash,
    validate_manifest, validate_reference)
from online_source_client import _authorization, _https_read, _limit


class DirectoryObjectStore:
    """Read ordinary files within a fixed root, without following child symlinks."""

    def __init__(self, root):
        path = Path(root)
        try:
            if path.is_symlink() or not path.is_dir():
                raise MaterialError("MATERIAL_INVALID_ROOT")
            self._root = path.resolve(strict=True)
        except OSError:
            raise MaterialError("MATERIAL_INVALID_ROOT") from None

    def read(self, path, max_bytes):
        parts = relative_path(path).split("/")
        _limit(max_bytes, "MATERIAL")
        descriptors = []
        stream = None
        try:
            if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
                directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                parent = os.open(self._root, directory_flags)
                descriptors.append(parent)
                for part in parts[:-1]:
                    parent = os.open(part, directory_flags, dir_fd=parent)
                    descriptors.append(parent)
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0), dir_fd=parent)
                stream = os.fdopen(fd, "rb")
            else:
                target = self._root
                for part in parts:
                    target = target / part
                    if target.is_symlink():
                        raise MaterialError("MATERIAL_INVALID_PATH")
                if not target.resolve(strict=True).is_relative_to(self._root):
                    raise MaterialError("MATERIAL_INVALID_PATH")
                before = target.stat(follow_symlinks=False)
                if not stat.S_ISREG(before.st_mode):
                    raise MaterialError("MATERIAL_INVALID_PATH")
                stream = target.open("rb")
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise MaterialError("MATERIAL_INVALID_PATH")
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise MaterialError("MATERIAL_INVALID_PATH")
            if info.st_size > max_bytes:
                raise MaterialError("MATERIAL_SIZE_LIMIT")
            data = stream.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise MaterialError("MATERIAL_SIZE_LIMIT")
            return data
        except FileNotFoundError:
            raise MaterialError("MATERIAL_NOT_FOUND") from None
        except PermissionError:
            raise MaterialError("MATERIAL_FORBIDDEN") from None
        except OSError:
            raise MaterialError("MATERIAL_INVALID_PATH") from None
        finally:
            if stream is not None:
                stream.close()
            for fd in reversed(descriptors):
                os.close(fd)


class HTTPSObjectStore:
    """Same-origin static objects only; Downloads redirects are not supported."""

    def __init__(self, base_url, token=None, username=None, opener=None):
        try:
            if (not isinstance(base_url, str) or len(base_url) > 4096
                    or any(ord(c) < 33 or ord(c) == 127 for c in base_url)):
                raise ValueError()
            parsed = urlsplit(base_url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                    or parsed.password is not None or parsed.query or parsed.fragment
                    or "\\" in parsed.netloc):
                raise ValueError()
            parsed.port
            decoded_path = unquote(parsed.path).strip("/")
            if decoded_path:
                relative_path(decoded_path)
            self._base = urlunsplit(("https", parsed.netloc, "/" + quote(decoded_path, safe="/") + "/" if decoded_path else "/", "", ""))
        except (ValueError, UnicodeError):
            raise MaterialError("MATERIAL_INVALID_URL") from None
        _authorization(token, username, "MATERIAL")
        self._token, self._username, self._opener = token, username, opener

    def read(self, path, max_bytes):
        path = relative_path(path)
        return _https_read(self._base + quote(path, safe="/"), token=self._token,
                           username=self._username, opener=self._opener,
                           max_bytes=max_bytes, prefix="MATERIAL")


def _count(value):
    return type(value) is int and 0 <= value <= 2 ** 63 - 1


def _references(values):
    if not isinstance(values, list):
        raise MaterialError("MATERIAL_INVALID_OBJECT")
    seen = set()
    for value in values:
        validate_reference(value)
        if value["sha256"] in seen:
            raise MaterialError("MATERIAL_DUPLICATE_REFERENCE")
        seen.add(value["sha256"])


def _bounded_operation(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._operation():
            return method(self, *args, **kwargs)
    return wrapped


class MaterialReader:
    def __init__(self, store, manifest_path="material.json", expected_repository=None,
                 expected_commit=None, expected_revision=None, check_cancel=None,
                 cache_bytes=32 * 1024 * 1024, max_operation_bytes=64 * 1024 * 1024,
                 max_operation_objects=2048, max_operation_entries=1_000_000):
        if type(cache_bytes) is not int or cache_bytes < 0:
            raise MaterialError("MATERIAL_INVALID_CACHE_LIMIT")
        if any(not _count(value) for value in (max_operation_bytes, max_operation_objects, max_operation_entries)):
            raise MaterialError("MATERIAL_INVALID_OPERATION_LIMIT")
        self._store, self._check_cancel = store, check_cancel
        self._cache, self._cache_size, self._cache_limit = OrderedDict(), 0, cache_bytes
        self._operation_depth = 0
        self._operation_limits = (max_operation_bytes, max_operation_objects, max_operation_entries)
        self._operation_usage = [0, 0, 0]
        self.fetched_bytes, self.fetched_objects = 0, 0
        self._tick()
        raw = store.read(relative_path(manifest_path), MAX_MANIFEST_BYTES)
        self._tick()
        manifest = validate_manifest(decode_json(raw, maximum=MAX_MANIFEST_BYTES))
        self.fetched_bytes += len(raw)
        if expected_repository is not None and manifest["repository"]["identity"] != expected_repository:
            raise MaterialError("MATERIAL_REPOSITORY_MISMATCH")
        if expected_commit is not None and manifest["repository"]["commit"] != valid_commit(expected_commit):
            raise MaterialError("MATERIAL_COMMIT_MISMATCH")
        if expected_revision is not None and manifest["revision"] != valid_hash(expected_revision):
            raise MaterialError("MATERIAL_REVISION_MISMATCH")
        self._manifest = copy.deepcopy(manifest)

    @property
    def manifest(self):
        return copy.deepcopy(self._manifest)

    @property
    def cached_bytes(self):
        return self._cache_size

    @property
    def network_fetched_bytes(self):
        return getattr(self._store, "network_fetched_bytes", self.fetched_bytes)

    @property
    def network_requests(self):
        return getattr(self._store, "network_requests", self.fetched_objects + 1)

    def reset_network_budget(self):
        reset = getattr(self._store, "reset_network_budget", None)
        if reset is not None:
            reset()

    def _tick(self):
        if self._check_cancel:
            self._check_cancel()

    @contextmanager
    def _operation(self):
        if self._operation_depth == 0:
            self._operation_usage = [0, 0, 0]
        self._operation_depth += 1
        try:
            yield
        finally:
            self._operation_depth -= 1

    def _charge(self, *, byte_count=0, objects=0, entries=0):
        usage = [old + new for old, new in zip(self._operation_usage, (byte_count, objects, entries))]
        if any(value > limit for value, limit in zip(usage, self._operation_limits)):
            raise MaterialError("MATERIAL_OPERATION_LIMIT")
        self._operation_usage = usage

    @_bounded_operation
    def read_object(self, ref, kind=None):
        reference = copy.deepcopy(validate_reference(ref))
        self._tick()
        # Cached bytes also count: they still become newly decoded Python objects.
        self._charge(byte_count=reference["size"], objects=1)
        key = reference["sha256"]
        raw = self._cache.get(key)
        if raw is None:
            raw = self._store.read(reference["path"], reference["size"])
            self._tick()
            if not isinstance(raw, bytes):
                raise MaterialError("MATERIAL_INVALID_OBJECT")
            self.fetched_objects += 1
            self.fetched_bytes += len(raw)
            if len(raw) != reference["size"] or len(raw) > MAX_OBJECT_BYTES:
                raise MaterialError("MATERIAL_SIZE_MISMATCH")
            if digest(raw) != key:
                raise MaterialError("MATERIAL_HASH_MISMATCH")
        elif len(raw) != reference["size"]:
            raise MaterialError("MATERIAL_SIZE_MISMATCH")
        value = decode_json(raw)
        if not isinstance(value, dict) or value.get("kind") not in {"files", "terms", "references", "facts", "postings"}:
            raise MaterialError("MATERIAL_INVALID_OBJECT")
        if kind is not None and value["kind"] != kind:
            raise MaterialError("MATERIAL_KIND_MISMATCH")
        if key in self._cache:
            self._cache.move_to_end(key)
        elif len(raw) <= self._cache_limit:
            while self._cache and self._cache_size + len(raw) > self._cache_limit:
                _, previous = self._cache.popitem(last=False)
                self._cache_size -= len(previous)
            self._cache[key] = raw
            self._cache_size += len(raw)
        return value

    def _route(self, key, route):
        self._tick()
        routing = self._manifest["routes"][route]
        reference = routing.get(bucket(key))
        if reference is None:
            return None
        value = self.read_object(reference, kind=route)
        if set(value) != {"kind", "entries"} or not isinstance(value["entries"], dict):
            raise MaterialError("MATERIAL_INVALID_OBJECT")
        if any(not isinstance(entry, str) or not entry or bucket(entry) != bucket(key) for entry in value["entries"]):
            raise MaterialError("MATERIAL_ROUTE_MISMATCH")
        self._charge(entries=len(value["entries"]))
        return value["entries"]

    @_bounded_operation
    def file(self, path):
        path = relative_path(path)
        entries = self._route(path, "files")
        if entries is None:
            return None
        for name, item in entries.items():
            relative_path(name)
            if not isinstance(item, dict) or set(item) != {"relative_path", "sha256", "size", "encoding", "source_format", "line_count", "facts"}:
                raise MaterialError("MATERIAL_INVALID_FILE")
            if item["relative_path"] != name or not _count(item["size"]) or not _count(item["line_count"]):
                raise MaterialError("MATERIAL_INVALID_FILE")
            valid_hash(item["sha256"])
            for field in ("encoding", "source_format"):
                if not isinstance(item[field], str) or not item[field] or len(item[field]) > 128 or any(ord(c) < 32 for c in item[field]):
                    raise MaterialError("MATERIAL_INVALID_FILE")
            _references(item["facts"])
        return copy.deepcopy(entries.get(path))

    @_bounded_operation
    def facts(self, path):
        path = relative_path(path)
        metadata = self.file(path)
        if metadata is None:
            return None
        result = {"kind": "facts", "relative_path": path, "source_sha256": metadata["sha256"],
                  "symbols": [], "relations": [], "rules": []}
        for ref in metadata["facts"]:
            self._tick()
            item = self.read_object(ref, kind="facts")
            if set(item) != set(result):
                raise MaterialError("MATERIAL_INVALID_FACTS")
            if item["relative_path"] != path or item["source_sha256"] != metadata["sha256"]:
                raise MaterialError("MATERIAL_SOURCE_MISMATCH")
            for field in ("symbols", "relations", "rules"):
                if not isinstance(item[field], list) or any(not isinstance(row, dict) for row in item[field]):
                    raise MaterialError("MATERIAL_INVALID_FACTS")
                self._charge(entries=len(item[field]))
                result[field].extend(item[field])
        return result

    @_bounded_operation
    def files(self, limit=20, *, cursor=None):
        """Page the immutable file routes without requiring a lexical seed."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise MaterialError("MATERIAL_INVALID_LIMIT")
        routes = self._manifest["routes"]["files"]
        if cursor is not None:
            if (not isinstance(cursor, dict) or set(cursor) != {"bucket", "after"}
                    or not isinstance(cursor["bucket"], str) or cursor["bucket"] not in routes):
                raise MaterialError("MATERIAL_INVALID_CURSOR")
            relative_path(cursor["after"])
            if bucket(cursor["after"]) != cursor["bucket"]:
                raise MaterialError("MATERIAL_INVALID_CURSOR")
        rows, last, more = [], None, False
        for prefix in sorted(routes):
            if cursor is not None and prefix < cursor["bucket"]:
                continue
            value = self.read_object(routes[prefix], kind="files")
            if set(value) != {"kind", "entries"} or not isinstance(value["entries"], dict):
                raise MaterialError("MATERIAL_INVALID_OBJECT")
            entries = value["entries"]
            self._charge(entries=len(entries))
            for path in entries:
                relative_path(path)
                if bucket(path) != prefix:
                    raise MaterialError("MATERIAL_ROUTE_MISMATCH")
            if cursor is not None and prefix == cursor["bucket"] and cursor["after"] not in entries:
                raise MaterialError("MATERIAL_INVALID_CURSOR")
            for path in sorted(entries):
                if cursor is not None and prefix == cursor["bucket"] and path <= cursor["after"]:
                    continue
                if len(rows) == limit:
                    more = True
                    break
                # Reuse the existing full metadata and content-reference checks.
                rows.append(self.file(path))
                last = {"bucket": prefix, "after": path}
            if more:
                break
        return {"files": rows, "cursor": copy.deepcopy(cursor), "next_cursor": last if more else None,
                "complete": not more}

    @_bounded_operation
    def postings(self, key, route="terms"):
        if route not in {"terms", "references"} or not isinstance(key, str) or not key or len(key) > 1024:
            raise MaterialError("MATERIAL_INVALID_QUERY")
        entries = self._route(key, route)
        if entries is None or key not in entries:
            return []
        descriptor = entries[key]
        if not isinstance(descriptor, dict) or set(descriptor) not in ({"count", "pages"}, {"count", "paths"}) or not _count(descriptor["count"]):
            raise MaterialError("MATERIAL_INVALID_POSTINGS")
        if "paths" in descriptor:
            paths = descriptor["paths"]
            if (not isinstance(paths, list) or len(paths) > INLINE_POSTING_PATHS
                    or len(canonical_json(paths)) > INLINE_POSTING_BYTES):
                raise MaterialError("MATERIAL_INVALID_POSTINGS")
            self._charge(entries=len(paths))
            for path in paths:
                relative_path(path)
            if paths != sorted(set(paths)):
                raise MaterialError("MATERIAL_INVALID_POSTINGS")
            if len(paths) != descriptor["count"]:
                raise MaterialError("MATERIAL_POSTINGS_COUNT_MISMATCH")
            return list(paths)
        _references(descriptor["pages"])
        result = []
        previous = None
        for ref in descriptor["pages"]:
            self._tick()
            page = self.read_object(ref, kind="postings")
            if set(page) != {"kind", "key", "paths"} or page["key"] != key or not isinstance(page["paths"], list):
                raise MaterialError("MATERIAL_INVALID_POSTINGS")
            self._charge(entries=len(page["paths"]))
            for path in page["paths"]:
                relative_path(path)
                if previous is not None and path <= previous:
                    raise MaterialError("MATERIAL_INVALID_POSTINGS")
                result.append(path)
                previous = path
                if len(result) > descriptor["count"]:
                    raise MaterialError("MATERIAL_POSTINGS_COUNT_MISMATCH")
        if len(result) != descriptor["count"]:
            raise MaterialError("MATERIAL_POSTINGS_COUNT_MISMATCH")
        return result
