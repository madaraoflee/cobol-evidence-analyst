"""Neutral, independently authored large-repository calculation expectations.

The source and gold are specified together before any production parser runs.
The oracle never obtains its expected set from an index or model result.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


def create_large_fixture(root, count=10001):
    root = Path(root)
    source = root / "source"
    source.mkdir(parents=True, exist_ok=True)
    gold_path = root / "gold.jsonl"
    entries = []
    for ordinal in range(count):
        name = f"unit-{ordinal:05d}.cbl"
        label = f"保费金额 {ordinal:05d}" if ordinal % 10 == 0 else f"Premium charge {ordinal:05d}"
        formula = f"COMPUTE DUE-AMOUNT = BASE-AMOUNT * 0.01 + {ordinal}."
        lines = ["IDENTIFICATION DIVISION.", f"PROGRAM-ID. UNIT-{ordinal:05d}.", "DATA DIVISION.",
            "WORKING-STORAGE SECTION.", "01 BASE-AMOUNT PIC 9(7)V99 VALUE 1000.",
            "01 DUE-AMOUNT PIC 9(7)V99 VALUE ZERO.", "PROCEDURE DIVISION.", "MAIN-PROCESS.",
            "*> Business calculation: " + label, formula, "GOBACK."]
        raw = ("\n".join(lines) + "\n").encode("utf-8")
        path = source / name
        if path.exists() and path.read_bytes() != raw:
            raise ValueError("Refusing to replace a different source: " + str(path))
        if not path.exists():
            path.write_bytes(raw)
        entries.append({"ordinal": ordinal, "path": name, "program_id": f"UNIT-{ordinal:05d}",
            "start_line": 10, "end_line": 10, "line_count": 11,
            "source_sha256": hashlib.sha256(raw).hexdigest(), "business_label": label, "formula": formula})
    gold_path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in entries), encoding="utf-8")
    manifest = {"schema": "large-business-inventory-gold/v1", "source_files": count,
        "independent_roots": count, "business_formula_positions": count,
        "source": str(source), "gold": str(gold_path),
        "gold_sha256": hashlib.sha256(gold_path.read_bytes()).hexdigest(),
        "sentinel_ordinals": [0, count // 2, count - 1],
        "authorship": "Generated business specification; expectations are independent of production discovery and model output.",
        "semantics": "Fixture formula and business-role retention only; no real-model business quality claim."}
    (root / "fixture.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def read_gold(path):
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def _normalize(value):
    return re.sub(r"\s+", " ", str(value)).strip().rstrip(".")


def evaluate_delivery(gold_path, records):
    """Score a fully paged or exported inventory, without trusting its counts."""
    expected = {(row["path"], row["start_line"], row["end_line"]): row for row in read_gold(gold_path)}
    observed, duplicates, unexpected, invalid = set(), [], [], []
    for record in records:
        if record.get("disposition", "included") != "included":
            continue
        key = (record.get("path", record.get("relative_path")), record.get("start_line"), record.get("end_line"))
        if key in observed:
            duplicates.append(key)
        observed.add(key)
        authored = expected.get(key)
        if authored is None:
            unexpected.append(key)
            continue
        body = " ".join(str(record.get(field, "")) for field in ("summary", "formula", "conditions"))
        digest = record.get("source_sha256", record.get("sha256"))
        if (_normalize(authored["formula"]) not in _normalize(body)
                or authored["business_label"] not in body
                or digest != authored["source_sha256"]):
            invalid.append(key)
    missing = sorted(set(expected) - observed)
    ordinals = [0, len(expected) // 2, len(expected) - 1]
    sentinels = [row for row in expected.values() if row["ordinal"] in ordinals]
    sentinel_valid = all((row["path"], row["start_line"], row["end_line"]) in observed
        and (row["path"], row["start_line"], row["end_line"]) not in invalid for row in sentinels)
    return {"expected_positions": len(expected), "unique_delivered_positions": len(observed),
        "missing_count": len(missing), "duplicate_count": len(duplicates), "unexpected_count": len(unexpected),
        "invalid_business_rows": len(invalid), "first_middle_last_preserved": sentinel_valid,
        "missing_sample": missing[:8], "duplicate_sample": duplicates[:8], "invalid_sample": invalid[:8],
        "complete": not (missing or duplicates or unexpected or invalid), "real_model_semantics_verified": False}
