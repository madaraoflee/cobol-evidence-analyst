"""Compare locally built native observations with the default Python IR."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "poc"))

from compilation_context import CompilationContext
from parser_adapter import BuiltinParserAdapter, IsolatedParserAdapter, PROLEAP, compare_normalized_ir


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = args.runtime_root.resolve(strict=True)
    java = next(root.glob("*/Contents/Home/bin/java"))
    bridge = Path(__file__).resolve().parent / "bridge.py"
    classpath = str(root / "classes") + ":" + ":".join(str(path) for path in root.glob("*.jar"))
    artifacts = [Path(sys.executable).resolve(), bridge, java, *root.glob("*.jar"), *(root / "classes").rglob("*.class")]
    hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in artifacts}
    adapter = IsolatedParserAdapter(PROLEAP, command=(sys.executable, str(bridge), str(java), classpath),
                                    artifact_hashes=hashes, timeout_seconds=30)
    template = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. TRIALPG.\nDATA DIVISION.\nWORKING-STORAGE SECTION.\n"
        "01 A PIC 9(4).\n01 B PIC 9(4).\n01 C PIC 9(4).\n01 INPUT-REC.\n05 AMOUNT PIC 9(4).\n"
        "01 OUTPUT-REC.\n05 AMOUNT PIC 9(4).\nPROCEDURE DIVISION.\nMAIN.\n{body}\nGOBACK.\n")
    probes = [("giving-distinct", "ADD A TO B GIVING C."),
              ("giving-same-storage", "ADD A TO A GIVING A."),
              ("terminator-scope", "COMPUTE C = A * 2 END-COMPUTE."),
              ("same-physical-line", "MOVE 1 TO A MOVE 2 TO B."),
              ("qualified-storage", "COMPUTE AMOUNT OF OUTPUT-REC = AMOUNT OF INPUT-REC * 2."),
              ("scope-not-converted", "IF A > 0 COMPUTE C = A * 2 END-IF.")]
    report = {"schema_version": "native-parser-ir-trial/v1", "parser": {"name": PROLEAP.name, "version": PROLEAP.version},
        "native_asg_executed": False, "source_domain": "original_synthetic_only", "enterprise_source_used": False,
        "default_parser_changed": False, "bridge_scope": "MOVE, COMPUTE, ADD GIVING and simple exits; control scopes unsupported",
        "artifact_fingerprint": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(),
        "artifact_hashes": {path.name: value for path, value in ((Path(key), value) for key, value in hashes.items()) if path.suffix != ".class"},
        "native_classes_fingerprint": hashlib.sha256(json.dumps({key: value for key, value in hashes.items()
            if key.endswith(".class")}, sort_keys=True).encode()).hexdigest(),
        "source_archive_sha256": hashlib.sha256((root / "parser-source.tar.gz").read_bytes()).hexdigest(),
        "runtime_archive_sha256": hashlib.sha256((root / "jre.tar.gz").read_bytes()).hexdigest(),
        "native_probe_source_sha256": hashlib.sha256((bridge.parent / "SyntaxProbe.java").read_bytes()).hexdigest(),
        "probes": [],
        "counts": {"correct": 0, "unsupported": 0, "incorrect": 0}}
    context = CompilationContext(source_format="free", dialect="cobol-85")
    report["compilation_context"] = context.to_dict()
    report["compilation_context_sha256"] = context.fingerprint
    report["builtin_adapter_version"] = BuiltinParserAdapter.version
    for name, body in probes:
        source = template.format(body=body)
        reference = BuiltinParserAdapter().parse(source, relative_path="entry.cbl", context=context)
        started = time.monotonic()
        candidate = adapter.parse(source, relative_path="entry.cbl", context=context)
        report["native_asg_executed"] |= bool(candidate.adapter.get("native_asg_executed"))
        compared = compare_normalized_ir(reference, candidate)
        for key, value in compared["counts"].items():
            report["counts"][key] += value
        report["probes"].append({"id": name, "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "native_elapsed_seconds": round(time.monotonic()-started, 4), **compared,
            "reference_ir": reference.to_dict(), "candidate_ir": candidate.to_dict()})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"native_asg_executed": report["native_asg_executed"], "counts": report["counts"], "report": str(args.output)}))


if __name__ == "__main__":
    main()
