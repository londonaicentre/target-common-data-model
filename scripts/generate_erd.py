# /// script
# requires-python = ">=3.10"
# dependencies = ["linkml-runtime==1.11.1"]
# ///
"""Generate an ERD of the CDM from the LinkML spec, as DBML.

For each cdm/<resource>.yaml, resolve the schema via LinkML SchemaView and emit
docs/erd.dbml - paste into https://dbdiagram.io to render, or publish with
dbdocs. Carries enums as first-class objects and the FHIR lineage
(fhir_path / fhir_type / value_set / replaces_value_set) as column notes.

One entity per resource table. Following CONVENTIONS.md:

  * References are `_id` scalars carrying an `fk_target` annotation (§2, §10),
    which is what becomes a relationship. Many-to-one onto the target's PK;
    optional on the FK side where the slot is not `required`.
  * Inlined multivalued variant classes are Snowflake VARIANT columns on their
    parent (§5), NOT entities - drawing them as tables would misrepresent the
    grain. They render as a `variant` column, with the inner shape and the
    `key_fields` (§9) in the note.
  * Enums (cdm/enums.yaml) become DBML enum objects, so a bound column links
    through to its permissible values.

Every fk_target must be a resource the CDM models; anything else is an error.

Usage:  uv run scripts/generate_erd.py [OUTPUT_DIR]   (default: docs/)
"""
from __future__ import annotations

import sys
from pathlib import Path

from linkml_runtime import SchemaView

REPO = Path(__file__).parent.parent
CDM = REPO / "cdm"
NON_RESOURCE = {"core", "enums"}   # shared slots/types + enum definitions, not resource tables

# LinkML range -> the type shown on the diagram. Mirrors TYPE_MAP in
# generate_dbt_yaml.py so the ERD and the dbt contracts agree.
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


def esc(text):
    """Escape a string for a single-quoted DBML note."""
    return one_line(text).replace("\\", "\\\\").replace("'", "\\'")


def ident(name):
    """A DBML identifier, quoted where it is not a bare word."""
    bare = name.replace("_", "").isalnum() and not name[:1].isdigit()
    return name if bare else f'"{name}"'


def nested_classes(sv: SchemaView, defined: list[str]) -> set[str]:
    """Classes reached as the range of a slot - i.e. structure, not a table.

    Covers both variants (multivalued, §5) and inlined 0..1 structs such as
    `period {start, end}`, at any depth: a Coding sitting inside a variant is
    itself reached from that variant, so it is caught too. What remains is the
    resource table - the one class nothing points at.

    A reference (§10) is excluded: its range is the target class, but it holds
    that class's identifier rather than nesting it, so the target is a table in
    its own right and not structure belonging to this one.
    """
    classes = set(sv.all_classes())
    out = set()
    for cn in defined:
        for slot in sv.class_induced_slots(cn):
            if slot.range in classes and slot.inlined is not False:
                out.add(slot.range)
    return out


def column_order(sv: SchemaView, cls_name: str) -> list[str]:
    """Own attributes first, referenced (shared) slots such as meta_* last."""
    cls = sv.get_class(cls_name)
    return list(cls.attributes or {}) + list(cls.slots or [])


def variant_note(sv: SchemaView, rng: str) -> str:
    """The inner shape of a variant, plus its key fields (§9)."""
    fields = [s.name for s in sv.class_induced_slots(rng) if not s.identifier]
    note = f"{rng} [{{{', '.join(fields)}}}]"
    keys = ann(sv.get_class(rng), "key_fields")
    if keys:
        note += f" - key: {keys}"
    return note


def collect(sv: SchemaView, cls_name: str, enums, classes) -> dict:
    """One entity: its columns, and the foreign keys leaving it."""
    columns, refs = [], []

    for slot_name in column_order(sv, cls_name):
        slot = sv.induced_slot(slot_name, cls_name)
        rng = slot.range or "string"
        is_variant = slot.multivalued and (slot.inlined or slot.inlined_as_list) and rng in classes
        # §10. A reference holds the target's id, so it is a scalar column
        # here, not the target's structure inlined.
        is_reference = rng in classes and slot.inlined is False
        fk_target = ann(slot, "fk_target")

        if is_variant:
            data_type = "variant"
        elif is_reference:
            data_type = "array" if slot.multivalued else "varchar"
        elif rng in enums:
            data_type = rng            # a DBML enum object, linked from the column
        elif rng in classes:
            data_type = "variant"      # inlined 0..1 struct, e.g. period {start, end}
        else:
            data_type = TYPE_MAP.get(rng, "varchar")

        settings = []
        if slot.identifier:
            settings.append("pk")
        elif slot.required:
            settings.append("not null")

        # A Reference is a scalar `_id` carrying fk_target (§2). Always
        # many-to-one onto the target PK. Whether the FK is mandatory is carried
        # by `not null` on the column rather than by the ref: DBML's optional-side
        # `>?` is dbdiagram-only and is rejected by the @dbml/core parser.
        if fk_target:
            refs.append((slot.name, fk_target, bool(slot.required)))

        notes = []
        if slot.description:
            notes.append(one_line(slot.description))
        if is_variant:
            notes.append(variant_note(sv, rng))
        elif is_reference:
            pass                       # the Ref line carries the target
        elif rng in classes:
            fields = [s.name for s in sv.class_induced_slots(rng) if not s.identifier]
            notes.append(f"{rng} {{{', '.join(fields)}}}")
        for key in ("fhir_path", "fhir_type", "value_set", "replaces_value_set"):
            val = ann(slot, key)
            if val is not None:
                notes.append(f"{key}: {val}")

        columns.append({
            "name": slot.name,
            "type": data_type,
            "settings": settings,
            "note": " | ".join(notes),
        })

    cls = sv.get_class(cls_name)
    return {
        "name": cls_name,
        "description": one_line(cls.description),
        "columns": columns,
        "refs": refs,
    }


def collect_enums(sv: SchemaView, used: set[str]) -> list[dict]:
    """The enums the schemas actually reference, with their permissible values."""
    out = []
    for name in sorted(used):
        enum = sv.get_enum(name)
        if enum is None:
            continue
        values = []
        for code, pv in enum.permissible_values.items():
            values.append({"code": code, "description": one_line(getattr(pv, "description", "") or "")})
        out.append({"name": name, "description": one_line(enum.description), "values": values})
    return out


def render_dbml(entities: list[dict], enums: list[dict]) -> str:
    """DBML: full detail - columns, enums, notes carrying the FHIR lineage."""
    lines = [
        "// AUTO-GENERATED by scripts/generate_erd.py - DO NOT EDIT.",
        "//",
        "// An ERD of the FlatFHIR CDM, generated from the LinkML spec in cdm/.",
        "// Source of truth is cdm/<Resource>.yaml; regenerate rather than editing.",
        "//",
        "// View by pasting into https://dbdiagram.io, or publish with dbdocs.",
        "//",
        "// Variant columns (CONVENTIONS.md §5) are Snowflake VARIANT on the parent",
        "// table, not tables of their own - the note carries the inner shape.",
        "",
        "Project flatfhir {",
        "  database_type: 'Snowflake'",
        "  Note: 'FlatFHIR CDM - a flattened analytical model derived from UK Core FHIR R4.'",
        "}",
        "",
    ]

    for enum in enums:
        lines.append(f"enum {enum['name']} {{")
        for val in enum["values"]:
            # A DBML enum value must be quoted unless it is a bare identifier -
            # FHIR codes are routinely hyphenated (`entered-in-error`) or numeric.
            code = val["code"]
            bare = code.replace("_", "").isalnum() and not code[:1].isdigit()
            token = code if bare else f'"{code}"'
            if val["description"]:
                lines.append(f"  {token} [note: '{esc(val['description'])}']")
            else:
                lines.append(f"  {token}")
        lines.append("}")
        lines.append("")

    for ent in entities:
        lines.append(f"Table {ent['name'].lower()} {{")
        for col in ent["columns"]:
            settings = list(col["settings"])
            if col["note"]:
                settings.append(f"note: '{esc(col['note'])}'")
            suffix = f" [{', '.join(settings)}]" if settings else ""
            lines.append(f"  {ident(col['name'])} {ident(col['type'])}{suffix}")
        if ent["description"]:
            lines.append("")
            lines.append(f"  Note: '{esc(ent['description'])}'")
        lines.append("}")
        lines.append("")

    lines.append("// --- Relationships (CONVENTIONS.md §2, §10) ---------------------------")
    lines.append("")
    for ent in entities:
        for col, target, _ in ent["refs"]:
            lines.append(f"Ref: {ent['name'].lower()}.{col} > {target.lower()}.id")
    lines.append("")

    return "\n".join(lines)


def main(outdir: Path) -> None:
    entities, used_enums = [], set()

    for spec_path in sorted(p for p in CDM.glob("*.yaml") if p.stem.lower() not in NON_RESOURCE):
        sv = SchemaView(str(spec_path))
        enums = set(sv.all_enums())
        classes = set(sv.all_classes())
        defined = list(sv.all_classes(imports=False))
        nested = nested_classes(sv, defined)

        for cls_name in (c for c in defined if c not in nested):
            ent = collect(sv, cls_name, enums, classes)
            entities.append(ent)

        # Every enum the schema references, including those bound on a field
        # nested inside a variant (§12) - they belong in the DBML either way.
        for cls_name in defined:
            for slot in sv.class_induced_slots(cls_name):
                if slot.range in enums:
                    used_enums.add(slot.range)

    # Every fk_target must be a resource the CDM models (CONVENTIONS.md §10).
    modelled = {e["name"] for e in entities}
    dangling = sorted(
        {(e["name"], col, target) for e in entities for col, target, _ in e["refs"]
         if target not in modelled}
    )
    if dangling:
        print("ERROR: fk_target names a resource the CDM does not model:", file=sys.stderr)
        for src, col, target in dangling:
            print(f"  {src}.{col} -> {target}", file=sys.stderr)
        print(
            "\nEither model the target, or drop the reference from "
            "config/resources/<Resource>.yaml and regenerate.",
            file=sys.stderr,
        )
        sys.exit(1)

    # A single SchemaView over enums.yaml, so a value is described once.
    enum_defs = collect_enums(SchemaView(str(CDM / "enums.yaml")), used_enums)

    outdir.mkdir(parents=True, exist_ok=True)
    path = outdir / "erd.dbml"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(render_dbml(entities, enum_defs))
    print(f"  wrote {path.relative_to(REPO)}")

    n_refs = sum(len(e["refs"]) for e in entities)
    print(f"\n{len(entities)} entities, {n_refs} relationships, {len(enum_defs)} enums")


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "docs"
    main(out)
