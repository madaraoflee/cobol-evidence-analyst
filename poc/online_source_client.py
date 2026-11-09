"""Read bounded source bytes from one immutable cloud repository revision."""

from __future__ import annotations

import base64
from http.client import HTTPException
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from online_material_format import MAX_SOURCE_BYTES, MaterialError, relative_path as _relative_path, valid_commit

REQUEST_TIMEOUT_SECONDS = 30
_CHUNK_BYTES = 64 * 1024
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}\Z")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def _authorization(token, username, prefix):
    for value, limit in ((token, 8192), (username, 512)):
        if value is not None and (not isinstance(value, str) or not value or len(value) > limit
                                  or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise MaterialError(prefix + "_INVALID_CREDENTIAL")
    if username is not None and (token is None or ":" in username):
        raise MaterialError(prefix + "_INVALID_CREDENTIAL")
    if token is None:
        return None
    if username is not None:
        try:
            value = base64.b64encode((username + ":" + token).encode("utf-8")).decode("ascii")
        except UnicodeError:
            raise MaterialError(prefix + "_INVALID_CREDENTIAL") from None
        return "Basic " + value
    if not token.isascii() or any(c.isspace() for c in token):
        raise MaterialError(prefix + "_INVALID_CREDENTIAL")
    return "Bearer " + token


def _limit(maximum, prefix):
    if type(maximum) is not int or not 0 <= maximum <= MAX_SOURCE_BYTES:
        raise MaterialError(prefix + "_SIZE_LIMIT")
    return maximum


def _status_error(status, prefix):
    if 300 <= status < 400:
        return MaterialError(prefix + "_REDIRECT")
    return MaterialError(prefix + {401: "_UNAUTHORIZED", 403: "_FORBIDDEN",
                                  404: "_NOT_FOUND", 429: "_RATE_LIMIT"}.get(status, "_HTTP_ERROR"))


def _https_read(url, *, token=None, username=None, opener=None, max_bytes=MAX_SOURCE_BYTES,
                prefix="SOURCE", reject_lfs=False):
    """Injected openers are trusted transports; the default never follows redirects."""
    _limit(max_bytes, prefix)
    authorization = _authorization(token, username, prefix)
    request = Request(url, headers={"Accept": "application/octet-stream"}, method="GET")
    if authorization:
        request.add_header("Authorization", authorization)
    transport = opener if opener is not None else build_opener(_NoRedirect())
    try:
        with transport.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode() if hasattr(response, "getcode") else 200
            if type(status) is not int or status != 200:
                raise _status_error(status, prefix) if type(status) is int else MaterialError(prefix + "_INVALID_RESPONSE")
            final_url = response.geturl() if hasattr(response, "geturl") else url
            if final_url != url:
                raise MaterialError(prefix + "_REDIRECT")
            length = response.headers.get("Content-Length")
            if length is not None:
                if not isinstance(length, str) or len(length) > 20 or not length.isascii() or not length.isdecimal():
                    raise MaterialError(prefix + "_INVALID_RESPONSE")
                length = int(length)
                if length > max_bytes:
                    raise MaterialError(prefix + "_SIZE_LIMIT")
            content_type = response.headers.get("Content-Type", "")
            if not isinstance(content_type, str):
                raise MaterialError(prefix + "_INVALID_RESPONSE")
            content_type = content_type.lower()
            if reject_lfs and "git-lfs" in content_type:
                raise MaterialError("SOURCE_LFS_UNSUPPORTED")
            result = bytearray()
            while True:
                part = getattr(response, "read1", response.read)(min(_CHUNK_BYTES, max_bytes + 1 - len(result)))
                if not isinstance(part, bytes):
                    raise MaterialError(prefix + "_INVALID_RESPONSE")
                if not part:
                    break
                result.extend(part)
                if len(result) > max_bytes:
                    raise MaterialError(prefix + "_SIZE_LIMIT")
            if length is not None and len(result) != length:
                raise MaterialError(prefix + "_LENGTH_MISMATCH")
            data = bytes(result)
            if reject_lfs and data.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"version https://git-lfs.github.com/spec/v1"):
                raise MaterialError("SOURCE_LFS_UNSUPPORTED")
            return data
    except HTTPError as exc:
        code = exc.code
        exc.close()
        raise _status_error(code, prefix) from None
    except (TimeoutError, socket.timeout):
        raise MaterialError(prefix + "_TIMEOUT") from None
    except (URLError, OSError, HTTPException):
        raise MaterialError(prefix + "_CONNECTION_FAILED") from None


class PinnedSourceClient:
    """No branch resolution, cloning, writes, retry, or credential persistence."""

    def __init__(self, workspace, repository, commit, *, token=None, username=None, opener=None):
        if any(not isinstance(value, str) or not _SLUG.fullmatch(value)
               for value in (workspace, repository)):
            raise MaterialError("SOURCE_INVALID_REPOSITORY")
        valid_commit(commit)
        _authorization(token, username, "SOURCE")
        self._base = ("https://api.bitbucket.org/2.0/repositories/" + quote(workspace, safe="")
                      + "/" + quote(repository, safe="") + "/src/" + commit + "/")
        self._token, self._username, self._opener = token, username, opener
        self._commit = commit

    @property
    def commit(self):
        return self._commit

    def read_file(self, relative_path, *, max_bytes=MAX_SOURCE_BYTES):
        path = _relative_path(relative_path)
        return _https_read(self._base + quote(path, safe="/"), token=self._token,
                           username=self._username, opener=self._opener,
                           max_bytes=max_bytes, reject_lfs=True)
