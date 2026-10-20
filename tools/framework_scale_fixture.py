"""Deterministic neutral framework corpus; large generated sources stay outside Git.

The oracle records physical source ranges while writing them. It never reads
the application's parser, index, retrieval rankings, or generated answer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


SCALES = {
    "focused": {"programs": 2048, "copies": 512, "entry_lines": 100000},
    "stress": {"programs": 16384, "copies": 4096, "entry_lines": 150000},
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


class SourceWriter:
    def __init__(self):
        self.rows, self.ranges = [], {}

    def add(self, text, label=None):
        rows = text.splitlines()
        start = len(self.rows) + 1
        self.rows.extend(rows)
        if label:
            self.ranges[label] = {"start_line": start, "end_line": len(self.rows), "text": text}

    def fill(self, until, *, sentence_end=True):
        while len(self.rows) < until:
            number = len(self.rows) + 1
            suffix = "." if sentence_end else ""
            self.rows.append(f"ADD {number % 7} TO AUX-COUNT{suffix} *> neutral row {number:06d}")

    def save(self, source, relative):
        raw = ("\n".join(self.rows) + "\n").encode()
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return {name: {**span, "path": relative, "sha256": digest(raw)}
                for name, span in self.ranges.items()}


def build_fixture(directory, scale="focused", *, specification=None):
    directory = Path(directory)
    source = directory / "source"
    source.mkdir(parents=True, exist_ok=False)
    spec = dict(SCALES[scale] if specification is None else specification)
    if not 0 <= spec["programs"] <= 20000 or not 4 <= spec["copies"] <= 5000 or not 160 <= spec["entry_lines"] <= 200000:
        raise ValueError("Requested corpus exceeds the explicit generation bounds")
    spans = {}

    def write(relative, text, label):
        writer = SourceWriter()
        writer.add(text, label)
        spans.update(writer.save(source, relative))

    write("active/layout-leaf.cpy", "01 LEDGER-PARAMS.\n  05 LEDGER-FUNCTION PIC XX.\n  05 LEDGER-KEY PIC X(8).\n  05 LEDGER-VIEW PIC X(10).\n  05 LEDGER-STATUS PIC XX.", "layout_leaf")
    write("active/layout-root.cpy", 'COPY "active/layout-leaf.cpy".', "layout_root")
    write("active/rate-template.cpy", "01 RATE-TOKEN PIC 9 VALUE FACTOR.", "rate_template")
    write("active/rule-leaf.cpy", "IF INPUT-AMOUNT > 0\n COMPUTE FINAL-AMOUNT ROUNDED =\n INPUT-AMOUNT * RATE-VALUE + SERVICE-FEE\n END-COMPUTE\nEND-IF", "rule_leaf")
    write("active/rule-root.cpy", 'COPY "active/rule-leaf.cpy".', "rule_root")
    write("active/request-skeleton.cpy", "REQUEST-BOOT.\nPERFORM INITIALIZE-REQUEST.\nPERFORM MAIN-ACTION.\nGOBACK.", "request_skeleton")
    write("active/batch-skeleton.cpy", "BATCH-BOOT.\nPERFORM INITIALIZE-BATCH.\nPERFORM MAIN-ACTION UNTIL STOP-FLAG = 1.\nGOBACK.", "batch_skeleton")
    write("data/ledger-lf.dds", "     A          R LEDGERREC                 PFILE(LEDGERPF)\n     A            ACCOUNTKEY\n     A            FINALAMT\n     A          K ACCOUNTKEY\n     A          S ACTIVE                    COMP(EQ 'Y')", "logical_view")
    write("data/ledger-pf.dds", "     A          R LEDGERREC\n     A            ACCOUNTKEY     8A\n     A            FINALAMT      11P 2\n     A            ACTIVE         1A", "physical_record")

    cases = []
    for kind, factor in (("request", 3), ("batch", 5)):
        writer = SourceWriter()
        program = f"{kind.upper()}-ENTRY"
        relative = f"entry/{kind}.cbl"
        writer.add(f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {program}.\nENVIRONMENT DIVISION.\nINPUT-OUTPUT SECTION.\nFILE-CONTROL.")
        writer.add('SELECT LEDGER-FILE ASSIGN TO "LEDGER-LF"\n ORGANIZATION IS INDEXED\n ACCESS MODE IS DYNAMIC\n RECORD KEY IS ACCOUNT-KEY\n FILE STATUS IS FILE-STATUS.', "file_select")
        writer.add("DATA DIVISION.\nFILE SECTION.\nFD LEDGER-FILE.\n01 LEDGER-RECORD.\n 05 ACCOUNT-KEY PIC X(8).\n 05 RECORD-AMOUNT PIC 9(9)V99.", "file_layout")
        writer.add("WORKING-STORAGE SECTION.")
        writer.add('COPY "active/layout-root.cpy".', "layout_include")
        writer.add("01 INPUT-AMOUNT PIC 9(9)V99.\n01 SERVICE-FEE PIC 9(5)V99 VALUE 7.\n01 FINAL-AMOUNT PIC 9(9)V99.\n01 OVERRIDE-FLAG PIC 9.\n01 ROUTE-ENABLED PIC 9.\n01 STOP-FLAG PIC 9.\n01 FILE-STATUS PIC XX.\n01 AUX-COUNT PIC 9(12).", "declarations")
        writer.add(f'COPY "active/rate-template.cpy"\n REPLACING ==RATE-TOKEN== BY ==RATE-VALUE==\n ==FACTOR== BY =={factor}==.', "rate_include")
        writer.add("PROCEDURE DIVISION.")
        writer.add(f'COPY "active/{kind}-skeleton.cpy".', "entry_include")
        writer.add(f"INITIALIZE-{kind.upper()}.\nMOVE 0 TO STOP-FLAG.\nEXIT.\nMAIN-ACTION.")
        writer.add("IF ROUTE-ENABLED = 1", "outer_guard")
        writer.fill(spec["entry_lines"] // 3, sentence_end=False)
        writer.add('COPY "active/rule-root.cpy".', "rule_include")
        writer.fill(spec["entry_lines"] * 2 // 3, sentence_end=False)
        writer.add("IF OVERRIDE-FLAG = 1\n MOVE 0 TO FINAL-AMOUNT\nEND-IF\nEND-IF.", "later_override")
        writer.fill(spec["entry_lines"] - 20)
        writer.add('MOVE "NX" TO LEDGER-FUNCTION\nMOVE ACCOUNT-KEY TO LEDGER-KEY\nMOVE "LEDGER-LF" TO LEDGER-VIEW\nCALL "LEDGERIO"\n USING BY REFERENCE\n LEDGER-PARAMS\n ON EXCEPTION MOVE "90" TO LEDGER-STATUS\nEND-CALL.', "runtime_call")
        writer.add('IF LEDGER-STATUS = "00"\n MOVE FINAL-AMOUNT TO RECORD-AMOUNT\n WRITE LEDGER-RECORD\nEND-IF.\nMOVE 1 TO STOP-FLAG.', "output_guard")
        writer.fill(spec["entry_lines"] - 1)
        writer.add("EXIT.")
        locations = writer.save(source, relative)
        required = [locations[name] for name in ("entry_include", "outer_guard", "rule_include", "declarations", "rate_include", "later_override", "runtime_call", "output_guard", "file_select", "file_layout", "layout_include")]
        required += [spans[name] for name in (f"{kind}_skeleton", "layout_root", "layout_leaf", "rate_template", "rule_root", "rule_leaf", "logical_view", "physical_record")]
        cases.append({"id": kind, "entry_program": program, "entry_path": relative,
            "question": f"{program} 从入口COPY骨架怎样进入业务处理？FINAL-AMOUNT 的完整公式、两层触发条件及后续覆盖是什么？LEDGER-LF 与 LEDGERIO 的参数/状态/输出怎样对应；缺少封闭运行库时哪些结果仍不能确认？",
            "required_ranges": required,
            "semantic_gold": {"factor": factor, "service_fee": 7, "outer_guard": "ROUTE-ENABLED = 1",
                "inner_guard": "INPUT-AMOUNT > 0", "override": "OVERRIDE-FLAG = 1 => FINAL-AMOUNT = 0",
                "operation_literal": "NX", "logical_view": "LEDGER-LF", "missing_targets": ["LEDGERIO"],
                "runtime_claims_forbidden": ["returned_status_proven", "records_actually_updated", "commit_completed", "runtime_filter_applied"]}})

    longline = SourceWriter()
    longline.add("IDENTIFICATION DIVISION.\nPROGRAM-ID. PHYSICAL-LINE-PROBE.\nPROCEDURE DIVISION.")
    longline.add('IF PROBE-TEXT = "' + "X" * 13000 + '"', "long_physical_line")
    longline.add("MOVE 7 TO PROBE-RESULT\nEND-IF.\nGOBACK.", "longline_tail")
    spans.update(longline.save(source, "probes/longline.cbl"))
    for index in range(spec["copies"]):
        # Same basename, some identical bytes, some conflicting declarations.
        text = "01 SHARED-ROW.\n 05 SHARED-AMOUNT PIC 9(9).\n"
        if index % 4 == 0:
            text = f"01 SHARED-ROW.\n 05 SHARED-AMOUNT PIC 9(9) VALUE {index % 97}.\n"
        path = source / f"redundant/copy-{index:05d}/layout-leaf.cpy"
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")
    for index in range(spec["programs"]):
        rows = ["IDENTIFICATION DIVISION.", f"PROGRAM-ID. AUX-{index:05d}.", "DATA DIVISION.", "WORKING-STORAGE SECTION.", 'COPY "layout-leaf.cpy".', "01 FINAL-AMOUNT PIC 9(9).", "01 INPUT-AMOUNT PIC 9(9).", "PROCEDURE DIVISION."]
        for repeat in range(12):
            rows += [f"IF INPUT-AMOUNT > {repeat}", f"COMPUTE FINAL-AMOUNT = INPUT-AMOUNT * {index % 89 + 11}", "END-IF.", "DISPLAY FINAL-AMOUNT."]
        rows += ["GOBACK."]
        path = source / f"redundant/program-{index:05d}.cbl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    files = []
    for path in sorted(source.rglob("*")):
        if path.is_file():
            raw = path.read_bytes()
            files.append({"path": path.relative_to(source).as_posix(), "sha256": digest(raw), "bytes": len(raw), "lines": len(raw.splitlines())})
    manual = """# Record processing R1.0

## Database I/O

CALL XXXXXXIO USING XXXXXX-PARAMS

The operation is selected by XXXXXX-FUNCTION. XXXXXX-KEY is a caller supplied key;
XXXXXX-VIEW names the requested logical view. The caller must inspect XXXXXX-STATUS.

### Operations

| Function | Meaning | Caution |
| --- | --- | --- |
| NX | Request the following record. | No returned row or cursor behavior is guaranteed by the request. |

### Status

| Status | Meaning |
| --- | --- |
| 00 | The caller treats this as success. |
| 90 | The caller assigns this if the call raises an exception. |

## Evidence boundary

The runtime implementation is intentionally absent. The manual describes an
interface convention, not proof of runtime filters, returned values, physical
file updates, transaction completion, or compiler acceptance. The generated
source uses ordinary nested COPY and a separate nonnested COPY REPLACING.
The overlong physical line is an analyzer stress probe, not a compiler test.
"""
    (directory / "framework-reference.md").write_text(manual, encoding="utf-8")
    oracle = {"schema": "framework-scale-oracle/v1", "synthetic": True, "scale": scale,
        "specification": spec, "files": files, "cases": cases,
        "tool_chain_probes": {"longline": [spans["long_physical_line"], spans["longline_tail"]]},
        "scope": "Source evidence sufficiency and deterministic transport only; not model answer accuracy or compiler acceptance.",
        "manual_sha256": digest(manual.encode()),
        "generation": {"files": len(files), "unique_content_hashes": len({row["sha256"] for row in files}), "source_bytes": sum(row["bytes"] for row in files), "lines": sum(row["lines"] for row in files)}}
    (directory / "oracle.json").write_text(json.dumps(oracle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return oracle


def evidence_coverage(source, pages, required):
    """Validate bytes/physical lines before counting any oracle range as covered."""
    source = Path(source).resolve()
    verified, invalid = [], []
    cache = {}
    for index, page in enumerate(pages):
        try:
            path = (source / page["relative_path"]).resolve(strict=True)
            if not path.is_relative_to(source):
                raise ValueError("outside source root")
            if path not in cache:
                raw = path.read_bytes()
                cache[path] = digest(raw), raw.decode("utf-8").splitlines()
            version, lines = cache[path]
            start, end = page["start_line"], page["end_line"]
            if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
                raise ValueError("invalid physical line range")
            if version != page["source_sha256"]:
                raise ValueError("file hash mismatch")
            expected = "\n".join(lines[start - 1:end])
            if page.get("span_truncated") or page["source_text"] != expected:
                raise ValueError("truncated or mismatched actual wire text")
            verified.append(page)
        except (KeyError, OSError, ValueError, TypeError) as error:
            invalid.append({"page": index, "reason": str(error)})
    details = []
    for need in required:
        covered = set()
        for page in verified:
            if page["relative_path"] == need["path"] and page["source_sha256"] == need["sha256"]:
                covered.update(range(page["start_line"], page["end_line"] + 1))
        missing = sorted(set(range(need["start_line"], need["end_line"] + 1)) - covered)
        details.append({"path": need["path"], "start_line": need["start_line"], "end_line": need["end_line"],
                        "complete": not missing, "missing_lines": missing})
    return {"complete": bool(required) and all(row["complete"] for row in details) and not invalid,
        "required_ranges": len(details), "covered_ranges": sum(row["complete"] for row in details),
        "ranges": details, "invalid_pages": invalid,
        "verified_wire_lines": sum(len(page["source_text"].splitlines()) for page in verified),
        "unique_verified_spans": len({(page["relative_path"], page["source_sha256"], page["start_line"], page["end_line"]) for page in verified})}


def audit_wire_memory(payload, prior_wires):
    """Separate actually retransmitted source from notes pointing at old source.

    Prior wires must contain their actual body hash and pages to validate the
    declared origin. A model draft or source locator never counts as raw text.
    """
    bindings = ("relative_path", "source_sha256", "start_line", "end_line",
                "include_chain", "replacement_trace", "copy_instance")
    report = {"blocks": [], "retransmitted_pages": []}
    for block in payload.get("prior_source_memory", {}).get("blocks", []):
        row = {"content_kind": block.get("content_kind"), "origins": [], "full_source_retransmitted": False}
        for identifier, origin in zip(block.get("evidence_ids", []), block.get("origins", [])):
            matched = None
            for previous in prior_wires:
                if origin.get("round_id") != f"round-{previous['sequence']}":
                    continue
                for page in previous["pages"]:
                    binding = {key: page[key] for key in bindings if key in page}
                    encoded = json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                    if (page.get("evidence_id") == identifier and origin.get("binding") == binding
                            and origin.get("binding_sha256") == digest(encoded)
                            and origin.get("excerpt_sha256") == digest(page["source_text"].encode())):
                        matched = (page, bool(previous.get("request_body_sha256")) and
                            previous["request_body_sha256"] == origin.get("request_body_sha256"))
            row["origins"].append({"evidence_id": identifier, "prior_wire_binding_matches": matched is not None,
                                   "request_body_hash_matches": bool(matched and matched[1])})
            if (matched and matched[1] and block.get("content_kind") == "source_reference"
                    and block.get("text") == matched[0]["source_text"]):
                report["retransmitted_pages"].append(matched[0])
                row["full_source_retransmitted"] = True
        report["blocks"].append(row)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--scale", choices=SCALES, default="focused")
    args = parser.parse_args()
    oracle = build_fixture(args.directory, args.scale)
    print(json.dumps(oracle["generation"]))


if __name__ == "__main__":
    main()
