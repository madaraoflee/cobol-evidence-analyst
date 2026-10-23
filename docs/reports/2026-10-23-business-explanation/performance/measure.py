"""Offline source projection/review timings; no model or network is used."""
import argparse
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import socket
import statistics
import sys
import time

def blocked(*args, **kwargs):
    raise AssertionError('Network unavailable in offline timing experiment')
socket.socket.connect = blocked
socket.create_connection = blocked
parser=argparse.ArgumentParser()
parser.add_argument('root',type=Path)
parser.add_argument('mode',choices=['head','current_cached','current_uncached'])
parser.add_argument('output',type=Path)
args=parser.parse_args()
sys.path[:0]=[str(args.root/'poc'), str(args.root/'tools')]
import business_orientation as orientation
import business_synthesis as synthesis
import business_onboarding_fixture as fixture
cache_scope=nullcontext
if args.mode == 'current_cached':
    from business_explanation_cache import business_explanation_cache
    cache_scope=business_explanation_cache

sources=fixture.sources()
def page(path):
    text=sources[path]
    return {'relative_path':path,'source_text':text,
        'source_sha256':hashlib.sha256(text.encode()).hexdigest(),
        'evidence_id':'ev-'+path,'start_line':1,'end_line':len(text.splitlines()),'format_hint':'free'}
report={'mode':args.mode,'python':sys.version,'scope':'one brief plus three existing-answer reviews per sample; source-only; no model','paid_model_calls':0,'original_orientation_parse_lru':'retained in every variant, cleared only for cold samples','samples':[]}
for scenario,question,paths in [('batch',fixture.BATCH_QUESTION,['nightjob.clle','settle.cbl']),('online',fixture.ONLINE_QUESTION,['balance.cbl'])]:
    pages=[page(p) for p in paths]
    investigation={'state':'located','can_answer':True,'required_items':[]}
    answer=fixture.scripted_answer({'question':question,'source_context':[{'pages':pages}]})
    answers=['这里包含入口、程序以及文件。',answer,answer+'\n以上关系以当前提供的源码为依据。']
    def one():
        with cache_scope() as cache:
            brief=synthesis.build_analysis_brief(question, investigation, pages, [], 8192)
            reviews=[synthesis.assess_business_answer(question, answer, investigation,pages) for answer in answers]
            info=cache.info() if cache else None
            return {'review_hash':hashlib.sha256(json.dumps(reviews,ensure_ascii=False,sort_keys=True).encode()).hexdigest(), 'cache':info}
    one() # Imports/regular expressions warm-up; excluded from measurement.
    for temperature in ['cold_parse_lru','warm_parse_lru']:
        durations=[]
        for repetition in range(25):
            if temperature=='cold_parse_lru':
                orientation._cached_parse_run.cache_clear()
            start=time.perf_counter_ns()
            outcome=one()
            elapsed=(time.perf_counter_ns()-start)/1000000
            durations.append(elapsed)
            report['samples'].append({'scenario':scenario,'temperature':temperature,'repetition':repetition,'elapsed_ms':elapsed,**outcome})
report['summaries']=[{'scenario':scenario,'temperature':temperature,
    'n':len(vals),'median_ms':statistics.median(vals),'min_ms':min(vals),'max_ms':max(vals),
    'p95_ms':sorted(vals)[int(.95*(len(vals)-1))]}
    for scenario in ['batch','online'] for temperature in ['cold_parse_lru','warm_parse_lru']
    for vals in [[r['elapsed_ms'] for r in report['samples'] if r['scenario']==scenario and r['temperature']==temperature]]]
args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps(report['summaries'],ensure_ascii=False))
