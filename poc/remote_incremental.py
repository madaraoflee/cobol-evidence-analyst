"""Create a new cloud snapshot from verified local files and pinned remote changes."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import stat
import unicodedata
from urllib.parse import quote, urlsplit, parse_qs

from remote_source import (CHUNK_BYTES, MAX_EXTRACTED_BYTES, MAX_FILES,
    MAX_ZIP_DIRECTORY_BYTES, RemoteSourceError, _archive_parts, _safe_parts,
    _commit, _read_metadata, _request, parse_remote_source)


SCHEMA = "remote-file-manifest/v1"


def _invalid():
    return RemoteSourceError("REMOTE_SOURCE_INVALID_RESPONSE", "仓库返回的文件或变更清单不完整，请检查来源后重试。")


def _identity(spec):
    return [spec.provider, spec.base_url, spec.project, spec.repository, spec.subdirectory]


def manifest_path(source):
    source = Path(source)
    return source.with_name(source.name + ".remote-manifest.json")


def _selected(spec, path):
    parts = _archive_parts(path)
    prefix = tuple(filter(None, spec.subdirectory.split("/")))
    if parts[:len(prefix)] != prefix or len(parts) <= len(prefix):
        return None
    relative = "/".join(parts[len(prefix):])
    _safe_parts(relative)
    return relative


def _validate_plan(files):
    if not files:
        raise RemoteSourceError("REMOTE_SOURCE_EMPTY", "所选目录没有可下载的文件，请检查分支和目录。")
    if len(files) > MAX_FILES:
        raise RemoteSourceError("REMOTE_SOURCE_TOO_LARGE", "所选源码目录超过文件或容量限制。")
    seen, size = {}, 0
    for path, entry in files.items():
        parts = _safe_parts(path)
        for length in range(1, len(parts) + 1):
            prefix = parts[:length]
            key = "/".join(unicodedata.normalize("NFC", value).casefold() for value in prefix)
            value = (prefix, length < len(parts))
            if key in seen and seen[key] != value:
                raise ValueError("paths collide across filesystems")
            seen[key] = value
        if not isinstance(entry, dict):
            raise ValueError("invalid file record")
        count = entry.get("size", 0)
        if type(count) is not int or count < 0:
            raise ValueError("invalid file size")
        size += count
    if size > MAX_EXTRACTED_BYTES:
        raise RemoteSourceError("REMOTE_SOURCE_TOO_LARGE", "所选源码目录超过文件或容量限制。")


def _regular_path(source, relative):
    current = Path(source)
    if current.is_symlink() or not current.is_dir():
        return None
    parts = _safe_parts(relative)
    for number, part in enumerate(parts):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError:
            return None
        if not (stat.S_ISREG(mode) if number == len(parts) - 1 else stat.S_ISDIR(mode)):
            return None
    return current


def write_manifest(source, spec, commit, tick, files=None):
    """Record download-time bytes outside the indexed source tree."""
    source = Path(source)
    if files is None:
        files = {}
        for path in sorted(source.rglob("*")):
            tick()
            if path.is_dir() and not path.is_symlink():
                continue
            relative = path.relative_to(source).as_posix()
            if _regular_path(source, relative) is None:
                raise _invalid()
            digest, size = hashlib.sha256(), 0
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
                    tick()
                    digest.update(chunk)
                    size += len(chunk)
            files[relative] = {"sha256": digest.hexdigest(), "size": size}
    _validate_plan(files)
    payload = json.dumps({"schema_version": SCHEMA, "identity": _identity(spec),
        "commit": commit, "files": files}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(payload) > MAX_ZIP_DIRECTORY_BYTES:
        raise RemoteSourceError("REMOTE_SOURCE_TOO_LARGE", "所选源码目录的文件清单超过容量限制。")
    with manifest_path(source).open("xb") as stream:
        stream.write(payload)
    return files


def _pages(opener, url, token, username, tick):
    """Follow only same endpoint pages, never remote supplied credential targets."""
    expected = urlsplit(url)
    query = parse_qs(expected.query, keep_blank_values=True)
    # The API defines next as opaque: it may omit default parameters or use
    # a cursor instead of page. Only explicit changes to collection semantics
    # are rejected; page-size and representation parameters may be normalized.
    collection_parameters = {"topic", "renames", "merge", "ignore_whitespace", "path", "q", "sort", "max_depth", "format"}
    seen = set()
    count, expected_count = 0, None
    while url:
        tick()
        if len(url) > 8192 or any(ord(char) < 32 or ord(char) == 127 for char in url):
            raise _invalid()
        current = urlsplit(url)
        current_query = parse_qs(current.query, keep_blank_values=True, max_num_fields=64)
        if (url in seen or current.scheme != expected.scheme or current.netloc != expected.netloc
                or current.path != expected.path or current.username is not None or current.fragment
                or any(key in current_query and current_query[key] != query.get(key)
                       for key in collection_parameters)):
            raise _invalid()
        seen.add(url)
        if len(seen) > MAX_FILES:
            raise _invalid()
        response = _read_metadata(opener, url, token, username, tick)
        values = response.get("values")
        if not isinstance(values, list):
            raise _invalid()
        if "size" in response:
            size = response["size"]
            if (type(size) is not int or not 0 <= size <= MAX_FILES
                    or expected_count is not None and size != expected_count):
                raise _invalid()
            expected_count = size
        count += len(values)
        if count > MAX_FILES:
            raise RemoteSourceError("REMOTE_SOURCE_TOO_LARGE", "仓库变更清单超过容量限制。")
        yield from values
        following = response.get("next")
        if following is not None and (not isinstance(following, str) or not following or not values):
            raise _invalid()
        url = following
    if expected_count is not None and count != expected_count:
        raise _invalid()


def _file_metadata(item, path, commit):
    if (not isinstance(item, dict) or item.get("type") != "commit_file" or item.get("path") != path
            or _commit((item.get("commit") or {}).get("hash")) != commit
            or type(item.get("size")) is not int or not 0 <= item["size"] <= MAX_EXTRACTED_BYTES
            or not isinstance(item.get("attributes"), list)
            or any(value not in {"executable", "binary"} for value in item["attributes"])):
        raise _invalid()
    return {"size": item["size"]}


def _legacy_hashes(output, source):
    """Only hashes recorded for this source's earliest indexed version qualify."""
    if output is None:
        return {}
    db_path = Path(output) / "source-versions.sqlite"
    if any(Path(str(db_path) + suffix).is_symlink() for suffix in ("", "-wal", "-shm", "-journal")):
        return {}
    if not db_path.is_file():
        return {}
    try:
        with closing(sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)) as db:
            source_key = hashlib.sha256(str(Path(source).resolve()).encode("utf-8")).hexdigest()
            row = db.execute("SELECT manifest_json FROM source_versions WHERE source_key=? ORDER BY revision LIMIT 1",
                             (source_key,)).fetchone()
        value = json.loads(row[0]) if row and len(row[0]) <= MAX_ZIP_DIRECTORY_BYTES else {}
        if not isinstance(value, dict):
            return {}
        return {name: digest for name, digest in value.items() if isinstance(name, str)
                and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)}
    except (sqlite3.Error, ValueError, TypeError, OSError):
        return {}


def _saved_manifest(spec, old_commit, source):
    sidecar = manifest_path(source)
    if sidecar.exists() or sidecar.is_symlink():
        if sidecar.is_symlink() or sidecar.stat().st_size > MAX_ZIP_DIRECTORY_BYTES:
            raise _invalid()
        try:
            value = json.loads(sidecar.read_text(encoding="utf-8"))
            if (value.get("schema_version") != SCHEMA or value.get("identity") != _identity(spec)
                    or value.get("commit") != old_commit or not isinstance(value.get("files"), dict)):
                raise ValueError("invalid snapshot manifest")
            files = value["files"]
            _validate_plan(files)
            if any(not isinstance(entry.get("sha256"), str)
                   or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) for entry in files.values()):
                raise ValueError("invalid file hash")
            return files
        except (ValueError, TypeError, AttributeError, OSError):
            raise _invalid() from None
    return None


def cached_snapshot_matches(spec, source, metadata, *, output=None, check_cancel=None, progress=None):
    """Explicit update checks bytes; ordinary offline opening never calls this."""
    tick = check_cancel or (lambda: None)
    source = Path(source)
    try:
        commit = _commit(metadata.get("commit"))
        files = _saved_manifest(spec, commit, source)
        if files is None:
            hashes = _legacy_hashes(output, source)
            if not hashes:
                return False
            files = {name: {"sha256": value} for name, value in hashes.items()}
        if not files or len(files) > MAX_FILES or source.is_symlink() or not source.is_dir():
            return False
        if progress:
            progress({"phase": "remote_verifying", "completed": 0, "total": len(files), "unit": "files"})
        actual = set()
        for path in source.rglob("*"):
            tick()
            if path.is_symlink():
                return False
            if path.is_file():
                actual.add(path.relative_to(source).as_posix())
                if len(actual) > MAX_FILES:
                    return False
        if actual != set(files):
            return False
        total_bytes = 0
        for number, (relative, entry) in enumerate(files.items(), 1):
            tick()
            path = _regular_path(source, relative)
            if path is None:
                return False
            size, digest = 0, hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
                    tick()
                    size += len(chunk)
                    total_bytes += len(chunk)
                    if total_bytes > MAX_EXTRACTED_BYTES or "size" in entry and size > entry["size"]:
                        return False
                    digest.update(chunk)
            if digest.hexdigest() != entry["sha256"] or "size" in entry and size != entry["size"]:
                return False
            if progress:
                progress({"phase": "remote_verifying", "completed": number, "total": len(files), "unit": "files"})
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _baseline(spec, old_commit, source, output, opener, token, username, tick):
    files = _saved_manifest(spec, old_commit, source)
    if files is not None:
        return files
    # Older releases did not save a full download manifest. Enumerate only
    # metadata at their pinned revision, and reuse independently indexed bytes.
    hashes = _legacy_hashes(output, source)
    directories, seen, files = [spec.subdirectory], set(), {}
    while directories:
        directory = directories.pop()
        if directory in seen or len(seen) > MAX_FILES:
            raise _invalid()
        seen.add(directory)
        url = spec.api_url + "/src/" + old_commit + "/" + quote(directory, safe="/")
        if not url.endswith("/"):
            url += "/"
        for entry in _pages(opener, url + "?pagelen=100", token, username, tick):
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise _invalid()
            path = entry["path"]
            parts = _archive_parts(path)
            if "/".join(parts[:-1]) != directory or _commit((entry.get("commit") or {}).get("hash")) != old_commit:
                raise _invalid()
            relative = _selected(spec, path)
            if relative is None:
                raise _invalid()
            if entry.get("type") == "commit_directory":
                directories.append(path)
            else:
                if relative in files:
                    raise _invalid()
                files[relative] = _file_metadata(entry, path, old_commit)
                if relative in hashes:
                    files[relative]["sha256"] = hashes[relative]
                if len(files) > MAX_FILES:
                    raise _invalid()
    _validate_plan(files)
    return files


def fetch_incremental(spec, destination, commit, *, previous_source, previous_metadata,
                      previous_output, opener, token, username, tick, emit):
    """No archive fallback: an incomplete change listing must leave the old snapshot intact."""
    old_spec = parse_remote_source(previous_metadata.get("url"), ref=previous_metadata.get("ref"),
                                   subdirectory=previous_metadata.get("subdirectory"))
    if _identity(old_spec) != _identity(spec):
        raise _invalid()
    old_commit = _commit(previous_metadata.get("commit"))
    previous_source = Path(previous_source)
    files = _baseline(spec, old_commit, previous_source, previous_output, opener, token, username, tick)
    changes, removed = {}, set()
    if old_commit != commit:
        url = spec.api_url + "/diffstat/" + commit + ".." + old_commit + "?topic=false&renames=true&pagelen=100"
        for entry in _pages(opener, url, token, username, tick):
            if not isinstance(entry, dict) or entry.get("status") not in {"added", "removed", "modified", "renamed"}:
                raise _invalid()
            old, new = entry.get("old"), entry.get("new")
            if old is not None and (not isinstance(old, dict) or old.get("type") != "commit_file"):
                raise _invalid()
            if new is not None and (not isinstance(new, dict) or new.get("type") != "commit_file"):
                raise _invalid()
            status = entry["status"]
            if (status == "added" and (old is not None or new is None)
                    or status == "removed" and (new is not None or old is None)
                    or status in {"modified", "renamed"} and (old is None or new is None)):
                raise _invalid()
            old_path = _selected(spec, old["path"]) if old else None
            new_path = _selected(spec, new["path"]) if new else None
            if old_path:
                if old_path not in files or old_path in removed:
                    raise _invalid()
                removed.add(old_path)
            if new_path:
                if new_path in changes:
                    raise _invalid()
                changes[new_path] = {"size": 0}
    target = {name: entry for name, entry in files.items() if name not in removed}
    for name, entry in changes.items():
        if name in target:
            raise _invalid()
        target[name] = entry
    _validate_plan(target)
    complete, downloaded, reused, total_bytes = {}, 0, 0, 0
    network_bytes = 0
    for number, (name, entry) in enumerate(sorted(target.items()), 1):
        tick()
        emit("remote_syncing", number - 1, len(target), "files", network_bytes, None)
        output = destination.joinpath(*_safe_parts(name))
        output.parent.mkdir(parents=True, exist_ok=True)
        cached = _regular_path(previous_source, name) if name not in changes and entry.get("sha256") else None
        digest, size = hashlib.sha256(), 0
        copied = False
        if cached is not None:
            with cached.open("rb") as stream, output.open("xb") as sink:
                for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
                    tick()
                    digest.update(chunk)
                    size += len(chunk)
                    if size > entry["size"]:
                        break
                    sink.write(chunk)
            copied = size == entry["size"] and digest.hexdigest() == entry["sha256"]
            if not copied:
                output.unlink()
        if copied:
            reused += 1
        else:
            raw_path = "/".join(filter(None, (spec.subdirectory, name)))
            url = spec.api_url + "/src/" + commit + "/" + quote(raw_path, safe="/")
            metadata = _file_metadata(_read_metadata(opener, url + "?format=meta", token, username, tick), raw_path, commit)
            size, digest = 0, hashlib.sha256()
            with _request(opener, url, token, "application/octet-stream", username) as response, output.open("xb") as sink:
                while True:
                    tick()
                    chunk = getattr(response, "read1", response.read)(CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    network_bytes += len(chunk)
                    if size > metadata["size"] or total_bytes + size > MAX_EXTRACTED_BYTES:
                        raise _invalid()
                    digest.update(chunk)
                    sink.write(chunk)
            if size != metadata["size"]:
                raise _invalid()
            downloaded += 1
        total_bytes += size
        if total_bytes > MAX_EXTRACTED_BYTES:
            raise _invalid()
        complete[name] = {"size": size, "sha256": digest.hexdigest()}
        emit("remote_syncing", number, len(target), "files", network_bytes, None)
    write_manifest(destination, spec, commit, tick, complete)
    return {"file_count": len(complete), "sync_mode": "incremental", "downloaded_files": downloaded,
            "reused_files": reused, "removed_files": len(set(files) - set(target)), "downloaded_bytes": network_bytes}
