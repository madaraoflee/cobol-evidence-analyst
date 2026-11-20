"""Capture normal local-workbench behavior against the pinned baseline or current product files."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[3]
BASELINE = "839d085166fbef89a882c917ea73c21f7eda380b"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--baseline", action="store_true")
    modes.add_argument("--baseline-tests", action="store_true", help="run the new regression tests against the old product")
    parser.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.baseline or args.baseline_tests:
        with tempfile.TemporaryDirectory() as temporary:
            archive = subprocess.check_output(["git", "-C", str(ROOT), "archive", "--format=zip", BASELINE, "poc"])
            with ZipFile(io.BytesIO(archive)) as files:
                files.extractall(temporary)
            if args.baseline_tests:
                for name in ("local_grep_acceptance.py", "test_local_grep_delivery.py"):
                    shutil.copyfile(ROOT / "poc/tests" / name, Path(temporary) / "poc/tests" / name)
                cmd = [sys.executable, "-I", "-B", "-m", "unittest", "discover", "-s", "poc/tests",
                       "-p", "test_local_grep_delivery.py", "-v"]
                result = subprocess.run(cmd, cwd=temporary, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                print("Product revision", BASELINE)
                print(result.stdout.replace(str(Path(temporary).resolve()), "<fixed-baseline>"), end="")
                raise SystemExit(result.returncode)
            subprocess.run([sys.executable, "-I", "-B", __file__, "--root", temporary], check=True)
        return
    code = args.root or ROOT
    sys.path[:0] = [str(code / "poc/tests"), str(code / "poc"), str(ROOT / "poc/tests")]
    from local_grep_acceptance import QUESTION, SCENARIOS, replay

    product = {}
    for pattern in ("poc/*.py", "poc/web/*"):
        for path in sorted(code.glob(pattern)):
            if path.is_file():
                product[path.relative_to(code).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps(product, sort_keys=True).encode()).hexdigest()
    print(json.dumps({"question": QUESTION, "product_sha256": fingerprint,
                      "scenarios": {scenario: replay(scenario) for scenario in SCENARIOS}},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
