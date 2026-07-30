#!/usr/bin/env python3
"""Import the generated CDM dbt artifacts into a consumer dbt project.

Run this from a dbt project to pull in the current model contracts and reference
seeds.

  python import_cdm.py --check          report where this project stands
  python import_cdm.py                  import the latest from main
  python import_cdm.py --ref r7         import a specific revision
  python import_cdm.py --target phm     dbt project lives in a subdirectory

Everything lands in a single directory, <target>/cdm_target_spec/:

  models/     dbt model contracts, one per resource
  seeds/      reference seed CSVs and their properties file
  REVISION    which CDM revision this project currently holds

The output of this script sits outside the common convention for dbt models. It cannot break your build
even for resources you have not modelled yet. Nothing takes effect until someone manually
copies a file out of here into the live models/ or seeds/ path.

The directory is mirrored wholesale on each import, so anything dropped from the
spec disappears here too. Never keep hand written files inside it.
"""

from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SPEC_REPO = os.environ.get(
    "CDM_SPEC_REPO", "https://github.com/londonaicentre/target-common-data-model.git"
)
SPEC_SRC = "dbt_metadata"        # generated artifacts inside the spec repo
DEST = "cdm_target_spec"         # where they land in the consumer repo
REVISION = "REVISION"


def clone(ref: str, into: Path) -> None:
    subprocess.run(
        ["git", "clone", "--quiet", "--depth", "1", "--branch", ref, SPEC_REPO, str(into)],
        check=True,
    )


def read_revision(path: Path) -> str | None:
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def promotion_status(target: Path, models_path: str) -> list[tuple[str, str]]:
    """For each imported contract, is it promoted into the live models path?

    Matches on filename, and compares content so that drift shows up whether the
    spec moved on or the promoted copy was hand-edited.
    """
    imported = sorted((target / DEST / "models").rglob("*.yml"))
    live = target / models_path
    rows = []
    for contract in imported:
        matches = [p for p in live.rglob(contract.name) if DEST not in p.parts]
        if not matches:
            rows.append((contract.stem, "not promoted"))
        elif filecmp.cmp(contract, matches[0], shallow=False):
            rows.append((contract.stem, f"promoted, matches ({matches[0].relative_to(target)})"))
        else:
            rows.append((contract.stem, f"promoted, DIFFERS ({matches[0].relative_to(target)})"))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Import generated CDM dbt artifacts into this dbt project.",
    )
    ap.add_argument("--target", default=".", help="path to the dbt project (default: .)")
    ap.add_argument("--ref", default="main", help="revision tag to import, or main (default)")
    ap.add_argument("--check", action="store_true", help="report status, change nothing")
    ap.add_argument("--models-path", default="models", help="live models directory (default: models)")
    args = ap.parse_args()

    target = Path(args.target).resolve()
    if not (target / "dbt_project.yml").exists():
        sys.exit(f"error: no dbt_project.yml found in {target} - check --target")

    held = read_revision(target / DEST / REVISION)

    with tempfile.TemporaryDirectory() as tmp:
        spec = Path(tmp) / "spec"
        clone(args.ref, spec)

        src = spec / SPEC_SRC
        available = read_revision(src / REVISION)
        if available is None:
            sys.exit(f"error: no {SPEC_SRC}/{REVISION} in the spec repo at '{args.ref}'")

        if args.check:
            print("CDM revision")
            print(f"  {f'published ({args.ref})':<18}: {available}")
            print(f"  {'imported here':<18}: {held or 'nothing imported yet'}")
            print("  up to date" if held == available else "  -> an update is available")
            if held:
                print(f"\nPromoted contracts (vs imported copy, under {args.models_path}/)")
                for name, state in promotion_status(target, args.models_path):
                    print(f"  {name:<14} {state}")
            return

        dest = target / DEST
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)

    print(f"imported CDM revision {held or 'none'} -> {available}")
    print(f"Review the changes under {DEST}/, then commit them and open a PR.")


if __name__ == "__main__":
    main()
