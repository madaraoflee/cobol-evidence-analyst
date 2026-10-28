"""Read a pinned repository snapshot into a new, isolated local source folder."""

from __future__ import annotations

from dataclasses import dataclass
from http.client import HTTPException
import json
from pathlib import Path
import re
import shutil
import socket
import ssl
import stat
import struct
import sys
import tempfile
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile


MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_EXTRACTED_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILES = 100_000
MAX_METADATA_BYTES = 1024 * 1024
MAX_ZIP_DIRECTORY_BYTES = 32 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30
TRANSFER_TIMEOUT_SECONDS = 900
CHUNK_BYTES = 64 * 1024
_RESERVED_NAME = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", re.I)


class RemoteSourceError(ValueError):
    """Safe public error; remote response bodies and credentials are never included."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _invalid_url():
    return RemoteSourceError("REMOTE_SOURCE_INVALID_URL", "请提供 HTTPS 仓库或目录链接，链接中不要包含账号、密码或令牌。")


def _has_controls(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _safe_parts(value: str) -> tuple[str, ...]:
    """Require names with the same meaning on Windows, macOS and POSIX."""
    if not value or len(value) > 4096 or value.startswith("/") or "\\" in value or _has_controls(value):
        raise ValueError("invalid relative path")
    parts = tuple(value.split("/"))
    for part in parts:
        if (not part or part in {".", ".."} or part.endswith((".", " "))
                or any(char in '<>:"|?*' for char in part) or _RESERVED_NAME.fullmatch(part)):
            raise ValueError("invalid relative path")
    return parts


def _reference(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if (not isinstance(value, str) or len(value) > 1024 or value != value.strip()
            or _has_controls(value) or any(char in value for char in "~^:?*[\\")
            or ".." in value or "@{" in value or value.startswith(("/", "-"))
            or value.endswith(("/", ".")) or "//" in value or " " in value
            or any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))):
        raise RemoteSourceError("REMOTE_SOURCE_INVALID_REF", "分支、标签或提交编号格式无效。")
    return value


def _subdirectory(value: str | None) -> str:
    if value is None or value == "":
        return ""
    try:
        if not isinstance(value, str):
            raise ValueError("invalid path")
        return "/".join(_safe_parts(value.rstrip("/")))
    except ValueError:
        raise RemoteSourceError("REMOTE_SOURCE_INVALID_PATH", "目录须为仓库内的相对路径，不能包含上级目录或绝对路径。") from None


@dataclass(frozen=True)
class RemoteSourceSpec:
    base_url: str
    project: str
    repository: str
    ref: str | None = None
    subdirectory: str = ""

    @property
    def api_url(self) -> str:
        return (f"{self.base_url}/rest/api/1.0/projects/{quote(self.project, safe='')}/repos/"
                f"{quote(self.repository, safe='')}")

    def public_metadata(self) -> dict:
        url = (f"{self.base_url}/projects/{quote(self.project, safe='')}/repos/"
               f"{quote(self.repository, safe='')}/browse")
        if self.subdirectory:
            url += "/" + quote(self.subdirectory, safe="/")
        if self.ref:
            url += "?" + urlencode({"at": self.ref})
        return {"provider": "bitbucket", "url": url, "ref": self.ref,
                "subdirectory": self.subdirectory}


def parse_remote_source(url: str, *, ref: str | None = None,
                        subdirectory: str | None = None) -> RemoteSourceSpec:
    """Accept Data Center browser and HTTPS clone links, including a context path."""
    if (not isinstance(url, str) or not url or len(url) > 8192 or url != url.strip()
            or _has_controls(url) or "\\" in url):
        raise _invalid_url()
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
        if parts.scheme != "https" or not host or parts.username is not None or parts.password is not None or parts.fragment:
            raise ValueError("invalid URL")
        if host.lower().rstrip(".") in {"bitbucket.org", "www.bitbucket.org", "api.bitbucket.org"}:
            raise RemoteSourceError("REMOTE_SOURCE_UNSUPPORTED", "当前入口支持 Bitbucket Data Center / Server，暂不支持 Bitbucket Cloud 链接。")
        if any(char.isspace() for char in host) or "%" in host:
            raise ValueError("invalid host")
        host = host.encode("idna").decode("ascii").lower()
        authority = f"[{host}]" if ":" in host else host
        if port is not None:
            authority += f":{port}"
        # Decode per component so an encoded slash cannot change route structure.
        encoded_parts = parts.path.strip("/").split("/")
        route = tuple(unquote(part, encoding="utf-8", errors="strict") for part in encoded_parts)
        if any("/" in part for part in route):
            raise ValueError("encoded path separator")
        _safe_parts("/".join(route))
        candidates = [index for index, part in enumerate(route) if part == "projects"
                      and len(route) >= index + 4 and route[index + 2] == "repos"]
        browse_path = ""
        if candidates:
            index = candidates[-1]
            project, repository = route[index + 1], route[index + 3]
            tail = route[index + 4:]
            if tail and tail[0] != "browse":
                raise ValueError("unsupported route")
            browse_path = "/".join(tail[1:])
        elif len(route) >= 3 and route[-3] == "scm" and route[-1].endswith(".git"):
            index = len(route) - 3
            project, repository = route[-2], route[-1][:-4]
            _safe_parts(repository)
        else:
            raise ValueError("unsupported route")
        if re.fullmatch(r"[A-Za-z0-9_~.-]+", project) is None or re.fullmatch(r"[A-Za-z0-9_.-]+", repository) is None:
            raise ValueError("invalid repository identity")
        query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
        if set(query) - {"at"} or any(len(values) != 1 for values in query.values()):
            raise ValueError("unsupported query")
        chosen_ref = _reference(ref if ref not in (None, "") else query.get("at", [None])[0])
        chosen_path = _subdirectory(subdirectory if subdirectory not in (None, "") else browse_path)
        context = "/".join(quote(part, safe="") for part in route[:index])
        base = "https://" + authority + ("/" + context if context else "")
        return RemoteSourceSpec(base, project, repository, chosen_ref, chosen_path)
    except RemoteSourceError:
        raise
    except (ValueError, UnicodeError):
        raise _invalid_url() from None


def validate_remote_token(token: str | None) -> str | None:
    if token is None or token == "":
        return None
    if (not isinstance(token, str) or len(token) > 8192
            or any(ord(char) < 33 or ord(char) > 126 for char in token)):
        raise RemoteSourceError("REMOTE_SOURCE_INVALID_TOKEN", "访问令牌格式无效，请重新输入只读访问令牌。")
    return token


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        if response is not None:
            response.close()
        raise RemoteSourceError("REMOTE_SOURCE_REDIRECT", "仓库服务返回登录或跳转页面。请使用仓库的最终 HTTPS 地址和只读访问令牌。")


def _request(opener, url, token, accept):
    headers = {"Accept": accept, "Accept-Encoding": "identity", "User-Agent": "SourceWorkbench/1.0"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(url, headers=headers, method="GET")
    try:
        response = opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS)
    except HTTPError as exc:
        status = exc.code
        exc.close()
        if status in {401, 403}:
            raise RemoteSourceError("REMOTE_SOURCE_AUTH_FAILED", "仓库访问被拒绝，请检查令牌和仓库读取权限。") from None
        if status == 404:
            raise RemoteSourceError("REMOTE_SOURCE_NOT_FOUND", "未找到仓库、分支或目录，请检查链接和读取权限。") from None
        if 300 <= status < 400:
            raise RemoteSourceError("REMOTE_SOURCE_REDIRECT", "仓库服务要求跳转，请使用最终 HTTPS 地址和只读访问令牌。") from None
        raise RemoteSourceError("REMOTE_SOURCE_NETWORK_FAILED", "仓库服务暂时无法完成下载，请稍后重试。") from None
    if getattr(response, "status", 200) != 200:
        response.close()
        raise RemoteSourceError("REMOTE_SOURCE_INVALID_RESPONSE", "仓库服务未返回有效的源码响应。")
    return response


def _network_error(exc):
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, ssl.SSLError):
        return RemoteSourceError("REMOTE_SOURCE_TLS_FAILED", "无法验证仓库的 HTTPS 证书，请配置受信任的企业证书。")
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return RemoteSourceError("REMOTE_SOURCE_TIMEOUT", "仓库连接或下载超时，请检查网络后重试。")
    return RemoteSourceError("REMOTE_SOURCE_NETWORK_FAILED", "无法连接仓库，请检查地址、网络或企业 VPN。")


def _check_zip_directory(archive, tick):
    """Bound central-directory allocation before ZipFile creates its entry objects."""
    archive.seek(0, 2)
    size = archive.tell()
    archive.seek(max(0, size - 65557))
    tail = archive.read(65557)
    offset = tail.rfind(b"PK\x05\x06")
    if offset < 0 or offset + 22 > len(tail):
        raise ValueError("missing ZIP footer")
    _, disk, directory_disk, disk_count, count, directory_size, directory_offset, comment_size = struct.unpack(
        "<4s4H2LH", tail[offset:offset + 22])
    footer_offset = size - len(tail) + offset
    if disk or directory_disk or offset + 22 + comment_size != len(tail):
        raise ValueError("unsupported ZIP footer")
    locator = b""
    if footer_offset >= 20:
        archive.seek(footer_offset - 20)
        locator = archive.read(20)
    if locator.startswith(b"PK\x06\x07"):
        signature, disk, record_offset, disk_total = struct.unpack("<4sLQL", locator)
        if signature != b"PK\x06\x07" or disk or disk_total != 1 or record_offset + 56 > footer_offset - 20:
            raise ValueError("invalid ZIP64 locator")
        archive.seek(record_offset)
        signature, record_size, _, _, disk, directory_disk, disk_count, count, directory_size, directory_offset = struct.unpack(
            "<4sQ2H2L4Q", archive.read(56))
        if (signature != b"PK\x06\x06" or disk or directory_disk or record_size < 44
                or record_offset + record_size + 12 != footer_offset - 20):
            raise ValueError("invalid ZIP64 footer")
        footer_offset = record_offset
    elif directory_size == 0xFFFFFFFF or directory_offset == 0xFFFFFFFF:
        raise ValueError("missing ZIP64 locator")
    if disk_count != count or directory_offset + directory_size != footer_offset:
        raise ValueError("invalid ZIP directory bounds")
    if count > MAX_FILES or directory_size > MAX_ZIP_DIRECTORY_BYTES:
        raise RemoteSourceError("REMOTE_SOURCE_TOO_LARGE", "仓库快照超过下载或解压限制，请选择更小的源码目录。")
    archive.seek(directory_offset)
    observed = 0
    while archive.tell() < footer_offset:
        tick()
        header = archive.read(46)
        if len(header) != 46 or header[:4] != b"PK\x01\x02":
            raise ValueError("invalid ZIP directory entry")
        name_size, extra_size, entry_comment_size = struct.unpack("<3H", header[28:34])
        observed += 1
        if observed > MAX_FILES or name_size > 4096:
            raise RemoteSourceError("REMOTE_SOURCE_TOO_LARGE", "仓库快照超过下载或解压限制，请选择更小的源码目录。")
        archive.seek(name_size + extra_size + entry_comment_size, 1)
    if observed != count or archive.tell() != footer_offset:
        raise ValueError("invalid ZIP directory size")
    archive.seek(0)


def _extract_archive(archive, destination, specification, tick, emit):
    _check_zip_directory(archive, tick)
    expected_prefix = ("source",) + tuple(filter(None, specification.subdirectory.split("/")))
    with zipfile.ZipFile(archive) as contents:
        entries = contents.infolist()
        if len(entries) > MAX_FILES:
            raise RemoteSourceError("REMOTE_SOURCE_TOO_LARGE", "仓库快照超过下载或解压限制，请选择更小的源码目录。")
        files, names, path_kinds, total_bytes = [], set(), {}, 0
        for entry in entries:
            tick()
            raw_name = entry.orig_filename
            is_directory = raw_name.endswith("/")
            parts = _safe_parts(raw_name[:-1] if is_directory else raw_name)
            mode = (entry.external_attr >> 16) & 0xFFFF
            kind = stat.S_IFMT(mode)
            if kind not in {0, stat.S_IFREG, stat.S_IFDIR} or (kind == stat.S_IFDIR and not is_directory):
                raise ValueError("archive contains unsupported file types")
            if entry.flag_bits & 1:
                raise ValueError("encrypted archive")
            if is_directory and parts == expected_prefix[:len(parts)]:
                continue
            if parts[:len(expected_prefix)] != expected_prefix or len(parts) <= len(expected_prefix):
                raise ValueError("archive content outside selected directory")
            relative = parts[len(expected_prefix):]
            key = "/".join(unicodedata.normalize("NFC", part).casefold() for part in relative)
            if key in names:
                raise ValueError("duplicate archive path")
            names.add(key)
            for length in range(1, len(relative) + 1):
                prefix = relative[:length]
                prefix_key = "/".join(unicodedata.normalize("NFC", part).casefold() for part in prefix)
                directory = length < len(relative) or is_directory
                existing = path_kinds.get(prefix_key)
                if existing is not None and existing != (prefix, directory):
                    raise ValueError("archive paths collide across filesystems")
                path_kinds[prefix_key] = (prefix, directory)
            if not is_directory:
                total_bytes += entry.file_size
                if total_bytes > MAX_EXTRACTED_BYTES:
                    raise RemoteSourceError("REMOTE_SOURCE_TOO_LARGE", "仓库快照超过下载或解压限制，请选择更小的源码目录。")
                files.append((entry, relative))
        if not files:
            raise RemoteSourceError("REMOTE_SOURCE_EMPTY", "所选目录没有可下载的文件，请检查分支和目录。")
        completed_bytes = 0
        emit("remote_extracting", 0, len(files), "files", 0, total_bytes)
        for completed, (entry, relative) in enumerate(files, 1):
            tick()
            target = destination.joinpath(*relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            with contents.open(entry) as source, target.open("xb") as output:
                file_bytes = 0
                while True:
                    tick()
                    chunk = source.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    file_bytes += len(chunk)
                    completed_bytes += len(chunk)
                    if file_bytes > entry.file_size or completed_bytes > MAX_EXTRACTED_BYTES:
                        raise ValueError("invalid archive size")
                    output.write(chunk)
            emit("remote_extracting", completed, len(files), "files", completed_bytes, total_bytes)
        return len(files)


def fetch_remote_source(spec: RemoteSourceSpec, destination: Path, *, token: str | None = None,
                        progress=None, check_cancel=None, opener=None) -> dict:
    """Download a pinned commit, preserving no credentials or repository executable hooks.

    The caller owns the destination's parent. An existing destination is never
    reused, overwritten or removed. Only a directory created by this call is
    cleaned up if download, extraction or cancellation fails.
    """
    token = validate_remote_token(token)
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise RemoteSourceError("REMOTE_SOURCE_DESTINATION_EXISTS", "下载位置已存在，请使用新的独立源码目录。")
    # Revalidate even when a caller constructs the dataclass itself.
    canonical = spec.public_metadata()
    if parse_remote_source(canonical["url"]) != spec:
        raise _invalid_url()
    started = time.monotonic()

    def tick():
        if check_cancel:
            check_cancel()
        if time.monotonic() - started > TRANSFER_TIMEOUT_SECONDS:
            raise RemoteSourceError("REMOTE_SOURCE_TIMEOUT", "仓库下载超时，请选择更小的源码目录后重试。")

    def emit(phase, completed=0, total=None, unit="files", byte_count=0, byte_total=None):
        tick()
        if progress:
            progress({"phase": phase, "completed": completed, "total": total, "unit": unit,
                      "bytes_completed": byte_count, "bytes_total": byte_total})

    created = False
    try:
        destination.mkdir(mode=0o700)
        created = True
        opener = opener if opener is not None else build_opener(_NoRedirect())
        emit("remote_resolving", 0, 1)
        query = {"limit": "1"}
        if spec.ref:
            query["until"] = spec.ref
        with _request(opener, spec.api_url + "/commits?" + urlencode(query), token, "application/json") as response:
            metadata = bytearray()
            while True:
                tick()
                chunk = getattr(response, "read1", response.read)(min(CHUNK_BYTES, MAX_METADATA_BYTES + 1 - len(metadata)))
                if not chunk:
                    break
                metadata.extend(chunk)
                if len(metadata) > MAX_METADATA_BYTES:
                    raise RemoteSourceError("REMOTE_SOURCE_INVALID_RESPONSE", "仓库返回的提交信息无效。")
        try:
            values = json.loads(metadata)["values"]
            if not isinstance(values, list):
                raise ValueError("invalid commits")
            if not values:
                raise RemoteSourceError("REMOTE_SOURCE_NOT_FOUND", "所选仓库或分支没有可下载的提交。")
            commit = values[0]["id"]
            if not isinstance(commit, str) or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", commit) is None:
                raise ValueError("invalid commit")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            if isinstance(exc, RemoteSourceError):
                raise
            raise RemoteSourceError("REMOTE_SOURCE_INVALID_RESPONSE", "仓库返回的提交信息无效。") from None
        emit("remote_resolving", 1, 1)
        query = {"at": commit, "format": "zip", "prefix": "source/"}
        if spec.subdirectory:
            query["path"] = spec.subdirectory
        with tempfile.TemporaryFile(mode="w+b", dir=destination.parent) as archive:
            with _request(opener, spec.api_url + "/archive?" + urlencode(query), token, "application/octet-stream") as response:
                length = response.headers.get("Content-Length")
                total = int(length) if length and length.isdecimal() else None
                if total is not None and total > MAX_ARCHIVE_BYTES:
                    raise RemoteSourceError("REMOTE_SOURCE_TOO_LARGE", "仓库快照超过下载或解压限制，请选择更小的源码目录。")
                downloaded = 0
                emit("remote_downloading", 0, total, "bytes", 0, total)
                while True:
                    tick()
                    chunk = getattr(response, "read1", response.read)(CHUNK_BYTES)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > MAX_ARCHIVE_BYTES:
                        raise RemoteSourceError("REMOTE_SOURCE_TOO_LARGE", "仓库快照超过下载或解压限制，请选择更小的源码目录。")
                    archive.write(chunk)
                    emit("remote_downloading", downloaded, total, "bytes", downloaded, total)
                if total is not None and downloaded != total:
                    raise RemoteSourceError("REMOTE_SOURCE_INVALID_ARCHIVE", "源码下载不完整，请重试。")
            archive.seek(0)
            try:
                file_count = _extract_archive(archive, destination, spec, tick, emit)
            except RemoteSourceError:
                raise
            except (ValueError, zipfile.BadZipFile, NotImplementedError, EOFError,
                    FileExistsError, NotADirectoryError):
                raise RemoteSourceError("REMOTE_SOURCE_INVALID_ARCHIVE", "仓库未返回有效的源码 ZIP，或其中包含无法安全读取的文件。") from None
        tick()
        return {**canonical, "commit": commit.lower(), "file_count": file_count}
    except (URLError, TimeoutError, ssl.SSLError, ConnectionError, HTTPException) as exc:
        raise _network_error(exc) from None
    finally:
        # A successful return has a complete extraction and no active exception.
        if created and sys.exc_info()[0] is not None:
            shutil.rmtree(destination, ignore_errors=True)
