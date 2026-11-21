"""Replay the same local workbench exchanges on the frozen baseline or the checkout."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[3]
BASELINE = "87bdb9361aae5a4e3e28e638e21d70d8d78aaed8"
NEW_TEST = "poc/tests/test_local_grep_followup_delivery.py"


def capture(root):
    sys.path[:0] = [str(root / "poc/tests"), str(root / "poc")]
    from test_local_grep_followup_delivery import SCENARIOS, replay_case
    import local_grep_acceptance as previous

    product = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted((root / "poc").glob("*.py"))}
    output = {"question": previous.QUESTION,
              "python_product_sha256": hashlib.sha256(json.dumps(product, sort_keys=True).encode()).hexdigest(),
              "new_cases": {}, "previous_four": {}}
    for scenario in SCENARIOS:
        run = replay_case(scenario)
        # Each round's returned grep records and source windows are shown, as seen by the model.
        # Only keep the latest record for a term when the transport repeats older history.
        for row in run["requests"]:
            latest = {action["target"]: action for action in row.pop("grep_actions")}
            row["searches"] = [{k: action[k] for k in ("target", "checked_files", "complete", "has_more",
                "pending_hit_count", "matches", "read_ids") if k in action} for action in latest.values()]
            row["visible_search_steps"] = row["visible_search_steps"][-1:]
        run["cited_originals"] = [dict(ref) for ref in run.pop("citations")
                                  if ref["evidence_id"] in run["inline_citations"]]
        output["new_cases"][scenario] = run
    for scenario in previous.SCENARIOS:
        run = previous.replay(scenario)
        output["previous_four"][scenario] = {"rounds": len(run["requests"]), "status": run["status"],
            "stop_reason": run["stop_reason"], "network_connections": run["network_connections"],
            "inline_citations": run["inline_citations"], "answer_sha256": hashlib.sha256(run["answer"].encode()).hexdigest()}
    print(json.dumps(output, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.baseline:
        with TemporaryDirectory() as temporary:
            code = Path(temporary)
            archive = subprocess.check_output(["git", "-C", str(ROOT), "archive", "--format=zip", BASELINE, "poc"])
            with ZipFile(io.BytesIO(archive)) as files:
                files.extractall(code)
            shutil.copyfile(ROOT / NEW_TEST, code / NEW_TEST)
            subprocess.run([sys.executable, "-I", "-B", __file__, "--root", str(code)], check=True)
    else:
        capture(args.root or ROOT)


if __name__ == "__main__":
    main()
