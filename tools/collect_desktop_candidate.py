"""Collect an accepted candidate without copying profiles or raw source traces."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "poc"))
from release_acceptance import evaluate_release, file_sha256


def collect(*, package, report, manifest, policy, destination):
    result = evaluate_release(package=package, compiled_report=report, manifest=manifest, policy=policy)
    if result["candidate_allowed"] is not True:
        raise ValueError("CANDIDATE_ACCEPTANCE_FAILED")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    checksum = Path(package).with_suffix(".zip.sha256")
    expected = f"{file_sha256(package)}  {Path(package).name}"
    if checksum.read_text(encoding="ascii").strip() != expected:
        raise ValueError("DELIVERY_CHECKSUM_MISMATCH")
    for source, name in ((package, Path(package).name), (checksum, checksum.name),
                         (report, "COMPILED-ACCEPTANCE.json"), (manifest, "ACCEPTANCE-MANIFEST.json"),
                         (policy, "CANDIDATE-POLICY.json")):
        shutil.copyfile(source, destination / name)
    with zipfile.ZipFile(package) as archive:
        (destination / "BUILD-INFO.json").write_bytes(archive.read("COBOLWorkbench/BUILD-INFO.json"))
    # Re-evaluate the copied bytes; a modified input cannot race past collection.
    copied = evaluate_release(package=destination / Path(package).name,
        compiled_report=destination / "COMPILED-ACCEPTANCE.json",
        manifest=destination / "ACCEPTANCE-MANIFEST.json", policy=destination / "CANDIDATE-POLICY.json")
    if copied != result:
        raise ValueError("CANDIDATE_CHANGED_DURING_COLLECTION")
    (destination / "CANDIDATE-GATE.json").write_text(json.dumps(copied, indent=2) + "\n", encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("package", "report", "manifest", "policy", "destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args(argv)
    result = collect(**vars(args))
    print(json.dumps({"candidate_allowed": result["candidate_allowed"],
                      "publish_allowed": result["publish_allowed"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
