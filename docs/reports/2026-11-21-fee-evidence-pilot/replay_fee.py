"""Replay the public fee pilot locally. Print deterministic JSON to stdout."""

import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import subprocess
import sys
import tempfile


COMMIT = "81b91c76e8f657de871cb6a7f675dcd628d0a3ad"
REPORT = Path("docs/reports/2026-10-20-live-ui")
SCOPE = REPORT / "source"
IDENTITY = "neutral/fee-pilot"


def require(condition, description):
    if not condition:
        raise ValueError(description)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def checked_examples(oracle):
    rows = []
    for question in oracle["questions"]:
        for example in question.get("examples", []):
            amount = Decimal(example["base_amount"])
            raw = amount * Decimal("1.25") / 100
            rounded = raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            bounded = max(Decimal("2.00"), min(Decimal("15.00"), rounded))
            fee = Decimal("0.00") if example["request_state"] == "REVIEW" else bounded
            actual = (str(rounded), str(bounded), str(fee))
            expected = tuple(example[k] for k in ("rounded_formula_result", "after_minimum_maximum", "final_fee"))
            require(actual == expected, f"Oracle disagreement on {example}")
            rows.append(dict(example))
    require(len(rows) == 7, "Incomplete archived Decimal example set")
    return rows


def measure(checkout, material):
    # Import only the analysis modules shipped at the measured commit.
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(checkout / "poc"))
    from online_material_query import MaterialQuery
    from online_material_reader import DirectoryObjectStore, MaterialReader

    reader = MaterialReader(DirectoryObjectStore(material), expected_commit=COMMIT, expected_repository=IDENTITY)
    query = MaterialQuery(reader)
    source = checkout / SCOPE
    oracle_bytes = (checkout / REPORT / "oracle.json").read_bytes()
    oracle = json.loads(oracle_bytes)
    original_questions = [
        line[2:] for line in (checkout / REPORT / "README.md").read_text("utf-8").splitlines()
        if line.startswith("> ")
    ][:2]
    require(len(original_questions) == 2, "Missing original questions")
    sources = {}
    for spec in oracle["sources"]:
        name = spec["relative_path"]
        data = (source / name).read_bytes()
        require(sha256(data) == spec["sha256"], f"Fixture differs from oracle: {name}")
        sources[name] = {"sha256": sha256(data), "lines": len(data.splitlines())}

    def read(name, *, max_bytes):
        require(name in sources, f"Unexpected path: {name}")
        data = (source / name).read_bytes()
        require(len(data) <= max_bytes, f"Source exceeds read limit: {name}")
        return data

    searches = []
    for question in [*original_questions, "手续费如何计算"]:
        result = query.search(question)
        searches.append({"input": question, "status": result["status"], "matches": [
            {"file": item["relative_path"], "matched_terms": item["matched_terms"], "score": item["score"]}
            for item in result["matches"]
        ]})
    anchors = {}
    for word in ("FINAL-FEE", "RATE", "REQUEST-STATE", "STANDARD"):
        result = query.locate_global(word, read)
        require(result["complete"], f"Unconsumed literal result page for {word}")
        anchors[word] = result["matches"]
    candidates = query.arithmetic_candidates("fee.cbl")
    require(not candidates["truncated"], "Unconsumed arithmetic candidate page")
    writes = [row for row in candidates["candidates"] if "FINAL-FEE" in row["writes"]]
    require([row["start_line"] for row in writes] == [11, 14, 17, 20], "Unexpected write locations")
    calls = query.relations("entry.cbl", "CALLS")["relations"]
    call_rows = [{"target": row["target_name"], "line": row["start_line"], "status": row["status"]} for row in calls]
    require(len(calls) == 1 and call_rows[0]["line"] == 311, "Unexpected static call locations")

    # The assessor specifies full ranges. Navigation hits alone are not verified business evidence.
    excerpts = []
    lines = {}
    for path, spec in sources.items():
        page = query.evidence(path, 1, spec["lines"], read)
        require(not page["truncated"] and page["citation"]["hash"] == spec["sha256"], "Incomplete source read")
        lines[path] = page["text"].splitlines()
        excerpts.append({"citation": page["citation"], "complete_line_count": page["complete_line_count"]})
    require(all(line.strip() == "CONTINUE" for line in lines["entry.cbl"][10:310]), "Intervening scope changed")
    guard_checks = []
    for path, line, dependency, purpose in (
        ("fee.cbl", 5, "RATE", "rate value"),
        ("fee.cbl", 19, "REQUEST-STATE", "review override guard"),
        ("entry.cbl", 10, "STANDARD", "call guard"),
    ):
        key = {"path": path, "line": line}
        require(key not in anchors["FINAL-FEE"] and key in anchors[dependency], "Unexpected dependency anchors")
        guard_checks.append({**key, "source": lines[path][line-1].strip(), "role": purpose,
                             "in_result_literal_hits": False, "found_with_literal": dependency})
    require(lines["entry.cbl"][310].strip() == "CALL 'FEE-RULE' USING BASE-AMOUNT FINAL-FEE REQUEST-STATE"
            and lines["entry.cbl"][312].strip() == "END-IF", "Caller binding or scope differs")
    require(lines["fee.cbl"][9].strip() == "PROCEDURE DIVISION USING BASE-AMOUNT FINAL-FEE REQUEST-STATE.",
            "Callee parameter order differs")
    return {
        "fixture": oracle["fixture_id"], "commit": COMMIT, "scope": SCOPE.as_posix(),
        "material_revision": reader.manifest["revision"], "oracle_sha256": sha256(oracle_bytes), "sources": sources,
        "method": "Investigator-selected actions on the pinned online query API, using local Git-verified material.",
        "navigation": {"question_searches": searches, "global_literal_anchors": anchors,
                       "fee_arithmetic_candidates": writes, "entry_call_relations": call_rows},
        "assessor_selected_source_reads": excerpts,
        "target_field_only_comparison": guard_checks,
        "checked_decimal_oracle_examples": checked_examples(oracle),
        "call_semantics": {
            "guard": "entry.cbl:10, closed at 313, conditions the call at 311; lines 11-310 are CONTINUE.",
            "writeback": "Ordinary default BY REFERENCE; second caller argument matches second callee parameter.",
            "skip": "A non-STANDARD request skips the call and retains the existing fee.",
            "assumptions": oracle["assumptions"],
        },
        "new_real_model_requests": 0, "automatic_model_selected_source": "not measured",
        "business_answer_pair": "not generated or scored", "cobol_execution": "not performed",
        "enterprise_case": "not measured",
    }


def main():
    root = Path(__file__).resolve().parents[3]
    with tempfile.TemporaryDirectory() as directory:
        checkout = Path(directory) / "checkout"
        material = Path(directory) / "material"
        subprocess.run(["git", "clone", "--local", "--shared", "--no-checkout", "--quiet", str(root), str(checkout)],
                       check=True)
        subprocess.run(["git", "sparse-checkout", "set", "poc", REPORT.as_posix()], cwd=checkout, check=True)
        subprocess.run(["git", "-c", "core.autocrlf=false", "checkout", "--quiet", "--detach", COMMIT],
                       cwd=checkout, check=True)
        subprocess.run([sys.executable, "-B", "poc/online_analysis.py", "build", "--source", SCOPE.as_posix(),
                        "--output", str(material), "--repository", IDENTITY, "--commit", COMMIT,
                        "--subdirectory", SCOPE.as_posix()], cwd=checkout, check=True, stdout=subprocess.DEVNULL)
        print(json.dumps(measure(checkout, material), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
