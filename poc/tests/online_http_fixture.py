"""Synthetic source and deterministic model for HTTP/browser acceptance only."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from business_index import build_business_index
from company_api import CompanyAPIConfig
from online_investigation import run_investigation
from online_material_export import export_material
from online_material_reader import DirectoryObjectStore, MaterialReader
from online_workspace import OnlineWorkbench

REPOSITORY = "example/business-source"
CONFIG = CompanyAPIConfig(base_url="https://model.example.invalid/v1", chat_model="synthetic-chat",
                          api_key="synthetic-model-secret")
MAIN = ('IDENTIFICATION DIVISION.\nPROGRAM-ID. MAIN-ENTRY.\nDATA DIVISION.\n'
        'WORKING-STORAGE SECTION.\nCOPY RECORDS.\nPROCEDURE DIVISION.\nMAIN-PARA.\n'
        'IF INPUT-AMOUNT > 100\nCALL "WORKER-ENTRY"\nELSE\nDISPLAY "REVIEW"\nEND-IF.\nGOBACK.\n')
WORKER = ('IDENTIFICATION DIVISION.\nPROGRAM-ID. WORKER-ENTRY.\nDATA DIVISION.\n'
          'WORKING-STORAGE SECTION.\nCOPY RECORDS.\nPROCEDURE DIVISION.\n'
          'MOVE "Y" TO PROCESSED-FLAG.\nGOBACK.\n')
COPY = '01 INPUT-AMOUNT PIC 9(7).\n01 PROCESSED-FLAG PIC X.\n'


class SyntheticModel:
    """An oracle for mechanism checks, never a replacement for model evaluation."""
    last_request_outcome = "received"

    def __init__(self):
        self.requests = []

    def complete(self, *, messages, check_cancel=None, **_):
        if check_cancel:
            check_cancel()
        context = json.loads(messages[-1]["content"])
        self.requests.append(context)
        pages = context.get("source_context", [])
        paths = {item["citation"]["path"] for item in pages}
        observations = context.get("observations", [])
        prior_hints = [hint for message in context.get("history", [])
                       for hint in message.get("citation_hints", [])]
        if not observations and prior_hints:
            result = {"actions": [{"tool": "read", "path": hint["path"],
                                   "start_line": hint["start"], "end_line": hint["end"]}
                                  for hint in prior_hints[-3:]], "remaining": []}
        elif not observations:
            result = {"actions": [{"tool": "search", "text": "MAIN-ENTRY"}], "remaining": []}
        elif "main.cbl" not in paths:
            result = {"actions": [{"tool": "relations", "path": "main.cbl"},
                                   {"tool": "read", "path": "main.cbl", "start_line": 1, "end_line": 13}],
                      "remaining": []}
        elif "worker.cbl" not in paths or "records.cpy" not in paths:
            result = {"actions": [{"tool": "read", "path": "worker.cbl", "start_line": 1, "end_line": 8},
                                   {"tool": "read", "path": "records.cpy", "start_line": 1, "end_line": 2}],
                      "remaining": []}
        else:
            ids = [item["id"] for item in pages]
            result = {"answer": ("输入金额大于 100 时，主程序调用处理程序；处理程序将已处理标记设为 Y。"
                                  "金额未超过门槛时，主程序显示 REVIEW，随后结束。"
                                  "这些是已核对的静态分支；尚未验证运行时调用或业务调度。 "
                                  + " ".join("[" + item + "]" for item in ids)),
                      "citations": ids, "complete": True, "remaining": []}
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps(result, ensure_ascii=False)},
                              "finish_reason": "stop"}]}


class OnlineFixture:
    def __init__(self, root):
        self.root = Path(root)
        self.materials = {}
        self.raw = {}
        self.source_reads = []
        self.models = []
        self.add_revision("a" * 40, "v1")

    def add_revision(self, commit, label):
        source = self.root / ("producer-" + label)
        source.mkdir(parents=True)
        files = {"main.cbl": MAIN.encode(), "worker.cbl": WORKER.encode(), "records.cpy": COPY.encode()}
        for number in range(5):
            files[f"unrelated-{number}.cbl"] = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. OTHER-" + str(number)
                                                + ".\nPROCEDURE DIVISION.\nGOBACK.\n").encode()
        for name, data in files.items():
            (source / name).write_bytes(data)
        database = self.root / (label + ".sqlite")
        report = build_business_index(source, database, source_format="free", verify_content=True,
                                       framework_reference_path="", quiet=True)
        output = self.root / ("material-" + label)
        manifest = export_material(database, source, output, repository_id=REPOSITORY,
                                    commit=commit, build_report=report)
        self.materials[label] = (output, manifest)
        self.raw[commit] = files
        shutil.rmtree(source)
        for file in self.root.glob(label + ".sqlite*"):
            file.unlink()
        return manifest

    def material_factory(self, location, credentials, expected, check_cancel):
        label = location["base_url"].rstrip("/").rsplit("/", 1)[-1]
        root, _ = self.materials[label]
        return MaterialReader(DirectoryObjectStore(root), expected_repository=expected.get("repository"),
                              expected_commit=expected.get("commit"), expected_revision=expected.get("revision"),
                              check_cancel=check_cancel)

    def source_factory(self, repository, credentials, check_cancel):
        def read(path, *, max_bytes):
            check_cancel()
            self.source_reads.append((repository["commit"], path))
            data = self.raw[repository["commit"]][path]
            assert len(data) <= max_bytes
            return data
        return read

    def investigator(self, *args, **kwargs):
        client = SyntheticModel()
        self.models.append(client)
        return run_investigation(*args, **kwargs, client=client)

    def controller(self):
        return OnlineWorkbench(self.root / "profile" / "online", config_provider=lambda: CONFIG,
                                material_factory=self.material_factory, source_factory=self.source_factory,
                                investigator=self.investigator)

    def connect_payload(self, label="v1"):
        _, manifest = self.materials[label]
        return {"location": {"kind": "https", "base_url": "https://material.example.invalid/" + label + "/"},
                "repository": REPOSITORY, "commit": manifest["repository"]["commit"],
                "revision": manifest["revision"],
                "credentials": {"material_token": "synthetic-material-secret", "source_token": "synthetic-source-secret"}}
