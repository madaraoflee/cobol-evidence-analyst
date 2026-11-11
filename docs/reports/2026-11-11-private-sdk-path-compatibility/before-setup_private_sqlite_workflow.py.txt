"""Prepare and verify an isolated Windows SQLite SDK for explicit CI builds.

This command changes only a fresh private copy and the Actions step environment
files. It never modifies the source Python installation or starts a build.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PIN = ROOT / 'tools' / 'sqlite-runtime-pin.json'
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024


def require(condition, code):
    if not condition:
        raise ValueError(code)


def _json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _line(value):
    value = str(value)
    require(not any(char in value for char in ('\r', '\n', '\0')), 'PRIVATE_SQLITE_ACTIONS_VALUE_INVALID')
    return value


def _control_file(value):
    require(value is not None and str(value), 'PRIVATE_SQLITE_ACTIONS_FILES_REQUIRED')
    path = Path(value).absolute()
    require(not path.is_symlink() and path.parent.is_dir() and not path.parent.is_symlink()
            and (not path.exists() or path.is_file()), 'PRIVATE_SQLITE_ACTIONS_FILE_INVALID')
    _line(path)
    return path


def _pin(path):
    value = _json(path)
    require(isinstance(value, dict) and set(value) == {
        'version', 'url', 'archive_sha3_256', 'dll_sha256', 'source_id'}, 'PRIVATE_SQLITE_PIN_INVALID')
    require(isinstance(value['version'], str) and re.fullmatch(r'\d+\.\d+\.\d+', value['version'])
            and isinstance(value['source_id'], str) and value['source_id'], 'PRIVATE_SQLITE_PIN_INVALID')
    for key in ('archive_sha3_256', 'dll_sha256'):
        require(isinstance(value[key], str) and re.fullmatch(r'[0-9a-f]{64}', value[key]), 'PRIVATE_SQLITE_PIN_INVALID')
    require(isinstance(value['url'], str), 'PRIVATE_SQLITE_URL_INVALID')
    parsed = urlsplit(value['url'])
    require(parsed.scheme == 'https' and parsed.hostname in {'sqlite.org', 'www.sqlite.org'}
            and not parsed.username and not parsed.password and parsed.port in {None, 443}
            and re.fullmatch(r'/\d{4}/sqlite-dll-win-x64-\d+\.zip', parsed.path)
            and not parsed.query and not parsed.fragment, 'PRIVATE_SQLITE_URL_INVALID')
    return value


def download_archive(pin, destination):
    """Use normal verified HTTPS, then require the frozen official SHA3 digest."""
    request = Request(pin['url'], headers={'User-Agent': 'private-runtime-workflow/1'})
    digest, size = hashlib.sha3_256(), 0
    with urlopen(request, timeout=60) as response:
        require(response.geturl() == pin['url'], 'PRIVATE_SQLITE_DOWNLOAD_REDIRECT_REJECTED')
        with Path(destination).open('xb') as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                require(size <= MAX_ARCHIVE_BYTES, 'PRIVATE_SQLITE_ARCHIVE_TOO_LARGE')
                digest.update(chunk)
                handle.write(chunk)
    require(size > 0 and digest.hexdigest() == pin['archive_sha3_256'], 'PRIVATE_SQLITE_ARCHIVE_HASH_MISMATCH')
    return {'bytes': size, 'sha3_256': digest.hexdigest()}


def _run(command, *, stdout, stderr, timeout):
    with Path(stdout).open('x', encoding='utf-8') as out, Path(stderr).open('x', encoding='utf-8') as err:
        subprocess.run([str(item) for item in command], check=True, stdout=out, stderr=err,
                       timeout=timeout)


def _verified_prepare(receipt, *, source_base, output, pin_path, pin):
    require(isinstance(receipt, dict) and receipt.get('status') == 'completed_private_runtime'
            and receipt.get('exit_code') == 0 and receipt.get('source_unchanged') is True,
            'PRIVATE_SQLITE_PREPARATION_NOT_VERIFIED')
    for key, expected in (('source_base', source_base), ('output', output), ('pin_manifest', pin_path)):
        require(isinstance(receipt.get(key), str) and Path(receipt[key]).resolve() == expected.resolve(),
                'PRIVATE_SQLITE_PREPARATION_PATH_MISMATCH')
    require(receipt.get('pin') == pin and isinstance(receipt.get('source_before'), dict) and receipt['source_before']
            and receipt['source_before'] == receipt.get('source_after'), 'PRIVATE_SQLITE_SOURCE_GUARD_FAILED')
    fingerprint = receipt.get('source_manifest_sha256')
    require(isinstance(fingerprint, str) and re.fullmatch(r'[0-9a-f]{64}', fingerprint),
            'PRIVATE_SQLITE_SOURCE_FINGERPRINT_MISSING')
    interpreter = output / 'python' / 'python.exe'
    require(isinstance(receipt.get('private_python'), str)
            and Path(receipt['private_python']).resolve() == interpreter
            and interpreter.is_file() and not interpreter.is_symlink(), 'PRIVATE_SQLITE_INTERPRETER_MISMATCH')
    return interpreter, fingerprint


def setup(*, output, pin_manifest=DEFAULT_PIN, source_base=None, github_path=None, github_env=None):
    require(sys.platform == 'win32', 'PRIVATE_SQLITE_WINDOWS_ONLY')
    github_path = _control_file(github_path if github_path is not None else os.environ.get('GITHUB_PATH'))
    github_env = _control_file(github_env if github_env is not None else os.environ.get('GITHUB_ENV'))
    require(github_path != github_env, 'PRIVATE_SQLITE_ACTIONS_FILES_IDENTICAL')
    output = Path(output).absolute()
    require(not output.is_symlink() and not output.exists() and output.parent.is_dir()
            and not output.parent.is_symlink(), 'PRIVATE_SQLITE_FRESH_OUTPUT_REQUIRED')
    output.mkdir()
    output = output.resolve(strict=True)
    receipt_path = output / 'WORKFLOW-SQLITE-SETUP.json'
    receipt = {'schema_version': 'private-sqlite-workflow/v1', 'status': 'preparing',
               'exit_code': 1, 'stages': [], 'global_python_mutation_requested': False}
    primary_error = None
    try:
        pin_path = Path(pin_manifest).resolve(strict=True)
        require(pin_path.is_file(), 'PRIVATE_SQLITE_PIN_INVALID')
        pin = _pin(pin_path)
        # Preserve lexical source components for prepare's reparse/link guard.
        base = Path(sys.base_prefix if source_base is None else source_base).absolute()
        receipt.update(source_base=str(base), pin_manifest=str(pin_path), pin=pin)
        archive = output / 'official-sqlite.zip'
        receipt['stages'].append('download')
        receipt['download'] = download_archive(pin, archive)
        private_root = output / 'runtime'
        prepare_receipt = output / 'PRIVATE-SQLITE-PREPARATION.json'
        receipt['stages'].append('private_copy')
        _run([sys.executable, '-I', '-B', ROOT / 'tools' / 'prepare_private_sqlite_runtime.py',
              '--source-base', base, '--output', private_root, '--pin-manifest', pin_path,
              '--archive', archive, '--receipt', prepare_receipt],
             stdout=output / 'prepare-stdout.txt', stderr=output / 'prepare-stderr.txt', timeout=900)
        prepared = _json(prepare_receipt)
        interpreter, fingerprint = _verified_prepare(prepared, source_base=base,
            output=private_root, pin_path=pin_path, pin=pin)
        receipt.update(private_python=str(interpreter), private_base=str(interpreter.parent),
                       source_manifest_sha256=fingerprint, preparation_receipt=str(prepare_receipt))
        receipt['stages'].append('isolated_private_probe')
        probe_path = output / 'PRIVATE-SQLITE-PROBE.json'
        _run([interpreter, '-I', '-S', '-B', ROOT / 'poc' / 'sqlite_runtime_probe.py',
              '--pin-manifest', pin_path, '--allowed-root', interpreter.parent],
             stdout=probe_path, stderr=output / 'probe-stderr.txt', timeout=120)
        probe = _json(probe_path)
        # Reuse the same strict report validator as source and compiled checks.
        sys.path.insert(0, str(ROOT / 'poc'))
        try:
            from sqlite_runtime_probe import validate_report
            validate_report(probe, pin, interpreter.parent, identity_kind='source')
        finally:
            sys.path.pop(0)
        require(probe.get('schema_version') == 'sqlite-runtime-probe/v1' and probe.get('status') == 'passed',
                'PRIVATE_SQLITE_PROBE_FAILED')
        receipt['probe'] = probe
        private_fingerprint = hashlib.sha256(json.dumps({'source_manifest_sha256': fingerprint, 'pin': pin},
            sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
        receipt['private_base_fingerprint'] = private_fingerprint
        receipt['stages'].append('actions_environment')
        environment = {
            'PYTHONDONTWRITEBYTECODE': '1',
            'WORKBENCH_PRIVATE_SQLITE_RECEIPT': str(prepare_receipt),
            'WORKBENCH_SQLITE_RUNTIME_PIN': str(pin_path),
            'WORKBENCH_PRIVATE_SQLITE_SETUP_RECEIPT': str(receipt_path),
            'WORKBENCH_PRIVATE_SQLITE_BASE': str(interpreter.parent),
            'WORKBENCH_PRIVATE_SQLITE_SOURCE_FINGERPRINT': fingerprint,
            'WORKBENCH_PRIVATE_SQLITE_FINGERPRINT': private_fingerprint,
        }
        env_lines = ''.join(key + '=' + _line(value) + '\n' for key, value in environment.items())
        # Copied Scripts launchers can retain an embedded host-interpreter path.
        # Activate only python.exe; package tools use this Python's -m entry.
        path_lines = _line(interpreter.parent) + '\n'
        with github_env.open('a', encoding='utf-8', newline='\n') as handle:
            handle.write(env_lines)
        with github_path.open('a', encoding='utf-8', newline='\n') as handle:
            handle.write(path_lines)
        receipt.update(status='completed_private_workflow', exit_code=0,
                       environment_keys=sorted(environment), github_path_order='private_root_only')
        return receipt
    except BaseException as error:
        primary_error = error
        receipt.update(status='failed_private_workflow', failure_type=type(error).__name__)
        raise
    finally:
        try:
            with receipt_path.open('x', encoding='utf-8') as handle:
                json.dump(receipt, handle, indent=2)
                handle.write('\n')
        except BaseException:
            if primary_error is None:
                raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pin-manifest', type=Path, default=DEFAULT_PIN)
    args = parser.parse_args(argv)
    try:
        result = setup(output=args.output, pin_manifest=args.pin_manifest)
    except Exception as error:
        print(json.dumps({'status': 'failed_private_workflow', 'error_type': type(error).__name__}), file=sys.stderr)
        return 1
    print(json.dumps({'status': result['status'], 'private_python': result['private_python'],
                      'preparation_receipt': result['preparation_receipt']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
