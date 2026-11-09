"""Portable, read-only material contracts, independent of local workspaces."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata

SCHEMA_VERSION = "online-analysis/v1.1"
SUPPORTED_SCHEMA_VERSIONS = {"online-analysis/v1", SCHEMA_VERSION}
INLINE_POSTING_PATHS = 16
INLINE_POSTING_BYTES = 4096
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_OBJECT_BYTES = 8 * 1024 * 1024
MAX_SOURCE_BYTES = 32 * 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_BUCKET = re.compile(r"[0-9a-f]{2}\Z")
_WORDS = re.compile(r"[\w$#@]+(?:-[\w$#@]+)*", re.UNICODE)


class MaterialError(ValueError):
    """A bounded diagnostic code; never include a remote response or credential."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def canonical_json(value) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise MaterialError("MATERIAL_INVALID_JSON") from None


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MaterialError("MATERIAL_DUPLICATE_KEY")
        result[key] = value
    return result


def decode_json(data: bytes, *, maximum=MAX_OBJECT_BYTES):
    if not isinstance(data, bytes) or len(data) > maximum:
        raise MaterialError("MATERIAL_SIZE_LIMIT")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        # JSON permits escaped isolated surrogates; portable paths and object
        # keys must still be encodable as UTF-8 before downstream hashing.
        def validate_strings(item):
            if isinstance(item, str):
                item.encode("utf-8")
            elif isinstance(item, dict):
                for key, child in item.items():
                    validate_strings(key)
                    validate_strings(child)
            elif isinstance(item, list):
                for child in item:
                    validate_strings(child)
        if b"\\u" in data:
            validate_strings(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise MaterialError("MATERIAL_INVALID_JSON") from None


def valid_hash(value) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise MaterialError("MATERIAL_INVALID_HASH")
    return value


def valid_commit(value) -> str:
    if not isinstance(value, str) or not _COMMIT.fullmatch(value):
        raise MaterialError("MATERIAL_INVALID_COMMIT")
    return value


def relative_path(value, *, allow_empty=False) -> str:
    if value == "" and allow_empty:
        return value
    if (not isinstance(value, str) or not value or len(value) > 4096
            or value.startswith("/") or "\\" in value or ":" in value
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
            or any(p in {"", ".", ".."} for p in value.split("/"))):
        raise MaterialError("MATERIAL_INVALID_PATH")
    return value


def normalized_term(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def tokens(text: str) -> set[str]:
    result = set()
    for value in _WORDS.findall(unicodedata.normalize("NFKC", text).casefold()):
        if len(value) <= 256:
            result.add(value)
            if "-" in value:
                result.update(piece for piece in value.split("-") if piece)
    return result


def bucket(key: str) -> str:
    if not isinstance(key, str):
        raise MaterialError("MATERIAL_INVALID_QUERY")
    try:
        return digest(key.encode("utf-8"))[:2]
    except UnicodeError:
        raise MaterialError("MATERIAL_INVALID_QUERY") from None


def reference_key(relation_type: str, target: str) -> str:
    return relation_type.upper() + ":" + normalized_term(target)


def object_reference(data: bytes) -> dict:
    if len(data) > MAX_OBJECT_BYTES:
        raise MaterialError("MATERIAL_SIZE_LIMIT")
    sha = digest(data)
    return {"path": "objects/" + sha + ".json", "sha256": sha, "size": len(data)}


def validate_reference(value) -> dict:
    if not isinstance(value, dict) or set(value) != {"path", "sha256", "size"}:
        raise MaterialError("MATERIAL_INVALID_REFERENCE")
    sha = valid_hash(value.get("sha256"))
    if value.get("path") != "objects/" + sha + ".json":
        raise MaterialError("MATERIAL_INVALID_REFERENCE")
    if type(value.get("size")) is not int or not 0 <= value["size"] <= MAX_OBJECT_BYTES:
        raise MaterialError("MATERIAL_SIZE_LIMIT")
    return value


def seal_manifest(body: dict) -> dict:
    if "revision" in body:
        raise MaterialError("MATERIAL_INVALID_MANIFEST")
    result = {**body, "revision": digest(canonical_json(body))}
    validate_manifest(result)
    return result


def validate_manifest(value) -> dict:
    required = {"schema_version", "repository", "analysis", "coverage", "routes", "revision"}
    if not isinstance(value, dict) or set(value) != required:
        raise MaterialError("MATERIAL_INVALID_MANIFEST")
    if not isinstance(value["schema_version"], str) or value["schema_version"] not in SUPPORTED_SCHEMA_VERSIONS:
        raise MaterialError("MATERIAL_UNSUPPORTED_VERSION")
    repository = value["repository"]
    if not isinstance(repository, dict) or set(repository) != {"identity", "commit", "subdirectory"}:
        raise MaterialError("MATERIAL_INVALID_REPOSITORY")
    identity = repository["identity"]
    if (not isinstance(identity, str) or not identity or len(identity) > 512
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in identity)
            or "://" in identity or "@" in identity):
        raise MaterialError("MATERIAL_INVALID_REPOSITORY")
    valid_commit(repository["commit"])
    relative_path(repository["subdirectory"], allow_empty=True)
    if not isinstance(value["analysis"], dict) or not isinstance(value["coverage"], dict):
        raise MaterialError("MATERIAL_INVALID_MANIFEST")
    routes = value["routes"]
    if not isinstance(routes, dict) or set(routes) != {"files", "terms", "references"}:
        raise MaterialError("MATERIAL_INVALID_MANIFEST")
    for routing in routes.values():
        if not isinstance(routing, dict) or len(routing) > 256:
            raise MaterialError("MATERIAL_INVALID_MANIFEST")
        for key, reference in routing.items():
            if not isinstance(key, str) or not _BUCKET.fullmatch(key):
                raise MaterialError("MATERIAL_INVALID_MANIFEST")
            validate_reference(reference)
    valid_hash(value["revision"])
    body = {key: item for key, item in value.items() if key != "revision"}
    if digest(canonical_json(body)) != value["revision"]:
        raise MaterialError("MATERIAL_REVISION_MISMATCH")
    if len(canonical_json(value)) > MAX_MANIFEST_BYTES:
        raise MaterialError("MATERIAL_SIZE_LIMIT")
    return value
