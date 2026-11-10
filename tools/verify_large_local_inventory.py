#!/usr/bin/env python3
"""Replay the neutral large-library pipeline without a network or real model.

Uses the production local orchestration, source reads, checkpoint restoration,
strict review validator, versioned pages and full export. The controlled model
reads actual request pages. Gold is consulted only after delivery. This proves
orchestration and retention, not production model business understanding.

Example:
  python3 tools/verify_large_local_inventory.py --output /tmp/local-scale-run
Existing independently generated fixture/index can be reused with --fixture-root.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
import traceback
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "poc"), str(REPO / "poc/tests")]
from business_chat import run_business_chat
from business_index import build_business_index
from repository_discovery import ensure_repository_search
from company_api import CompanyAPIConfig, TransportResponse
from agent_policy import AgentPolicy
from repository_inventory import _cover
from repository_inventory_store import InventoryStore
from run_store import RunStore
from large_repository_fixture import create_large_fixture, evaluate_delivery


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixture-root", type=Path)
    parser.add_argument("--files", type=int, default=10001)
    args = parser.parse_args()
    if args.files < 257:
        parser.error("Use at least 257 neutral files to exercise production paging.")
    OUT = args.output.resolve()
    if OUT.exists() and any(OUT.iterdir()):
        parser.error("Output must be a new or empty directory; earlier evidence is never overwritten.")
    OUT.mkdir(parents=True, exist_ok=True)
    ROOT = args.fixture_root.resolve() if args.fixture_root else OUT / "fixture"
    if args.fixture_root:
        manifest = json.loads((ROOT / "fixture.json").read_text())
        if manifest.get("schema") != "large-business-inventory-gold/v1" or manifest.get("source_files") != args.files:
            parser.error("Fixture must match the independently generated neutral benchmark.")
        if hashlib.sha256((ROOT / "gold.jsonl").read_bytes()).hexdigest() != manifest.get("gold_sha256"):
            parser.error("Fixture gold checksum changed.")
    else:
        create_large_fixture(ROOT, args.files)
    DB = OUT / "index.sqlite"
    if (ROOT / "index.sqlite").is_file():
        with sqlite3.connect(ROOT / "index.sqlite") as source, sqlite3.connect(DB) as target:
            source.backup(target)
    else:
        build_business_index(ROOT / "source", DB, source_format="free", verify_content=True,
            quiet=True, framework_reference_path="")
        ensure_repository_search(DB, ROOT / "source")
    Q='列出系统所有计算所有premium formula 的地方'; stats={'source_files_sha256': {str(path.relative_to(REPO)):hashlib.sha256(path.read_bytes()).hexdigest() for path in [REPO/'poc'/name for name in ['business_chat.py','repository_inventory.py','repository_inventory_store.py','question_investigation.py','business_map.py','conversation_focus.py','repository_discovery.py','complete_working_set.py','source_frontier.py','repository_scope.py','web_app.py','report_view.py']]},'analysis_requests':0,'review_requests':0,'compaction_requests':0,'wire_max':0,'turns':[]};seen=set();reviews=set();narrative_reviews=set();started=time.perf_counter();last_print=started

    def transport(request):
     nonlocal last_print
     wire=json.loads(request.body);p=json.loads(wire['messages'][-1]['content']);stats['wire_max']=max(stats['wire_max'],len(request.body))
     if p.get('schema')=='semantic-answer-review/v1':
      stats['review_requests']+=1
      key=(p['answer_sha256'],p['evidence_sha256'])
      if key in reviews:raise AssertionError('Repeated completed review')
      reviews.add(key);narrative_reviews.update(re.findall(r'Business explanation ([a-f0-9]+)',json.dumps(p.get('units',[]))));ev=p['evidence'][0]
      response={'answer_sha256':p['answer_sha256'],'evidence_sha256':p['evidence_sha256'],'reviews':[{'unit_id':u['unit_id'],'verdict':'supported','reason':'Offline controlled reviewer; original arithmetic retained.','evidence':[{'reference_id':ev['reference_id'],'quote':ev['text'].splitlines()[0]}]} for u in p['units']],'omissions':[]}
     elif p.get('schema')=='business-analysis-compaction/v1':
      stats['compaction_requests']+=1;raise AssertionError('Global all-source compaction reached')
     else:
      stats['analysis_requests']+=1;rows=[];pages=[v for b in p['source_context'] for v in b.get('pages',[])];items=p.get('repository_inventory',{}).get('items',[])
      for item in items:
       if item['candidate_id'] in seen:raise AssertionError('Repeated completed candidate')
       ids=_cover(item,pages)
       if not ids:continue
       row={'candidate_id':item['candidate_id'],'evidence_ids':ids}
       if item['kind']=='source_review':row.update(disposition='reviewed',summary='Reviewed original business arithmetic and source boundaries.')
       else:
        relevant=[v for v in pages if v['relative_path']==item['relative_path']]
        labels=[m.group(1).strip() for v in relevant for line in v['source_text'].splitlines() if (m:=re.search(r'Business calculation:\s*(.+)',line))]
        if not labels:continue
        formulas=[line.strip() for v in relevant for line in v['source_text'].splitlines() if 'COMPUTE ' in line]
        if not formulas:continue
        row.update(disposition='included',summary=labels[-1]+' determines this entry charge.',formula=formulas[-1],conditions='Applies when the shown paragraph executes; actual production inputs and scheduler are not established.')
       rows.append(row);seen.add(item['candidate_id'])
      response={'answer':'## Business calculation\nThis batch retains the exact known business formulas, applicability and original source references.','inventory_findings':rows}
     if time.perf_counter()-last_print>20:
      print(json.dumps({'elapsed':round(time.perf_counter()-started,2),'analysis':stats['analysis_requests'],'reviews':stats['review_requests'],'accepted_items':len(seen)}),flush=True);last_print=time.perf_counter()
     return TransportResponse(200,json.dumps({'choices':[{'message':{'role':'assistant','content':json.dumps(response,ensure_ascii=False)},'finish_reason':'stop'}]},ensure_ascii=False))

    result=None
    try:
     with patch('socket.create_connection',side_effect=AssertionError('network prohibited')),patch('socket.socket.connect',side_effect=AssertionError('network prohibited')):
      for turn in range(max(32, (args.files + 63) // 64 + 128)):
       opts={'continue_investigation':True,'resume_run_id':result['run_id']} if result else {}
       t=time.perf_counter()
       result=run_business_chat(Q,DB,ROOT/'source',CompanyAPIConfig('https://offline.example.invalid/v1','synthetic-model',api_key='synthetic-only'),transport=transport,allow_network=False,framework_reference_path='',conversation_id='large-local-real-orchestration',policy=AgentPolicy(max_model_requests=32,max_answer_revisions=0),answer_instructions='',**opts)['agent_result']
       checkpoint=RunStore(OUT/'investigation-runs.sqlite').load(result['run_id'])['checkpoint']
       entry={'turn':turn,'seconds':round(time.perf_counter()-t,3),'status':result['status'],'stop_reason':result['stop_reason'],'counts':result.get('repository_inventory',{}).get('counts'),'checkpoint_bytes':len(json.dumps(checkpoint).encode()),'inventory_checkpoint_bytes':len(json.dumps(checkpoint.get('repository_inventory_state',{})).encode())};stats['turns'].append(entry);print(json.dumps(entry),flush=True)
       (OUT/'progress.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2))
       if result['status']=='ANALYZED':break
       if result['status'] not in {'PARTIAL','NEEDS_EVIDENCE'}:raise AssertionError(result.get('diagnostics'))
       if turn>1 and stats['turns'][-1]['counts']==stats['turns'][-2]['counts']:raise AssertionError('No durable progress')
     store=InventoryStore(DB);descriptor=result['repository_inventory'];data=store.export(descriptor['handle']);rows=[json.loads(line) for line in data['jsonl'].splitlines()];included=[r for r in rows if r.get('record_type')=='formula' and r.get('disposition')=='included']
     stats.update(seconds=round(time.perf_counter()-started,3),status=result['status'],answer_characters=len(result['answer']),report_bytes=len(json.dumps(result).encode()),export_bytes=len(data['jsonl'].encode()),oracle=evaluate_delivery(ROOT/'gold.jsonl',included),unique_review_parts=len(reviews),narrative_review_count=len(narrative_reviews),current_narrative_count=len([r for r in rows if r.get('record_type')=='business_narrative' and not r.get('superseded')]),unique_classifications=len(seen),final_counts=descriptor['counts'])
     (OUT/'final-result.json').write_text(json.dumps(result,ensure_ascii=False))
     (OUT/'export.jsonl').write_text(data['jsonl'])
    except BaseException as exc:
     stats.update(error=type(exc).__name__+': '+str(exc),traceback=traceback.format_exc());print(stats['traceback'],flush=True)
    finally:
     stats['source_unchanged'] = all(hashlib.sha256((REPO/path).read_bytes()).hexdigest()==digest for path,digest in stats['source_files_sha256'].items())
     (OUT/'report.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2));print(json.dumps({k:v for k,v in stats.items() if k not in {'turns','traceback'}},ensure_ascii=False),flush=True)
    return 0 if stats.get('status')=='ANALYZED' and stats.get('oracle',{}).get('complete') and stats.get('source_unchanged') and stats.get('narrative_review_count')==stats.get('current_narrative_count') and not stats.get('error') else 1


if __name__ == "__main__":
    raise SystemExit(main())
