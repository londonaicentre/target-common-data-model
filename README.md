# Target Data Model

AI Centre CDM — a flattened analytical data model mapped from UK Core FHIR R4 (4.0.1).

Each table corresponds to a single FHIR resource, flattened into a fact or dimension table suitable for SQL analytics. Repeating/complex FHIR elements are held as `variant` columns (single-level flat objects in an array).

See [CONVENTIONS.md](./CONVENTIONS.md) for the full modelling rules.

## How generation works

Two stages. Stage 1 is pure FHIR with no opinions in it; stage 2 is where every modelling rule lives.

```
UK Core StructureDefinition (node_modules/)
          |
          |  stage 1   scripts/expand_fhir.py
          |            resolve datatypes, choice types and extensions
          v
build/    expanded/<Resource>.yaml   every element the CDM could name
          fhir_bindings.yaml         every bound field -> value set URL
          fhir_enums.yaml            every value set -> concepts
          |
          |  stage 2   scripts/generate_linkml.py
          |            apply the whitelist in config/resources/
          v
cdm/<Resource>.yaml                LinkML, plus cdm/enums.yaml
```

Everything in `build/` is an opinion-free cache of FHIR: three artefacts with three jobs — structure, which vocabulary applies where, and what each vocabulary contains. None of them knows anything about the whitelist, and all three record everything they find.

`build/fhir_bindings.yaml` is keyed by the field a binding *applies to* — the bare `code` — rather than the element FHIR declares it on, so `Condition.clinicalStatus` is recorded against `Condition.clinicalStatus.coding.code`.

```bash
npm install                        # once, for the FHIR packages
uv run scripts/expand_fhir.py      # stage 1
uv run scripts/generate_linkml.py  # stage 2
```

## Schemas

Schemas under `cdm/` are **generated** — written in [LinkML](https://linkml.io/) for RDF-compatible, toolable schema description, and produced from the FHIR packages by the two-stage generator below. Do not edit them by hand.

A schema exists for a resource only where `config/resources/<Resource>.yaml` exists; the config file is the opt-in.

| File | Class | Grain | OMOP analogue |
|---|---|---|---|
| `Patient.yaml` | `Patient` | One row per patient | `person` |
| `Encounter.yaml` | `Encounter` | One row per encounter | `visit_occurrence` |
| `Condition.yaml` | `Condition` | One row per condition | `condition_occurrence` |
| `Observation.yaml` | `Observation` | One row per observation | `measurement` / `observation` |
| `Organization.yaml` | `Organization` | One row per organisation | `care_site` / `provider` |
| `enums.yaml` | the enums the above reference | | |

### Variant classes

Where a whitelisted FHIR path repeats (`0..*`), the field is a **variant** — an array of objects — and the generator emits an inline class for it in the same file as its parent, named after the path it came from (`PatientIdentifier`, `EncounterDiagnosis`, `ObservationComponent`).

Each variant declares `key_fields`: the fields that, with the parent PK, identify one object in the array (CONVENTIONS.md §9). Whether a consumer also materialises a single hashed column over those fields is a downstream join-ergonomics decision, not part of this spec.

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

Enums in `cdm/enums.yaml` are generated, not hand-written. **Do not edit by hand.**

**Bindings are declared, never inferred.** A resource config names the field carrying the bare `code` and says where its vocabulary comes from, as set out in [CONVENTIONS.md](./CONVENTIONS.md) under *How bindings work*:

```yaml
Condition.clinicalStatus:
  bindings:
    Condition.clinicalStatus.coding.code: default        # the FHIR binding

Patient.maritalStatus:
  bindings:
    Patient.maritalStatus.coding.code: MaritalStatusEnum # a manifest entry
```

```
config/resources/*.yaml   bindings: <field path>: default | <EnumName>
        |
  default             <EnumName>
        |                   |
        v                   v
build/fhir_bindings.yaml   config/enum_manifest.yaml
build/fhir_enums.yaml      HAND-WRITTEN. Manual bindings only.
        |                        |
        +-----------+------------+
                    v
              cdm/enums.yaml     PUBLISHED, only what configs name.
```

`default` resolves only where FHIR genuinely constrains: the strength is `required` or `extensible`, **and** the value set expands offline. Anything else is an error rather than a silent fallback to `string` — a config that writes `default` on a `preferred` field, or on a SNOMED `is-a` subset, has asked for something FHIR does not offer.

A manifest entry is for the cases `default` cannot serve: a `preferred` vocabulary the CDM enforces anyway (much of the NHS CDS grid), an England codeset narrowing a UK-wide value set, or a CDM-defined vocabulary. A whitelisted field with no declaration is a plain field, and the run reports which bound fields were left undeclared.

Only enums a config names are emitted, so `cdm/enums.yaml` contains exactly what the schemas reference. A manifest entry referenced by no config, or one that reproduces what `default` would have resolved, is reported.

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

1. Add `config/resources/<Resource>.yaml` naming the profile and the whitelist of FHIR paths. The config file is the opt-in — no other registration is needed.
2. Run `uv run scripts/expand_fhir.py` and read `build/expanded/<Resource>.yaml` to see the shape beneath each path before whitelisting it.
3. Declare bindings. `build/fhir_bindings.yaml` lists every bound field in the resource, keyed by the field the binding applies to — copy those paths into `bindings:` and mark each `default` or a manifest entry name (CONVENTIONS.md §12). Nothing is bound that is not declared.
4. Run `uv run scripts/generate_linkml.py`. A `default` that FHIR cannot honour is an error; the run also reports any bound field left undeclared.
5. Regenerate the dbt artifacts; the new resource is included automatically.

### Regenerate everything

```bash
npm install                          # once, for FHIR packages
uv run scripts/expand_fhir.py        # stage 1 -> build/expanded/
uv run scripts/generate_linkml.py    # stage 2 -> cdm/
uv run scripts/generate_dbt_seeds.py # dbt_metadata/seeds/mapping/
uv run scripts/generate_dbt_yaml.py  # dbt_metadata/models/gold/
```
