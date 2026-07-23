# Flattened-FHIR CDM — Shared Conventions

These are binding conventions for every fact/dimension table in the CDM

FHIR version target: **R4 (4.0.1)** which is used in NHS, and using NHS England / UK Core bindings where it is available and appropriate

## 1. Core modelling principle

We flatten FHIR resources as **fact or dimension tables**, in the spirit of OMOP's `_occurrence`/`measurement`/`observation` tables and the SQL-on-FHIR ViewDefinition standard.

Each table, as defined by a schema, corresponds to a FHIR resource.

## 2. Fact grain and variants

The default grain is one fact row per source FHIR resource. However, originating data that carries several codes/values (e.g. an `Encounter` with multiple diagnoses) for the same fact, can be kept on that single row with the repeating/component structure held in a one-level `variant` column.

Variant objects are themselves flat - i.e. only a single layer of hierarchy is allowed.

Where FHIR carries a sequence (e.g. `diagnosis.rank`), the ordering fields are kept inside the element so the array can be re-sequenced.

Variants can either be a backbone element with a repeating object that is specific to a resource, or a `CodeableConcept` (see §5).

#### 2a. Surrogate keys for variant objects

Each variant object needs a surrogate PK that is deterministics and unique across all objects in the parent table.

Standardise on a content-based hash — `hash(<parent_pk>, <distinguishing source fields>)` — using the fields that best identify the object in the source:

- **CodeableConcept variant:** `hash(<parent_pk>, coding_system, coding_code)`.
- **Backbone/repeating variant:** `hash(<parent_pk>, <the natural key of the object>)` — e.g. a referenced resource id, a role code. Use `rank` or array index as a tie-breaker when no natural key exists, and where ordering field is guaranteed non-null.

## 3. Schema

We define the CDM in [LinkML](https://linkml.io/) under `cdm/`: one `<resource>.yaml` per resource, with shared slots and types in `core.yaml` and enumerations in `enums.yaml`. Each first-class table is a LinkML class and each column is a slot.

dbt model contracts and seed lookups are generated from the LinkML into `dist/` by the `scripts/generate_dbt_*.py` scripts. The generated files are the delivery artifacts and are not edited by hand; change the spec and regenerate.

Variant objects are defined as inline LinkML classes in the same file as their parent. dbt does not treat them as first class objects and cannot constrain or test their content, so they surface on the parent as a `variant` column, with the inner shape recorded in `meta.variant_fields`.

## 4. Referring back to FHIR

Every slot that represents a FHIR element must link back to a FHIR path via a `fhir_path` annotation (with `fhir_type`). If this is the case, the field must inherit all conventions and constraints from the original FHIR element.

Where the FHIR path is null or absent, the field is CDM specific and is not present in FHIR.

## 5. Handling `CodeableConcept`: the fact code vs. everything else

A FHIR `CodeableConcept` is modelled in one of two ways, decided by whether the code is a **fact**.

### 5a. Fact code (+ mappings)

Where a single column on a fact table (for example `condition`, `observation`) whose code is the fact being recorded, this is stored as a **triple (+bool)** variant:

| Column suffix        | FHIR source            | Meaning                                    |
|----------------------|------------------------|--------------------------------------------|
| `coding_system`      | `Coding.system`        | Code system URI/OID (REQUIRED when coded)  |
| `coding_code`        | `Coding.code`          | The code                                   |
| `coding_display`     | `Coding.display`       | Human-readable display                     |
| `is_source`          | null                   | Boolean, true if original source concept   |

The coded fact presented in source data is surfaced as a single `CodeableConcept` object with `is_source` = True. Note that `is_source` is NOT a canonical FHIR element.

A mapping transformation step can join a source code to standard codes (e.g. to standard vocabularies). New objects are added to the variant with `is_source` = False. There can only ever be one `is_source` = True in a given variant object.

This enables the representation of concept through its source appearance and different vocabularies via a source -> mapped (1:many) relationship.

### 5b. Flatten every other coded element

All other `CodeableConcept` / `Coding` elements (usually dimension attributes or qualifying attributes: statuses, categories, roles, types, etc) are flattened rather than being held as a mapping variant.

By default, use a scalar code + `_display` sidecar for elements bound to a closed/enumerable value set (e.g. `clinical_status`, `condition_category`). Note that The `_display` column is CDM-derived (`fhir_path: null`).

There is no `is_source` and no mapping on these flattened coded elements.

**This is a design choice that is subject to change**

## 6. Flatten non-repeatable objects

Wherever possible, FHIR nested elements which do not repeat in the source data should be flattened. For example - values, provenance metadata, event times.

## 6a. Polymorphic `[x]` choice types

Polymorphic `[x]` elements are a specific case (e.g. `deceased[x]`, `multipleBirth[x]`). For these, surface the most analytically useful form(s) as flat scalar columns. Each surfaced form is its own column named for the form (`_boolean`, `_datetime`, etc.).

E.g. `deceased[x]` on Patient surfaces both `deceased_boolean` and `deceased_datetime`, whereas `onset[x]` on Condition may surface only `onset_datetime`.

Forms that are not surfaced are dropped and listed in the table's EXCLUDED block.

## 7. FHIR-aligned naming

Column names use `snake_case` and mirror FHIR element paths where practical, so a reader can trace a column back to the spec.

## 8. Resource primary keys

A table's primary key is `<resource>_id` (e.g. `condition_id`, `encounter_id`), sourced from `Resource.id`.

The exception is the patient key, `master_person_id` (not `patient_id`), as this is the output of patient entity resolution (linkage across source patient records to one master person). Patient ID is used as a source system identifier, a Person may be a patient in many locales.

Clinical facts therefore carry `master_person_id` as their subject FK, resolved from `Resource.subject`.

## 9. Source provenance

Every table carries three flattened scalars from `Resource.meta`: `meta_source` (`meta.source`, origin system), `meta_tag_code` (`meta.tag.code`, feed/extract stamp), and `meta_last_updated` (`meta.lastUpdated`, source change time). These are never held as variants. `meta.tag` is a `Coding`; only its `.code` is surfaced (`fhir_type: code`), which the column name reflects.

## 10. Unused FHIR paths

At the bottom of each schema, FHIR paths for unused resources are listed for documentation purposes only.