#!/usr/bin/env python3
"""Import generated CDM dbt contracts and reference seeds into a dbt project.

Usage:
  python import_cdm.py [--check] [--ref REF] [--target DIR] [--models-path DIR]

Examples:
  python import_cdm.py --check              Report status without changing anything
  python import_cdm.py                      Import the latest revision from main
  python import_cdm.py --ref v7             Import a specific revision
  python import_cdm.py --target warehouse   Target a dbt project in a subdirectory

Everything is written to <target>/cdm_target_spec/:

  models/     dbt model contracts, one per resource
  seeds/      reference seed CSVs and their properties file
  REVISION    the CDM revision currently held by this project

That directory sits outside dbt's model-paths and seed-paths, so dbt never reads
it. Importing therefore cannot break the build, even for resources that have not
been modelled yet. A contract takes effect only when someone copies it into the
live models/ path, which should happen once that model's SQL matches it.

The directory is mirrored on each import, so files removed from the spec are also
removed here. Do not keep hand written files inside it.

Environment:
  CDM_SPEC_REPO   Override the spec repository (accepts a local path).
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

try:
    import yaml
except ImportError:
    yaml = None

SPEC_REPO = os.environ.get(
    "CDM_SPEC_REPO", "https://github.com/londonaicentre/target-common-data-model.git"
)
SPEC_SRC = "dbt_metadata"
DEST = "cdm_target_spec"
REVISION = "REVISION"


def clone(ref: str, into: Path) -> None:
    try:
        subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", "--branch", ref, SPEC_REPO, str(into)],
            check=True,
        )
    except subprocess.CalledProcessError:
        sys.exit(
            f"error: could not fetch '{ref}' from {SPEC_REPO}\n"
            f"  check that the revision tag or branch exists and that you can reach the repository"
        )


def read_revision(path: Path) -> str | None:
    return path.read_text(encoding="utf-8").strip() if path.exists() else None


def same_contract(a: Path, b: Path) -> bool:
    """Compare two contracts by parsed content, so formatting is not a difference."""
    if yaml is not None:
        try:
            return yaml.safe_load(a.read_text(encoding="utf-8")) == yaml.safe_load(
                b.read_text(encoding="utf-8")
            )
        except yaml.YAMLError:
            pass
    return filecmp.cmp(a, b, shallow=False)


def promotion_status(target: Path, models_path: str) -> list[tuple[str, str, str]]:
    """Report, for each imported contract, whether it is promoted and still matches."""
    live = target / models_path
    rows = []
    for contract in sorted((target / DEST / "models").rglob("*.yml")):
        found = [p for p in live.rglob(contract.name) if DEST not in p.parts] if live.exists() else []
        if not found:
            rows.append((contract.stem, "not promoted", ""))
        else:
            state = "identical" if same_contract(contract, found[0]) else "modified"
            rows.append((contract.stem, state, str(found[0].relative_to(target)).replace("\\", "/")))
    return rows


def report_status(args, held: str | None, available: str) -> None:
    print("CDM contract status\n")
    print(f"  Published revision   {available}   ({args.ref})")
    print(f"  Imported revision    {held or '-'}")
    print()
    if held is None:
        print("  Nothing imported yet. Rerun without --check to import.")
        return
    if held == available:
        print("  Up to date.")
    else:
        print("  An update is available. Rerun without --check to import it.")

    rows = promotion_status(Path(args.target).resolve(), args.models_path)
    if not rows:
        return
    print(f"\nPromoted contracts   (compared with {DEST}/models/)\n")
    for name, state, path in rows:
        print(f"  {name:<14} {state:<14} {path}".rstrip())
    counts = {s: sum(1 for _, st, _ in rows if st == s) for s in ("identical", "modified", "not promoted")}
    print("\n  " + ", ".join(f"{n} {s}" for s, n in counts.items() if n))


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="import_cdm.py",
        description="Import generated CDM dbt contracts and reference seeds into a dbt project.",
    )
    ap.add_argument("--target", default=".", metavar="DIR", help="dbt project directory (default: .)")
    ap.add_argument("--ref", default="main", metavar="REF", help="revision tag or branch (default: main)")
    ap.add_argument("--check", action="store_true", help="report status and exit without changing files")
    ap.add_argument("--models-path", default="models", metavar="DIR", help="live models directory (default: models)")
    args = ap.parse_args()

    target = Path(args.target).resolve()
    if not (target / "dbt_project.yml").exists():
        sys.exit(f"error: no dbt_project.yml in {target}\n  --target must point at a dbt project directory")

    held = read_revision(target / DEST / REVISION)

    with tempfile.TemporaryDirectory() as tmp:
        spec = Path(tmp) / "spec"
        clone(args.ref, spec)

        src = spec / SPEC_SRC
        available = read_revision(src / REVISION)
        if available is None:
            sys.exit(f"error: {SPEC_SRC}/{REVISION} not found in the spec repository at '{args.ref}'")

        if args.check:
            report_status(args, held, available)
            return

        dest = target / DEST
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)

    moved = "unchanged at" if held == available else f"{held or 'none'} ->"
    print(f"Imported CDM revision {moved} {available} from {args.ref}\n")
    print(f"  Contracts   {DEST}/models/")
    print(f"  Seeds       {DEST}/seeds/")


if __name__ == "__main__":
    main()
