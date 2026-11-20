"""Capture normal local-workbench behavior against the pinned baseline or current product files."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[3]
BASELINE = "59a00784bfcdb025f61bf749bd7869c3087f1d0e"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.baseline:
        with tempfile.TemporaryDirectory() as temporary:
            archive = subprocess.check_output(["git", "-C", str(ROOT), "archive", "--format=zip", BASELINE, "poc"])
            with ZipFile(io.BytesIO(archive)) as files:
                files.extractall(temporary)
            subprocess.run([sys.executable, "-I", "-B", __file__, "--root", temporary], check=True)
        return
    code = args.root or ROOT
    sys.path[:0] = [str(code / "poc/tests"), str(code / "poc"), str(ROOT / "poc/tests")]
    from local_progressive_acceptance import QUESTION, SCENARIOS, replay

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
