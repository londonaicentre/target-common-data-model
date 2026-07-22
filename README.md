# Target Data Model

AI Centre CDM — a flattened analytical data model mapped from UK Core FHIR R4 (4.0.1).

Each table corresponds to a single FHIR resource, flattened into a fact or dimension table suitable for SQL analytics. Repeating/complex FHIR elements are held as `variant` columns (single-level flat objects in an array).

See [CONVENTIONS.md](./CONVENTIONS.md) for the full modelling rules.

## Schemas

Schemas live under `cdm/`. Each resource has its own YAML file:

| File | Class | Grain | OMOP analogue |
|---|---|---|---|
| `patient.yaml` | `Patient` | One row per master person | `person` |
| `encounter.yml` | `Encounter` | One row per encounter | `visit_occurrence` |
| `condition.yml` | `Condition` | One row per condition | `condition_occurrence` |
| `core.yaml` | shared slots & types | — | — |
| `enums.yaml` | all enumerations | — | — |

The schemas are written in [LinkML](https://linkml.io/) for RDF-compatible, toolable schema description.

### Variant classes

Repeating FHIR elements are modelled as inline variant classes (arrays of flat objects). Defined in the same file as their parent:

| Variant class | Parent | Represents |
|---|---|---|
| `PatientIdentifierVariant` | `Patient` | `Patient.identifier[]` (non-NHS-number entries) |
| `EncounterDiagnosisVariant` | `Encounter` | `Encounter.diagnosis[]` |
| `ConditionConceptVariant` | `Condition` | `Condition.code` CodeableConcept + mappings |

Each variant object has a content-based surrogate PK: `hash(<parent_pk>, <distinguishing fields>)`.

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

Install FHIR terminology packages (requires Node.js):

```bash
npm install
```

Install Python tooling (requires [uv](https://github.com/astral-sh/uv)):

```bash
uv sync
```

## How To

### Validate a schema

```bash
uvx --with linkml linkml lint cdm/core.yaml
```

### Add a new resource

1. Copy `template_schema.yml` to `cdm/<resource>.yml`.
2. Add an `imports: [core]` block so shared provenance slots are available.
3. Define the primary class and any variant classes following the conventions in [CONVENTIONS.md](./CONVENTIONS.md).
4. If you need new enums, add entries to `scripts/enum_manifest.yaml` and regenerate.

### Regenerate enums

```bash
uv run scripts/generate_enums.py
```