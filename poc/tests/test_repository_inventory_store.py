"""Durable inventory contracts, using bounded fixtures rather than real models."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).resolve().parents[1])]
from repository_inventory_store import InventoryStore
from repository_inventory import (inventory_checkpoint, prepare_inventory, persist_inventory,
    inventory_batch, inventory_report, set_inventory_requirements)


class RepositoryInventoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "index.sqlite"
        self.store = InventoryStore(self.database)
        self.items = []
        self.pages = []
        for i in range(6):
            text = f"COMPUTE PREMIUM-AMT = BASE-AMT * {i + 1}."
            digest = hashlib.sha256(text.encode()).hexdigest()
            page = dict(evidence_id=f"ev_page_{i}", relative_path=f"calc-{i}.cbl", source_sha256=digest,
                start_line=1, end_line=1, source_text=text, span_truncated=False)
            self.pages.append(page)
            for kind in ("source_review", "formula"):
                self.items.append(dict(candidate_id=f"{kind}-{i}", kind=kind, relative_path=page["relative_path"],
                    source_sha256=digest, start_line=1,end_line=1))
        self.scope=hashlib.sha256(json.dumps(["snapshot","question"],ensure_ascii=False).encode()).hexdigest()
        self.state = dict(schema="repository-formula-inventory/v1",scope_key=self.scope,snapshot_id="snapshot",items=self.items,
            findings={},narratives={},reviewed_count=0,file_count=6,catalog_builds=1,paged=True,
            _store_database=str(self.database),review_pending=len(self.items))
        self.state["store_handle"] = self.store.create(self.state)

    def complete(self):
        for item in self.items:
            i = int(item["candidate_id"].rsplit("-", 1)[-1])
            disposition = "reviewed" if item["kind"] == "source_review" else "included" if i % 2 else "excluded"
            self.state["findings"][item["candidate_id"]] = {**item, "disposition":disposition,
                "summary":f"Business charge {i}","formula":f"charge = base * {i+1}",
                "conditions":"The shown business paragraph applies.","evidence_ids":[self.pages[i]["evidence_id"]]}
        self.state.update(reviewed_count=len(self.items),narratives={"first":"## Business calculation\nAll known rules remain accessible."})
        persist_inventory(self.state, list(self.state["findings"]),self.pages,run_id="run-one",update_key="first")
        return self.state["store_handle"]

    def test_filtered_dense_cursor_reaches_first_middle_last_with_review_and_excluded_rows(self):
        handle = self.complete()
        for cursor,expected in enumerate(("formula-1","formula-3","formula-5")):
            page=self.store.page(handle,cursor=cursor,page_size=1)
            self.assertEqual(page["total"],3)
            self.assertEqual([row["candidate_id"] for row in page["rows"]],[expected])
            self.assertEqual(page["next_cursor"],cursor+1 if cursor<2 else None)
        self.assertEqual(self.store.page(handle,cursor=3,page_size=1)["rows"],[])
        self.assertEqual(len(self.store.page(handle,audit=True,page_size=100)["rows"]),12)

    def test_recalled_span_selection_metadata_does_not_conflict_but_changed_original_does(self):
        handle=self.complete()
        page=self.store.evidence(handle,"formula-1")["spans"][0]
        page.update(selection_reasons=["archive_recall"],selection_roles=["business_formula"])
        persist_inventory(self.state,[],[page],run_id="run-two",update_key="recall")
        self.assertEqual(self.store.evidence(self.state["store_handle"],"formula-1")["integrity"],"VALID")
        with self.assertRaisesRegex(ValueError,"INVENTORY_SOURCE_CONFLICT"):
            persist_inventory(self.state,[],[{**page,"source_text":"tampered"}],run_id="run-two",update_key="tamper")
        self.assertEqual(self.store.evidence(handle,"formula-1")["spans"][0]["source_text"],self.pages[1]["source_text"])

    def test_immutable_handles_keep_corrected_history_and_all_narrative_versions(self):
        first=self.complete()
        original=self.store.page(first)
        self.state["findings"]["formula-1"]["formula"]="corrected charge = base * 2"
        self.state["narratives"]={"revision":"## Revised business calculation\nCorrected original explanation."}
        persist_inventory(self.state,["formula-1"],[],run_id="run-two",update_key="revision")
        current=self.state["store_handle"]
        self.assertEqual(self.store.page(first),original)
        self.assertEqual(self.store.page(current)["rows"][0]["formula"],"corrected charge = base * 2")
        lines=[json.loads(line) for line in self.store.export(current)["jsonl"].splitlines()]
        narratives=[line for line in lines if line["record_type"]=="business_narrative"]
        self.assertEqual([(r["narrative_id"],r["superseded"]) for r in narratives],[("first",True),("revision",False)])
        self.assertEqual(self.store.load(current)["narratives"],self.state["narratives"])
        old_state=self.store.load(first,scope_key=self.scope)
        old_state["_store_database"]=str(self.database)
        old_state["findings"]["formula-3"]["conditions"]="Branch-specific supported condition"
        persist_inventory(old_state,["formula-3"],[],run_id="branch",update_key="branch")
        self.assertEqual(self.store.page(current)["rows"][1]["conditions"],"The shown business paragraph applies.")
        self.assertIn("Branch-specific",self.store.page(old_state["store_handle"])["rows"][1]["conditions"])

    def test_bounded_checkpoint_rehydrates_without_source_catalog_scan_and_language_restarts_review(self):
        self.complete()
        saved=json.loads(json.dumps(inventory_checkpoint(self.state)))
        self.assertLess(len(json.dumps(saved)),512)
        original_connect=sqlite3.connect
        def only_archive(path,*args,**kwargs):
            self.assertNotEqual(Path(path),self.database,"source catalog rescan")
            return original_connect(path,*args,**kwargs)
        with patch("repository_inventory.sqlite3.connect",side_effect=only_archive):
            restored=prepare_inventory(self.database,"snapshot","question",saved=saved)
        self.assertEqual(restored["findings"],self.state["findings"])
        self.state.update(review_cursor=len(self.items),review_part_offset=0,review_pending=0)
        set_inventory_requirements(self.state,"en","")
        set_inventory_requirements(self.state,"zh-Hant","Preserve business sections")
        self.assertEqual(self.state["review_cursor"],0)
        self.assertEqual(self.state["review_pending"],12)
        self.assertEqual(len(self.state["presentation_pending"]),6)

    def test_source_tampering_and_store_sidecars_are_rejected(self):
        handle=self.complete()
        with self.store.connect() as db:
            db.execute("UPDATE inventory_sources SET payload_json='{}' WHERE evidence_id=?",(self.pages[1]["evidence_id"],))
        with self.assertRaisesRegex(ValueError,"INVENTORY_EVIDENCE_INVALID"):
            self.store.evidence(handle,"formula-1")
        for suffix in ("-journal","-wal","-shm"):
            path=Path(str(self.store.path)+suffix)
            path.symlink_to(self.root / "outside")
            try:
                with self.assertRaisesRegex(ValueError,"INVENTORY_STORE_PATH_INVALID"):
                    InventoryStore(self.database)
            finally:
                path.unlink()


    def replacement_revisions(self, old_details, new_details):
        from conversation_store import ConversationStore
        history=ConversationStore(self.root,self.root/"source")
        identifier=history.create()["id"]
        message=history.append(identifier,"assistant","Same business overview",status="partial",run_id="old",details=old_details)
        history.replace_assistant(identifier,message,"Same business overview",status="partial",run_id="new",
            details=new_details,preserve_previous=True)
        return history.get(identifier)["messages"][0].get("answer_revisions",[])

    def test_same_overview_with_corrected_inventory_handle_preserves_previous_revision(self):
        first=self.complete()
        previous={"repository_inventory":self.store.page(first),"investigation_run_id":"run-one"}
        self.state["findings"]["formula-1"]["formula"]="Corrected business charge"
        persist_inventory(self.state,["formula-1"],[],run_id="run-two",update_key="changed-formula")
        current={"repository_inventory":self.store.page(self.state["store_handle"]),"investigation_run_id":"run-two"}
        revisions=self.replacement_revisions(previous,current)
        self.assertEqual(len(revisions),1)
        self.assertEqual(revisions[0]["repository_inventory"]["handle"],first)
        self.assertNotEqual(self.store.page(first)["rows"][0]["formula"],"Corrected business charge")

    def test_unchanged_inventory_handle_does_not_add_duplicate_revision(self):
        first=self.complete()
        details={"repository_inventory":self.store.page(first),"investigation_run_id":"run-one"}
        self.assertEqual(self.replacement_revisions(details,details),[])

    def test_plain_answer_without_inventory_keeps_existing_revision_rule(self):
        self.assertEqual(self.replacement_revisions({},{}),[])
        self.assertEqual(self.replacement_revisions({"investigation_run_id":"old"},{"investigation_run_id":"new"}),[])

    def test_real_http_pages_exports_and_verified_spans_are_bound_to_message_and_revision(self):
        import http.client
        import threading
        from urllib.parse import urlencode
        from web_app import WorkbenchState, create_server
        from conversation_store import ConversationStore
        first=self.complete()
        app=WorkbenchState(state_path=self.root/"workspace.json")
        app.project={"source":str(self.root/"source"),"output":str(self.root)}
        app.conversation_store=ConversationStore(self.root,self.root/"source")
        conversation=app.conversation_store.create("snapshot")["id"]
        descriptor=self.store.page(first)
        descriptor.update(rows=[],next_cursor=0)
        message=app.conversation_store.append(conversation,"assistant","First partial business explanation",status="partial",
            run_id="run-one",details={"investigation_run_id":"run-one","repository_inventory":descriptor})
        server=create_server(0,app=app,web_root=self.root)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(**query):
            connection=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=10)
            try:
                connection.request("GET","/api/repository-inventory?"+urlencode({"handle":first,
                    "conversation_id":conversation,"message_id":message,**query}),headers={"Origin":server.origin,"X-Session-Token":app.session_token})
                response=connection.getresponse()
                return response.status,json.loads(response.read())
            finally:connection.close()
        try:
            status,page=request(cursor=2,page_size=1)
            self.assertEqual(status,200);self.assertEqual(page["rows"][0]["candidate_id"],"formula-5")
            status,source=request(action="evidence",candidate_id="formula-1",index=0)
            self.assertEqual(status,200);self.assertEqual(source["status"],"READY")
            self.assertTrue(all(span["integrity"]=="VALID" for span in source["spans"]))
            self.assertEqual(source["spans"][0]["source_text"],self.pages[1]["source_text"])
            status,export=request(export="jsonl")
            self.assertEqual(status,200)
            rows=[json.loads(line) for line in export["jsonl"].splitlines()]
            self.assertTrue(any(row["record_type"]=="business_narrative" for row in rows))
            foreign=app.conversation_store.create("snapshot")["id"]
            self.assertEqual(request(conversation_id=foreign)[0],404)
            self.assertEqual(request(handle="unknown")[0],404)
            self.assertEqual(request(action="evidence",candidate_id="missing")[0],404)
            self.state["findings"]["formula-1"]["conditions"]="New reviewed condition"
            persist_inventory(self.state,["formula-1"],[],run_id="run-two",update_key="next")
            newer=self.store.page(self.state["store_handle"])
            app.conversation_store.replace_assistant(conversation,message,"Updated business explanation",status="partial",
                run_id="run-two",details={"investigation_run_id":"run-two","repository_inventory":newer},preserve_previous=True)
            revision=app.conversation_store.get(conversation)["messages"][0]["answer_revisions"][0]["revision_id"]
            self.assertEqual(request()[0],404)
            self.assertEqual(request(revision_id=revision)[0],200)
            self.assertEqual(request(handle=newer["handle"],revision_id=revision)[0],404)
            self.assertEqual(request(handle=newer["handle"])[0],200)
            with self.store.connect() as db:
                db.execute("UPDATE inventory_sources SET payload_json='{}' WHERE evidence_id=?",(self.pages[1]["evidence_id"],))
            self.assertEqual(request(revision_id=revision,action="evidence",candidate_id="formula-1")[0],404)
            app.conversation_store=None
            self.assertEqual(request()[0],404)
        finally:
            server.shutdown();server.server_close();thread.join(2)


if __name__ == "__main__":
    unittest.main()


class PagedInventoryOrchestrationTests(unittest.TestCase):
    def setUp(self):
        from test_repository_formula_inventory import RepositoryFormulaInventoryTests, program
        self.fixture=RepositoryFormulaInventoryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        patcher=patch("repository_inventory.PAGED_FILE_THRESHOLD",1)
        patcher.start();self.addCleanup(patcher.stop)
        self.fixture.prepare({f"calc-{i:02}.cbl":program(f"ENTRY-{i:02}",f"COMPUTE PREMIUM-AMT = BASIS * FACTOR + {i}.") for i in range(12)})
        self.seen=set();self.reviewed=set();self.requests=0;self.compactions=0;self.fail_at=None;self.review_texts=[];self.narrative_texts=[];self.bad_narrative=False;self.revisions=0;self.corrections=0;self.maximum_window_sources=0

    def transport(self,request):
        from company_api import TransportResponse
        self.requests+=1
        if self.requests==self.fail_at:
            return TransportResponse(500,json.dumps({"error":{"message":"Synthetic transient server failure"}}))
        payload=json.loads(json.loads(request.body)["messages"][-1]["content"])
        current_source_count=len(payload.get("evidence",[])) if payload.get("schema")=="semantic-answer-review/v1" else len({page["evidence_id"] for bundle in payload.get("source_context",[]) for page in bundle.get("pages",[])})
        self.maximum_window_sources=max(self.maximum_window_sources,current_source_count)
        if payload.get("schema")=="semantic-answer-review/v1":
            key=(payload["answer_sha256"],payload["evidence_sha256"])
            self.assertNotIn(key,self.reviewed);self.reviewed.add(key)
            self.review_texts.extend(unit["text"] for unit in payload["units"])
            evidence=payload["evidence"][0]
            response={"answer_sha256":key[0],"evidence_sha256":key[1],"reviews":[{
                "unit_id":unit["unit_id"],"verdict":"contradicted" if "保費乘以五" in unit["text"] else "supported","reason":"Offline protocol fixture.",
                "evidence":[{"reference_id":evidence["reference_id"],"quote":evidence["text"].splitlines()[0]}]}
                for unit in payload["units"]],"omissions":[]}
        elif payload.get("repository_inventory_revision"):
            self.corrections+=1
            response={"answer":"業務結論已依供應的原文修正。","inventory_corrections":[],
                "inventory_narrative_corrections":[{"narrative_id":row["narrative_id"],
                    "text":row["text"].replace("保費乘以五","保費計算以所示公式為準"),"evidence_ids":row["evidence_ids"]}
                    for row in payload["repository_inventory_revision"]["narrative_items"]]}
        elif payload.get("schema")=="business-analysis-compaction/v1":
            self.compactions+=1
            raise AssertionError("Durable inventory must not require all prior source IDs in one compression response")
        else:
            items=payload["repository_inventory"]["items"]
            self.assertFalse(self.seen.intersection(row["candidate_id"] for row in items),"Completed batch replay")
            pages=[page for bundle in payload["source_context"] for page in bundle["pages"]]
            findings=self.fixture.findings(items,pages)
            self.seen.update(row["candidate_id"] for row in findings)
            narrative=f"## 業務結論\n第 {self.requests} 批保留保費業務公式、適用條件及原始來源。"
            if self.bad_narrative and not self.narrative_texts:narrative+=" 保費乘以五。"
            if findings:self.narrative_texts.append(narrative)
            response={"answer":narrative,"inventory_findings":findings}
        return TransportResponse(200,json.dumps({"choices":[{"message":{"role":"assistant","content":json.dumps(response)},"finish_reason":"stop"}]}))

    def run_turn(self,result=None):
        from business_chat import run_business_chat
        from agent_policy import AgentPolicy
        options={"continue_investigation":True,"resume_run_id":result["run_id"]} if result else {}
        return run_business_chat("列出系统所有计算所有premium formula 的地方",self.fixture.database,self.fixture.source,
            self.fixture.config,transport=self.transport,allow_network=False,framework_reference_path="",
            conversation_id="paged-durable-test",answer_language="zh-HK",answer_instructions="用繁體中文回答，保留業務結論章節",
            policy=AgentPolicy(max_model_requests=2,max_answer_revisions=self.revisions),**options)["agent_result"]

    def test_small_windows_and_explicit_resumes_deliver_every_formula_and_all_review_parts(self):
        from run_store import RunStore
        result=None;counts=[];handles=[]
        for _ in range(10):
            result=self.run_turn(result)
            descriptor=result["repository_inventory"];counts.append(descriptor["counts"]["reviewed"]);handles.append(descriptor["handle"])
            checkpoint=RunStore(self.fixture.root/"investigation-runs.sqlite").load(result["run_id"])["checkpoint"]
            self.assertLess(len(json.dumps(checkpoint["repository_inventory_state"])),512)
            self.assertLessEqual(len((checkpoint.get("prior_source_memory")or{}).get("pages",[])),self.maximum_window_sources,
                "Completed review windows must remain in the durable archive rather than accumulate in bounded source memory")
            if result["status"]=="ANALYZED":break
        self.assertEqual(result["status"],"ANALYZED",result["diagnostics"])
        self.assertEqual(counts,sorted(counts));self.assertEqual(len(self.seen),24);self.assertEqual(self.compactions,0)
        self.assertTrue(descriptor["complete"]);self.assertEqual(descriptor["counts"]["review_pending"],0)
        store=InventoryStore(self.fixture.database)
        rows=store.page(descriptor["handle"],page_size=100)["rows"]
        self.assertEqual(len(rows),12)
        self.assertEqual({row["relative_path"] for row in rows},{f"calc-{i:02}.cbl" for i in range(12)})
        self.assertIn("業務結論",result["answer"]);self.assertIn("原始碼",result["answer"])
        self.assertGreaterEqual(len(self.reviewed),3)
        reviewed_text="\n".join(self.review_texts)
        for narrative in self.narrative_texts:
            self.assertIn(narrative.splitlines()[-1],reviewed_text,"Every exported business explanation needs strict review")
        self.assertEqual(store.page(handles[0])["counts"]["reviewed"],counts[0])

    def test_server_failure_preserves_published_batch_and_resume_does_not_repeat_it(self):
        self.fail_at=2
        first=self.run_turn()
        self.assertGreater(first["repository_inventory"]["counts"]["reviewed"],0)
        initial_ids=set(self.seen)
        self.fail_at=None;result=first
        for _ in range(10):
            result=self.run_turn(result)
            if result["status"]=="ANALYZED":break
        self.assertEqual(result["status"],"ANALYZED",result["diagnostics"])
        self.assertTrue(initial_ids<=self.seen);self.assertEqual(len(self.seen),24)

    def test_earlier_business_narrative_is_reviewed_and_corrected_without_losing_other_batches(self):
        self.bad_narrative=True;self.revisions=1
        result=None
        for _ in range(16):
            result=self.run_turn(result)
            if result["status"]=="ANALYZED":break
        self.assertEqual(result["status"],"ANALYZED",result["diagnostics"])
        self.assertEqual(self.corrections,1)
        export=InventoryStore(self.fixture.database).export(result["repository_inventory"]["handle"])
        narratives=[json.loads(line) for line in export["jsonl"].splitlines()
            if json.loads(line).get("record_type")=="business_narrative"]
        self.assertTrue(any(row["superseded"] and "保費乘以五" in row["text"] for row in narratives))
        current=[row["text"] for row in narratives if not row["superseded"]]
        self.assertEqual(len(current),len(self.narrative_texts))
        self.assertFalse(any("保費乘以五" in text for text in current))
        self.assertTrue(any("以所示公式為準" in text for text in current))
