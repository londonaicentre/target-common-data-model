# /// script
# requires-python = ">=3.10"
# dependencies = ["linkml==1.12.1"]
# ///
"""Generate the browsable docs site sources from the LinkML spec.

Writes Markdown for an mkdocs site (config: mkdocs.yml) into build/site_docs/:

  * One page per class, slot and enum, via LinkML's DocGenerator, from a root
    schema that imports every cdm/<Resource>.yaml. FHIR lineage
    (fhir_path / fhir_type / value_set) shows up as annotations on each slot.
  * erd.dbml, a simplified copy of docs/erd.dbml for drawing: enums, notes and
    full structured types dropped, so the rendered SVG stays readable. The
    workflow renders it to erd.svg with dbml-renderer and erd.md embeds it.

The site is built and published to GitHub Pages by .github/workflows/docs.yml.
Nothing here is committed; regenerate rather than editing.

Usage:  uv run scripts/generate_docs_site.py [OUTPUT_DIR]   (default: build/site_docs)
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

from linkml.generators.docgen import DocGenerator

REPO = Path(__file__).parent.parent
CDM = REPO / "cdm"

ROOT_SCHEMA = """\
id: https://cdm.aicentre.co.uk/flatfhir
name: flatfhir
title: FlatFHIR CDM
description: >-
  A flattened analytical data model derived from UK Core FHIR R4. Each FHIR
  resource becomes one table; see the [ERD](erd.md) for how they relate.
prefixes:
  linkml: https://w3id.org/linkml/
  aiccdm: https://cdm.aicentre.co.uk/
default_prefix: aiccdm
imports:
- linkml:types
{imports}
"""

ERD_PAGE = """\
# ERD

Tables and their foreign keys. Struct and array columns are shown as `object` /
`array`; open a table's page for the full type and FHIR lineage of each column.
Click the diagram to open it full size.

[![FlatFHIR ERD](erd.svg)](erd.svg)
"""


def simplify_dbml(text: str) -> str:
    """Strip what dbml-renderer can't draw legibly (or at all)."""
    text = re.sub(r"(?ms)^enum \S+ \{.*?^\}\n\n?", "", text)   # '<' etc. break it
    text = re.sub(r" \[note: '(?:[^'\\]|\\.)*'\]", "", text)
    text = re.sub(r", note: '(?:[^'\\]|\\.)*'", "", text)
    text = re.sub(r'"array\(.*\)"', "array", text)
    text = re.sub(r'"object\(.*\)"', "object", text)
    return text


def main(outdir: Path) -> None:
    src = outdir.parent / "site_schema"
    for d in (outdir, src):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)

    # A literal "|" in a description (FHIR short text, e.g. "a | b | c")
    # splits DocGenerator's Markdown table cells; swap it on the copies.
    for f in CDM.glob("*.yaml"):
        text = re.sub(
            r"^(\s*description: .*)$",
            lambda m: m.group(1).replace(" | ", " / "),
            f.read_text(),
            flags=re.M,
        )
        (src / f.name).write_text(text)
    imports = "\n".join(f"- {f.stem}" for f in sorted(CDM.glob("*.yaml")))
    root = src / "flatfhir.yaml"
    root.write_text(ROOT_SCHEMA.format(imports=imports))

    DocGenerator(str(root), mergeimports=True).serialize(directory=str(outdir))

    (outdir / "erd.dbml").write_text(simplify_dbml((REPO / "docs/erd.dbml").read_text()))
    (outdir / "erd.md").write_text(ERD_PAGE)
    print(f"  wrote {len(list(outdir.glob('*.md')))} pages -> {outdir}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "build" / "site_docs")
