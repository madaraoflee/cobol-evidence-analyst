#!/usr/bin/env python3
"""Incremental, bounded source discovery before on-demand structural analysis.

Catalog entries are navigation hints, not source evidence. A stat-based catalog
snapshot does not certify that full file contents or all declarations were read.
"""

from __future__ import annotations

import codecs
from collections import deque
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Callable

from repo_inventory import (
    DEFAULT_EXTENSIONS, PROGRAM_ID_RE, SOURCE_FORMAT_RE,
    classify_artifact, decode_source, iter_source_files, validate_source_options,
)

HEADER_BYTES = 256 * 1024
COPY_EXTENSIONS = frozenset({'.cpy', '.copy', '.cpb', '.inc'})
PROGRAM_EXTENSIONS = frozenset({'.cbl', '.cob', '.cobol', '.cblle', '.sqlcblle', '.pco', '.sqb', '.src', ''})
COPY_TARGET_RE = re.compile(r"\bCOPY\s+(?:['\"]([^'\"]+)['\"]|([A-Z0-9_$#@.-]+))", re.I)
CALL_TARGET_RE = re.compile(r"\bCALL\s+(['\"])([^'\"]+)\1", re.I)
DYNAMIC_TARGET_RE = re.compile(r"\bCALL\s+(?!['\"])([A-Z0-9_$#@-]+)", re.I)
Progress = Callable[[dict], None]


def _notify(progress: Progress | None, **event: object) -> None:
    if progress:
        progress(event)


def _connect(path: Path) -> sqlite3.Connection:
    if any(Path(str(path) + suffix).is_symlink() for suffix in ('', '-wal', '-shm')):
        raise ValueError('Catalog database and sidecars must not be symbolic links.')
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS catalog_files (
            relative_path TEXT PRIMARY KEY,
            size_bytes INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
            ctime_ns INTEGER NOT NULL, inode INTEGER NOT NULL,
            option_key TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TEMP TABLE candidates (
            relative_path TEXT PRIMARY KEY, size_bytes INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL, ctime_ns INTEGER NOT NULL, inode INTEGER NOT NULL
        );
    ''')
    return connection


def _decode_prefix(data: bytes, encoding: str, truncated: bool):
    # A prefix can end inside a code point. Do not misclassify that as another
    # encoding, and never use replacement characters as verified source text.
    if encoding != 'auto':
        try:
            decoder = codecs.getincrementaldecoder(encoding)(errors='strict')
            return decoder.decode(data, final=not truncated).lstrip('\ufeff'), encoding
        except UnicodeError:
            return None, encoding
    for bom, candidate in ((b'\x00\x00\xfe\xff', 'utf-32'), (b'\xff\xfe\x00\x00', 'utf-32'),
                           (b'\xff\xfe', 'utf-16'), (b'\xfe\xff', 'utf-16'), (b'\xef\xbb\xbf', 'utf-8-sig')):
        if data.startswith(bom):
            return _decode_prefix(data, candidate, truncated)
    try:
        text = codecs.getincrementaldecoder('utf-8')(errors='strict').decode(data, final=not truncated)
        if '\x00' not in text:
            return text, 'utf-8'
    except UnicodeError:
        pass
    decoded = decode_source(data, 'auto')
    return (decoded.text, decoded.encoding) if decoded else (None, None)


def _complete_prefix(text: str | None, truncated: bool) -> str:
    text = text or ''
    if truncated and text and not text.endswith(('\n', '\r')):
        end = max(text.rfind('\n'), text.rfind('\r'))
        return text[:end + 1]
    return text


def _clean_lines(text: str, source_format: str) -> list[tuple[int, str]]:
    """Keep physical line numbers while dropping comments and sequence columns."""
    result = []
    active_format = source_format
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.expandtabs(8)
        directive = SOURCE_FORMAT_RE.search(line)
        if directive:
            if source_format == 'auto':
                active_format = directive.group(1).lower()
            continue
        fixed = active_format == 'fixed' or (active_format == 'auto' and len(line) >= 7 and (
            line[:6].strip().isdigit() or (not line[:6].strip() and line[6] in ' */-Dd')))
        if fixed:
            if len(line) >= 7 and line[6] in '*/':
                continue
            line = line[7:72]
        elif line.lstrip().startswith('*'):
            continue
        # Inline comments outside quoted strings only.
        quote = None
        index = 0
        has_inline_comment = '*>' in line
        while has_inline_comment and index < len(line):
            char = line[index]
            if quote:
                if char == quote:
                    if index + 1 < len(line) and line[index + 1] == quote:
                        index += 2
                        continue
                    quote = None
            elif char in "'\"":
                quote = char
            elif line[index:index + 2] == '*>':
                line = line[:index]
                break
            index += 1
        result.append((line_number, line))
    return result


def _read_prefix(path: Path, limit: int, *, verify_content: bool = False) -> tuple[bytes, int, str | None]:
    """Read a bounded header; optional full verification streams fixed chunks."""
    with path.open('rb') as handle:
        header = handle.read(limit)
        bytes_read = len(header)
        digest = hashlib.sha256(header) if verify_content else None
        if digest:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                bytes_read += len(chunk)
    return header, bytes_read, digest.hexdigest() if digest else None


def _inspect_header(path: Path, relative_path: str, size: int, *, encoding: str,
                    source_format: str, header_bytes: int, verify_content: bool) -> dict:
    first_limit = header_bytes if verify_content else min(header_bytes, 16 * 1024)
    data, bytes_read, full_hash = _read_prefix(path, first_limit, verify_content=verify_content)
    truncated = len(data) < size
    text, detected_encoding = _decode_prefix(data, encoding, truncated)
    lines = _clean_lines(_complete_prefix(text, truncated), source_format)
    normalized = '\n'.join(line for _, line in lines)
    # Most exported members declare PROGRAM-ID in the first few lines. Stop
    # early once found; unusually long prologues may use the configured bound.
    if first_limit < header_bytes and truncated and not PROGRAM_ID_RE.search(normalized) and path.suffix.lower() not in COPY_EXTENSIONS:
        data, extra_read, full_hash = _read_prefix(path, header_bytes)
        bytes_read += extra_read
        truncated = len(data) < size
        text, detected_encoding = _decode_prefix(data, encoding, truncated)
        lines = _clean_lines(_complete_prefix(text, truncated), source_format)
        normalized = '\n'.join(line for _, line in lines)
    definitions = []
    # Match across physical lines, including split PROGRAM-ID clauses.
    for match in PROGRAM_ID_RE.finditer(normalized):
        index = normalized.count('\n', 0, match.start())
        definitions.append({'program_name': match.group(1).upper(),
                            'relative_path': relative_path, 'start_line': lines[index][0],
                            'name_origin': 'program_id'})
        if len(definitions) >= 256:
            break
    kind = classify_artifact(path.suffix.lower(), [item['program_name'] for item in definitions], normalized)
    if path.suffix.lower() in COPY_EXTENSIONS:
        kind = 'copybook'
    if not definitions and kind != 'copybook' and (path.suffix.lower() in PROGRAM_EXTENSIONS or kind == 'cobol_program'):
        definitions = [{'program_name': path.stem.upper(), 'relative_path': relative_path,
                        'start_line': 1, 'name_origin': 'path'}]
    for item in definitions:
        item['entry_key'] = f"{relative_path}::{item['program_name']}::{item['start_line']}"
    return {
        'relative_path': relative_path, 'size_bytes': size, 'artifact_kind': kind,
        'encoding': detected_encoding, 'header_bytes_read': len(data), 'bytes_read': bytes_read,
        'header_truncated': truncated, 'header_sha256': hashlib.sha256(data).hexdigest(),
        'full_sha256': full_hash, 'programs': definitions, 'decoded': text is not None,
        'definition_limit_reached': len(definitions) >= 256,
        'error': None if text is not None else 'HEADER_DECODE_FAILED',
    }


def refresh_source_catalog(
    source: Path,
    database_path: Path,
    *,
    extensions: frozenset[str] = DEFAULT_EXTENSIONS,
    include_extensionless: bool = True,
    encoding: str = 'auto',
    source_format: str = 'auto',
    progress: Progress | None = None,
    verify_content: bool = False,
    header_bytes: int = HEADER_BYTES,
) -> dict:
    """Refresh navigation metadata with per-file durable checkpoints.

    An unchanged stat tuple reuses its header metadata without opening the file.
    ``verify_content`` forces a streaming full-file digest on every candidate.
    Neither mode creates statements, semantic evidence, or a structural index.
    """
    source = Path(source).expanduser().resolve()
    database_path = Path(database_path).expanduser()
    if not source.is_dir():
        raise ValueError('Source must be an existing directory.')
    validate_source_options(encoding, source_format)
    if header_bytes < 1024 or header_bytes > 16 * 1024 * 1024:
        raise ValueError('header_bytes must be between 1024 and 16777216.')
    if source == database_path.resolve() or source in database_path.resolve().parents:
        raise ValueError('Catalog database must be outside the source directory.')
    option_key = json.dumps([str(source), encoding, source_format, header_bytes], ensure_ascii=False)
    started = time.monotonic()
    stats = {'candidate': 0, 'decoded': 0, 'indexed_or_updated': 0, 'cached': 0,
             'removed': 0, 'failed': 0, 'bytes_total': 0, 'bytes_read': 0}
    warnings = []
    with closing(_connect(database_path)) as connection:
        for path in iter_source_files(source, extensions, include_extensionless):
            stat = path.stat()
            relative = path.relative_to(source).as_posix()
            connection.execute('INSERT INTO candidates VALUES (?, ?, ?, ?, ?)',
                               (relative, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino))
            stats['candidate'] += 1
            stats['bytes_total'] += stat.st_size
            if stats['candidate'] == 1 or stats['candidate'] % 100 == 0:
                _notify(progress, phase='discovery', completed=stats['candidate'], total=None,
                        unit='files', current_file=relative, bytes_total=stats['bytes_total'],
                        elapsed_seconds=time.monotonic() - started, eta_seconds=None)
        connection.commit()
        total = stats['candidate']
        completed = 0
        bytes_completed = 0
        scan_started = time.monotonic()
        _notify(progress, phase='catalog', completed=0, total=total, unit='files',
                current_file=None, bytes_completed=0, bytes_total=stats['bytes_total'],
                bytes_read=0, cached=0, elapsed_seconds=time.monotonic() - started, eta_seconds=None)
        # The cursor streams metadata; only one bounded prefix is resident.
        rows = connection.execute('''SELECT c.*, f.size_bytes old_size, f.mtime_ns old_mtime,
            f.ctime_ns old_ctime, f.inode old_inode, f.option_key, f.payload
            FROM candidates c LEFT JOIN catalog_files f USING(relative_path)
            ORDER BY c.relative_path''')
        for row in rows:
            relative = row['relative_path']
            cached = not verify_content and row['payload'] is not None and row['option_key'] == option_key and (
                row['size_bytes'], row['mtime_ns'], row['ctime_ns'], row['inode']) == (
                row['old_size'], row['old_mtime'], row['old_ctime'], row['old_inode'])
            if cached:
                payload = json.loads(row['payload'])
                # Transient read errors are retried even if stat metadata agrees.
                cached = payload.get('error') is None
            if cached:
                stats['cached'] += 1
            else:
                try:
                    candidate_path = source / relative
                    if candidate_path.is_symlink() or not candidate_path.resolve().is_relative_to(source):
                        raise OSError('Source path is no longer an in-root regular file.')
                    payload = _inspect_header(source / relative, relative, row['size_bytes'], encoding=encoding,
                                              source_format=source_format, header_bytes=header_bytes,
                                              verify_content=verify_content)
                    after = (source / relative).stat()
                    if (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino) != (
                            row['size_bytes'], row['mtime_ns'], row['ctime_ns'], row['inode']):
                        payload['error'] = 'SOURCE_CHANGED_DURING_CATALOG'
                        payload['programs'] = []
                except OSError as exc:
                    payload = {'relative_path': relative, 'size_bytes': row['size_bytes'],
                               'artifact_kind': 'unreadable', 'programs': [], 'decoded': False,
                               'bytes_read': 0, 'error': type(exc).__name__}
                stats['bytes_read'] += payload.get('bytes_read', 0)
                stats['indexed_or_updated'] += 1
                connection.execute('INSERT OR REPLACE INTO catalog_files VALUES (?, ?, ?, ?, ?, ?, ?)',
                                   (relative, row['size_bytes'], row['mtime_ns'], row['ctime_ns'],
                                    row['inode'], option_key, json.dumps(payload, ensure_ascii=False)))
                connection.commit()
            stats['decoded'] += int(payload.get('decoded', False))
            stats['failed'] += int(payload.get('error') is not None)
            completed += 1
            bytes_completed += row['size_bytes']
            elapsed = time.monotonic() - scan_started
            _notify(progress, phase='catalog', completed=completed, total=total, unit='files',
                    current_file=relative, bytes_completed=bytes_completed, bytes_total=stats['bytes_total'],
                    bytes_read=stats['bytes_read'], cached=stats['cached'],
                    elapsed_seconds=time.monotonic() - started,
                    eta_seconds=round(elapsed / completed * (total - completed), 1) if completed >= 3 else None)
        stats['removed'] = connection.execute(
            'DELETE FROM catalog_files WHERE relative_path NOT IN (SELECT relative_path FROM candidates)'
        ).rowcount
        connection.commit()
        programs = []
        file_entries = []
        digest = hashlib.sha256(option_key.encode('utf-8'))
        truncated_count = 0
        fallback_count = 0
        copybook_count = 0
        for row in connection.execute('SELECT * FROM catalog_files ORDER BY relative_path'):
            payload = json.loads(row['payload'])
            programs.extend(payload['programs'])
            file_entries.append({key: payload.get(key) for key in (
                'relative_path', 'size_bytes', 'artifact_kind', 'encoding', 'header_truncated', 'definition_limit_reached', 'error')})
            truncated_count += int(payload.get('header_truncated', False))
            fallback_count += sum(item['name_origin'] == 'path' for item in payload['programs'])
            copybook_count += int(payload['artifact_kind'] == 'copybook')
            digest.update(json.dumps([row['relative_path'], row['size_bytes'], row['mtime_ns'], row['ctime_ns'],
                                      row['inode'], payload.get('header_sha256'), payload.get('full_sha256'),
                                      payload.get('error')], ensure_ascii=False).encode('utf-8'))
    if truncated_count:
        warnings.append({'code': 'BOUNDED_HEADER_SCAN', 'file_count': truncated_count,
                         'message': 'Headers only: later PROGRAM-ID definitions and dependencies may not be discovered.'})
    if fallback_count:
        warnings.append({'code': 'PATH_BASED_ENTRIES', 'file_count': fallback_count,
                         'message': 'Some selectable entries use filenames; PROGRAM-ID is not yet confirmed.'})
    if stats['failed']:
        warnings.append({'code': 'CATALOG_READ_ERRORS', 'file_count': stats['failed'],
                         'message': 'Some files could not be cataloged. Other readable files remain available.'})
    return {
        'schema_version': 'source-catalog/v1', 'source_root': str(source),
        'snapshot_id': 'catalog-sha256:' + digest.hexdigest(), 'snapshot_kind': 'catalog-stat',
        'source_manifest_verified': False, 'programs': sorted(programs, key=lambda item: (
            item['program_name'], item['relative_path'], item['start_line'])),
        'files': stats, 'file_entries': file_entries, 'copybook_count': copybook_count,
        'warnings': warnings, 'elapsed_seconds': time.monotonic() - started,
        'scope': {'mode': 'catalog', 'header_bytes_per_file': header_bytes,
                  'full_file_content_verified': bool(verify_content and not stats['failed']),
                  'all_program_definitions_discovered': not truncated_count and not stats['failed'] and not any(item.get('definition_limit_reached') for item in file_entries),
                  'structural_analysis_performed': False, 'truncated_file_count': truncated_count,
                  'cache_validation': 'size_mtime_ctime_inode',
                  'stat_cache_limitation': 'Changes preserving all file metadata require explicit content verification.'},
    }


def _entry_matches(programs: list[dict], entry: str) -> list[dict]:
    key = entry.strip().replace('\\', '/').casefold().removeprefix('./')
    for field in ('entry_key', 'program_name', 'relative_path'):
        matches = [item for item in programs if str(item.get(field, '')).casefold() == key]
        if matches:
            return matches
    return [item for item in programs if Path(item['relative_path']).name.casefold() == key]


def select_related_sources(
    source: Path, catalog: dict, entry: str, *, encoding: str = 'auto',
    source_format: str = 'auto', max_files: int = 64, max_depth: int = 3,
    max_scan_bytes: int = 2 * 1024 * 1024, max_total_source_bytes: int = 64 * 1024 * 1024,
    progress: Progress | None = None, max_scope_bytes: int | None = None,
) -> dict:
    """Choose a bounded static COPY/CALL neighborhood without requiring closure.

    Missing, ambiguous, dynamic, and unscanned targets are recorded boundaries.
    Returned paths always refer to whole source files; callers must not present
    this dependency probe as a full source or runtime-path verification.
    """
    source = Path(source).expanduser().resolve()
    if max_scope_bytes is not None:
        max_total_source_bytes = max_scope_bytes
    if source != Path(catalog['source_root']).resolve():
        raise ValueError('Catalog belongs to a different source directory.')
    if max_files < 1 or max_depth < 0 or max_scan_bytes < 1024 or max_total_source_bytes < 1:
        raise ValueError('Invalid related-source limits.')
    matches = _entry_matches(catalog['programs'], entry)
    if len(matches) != 1:
        raise ValueError('ENTRY_NOT_FOUND' if not matches else 'ENTRY_AMBIGUOUS')
    selected_entry = matches[0]
    program_paths: dict[str, set[str]] = {}
    copy_paths: dict[str, set[str]] = {}
    file_sizes = {item['relative_path']: item['size_bytes'] for item in catalog['file_entries']}
    known_files = set(file_sizes)
    for item in catalog['programs']:
        program_paths.setdefault(item['program_name'].casefold(), set()).add(item['relative_path'])
    # COPY members are often extensionless exports, so lookup includes filename
    # stems for every file; ambiguity is preserved instead of guessing a library.
    for relative in known_files:
        for key in (Path(relative).stem, Path(relative).name):
            copy_paths.setdefault(key.casefold(), set()).add(relative)
    queue = deque([(selected_entry['relative_path'], 0)])
    queued = {selected_entry['relative_path']}
    selected = []
    bytes_selected = file_sizes[selected_entry['relative_path']]
    boundaries = []
    scan_details = []
    def boundary(relative: str, relation: str, target: str, status: str) -> None:
        boundaries.append({'relative_path': relative, 'relation_type': relation,
                           'target_name': target, 'status': status})
    if bytes_selected > max_total_source_bytes:
        boundary(selected_entry['relative_path'], 'SOURCE_FILE', selected_entry['relative_path'], 'ENTRY_EXCEEDS_BYTE_BUDGET')
    while queue:
        relative, depth = queue.popleft()
        path = source / relative
        if relative not in known_files or path.is_symlink() or not path.resolve().is_relative_to(source):
            boundary(relative, 'SOURCE_FILE', relative, 'UNAVAILABLE')
            continue
        selected.append(relative)
        _notify(progress, phase='scope', completed=len(selected), total=None, unit='files',
                current_file=relative, eta_seconds=None)
        try:
            before = path.stat()
            bytes_selected += before.st_size - file_sizes[relative]
            if bytes_selected > max_total_source_bytes:
                if relative == selected_entry['relative_path']:
                    boundary(relative, 'SOURCE_FILE', relative, 'ENTRY_EXCEEDS_BYTE_BUDGET')
                else:
                    selected.remove(relative)
                    bytes_selected -= before.st_size
                    boundary(relative, 'SOURCE_FILE', relative, 'SOURCE_BYTE_LIMIT')
                    continue
            data, bytes_read, _ = _read_prefix(path, max_scan_bytes)
            after = path.stat()
        except OSError:
            boundary(relative, 'SOURCE_FILE', relative, 'UNREADABLE')
            continue
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            boundary(relative, 'SOURCE_FILE', relative, 'CHANGED_DURING_SCAN')
            continue
        truncated = bytes_read < before.st_size
        text, detected_encoding = _decode_prefix(data, encoding, truncated)
        scan_details.append({'relative_path': relative, 'bytes_scanned': bytes_read,
                             'file_bytes': before.st_size, 'truncated': truncated,
                             'encoding': detected_encoding, 'depth': depth})
        if truncated:
            boundary(relative, 'SOURCE_FILE', relative, 'DEPENDENCY_SCAN_TRUNCATED')
        if text is None:
            boundary(relative, 'SOURCE_FILE', relative, 'DECODE_FAILED')
            continue
        normalized = '\n'.join(line for _, line in _clean_lines(_complete_prefix(text, truncated), source_format))
        targets = [('INCLUDES_COPY', (match.group(1) or match.group(2)).rstrip('.'), copy_paths)
                   for match in COPY_TARGET_RE.finditer(normalized)]
        targets.extend(('CALLS', match.group(2), program_paths) for match in CALL_TARGET_RE.finditer(normalized))
        for match in DYNAMIC_TARGET_RE.finditer(normalized):
            boundary(relative, 'CALL_TARGET_FROM', match.group(1), 'DYNAMIC_TARGET')
        for relation, target, mapping in targets:
            candidates = mapping.get(target.casefold(), set())
            if not candidates:
                boundary(relative, relation, target, 'MISSING_SOURCE')
            elif len(candidates) > 1:
                boundary(relative, relation, target, 'AMBIGUOUS_SOURCE')
            else:
                candidate = next(iter(candidates))
                if candidate in queued:
                    continue
                if depth >= max_depth:
                    boundary(relative, relation, target, 'DEPTH_LIMIT')
                elif len(queued) >= max_files:
                    boundary(relative, relation, target, 'FILE_LIMIT')
                elif bytes_selected + file_sizes[candidate] > max_total_source_bytes:
                    boundary(relative, relation, target, 'SOURCE_BYTE_LIMIT')
                else:
                    bytes_selected += file_sizes[candidate]
                    queue.append((candidate, depth + 1))
                    queued.add(candidate)
    unique = {tuple(item.values()): item for item in boundaries}
    return {'selected_entry': selected_entry, 'relative_paths': selected,
            'missing_dependencies': list(unique.values()),
            'scope': {'mode': 'entry_neighborhood', 'max_files': max_files, 'max_depth': max_depth,
                      'max_dependency_scan_bytes_per_file': max_scan_bytes,
                      'max_total_source_bytes': max_total_source_bytes, 'selected_source_bytes': bytes_selected,
                      'max_scope_bytes': max_total_source_bytes, 'total_scope_bytes': bytes_selected,
                      'truncated': any(item['status'] in {'DEPENDENCY_SCAN_TRUNCATED', 'DEPTH_LIMIT', 'FILE_LIMIT', 'SOURCE_BYTE_LIMIT', 'ENTRY_EXCEEDS_BYTE_BUDGET'} for item in boundaries),
                      'selected_file_count': len(selected), 'catalog_file_count': len(known_files),
                      'dependency_scan': scan_details, 'complete_dependency_closure': not boundaries,
                      'runtime_paths_verified': False}}
