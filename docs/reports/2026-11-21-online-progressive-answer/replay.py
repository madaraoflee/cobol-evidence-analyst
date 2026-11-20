"""Observe the ordinary online workbench using a held, deliberately incomplete reply."""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
from zipfile import ZipFile

BASE = "97231568836ab4ae5e9e7cc6f58d4be19abfab5f"
ROOT = Path(__file__).resolve().parents[3]


def main():
    # Load only the measurement helper from this change. All application and
    # historical scenario imports resolve to the requested code revision.
    from importlib.util import module_from_spec, spec_from_file_location
    spec = spec_from_file_location("pilot", ROOT / "poc/tests/online_progressive_acceptance.py")
    pilot = module_from_spec(spec)
    spec.loader.exec_module(pilot)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true", help="load production and fixture from the pinned Git revision")
    parser.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.baseline:
        with tempfile.TemporaryDirectory() as directory:
            archive = subprocess.check_output(["git", "-C", str(ROOT), "archive", "--format=zip", BASE, "poc"])
            with ZipFile(io.BytesIO(archive)) as files:
                files.extractall(directory)
            subprocess.run([sys.executable, "-I", "-B", __file__, "--root", directory], check=True)
        return
    code = args.root or ROOT
    sys.path[:0] = [str(code / "poc/tests"), str(code / "poc")]
    rows = {}
    with patch("socket.socket.connect", side_effect=AssertionError("network prohibited")) as connect:
        for name, variant, paused in (("historical_question", False, False), ("resume_same_question", False, True),
                                      ("changed_conditions", True, False)):
            with tempfile.TemporaryDirectory() as directory:
                rows[name] = pilot.replay(variant, paused, Path(directory))
        pilot.require(connect.call_count == 0, "unexpected network attempt")
    product = dict(sorted((p.name, pilot.digest(p.read_bytes())) for p in (code / "poc").glob("online_*.py")))
    print(json.dumps({"question": pilot.QUESTION, "scripted_final_draft": pilot.DRAFT, "model_requests_over_network": 0,
                      "measurement": "Public workbench state and individually reopened source citations; synthetic replies.",
                      "online_modules_sha256": pilot.digest(json.dumps(product, sort_keys=True).encode()),
                      "runs": rows}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
