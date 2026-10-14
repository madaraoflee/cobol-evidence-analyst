#!/usr/bin/env python3
"""Measure neutral record layout construction, with no model or network calls."""
from __future__ import annotations

import argparse
import hashlib
import json
import time

from parser_adapter import BuiltinParserAdapter


def record_source(fields):
    return ("IDENTIFICATION DIVISION.\nPROGRAM-ID. FIELD-RULE.\nDATA DIVISION.\n"
        "WORKING-STORAGE SECTION.\n01 SOURCE-RECORD.\n" + "".join(
            f"05 VALUE-{index:05d} PIC 9(9).\n" for index in range(fields)) +
        "PROCEDURE DIVISION.\nMAIN.\nCOMPUTE VALUE-00000 = VALUE-00001 * 2.\nGOBACK.\n")


def measure(fields):
    source = record_source(fields)
    started = time.perf_counter()
    ir = BuiltinParserAdapter().parse(source, relative_path="field-rule.cbl", context={"source_format": "free"})
    parsed = time.perf_counter()
    encoded = json.dumps(ir.to_dict())
    serialized = time.perf_counter()
    fingerprint = ir.fingerprint
    finished = time.perf_counter()
    return {"synthetic": True, "model_called": False, "fields": fields,
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(), "source_bytes": len(source.encode()),
        "adapter": ir.adapter, "parse_seconds": round(parsed - started, 6),
        "serialization_seconds": round(serialized - parsed, 6),
        "fingerprint_seconds": round(finished - serialized, 6),
        "overlap_entries": sum(len(region.overlaps) for region in ir.storage_regions),
        "serialized_ir_bytes": len(encoded.encode()), "ir_fingerprint": fingerprint,
        "statements": len(ir.statements), "parse_complete": ir.parse_complete}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fields", nargs="+", type=int, default=[500, 1000, 2000, 10000])
    args = parser.parse_args()
    if any(count < 2 or count > 100000 for count in args.fields):
        parser.error("field counts must be between 2 and 100000")
    print(json.dumps([measure(count) for count in args.fields], indent=2))


if __name__ == "__main__":
    main()
