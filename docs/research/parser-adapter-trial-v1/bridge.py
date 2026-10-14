"""Experimental local JSON bridge for a separately built fixed native parser."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "poc"))

from compilation_context import resolve_compilation_context
from parser_adapter import BuiltinParserAdapter, PROLEAP, _source_refs, expression_tree
from semantic_ir import CompilationIR, ParsedStatement, bind_reference, digest


def main():
    request = json.load(sys.stdin)
    context = resolve_compilation_context(request["compilation_context"])
    if context.source_format == "fixed" or request.get("source_map"):
        raise ValueError("native_trial_position_mapping_not_supported")
    source = request["source"]
    source_hash = request.get("source_sha256") or hashlib.sha256(source.encode()).hexdigest()
    local = BuiltinParserAdapter().parse(source, relative_path=request["relative_path"],
                                        context=context, source_sha256=source_hash)
    with tempfile.TemporaryDirectory(prefix="native-probe-") as directory:
        path = Path(directory) / "entry.cbl"
        # Fixed candidate release has no FREE enum. For this deliberately
        # continuation-free trial, a neutral TANDEM indicator is reversible.
        path.write_text("\n".join(" " + line for line in source.splitlines()) + "\n", encoding="utf-8")
        process = subprocess.run([sys.argv[1], "-Xmx512m", "-cp", sys.argv[2], "SyntaxProbe", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False)
        if process.returncode:
            error_lines = process.stderr.decode(errors="replace").splitlines()
            diagnostic = next((line for line in error_lines if "Exception" in line or "syntax error" in line), "native process failed")
            ir = CompilationIR(context.to_dict(), {"name": PROLEAP.name, "version": PROLEAP.version,
                "native_asg_executed": False}, diagnostics=({"reason": "native_parser_rejected_source", "detail": diagnostic},))
            print(json.dumps({"schema_version": "parser-bridge/v1", "parser": asdict(PROLEAP), "ir": ir.to_dict()}))
            return
        observed = json.loads(process.stdout)
    statements, diagnostics = [], []
    for index, row in enumerate(observed["statements"]):
        refs = _source_refs(request["relative_path"], source_hash, row["start_line"], row["end_line"],
                            row["start_column"] - 1, row["end_column"] - 1)
        reads = tuple(bind_reference(name, local.data_items, refs) for name in dict.fromkeys(row["reads"]))
        writes = tuple(bind_reference(name, local.data_items, refs) for name in dict.fromkeys(row["writes"]))
        expressions = tuple((target.identity, expression_tree(value)) for target, value in zip(writes, row["expressions"]))
        unsupported = row["scope_unsupported"]
        shape = digest([row["kind"], [ref.identity for ref in reads], [ref.identity for ref in writes],
                        [asdict(tree) for _, tree in expressions]])
        occurrence = digest([[asdict(ref) for ref in refs], index, shape])
        statements.append(ParsedStatement("native_" + occurrence[:24], "shape_" + shape[:24],
            row["kind"], row["text"], refs, reads, writes, expressions,
            parsed_status="unsupported" if unsupported else "parsed", binding_status="bound" if
                all(ref.binding_status == "bound" for ref in reads + writes) else "unresolved", rounded=row["rounded"],
            diagnostics=("native_trial_control_scope_not_converted",) if unsupported else ()))
        if unsupported:
            diagnostics.append({"reason": "native_trial_control_scope_not_converted", "source_refs": [asdict(ref) for ref in refs]})
    result = CompilationIR(context.to_dict(), {"name": PROLEAP.name, "version": PROLEAP.version, "native_asg_executed": True},
        local.data_items, local.storage_regions, tuple(statements), tuple(diagnostics), not diagnostics)
    print(json.dumps({"schema_version": "parser-bridge/v1", "parser": asdict(PROLEAP), "ir": result.to_dict()}))


if __name__ == "__main__":
    main()
