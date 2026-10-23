"""Reuse source-derived explanation work only within one synchronous question.

This cache is for orientation and requirement projections, never answers or
answer assessments. Every supplied argument participates in the key. A changed
source excerpt, scope, framework reference or option therefore needs new work.
No cache survives the surrounding question, including failed questions.
"""

from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
import asyncio
import hashlib
import inspect
import math
from threading import get_ident


_active_cache = ContextVar("business_explanation_cache", default=None)


def _execution_owner():
    # Context variables can be copied into worker threads and child tasks.
    # Inherited context must not make their mutable caches shared.
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return get_ident(), task


class _Uncacheable(ValueError):
    pass


def _fingerprint(value, limit):
    """Hash supported plain data with a bounded, type-preserving traversal."""
    digest = hashlib.sha256()
    size = 0
    active = set()

    def emit(data):
        nonlocal size
        size += len(data)
        if size > limit:
            raise _Uncacheable
        digest.update(data)

    def visit(item, depth=0):
        if depth > 64:
            raise _Uncacheable
        kind = type(item)
        if item is None:
            emit(b"n;")
        elif kind is bool:
            emit(b"b1;" if item else b"b0;")
        elif kind is int:
            # Bound conversion before constructing a decimal string, including
            # on runtimes without an integer-to-string digit limit.
            if (item.bit_length() - 1) * 30103 // 100000 + 1 > limit - size:
                raise _Uncacheable
            emit(b"i" + str(item).encode("ascii") + b";")
        elif kind is float:
            if not math.isfinite(item):
                raise _Uncacheable
            emit(b"f" + item.hex().encode("ascii") + b";")
        elif kind is str:
            if len(item) > limit - size:
                raise _Uncacheable
            encoded = item.encode("utf-8")
            emit(b"s" + str(len(encoded)).encode("ascii") + b":" + encoded)
        elif kind in (dict, list, tuple):
            identifier = id(item)
            if identifier in active or len(item) > limit - size:
                raise _Uncacheable
            active.add(identifier)
            try:
                if kind is dict:
                    emit(b"d" + str(len(item)).encode("ascii") + b":")
                    # Mapping insertion order can affect a source projection.
                    # Preserve it and avoid sorting an oversized mapping before
                    # its byte budget can stop traversal.
                    for key in item:
                        if type(key) is not str:
                            raise _Uncacheable
                        visit(key, depth + 1)
                        visit(item[key], depth + 1)
                else:
                    emit((b"l" if kind is list else b"t") + str(len(item)).encode("ascii") + b":")
                    for child in item:
                        visit(child, depth + 1)
                emit(b";")
            finally:
                active.remove(identifier)
        else:
            raise _Uncacheable

    visit(value)
    return digest.digest(), size


class _QuestionCache:
    def __init__(self, max_entries, max_bytes, max_input_bytes):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.max_input_bytes = max_input_bytes
        self.entries = OrderedDict()
        self.bytes = 0
        self.hits = self.misses = self.bypasses = 0
        self.owner = _execution_owner()
        self.closed = False

    def info(self):
        """Return counters without retaining or exposing source material."""
        return {"entries": len(self.entries), "bytes": self.bytes,
                "hits": self.hits, "misses": self.misses, "bypasses": self.bypasses}

    def clear(self):
        self.entries.clear()
        self.bytes = 0

    def close(self):
        self.closed = True
        self.clear()
        self.owner = None

    def call(self, namespace, function, args, kwargs, key_values):
        if self.closed or self.owner != _execution_owner():
            return function(*args, **kwargs)
        try:
            fingerprint, input_bytes = _fingerprint(key_values, self.max_input_bytes)
        except (ValueError, UnicodeError, RecursionError):
            self.bypasses += 1
            return function(*args, **kwargs)
        key = (namespace, fingerprint)
        if key in self.entries:
            self.hits += 1
            self.entries.move_to_end(key)
            return deepcopy(self.entries[key][0])
        self.misses += 1
        result = function(*args, **kwargs)
        try:
            _, output_bytes = _fingerprint(result, self.max_bytes - input_bytes)
        except (ValueError, UnicodeError, RecursionError):
            self.bypasses += 1
            return result
        # Charge both input and output encoded sizes, although only the digest
        # and result are retained. This is a conservative data-size allowance,
        # not a measurement of the interpreter's object overhead.
        weight = input_bytes + output_bytes
        while self.entries and (len(self.entries) >= self.max_entries or self.bytes + weight > self.max_bytes):
            _, (_, removed_bytes) = self.entries.popitem(last=False)
            self.bytes -= removed_bytes
        if weight <= self.max_bytes:
            self.entries[key] = (deepcopy(result), weight)
            self.bytes += weight
        return result


@contextmanager
def business_explanation_cache(*, max_entries=16, max_bytes=2 * 1024 * 1024,
                               max_input_bytes=512 * 1024):
    """Give one question its own cache, restoring any outer scope on exit."""
    for value in (max_entries, max_bytes, max_input_bytes):
        if type(value) is not int or value <= 0:
            raise ValueError("Explanation cache limits must be positive integers.")
    cache = _QuestionCache(max_entries, max_bytes, max_input_bytes)
    token = _active_cache.set(cache)
    try:
        yield cache
    finally:
        _active_cache.reset(token)
        cache.close()


def with_business_explanation_cache(function):
    """Decorate the synchronous question entry point; clear on every return."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        with business_explanation_cache():
            return function(*args, **kwargs)
    return wrapped


def cache_business_explanation(function):
    """Decorate a pure source projection; unsupported data bypasses caching."""
    namespace = object()
    signature = inspect.signature(function)

    @wraps(function)
    def wrapped(*args, **kwargs):
        cache = _active_cache.get()
        if cache is None:
            return function(*args, **kwargs)
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        return cache.call(namespace, function, args, kwargs, bound.arguments)
    return wrapped
