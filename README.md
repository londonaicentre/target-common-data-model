# Target Common Data Model (FlatFHIR)

This repository contains a computable and machine-readable CDM called "FlatFHIR" - a flattened analytical data model derived from UK Core FHIR R4 (4.0.1).

Within the CDM, each FHIR resource becomes one fact or dimension table, partially flattened for SQL analytics, and with specific elements taken as tabular fields. Repeating and complex FHIR elements may be held as `variant` columns.

The schemas in `cdm/` are **generated** and should never be edited by hand. All generation is driven by configuration files in `config/` that name the FHIR paths that become fields. These configuration files are **hand-written**, but we strongly recommend using a coding agent to help with validation of paths and bindings against the source.

See [CONVENTIONS.md](./CONVENTIONS.md) for detailed modelling rules and thought processes.

## Mechanics

Generation runs in two stages. Stage 1 is pure FHIR with no opinions in it. Stage 2 is where configurable modelling rules are applied.

```
UK Core StructureDefinition (node_modules/)
          |
          |  stage 1   scripts/expand_fhir.py
          v
build/    an opinion-free cache of FHIR: structure, which vocabulary
          applies where, and full expansion of each vocabulary
          |
          |  stage 2   scripts/generate_linkml.py
          |            apply the whitelist in config/resources/
          v
cdm/<Resource>.yaml   LinkML, plus cdm/enums.yaml
```

A schema exists for a resource only where `config/resources/<Resource>.yaml` exists.

## Why "CDM-as-code"?

The alternative is a schema maintained by hand, often in a DDL or a spreadsheet, that is liable to drift. This is particularly a concern when trying to align a target data model as set of rules based on a recognised standard.

Generating from the FHIR packages makes those things explicit and checkable:
- Every tabular field carries the `fhir_path` and `fhir_type` it came from, so any column traces back to the element that produced it.
- Path/field decisions are documented in code through the whitelist and reasoning for notable missing fields (via `rejected`). These are all reviewable in diff.
- Vocabularies are derived where possible from installed FHIR packages, but auditable and machine-readable vocabularies can be maintained,declared, and stay visible.
- **All changes are reviewable.** A modelling decision becomes a diff on a config file with a comment attached.

## Why LinkML?

Stage 2 emits [LinkML](https://linkml.io/), rather than a SQL DDL or a dbt project, or some other representation.

We take LinkML as the **canonical representation** of the model. It is a formal, toolable schema language with a stable API, which can be rendered to whatever a consumer needs — SQL DDL, JSON Schema, RDF/OWL, Pydantic, documentation. Keeping the canonical form target-agnostic means the modelling decisions live in one place and are not entangled with idioms of any one warehouse or transformation tool.

We currently generate dbt configuration from LinkML in this repo, but others can be added without touching `config/` or `cdm/`.

## dbt artifacts

In the current repo, dbt model contracts and seed lookups are generated from them into `dbt_metadata/`, which mirrors the consumer dbt project layout so delivery is a straight copy:

- `dbt_metadata/models/gold/<resource>/<resource>.yml` — one model contract per resource: column types, `not_null` / `unique` / `accepted_values` / `relationships` tests, and FHIR lineage in `meta`.
- `dbt_metadata/seeds/mapping/seed_<entity>.csv` — code/display lookups, one per enum, plus `seeds_mapping.yml`.

The contract generator reads `cdm/` through the LinkML `SchemaView` API rather than parsing the YAML.

## ERD

`scripts/generate_erd.py` renders the model as an entity-relationship diagram, for reviewing the shape of the CDM while iterating on configs. It reads `cdm/` through `SchemaView`, the same as the contract generator, and emits `docs/erd.dbml`.

Paste that file into [dbdiagram.io](https://dbdiagram.io) to render it. It is [DBML](https://dbml.dbdiagram.io/docs/), which carries every column with its `fhir_path`, `fhir_type` and `value_set` as a note, and the enums from `cdm/enums.yaml` as first-class objects, so a bound column links through to its permissible values.

## How to...

### Get started

Requires [uv](https://github.com/astral-sh/uv) and Node.js. The Python scripts declare their dependencies inline (PEP 723).

```bash
npm install                          # once, for the FHIR packages
uv run scripts/expand_fhir.py        # stage 1 -> build/
uv run scripts/generate_linkml.py    # stage 2 -> cdm/
uv run scripts/generate_dbt_seeds.py # -> dbt_metadata/seeds/mapping/
uv run scripts/generate_dbt_yaml.py  # -> dbt_metadata/models/gold/
uv run scripts/generate_erd.py       # -> docs/
```

### Add a new resource

1. Add `config/resources/<Resource>.yaml` naming the profile and, initially, any single path. The file is the opt-in.
2. Run stage 1, then **read `build/expanded/<Resource>.yaml`** to see the shape beneath each path before whitelisting it. Naming a path whose shape you have not looked at is how you get an array of objects holding arrays of objects.
3. Write the whitelist. Add `key` on every variant, `fk` on every reference, and `exclude` with a reason for unwanted children.
4. Declare bindings. `build/fhir_bindings.yaml` lists every bound field in the resource, keyed by the field the binding applies to — copy those paths into `bindings:` and mark each `default` or a manifest entry name (CONVENTIONS.md §12).
5. Run stage 2. A `default` FHIR cannot honour is an error; the run also reports any bound field left undeclared.
6. Regenerate the dbt artifacts.

### Change what a table contains

Edit `config/resources/<Resource>.yaml` and re-run stage 2. Stage 1 only needs re-running when the FHIR packages change.

### Add an optional FHIR enum (i.e. preferred or example)

Where a binding is `preferred` or `example`, or its value set will not expand offline, `default` cannot resolve it. Add a hand-written entry to `config/enum_manifest.yaml` and name it from the config. The manifest is hand-authored and holds nothing derived.

### Validate

```bash
uvx --with linkml linkml lint cdm --all --ignore-warnings
```

### Regenerate everything

Run the five commands under [Getting started](#getting-started) in order.
