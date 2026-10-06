from __future__ import annotations
import ctypes
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

START = time.perf_counter()
from tree_sitter import Language, Parser
ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / '.build' / 'parser-trial'
CANDIDATE = os.environ.get('PARSER_TRIAL_CANDIDATE', 'upstream')
LIBRARY = 'upstream-parser.dylib' if CANDIDATE == 'upstream' else 'cobol-parser.dylib'
COMMIT = '550020ddf42ef9b718ad897868a9308d2afcda35' if CANDIDATE == 'upstream' else '86a2c479cb7e299e8dc3bb9db7a346502335d21e'
lib = ctypes.CDLL(str(BASE / LIBRARY))
language_function = getattr(lib, 'tree_sitter_COBOL' if CANDIDATE == 'upstream' else 'tree_sitter_cobol')
language_function.restype = ctypes.c_void_p
capsule = ctypes.pythonapi.PyCapsule_New
capsule.restype = ctypes.py_object
capsule.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p]
language = Language(capsule(language_function(), b'tree_sitter.Language', None))
parser = Parser(language)
INIT_MS = (time.perf_counter() - START) * 1000

def walk(root):
    pending = [root]
    while pending:
        node = pending.pop()
        yield node
        pending.extend(reversed(node.children))

def inspect(path):
    data = path.read_bytes()
    start = time.perf_counter()
    tree = parser.parse(data)
    first = (time.perf_counter() - start) * 1000
    nodes = list(walk(tree.root_node))
    errors = [{'type': node.type, 'missing': node.is_missing, 'line': node.start_point.row+1, 'column': node.start_point.column+1, 'end_line': node.end_point.row+1, 'text': data[node.start_byte:node.end_byte].decode(errors='replace')[:140]} for node in nodes if node.type == 'ERROR' or node.is_missing]
    statements = [{'type': node.type, 'line': node.start_point.row+1, 'column': node.start_point.column+1, 'end_line': node.end_point.row+1, 'text': data[node.start_byte:node.end_byte].decode(errors='replace')} for node in nodes if node.type in ('move_statement','if_statement','evaluate_statement','compute_statement','call_statement','if_header','evaluate_header','when_header')]
    timings = []
    for _ in range(100):
        start = time.perf_counter()
        parser.parse(data)
        timings.append((time.perf_counter()-start)*1000)
    lines = data.splitlines(keepends=True)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    mapping_ok = all(starts[n.start_point.row] + n.start_point.column == n.start_byte and starts[n.end_point.row] + n.end_point.column == n.end_byte for n in nodes)
    counts = {}
    for node in nodes:
        if node.type.endswith('_statement'):
            counts[node.type] = counts.get(node.type, 0) + 1
    return {'path':str(path.relative_to(ROOT)), 'bytes':len(data), 'lines':len(lines), 'sha256':hashlib.sha256(data).hexdigest(), 'has_error':tree.root_node.has_error, 'errors':errors, 'counts':counts, 'all_node_offsets_consistent':mapping_ok, 'first_parse_ms':first, 'warm_parse_median_ms':statistics.median(timings), 'warm_parse_p95_ms':sorted(timings)[94], 'selected_statements':statements}

if len(sys.argv) > 1 and sys.argv[1] == '--cold':
    data = Path(sys.argv[2]).read_bytes()
    tree = parser.parse(data)
    print(json.dumps({'init_ms':INIT_MS, 'init_and_read_and_parse_ms':(time.perf_counter()-START)*1000, 'has_error':tree.root_node.has_error}))
    raise SystemExit

paths = sorted((ROOT/'poc/fixtures/synthetic-insurance-v1/programs').glob('*.cbl'))
paths += [ROOT/'poc/fixtures/framework-workbench/source/programs'/name for name in ('service-entry.cbl','screen-control.cbl')]
probes = BASE/'probes'
probes.mkdir(exist_ok=True)
head = ['IDENTIFICATION DIVISION.', 'PROGRAM-ID. INLINE-RULE.', 'DATA DIVISION.', 'WORKING-STORAGE SECTION.', '01 FLAG-A PIC 9.', '01 FLAG-B PIC 9.', '01 RESULT-A PIC 9(4).', '01 RESULT-B PIC 9(4).', 'PROCEDURE DIVISION.', 'MAIN.']
body = ['    MOVE 1 TO RESULT-A MOVE 2 TO RESULT-B', '    IF FLAG-A = 1', '        IF FLAG-B = 2', '            MOVE 3 TO RESULT-A MOVE 4 TO RESULT-B', '        ELSE', '            MOVE 5 TO RESULT-A', '        END-IF', '    ELSE', '        MOVE 6 TO RESULT-A', '    END-IF', '    EVALUATE FLAG-A', '        WHEN 1', '            EVALUATE FLAG-B', '                WHEN 2 MOVE 7 TO RESULT-A', '                WHEN OTHER MOVE 8 TO RESULT-A', '            END-EVALUATE', '        WHEN OTHER MOVE 9 TO RESULT-A', '    END-EVALUATE', '    GOBACK.']
probe_path = probes/'inline-nested.cbl'
probe_path.write_text(''.join(f'{(i+1)*100:06d} {line}\n' for i,line in enumerate(head+body)), encoding='utf-8')
results = [inspect(path) for path in paths]
probe = inspect(probe_path)
cold = []
for _ in range(7):
    start = time.perf_counter()
    output = subprocess.check_output([sys.executable, str(Path(__file__)), '--cold', str(paths[-1])], text=True)
    cold.append({'wall_ms':(time.perf_counter()-start)*1000, **json.loads(output)})
report = {'candidate':CANDIDATE, 'grammar_commit':COMMIT, 'runtime':'tree-sitter 0.25.2', 'grammar_abi':language.abi_version, 'runtime_init_ms':INIT_MS, 'files':results, 'probes':[probe], 'cold_process_runs':cold, 'summary':{'files':len(results), 'clean_files':sum(not r['has_error'] for r in results), 'error_nodes':sum(len(r['errors']) for r in results), 'source_mapping_consistent':all(r['all_node_offsets_consistent'] for r in results), 'total_source_lines':sum(r['lines'] for r in results), 'total_warm_parse_median_ms':sum(r['warm_parse_median_ms'] for r in results), 'cold_process_median_ms':statistics.median(r['wall_ms'] for r in cold)}}
(BASE/f'{CANDIDATE}-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
print(json.dumps({'summary':report['summary'], 'probe':{k:probe[k] for k in ('has_error','errors','counts','all_node_offsets_consistent','warm_parse_median_ms')}, 'grammar_abi':language.abi_version}, indent=2))
