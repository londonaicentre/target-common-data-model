# Flattened-FHIR CDM — Shared Conventions

These are binding conventions for every fact/dimension table in the CDM

FHIR version target: **R4 (4.0.1)** which is used in NHS, and using NHS England / UK Core bindings where it is available and appropriate

## 1. Core modelling principle

We flatten FHIR resources as **fact or dimension tables**, in the spirit of OMOP's `_occurrence`/`measurement`/`observation` tables and the SQL-on-FHIR ViewDefinition standard.

## 2. Fact grain and variants

The default grain is one fact row per source FHIR resource. However, originating data that carries several codes/values (e.g. an `Encounter` with multiple diagnoses) for the same fact, can be kept on that single row with the repeating/component structure held in a one-level `variant` column.

Variant objects are themselves flat - i.e. only a single layer of hierarchy is allowed.

Where FHIR carries a sequence (e.g. `diagnosis.rank`), keep the ordering field inside the element so the array can be re-sequenced

## 3. Schema

We use dbt `schema.yml` files as the contract for defining CDM tables.

By default, dbt does not treat variant objects as first class objects and cannot constrain or test their content. We instead use `schema_variant.yml` files to define a variant object in the same way as a table.

## 4. Referring back to FHIR

Every field definition in a `schema.yml` or `schema_variant.yml` must link back to a FHIR path, if that field represents a FHIR element. If this is the case, the field must inherit all conventions and constraints from the original FHIR element.

## 5. Handling `CodeableConcept` triples and mapping

This is a specific variant case. Every clinical code column is stored as a **triple (+bool)**:

| Column suffix        | FHIR source            | Meaning                                    |
|----------------------|------------------------|--------------------------------------------|
| `coding_system`      | `Coding.system`        | Code system URI/OID (REQUIRED when coded)  |
| `coding_code`        | `Coding.code`          | The code                                   |
| `coding_display`     | `Coding.display`       | Human-readable display                     |
| `is_source`          | null                   | Boolean, true if original source concept   |

A fact may be mapped to different clinical codes (e.g. to standard vocabularies). These are expressed in a variant object.

The coded fact presented in source data is surfaced as a single `CodeableConcept` object with `is_source` = True. Note that `is_source` is NOT a canonical FHIR element.

A mapping transformation step can join a source code to standard codes. In this case, new objects are added to the variant with `is_source` = False. There can only ever be one `is_source` = True in a given variant object.

## 6. Flatten non-repeatable objects

Wherever possible, elements which are not prone to repeating in the source data should be flattened. For example - values and event times.

## 7. FHIR-aligned naming

Column names use `snake_case` and mirror FHIR element paths where practical, so a reader can trace a column back to the spec.

## 8. Source provenance

Every table carries three flattened scalars from `Resource.meta`: `meta_source` (`meta.source`, origin system), `meta_tag` (`meta.tag`, feed/extract stamp), and `meta_last_updated` (`meta.lastUpdated`, source change time). These are never held as variants.
