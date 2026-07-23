# /// script
# dependencies = ["pyyaml"]
# ///
"""
Generate cdm/enums.yaml from scripts/enum_manifest.yaml.

Sources:
  codesystem  - concepts from a FHIR CodeSystem in node_modules/, looked up
                by canonical URL via the package's .index.json
  manual      - values defined inline in the manifest

Usage:
  npm install          # install FHIR packages once
  uv run scripts/generate_enums.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent
MANIFEST_PATH = REPO_ROOT / "scripts" / "enum_manifest.yaml"
NODE_MODULES = REPO_ROOT / "node_modules"
OUTPUT_PATH = REPO_ROOT / "cdm" / "enums.yaml"

# YAML keys that need quoting: start with a digit, contain special chars, or
# are YAML boolean/null literals.
_NEEDS_QUOTE = re.compile(
    r"^(\d|true$|false$|null$|yes$|no$|on$|off$)"
    r"|[:{}\[\],#&*?|<>=!%@`]"
    r"|\s",
    re.IGNORECASE,
)


def yaml_key(s: str) -> str:
    return f'"{s}"' if _NEEDS_QUOTE.search(s) else s


def yaml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def permissible_values_block(values: list[dict]) -> str:
    lines: list[str] = []
    for v in values:
        code = str(v["code"])
        display = v.get("display", "")
        key = yaml_key(code)
        if display:
            lines.append(f"      {key}: {{description: {yaml_str(display)}}}")
        else:
            lines.append(f"      {key}:")
    return "\n".join(lines)


def url_to_filename(url: str) -> str:
    """Derive FHIR package filename from canonical URL.

    e.g. https://fhir.hl7.org.uk/CodeSystem/UKCore-AdmissionMethodEngland
         -> CodeSystem-UKCore-AdmissionMethodEngland.json
    """
    parts = url.rstrip("/").rsplit("/", 2)
    # parts[-2] = ResourceType, parts[-1] = id
    return f"{parts[-2]}-{parts[-1]}.json"


def load_codesystem(url: str, package: str) -> list[dict]:
    pkg_dir = NODE_MODULES / package
    if not pkg_dir.exists():
        print(
            f"ERROR: {pkg_dir} not found.\n"
            f"       Run 'npm install' to install FHIR packages.",
            file=sys.stderr,
        )
        sys.exit(1)
    filename = url_to_filename(url)
    path = pkg_dir / filename
    if not path.exists():
        print(
            f"ERROR: {filename} not found in {package}.\n       URL: {url}",
            file=sys.stderr,
        )
        sys.exit(1)
    raw = json.loads(path.read_text(encoding="utf-8"))
    concepts = raw.get("concept", [])
    if not concepts:
        print(f"WARNING: no concept[] in {filename} ({package})", file=sys.stderr)
    return [{"code": c["code"], "display": c.get("display", "")} for c in concepts]


def build_enum_block(name: str, spec: dict) -> str:
    description = spec.get("description", "")
    source = spec["source"]

    if source == "codesystem":
        values = load_codesystem(spec["url"], spec["package"])
    elif source == "manual":
        values = [
            {"code": str(v["code"]), "display": v.get("display", "")}
            for v in spec.get("values", [])
        ]
    else:
        print(f"ERROR: unknown source type '{source}' for {name}", file=sys.stderr)
        sys.exit(1)

    pv = permissible_values_block(values)
    return (
        f"  {name}:\n"
        f"    description: {yaml_str(description)}\n"
        f"    permissible_values:\n"
        f"{pv}"
    )


def main() -> None:
    manifest = yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))
    enums = manifest.get("enums", {})

    # Collect which packages are used, for the header comment.
    packages_used = sorted(
        {
            spec["package"]
            for spec in enums.values()
            if spec.get("source") == "codesystem"
        }
    )

    header = (
        "# AUTO-GENERATED - do not edit by hand.\n"
        "# Regenerate with:  uv run scripts/generate_enums.py\n"
        "#\n"
        "# Sources:\n"
        "#   Manual enums    - defined in scripts/enum_manifest.yaml\n"
        + "".join(f"#   {p} - node_modules/{p}/package/\n" for p in packages_used)
        + "\n"
        "id: https://cdm.aicentre.co.uk/enums\n"
        "name: enums\n"
        "description: LinkML enums for the AI Centre CDM, generated from FHIR terminology sources.\n"
        "\n"
        "imports:\n"
        "  - linkml:types\n"
        "\n"
        "enums:\n"
    )

    enum_blocks = []
    for enum_name, spec in enums.items():
        block = build_enum_block(enum_name, spec)
        enum_blocks.append(block)

    output = header + "\n\n".join(enum_blocks) + "\n"
    OUTPUT_PATH.write_text(output, encoding="utf-8")
    print(f"Written {OUTPUT_PATH.relative_to(REPO_ROOT)}")
    print(f"  {len(enums)} enums generated.")


if __name__ == "__main__":
    main()
