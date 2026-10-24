import json
import sqlite3
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'poc'))
from business_index import build_business_index
from repository_discovery import ensure_repository_search, retrieve_repository_context, read_repository_context

with tempfile.TemporaryDirectory(prefix='retrieval-cache-probe-') as folder:
    root=Path(folder); source=root/'source'; source.mkdir(); database=root/'index.sqlite'
    lines=['IDENTIFICATION DIVISION.', 'PROGRAM-ID. ROUTECASE.', 'DATA DIVISION.', 'WORKING-STORAGE SECTION.', '01 RESULT-CODE PIC 99.', 'PROCEDURE DIVISION.', 'MAIN.']
    lines += ['*> neutral padding '+('x'*180) for _ in range(8500)]
    lines += ['MOVE 90 TO RESULT-CODE.', 'DISPLAY "LATEBUSINESSRULE" RESULT-CODE.', 'GOBACK.']
    (source/'entry.cbl').write_text('\n'.join(lines)+'\n')
    build_business_index(source,database,source_format='free',verify_content=True)
    built=ensure_repository_search(database,source)
    baseline=retrieve_repository_context(database,source,'LATEBUSINESSRULE')
    with sqlite3.connect(database) as db:
        missing=db.execute('SELECT page_id,start_line,end_line FROM repo_pages ORDER BY start_line DESC LIMIT 1').fetchone()
        db.execute('DELETE FROM repo_fts WHERE rowid=?',(missing[0],))
    refreshed=ensure_repository_search(database,source,verify_content=True)
    actual=retrieve_repository_context(database,source,'LATEBUSINESSRULE')
    print(json.dumps({'case':'missing_fts_after_explicit_update', 'question':'返回 90 的规则是什么（唯一合成标记 LATEBUSINESSRULE）？', 'source_lines':len(lines), 'built_pages':built['indexed_pages'], 'removed_fts_page':missing, 'baseline_matched':baseline['matched_page_count'],'baseline_rule_visible':any('MOVE 90 TO RESULT-CODE.' in p['source_text'] for p in baseline['pages']), 'update':{k:refreshed[k] for k in ['updated_files','cached_files','content_hash_verified','full_text_complete']}, 'actual_matched':actual['matched_page_count'],'actual_rule_visible':any('MOVE 90 TO RESULT-CODE.' in p['source_text'] for p in actual['pages']),'raw_fallback':actual.get('raw_fallback'),'actual_page_ranges':[(p['start_line'],p['end_line']) for p in actual['pages']]},ensure_ascii=False,indent=2))
    with sqlite3.connect(database) as db:
        missing2=db.execute('SELECT page_id,start_line,end_line FROM repo_pages ORDER BY start_line LIMIT 1 OFFSET 1').fetchone()
        db.execute('DELETE FROM repo_pages WHERE page_id=?',(missing2[0],))
        db.execute('DELETE FROM repo_fts WHERE rowid=?',(missing2[0],))
    refreshed2=ensure_repository_search(database,source,verify_content=True)
    partial=read_repository_context(database,source,relative_path='entry.cbl',start_line=missing2[1],end_line=missing2[2]+10,max_chars=32000)
    print(json.dumps({'case':'partly_missing_page_read','removed_page':missing2, 'update':{k:refreshed2[k] for k in ['updated_files','cached_files','full_text_complete']}, 'range_complete':partial['range_complete'],'next_start_line':partial['next_start_line'],'raw_fallback':partial.get('raw_fallback'),'page_ranges':[(p['start_line'],p['end_line']) for p in partial['pages']],'boundaries':partial['boundaries']},ensure_ascii=False,indent=2))
