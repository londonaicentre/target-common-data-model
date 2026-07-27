# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = ["linkml-runtime>=1.7", "pyyaml"]
# ///
"""Generate dbt model contract YAML from the LinkML CDM spec.

For each cdm/<resource>.yaml, resolve the import closure (core slots + enums)
via LinkML SchemaView and emit dbt_metadata/models/gold/<resource>/<resource>.yml -
mirroring the consumer (lgt_phm) directory layout so delivery is a straight copy.

Per model:
  * data_type on every column
  * not_null / unique expressed as data_tests on identifier/required slots
  * accepted_values with values nested under `arguments:` (binding_strength
    severity is implemented but commented out for now)
  * relationships (foreign-key) tests on class-typed reference columns
  * FHIR lineage in `meta` (fhir_path / fhir_type / value_set) - the meta block
    is omitted entirely for CDM-derived columns that have none
  * own columns first, shared provenance slots (meta_*) last

Inlined multivalued variant classes become Snowflake VARIANT columns on their
parent; their inner shape is surfaced as meta.variant_class / meta.variant_fields.

Usage:  uv run scripts/generate_dbt_yaml.py [OUTPUT_ROOT]   (default: dbt_metadata/)
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from linkml_runtime import SchemaView

REPO = Path(__file__).parent.parent
CDM = REPO / "cdm"
NON_RESOURCE = {"core", "enums"}   # shared slots/types + enum definitions, not resource tables

# LinkML range -> Snowflake/dbt data_type. TimestampNtz -> timestamp_ntz per the spec.
TYPE_MAP = {
    "string": "varchar", "uri": "varchar",
    "boolean": "boolean", "date": "date",
    "datetime": "timestamp_ntz", "TimestampNtz": "timestamp_ntz",
    "integer": "number", "decimal": "number",
}


def ann(el, key):
    """Read an annotation value off a slot/class, or None."""
    a = getattr(el, "annotations", None) or {}
    try:
        if key in a:
            return getattr(a[key], "value", a[key])
    except TypeError:
        pass
    return None


def one_line(text):
    return " ".join((text or "").split())


def column_order(sv: SchemaView, cls_name: str) -> list[str]:
    """Own attributes first, referenced (shared) slots such as meta_* last."""
    cls = sv.get_class(cls_name)
    return list(cls.attributes or {}) + list(cls.slots or [])


def build_column(sv: SchemaView, cls_name: str, slot_name: str, enums, classes) -> dict:
    slot = sv.induced_slot(slot_name, cls_name)
    rng = slot.range or "string"
    is_variant = slot.multivalued and (slot.inlined or slot.inlined_as_list) and rng in classes

    if is_variant:
        data_type = "variant"
    elif rng in enums or rng in classes:
        data_type = "varchar"          # coded value, or FK stored as id
    else:
        data_type = TYPE_MAP.get(rng, "varchar")

    col = {"name": slot.name, "data_type": data_type}
    if slot.description:
        col["description"] = one_line(slot.description)

    tests = []
    if slot.identifier:
        tests += ["unique", "not_null"]
    elif slot.required:
        tests += ["not_null"]
    if rng in enums:
        codes = list(sv.get_enum(rng).permissible_values)
        av = {"accepted_values": {"arguments": {"values": codes}}}
        # binding_strength -> warn severity: commented out for now.
        # if ann(slot, "binding_strength") in ("extensible", "preferred", "example"):
        #     av["accepted_values"]["config"] = {"severity": "warn"}
        tests.append(av)
    # relationships (foreign-key) test on class-typed reference columns.
    elif rng in classes and not is_variant:
        # foreign key -> referential-integrity test against the parent model
        target = sv.get_identifier_slot(rng)
        tests.append({"relationships": {"arguments": {
            "to": f"ref('{rng.lower()}')",
            "field": target.name if target else "id",
        }}})
    if tests:
        col["data_tests"] = tests

    # meta - only emit keys that exist; drop the block entirely if nothing to say
    meta = {}
    for key in ("fhir_path", "fhir_type", "value_set"):
        val = ann(slot, key)
        if val is not None:
            meta[key] = val
    if is_variant:
        meta["variant_class"] = rng
        meta["variant_fields"] = [s.name for s in sv.class_induced_slots(rng) if not s.identifier]
    if meta:
        col["meta"] = meta

    return col


def build_model(sv: SchemaView, cls_name: str) -> dict:
    enums = set(sv.all_enums())
    classes = set(sv.all_classes())
    cls = sv.get_class(cls_name)
    columns = [build_column(sv, cls_name, name, enums, classes) for name in column_order(sv, cls_name)]
    return {"name": cls_name.lower(), "description": one_line(cls.description), "columns": columns}


def variant_classes(sv: SchemaView, defined: list[str]) -> set[str]:
    classes = set(sv.all_classes())
    out = set()
    for cn in defined:
        for slot in sv.class_induced_slots(cn):
            if slot.multivalued and (slot.inlined or slot.inlined_as_list) and slot.range in classes:
                out.add(slot.range)
    return out


def main(outroot: Path) -> None:
    for spec_path in sorted(p for p in CDM.glob("*.yaml") if p.stem.lower() not in NON_RESOURCE):
        res = spec_path.stem.lower()
        sv = SchemaView(str(spec_path))
        defined = list(sv.all_classes(imports=False))
        variants = variant_classes(sv, defined)
        mains = [c for c in defined if c not in variants]
        doc = {"models": [build_model(sv, c) for c in mains]}

        outdir = outroot / "models" / "gold" / res
        outdir.mkdir(parents=True, exist_ok=True)
        path = outdir / f"{res}.yml"
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "# AUTO-GENERATED by the target-common-data-model (CDM) spec repo - DO NOT EDIT.\n"
                f"# Source of truth: cdm/{spec_path.name} in that repo. Regenerate there; edits here are overwritten.\n"
            )
            yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False, allow_unicode=True, width=100)
        n = len(doc["models"][0]["columns"])
        print(f"  wrote models/gold/{res}/{res}.yml  ({n} columns)")


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "dbt_metadata"
    main(out)
