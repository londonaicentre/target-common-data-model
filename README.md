# Target Data Model

AI Centre CDM — a flattened analytical data model mapped from UK Core FHIR R4 (4.0.1).

Each table corresponds to a single FHIR resource, flattened into a fact or dimension table suitable for SQL analytics. Repeating/complex FHIR elements are held as `variant` columns (single-level flat objects in an array).

See [CONVENTIONS.md](./CONVENTIONS.md) for the full modelling rules.

## Schemas

Schemas live under `cdm/`, written in [LinkML](https://linkml.io/) for RDF-compatible, toolable schema description. Each resource has its own YAML file:

| File | Class | Grain | OMOP analogue |
|---|---|---|---|
| `Patient.yaml` | `Patient` | One row per master person | `person` |
| `Encounter.yaml` | `Encounter` | One row per encounter | `visit_occurrence` |
| `Condition.yaml` | `Condition` | One row per condition | `condition_occurrence` |
| `core.yaml` | shared slots and types | | |
| `enums.yaml` | all enumerations | | |

### Variant classes

Repeating FHIR elements are modelled as inline variant classes (arrays of flat objects). Defined in the same file as their parent:

| Variant class | Parent | Represents |
|---|---|---|
| `PatientIdentifierVariant` | `Patient` | `Patient.identifier[]` (non-NHS-number entries) |
| `EncounterDiagnosisVariant` | `Encounter` | `Encounter.diagnosis[]` |
| `ConditionConceptVariant` | `Condition` | `Condition.code` CodeableConcept + mappings |

Each variant object has a content-based surrogate PK: `hash(<parent_pk>, <distinguishing fields>)`.

## Generated dbt artifacts

The LinkML schemas are the source of truth. dbt model contracts and seed lookups are generated from them into `dbt_metadata/`, which mirrors the consumer dbt project layout so delivery is a straight copy:

- `dbt_metadata/models/gold/<resource>/<resource>.yml` - one dbt model contract per resource: column types, `not_null` / `unique` / `accepted_values` / `relationships` tests, and FHIR lineage in `meta`.
- `dbt_metadata/seeds/mapping/seed_<entity>.csv` - code/display lookups, one per enumeration, plus `seeds_mapping.yml`.

Generated files carry a "do not edit" banner. To change them, edit the `cdm/` spec and regenerate:

```bash
uv run scripts/generate_dbt_yaml.py     # model contracts -> dbt_metadata/models/gold/
uv run scripts/generate_dbt_seeds.py    # reference seeds  -> dbt_metadata/seeds/mapping/
```

New resources are picked up automatically (every `cdm/*.yaml` except `core` and `enums`).

## Enumerations

Enums in `cdm/enums.yaml` are auto-generated from FHIR terminology packages. **Do not edit by hand.**

Regenerate with:

```bash
uv run scripts/generate_enums.py
```

Sources:
- `scripts/enum_manifest.yaml` — manually defined enums (e.g. CDM-specific `EncounterCategoryEnum`)
- `node_modules/fhir.r4.ukcore.stu2/` — UK Core value sets
- `node_modules/hl7.fhir.r4.core/` — core HL7 FHIR R4 value sets

## Templates

Two schema templates are provided as a starting point for new resources:

- `template_schema.yml` — first-class table columns
- `template_concept_variant_schema.yml` — CodeableConcept variant
- `template_element_variant_schema.yml` — backbone/repeating-element variant

## Prerequisites

Install FHIR terminology packages, required to regenerate enums (requires Node.js):

```bash
npm install
```

The Python scripts declare their dependencies inline (PEP 723) and run under [uv](https://github.com/astral-sh/uv); there is no separate install step.

## How To

### Validate the schemas

```bash
uvx --with linkml linkml lint cdm --all --ignore-warnings
```

### Add a new resource

1. Add `cdm/<Resource>.yaml` with an `imports: [core]` block so shared provenance slots are available.
2. Define the primary class and any variant classes following the conventions in [CONVENTIONS.md](./CONVENTIONS.md).
3. If you need new enums, add entries to `scripts/enum_manifest.yaml` and regenerate.
4. Regenerate the dbt artifacts; the new resource is included automatically.

### Regenerate everything

```bash
npm install                          # once, for FHIR packages
uv run scripts/generate_enums.py     # cdm/enums.yaml
uv run scripts/generate_dbt_seeds.py # dbt_metadata/seeds/mapping/
uv run scripts/generate_dbt_yaml.py  # dbt_metadata/models/gold/
```
