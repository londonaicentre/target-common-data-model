# Flattened-FHIR (FlatFHIR) CDM Conventions

FlatFHIR is used as an intermediate layer in a medallion data pipeline, predominantly built from transactional source data. Each FHIR resource is modelled as a table in a fact/dimensional schema, flattened where possible, using variants to capture repeating items. This document describes conventions that are followed when generating machine readable descriptions of schema.

FHIR version target: **R4 (4.0.1)**, using NHS England / UK Core bindings where available and appropriate.

Schemas under `cdm/` are auto-generated from the UK Core StructureDefinition.

**Part A** describes the general rules used in deriving a schema from a FHIR profile, which are baked into the generation script.

**Part B** describes configurations that are declared globally, and per resource.

---

# Part A - automated schema creation

## 1. Core modelling principle

We flatten FHIR resources as fact or dimension tables, in the spirit of OMOP's `_occurrence`/`measurement`/`observation` tables and the SQL-on-FHIR ViewDefinition standard. Each table corresponds to a FHIR resource. Default grain is one row per resource.

## 2. Naming

Drop the resource prefix, replace `.` and `:` with `_`, lowercase. No camelCase splitting and no shortening.
    Encounter.period.start                 -> period_start
    Encounter.hospitalization.admitSource  -> hospitalization_admitsource
    Patient.identifier:nhsNumber.value     -> identifier_nhsnumber_value
    Condition.onsetDateTime                -> onsetdatetime
    Patient.deceasedBoolean                -> deceasedboolean

The only time there is custom field naming is creation of FK equivalent `_id` fields (see §7)

Otherwise, we do not introduce custom field naming at this stage.

## 3. Primary keys

PK is `id` from `Resource.id`. Facts carry `subject_id` from `<Resource>.subject`. Patient is keyed by `id` like everything else.

## 4. Referring back to canonical FHIR resource

Every slot representing a FHIR element carries `fhir_path` and `fhir_type`, and inherits the constraints of that element.

## 5. Flattening

Everything flattens to a scalar column with the §2 name. Children of `Encounter.period` becomes `period_start`, `period_end`.

There are four exceptions, each covered below:

    Coding / CodeableConcept   -> an object of system, code, display   §6
    Reference                  -> a scalar _id foreign key             §7
    the fact code              -> a variant of codings                 §8
    a repeating element        -> a variant, an array of objects       §10

## 6. Coding Variants

`Coding` and `CodeableConcept` are never flattened - they surface as an object of `system`, `code`, `display`. Keys inside the object are the FHIR element names.

A bare `code` primitive (`Encounter.status`) stays scalar. Note that these do not carry a human readable `_display` value, but these are resolved downstream by joining to `seed_*.csv` in a later analyst view.

Either kind may carry a value set binding, which is handled in §9 (Enums).

## 7. References (foreign keys)

A FHIR `Reference` is a pointer from one resource to another - the equivalent of a foreign key. In FlatFHIR, the reference is flattened to a scalar field, with an `_id` suffix (exception to §2).

    Encounter.subject             -> subject_id     the patient the encounter was with
    Encounter.partOf              -> partof_id      the parent encounter, where one
                                                    encounter sits inside another - a
                                                    consultant episode within a spell
    Encounter.diagnosis.condition -> condition_id   the condition this encounter was about

The column holds the target's `id` (or primary key). So `subject_id` on an encounter row holds a value found in `patient.id`. Where sources already hold real keys (`patient_id`, `encounter_id`), the FK is a straight mapping from source data.

Note that the `_id` name comes from the FHIR element: `Encounter.subject` is `subject_id`, not `patient_id`.

A profile may allows several targets, for example, `subject` may be a Patient *or* a Group. Which resource the column is a foreign key into is declared in config (Part B §6).

This applies at any depth, so a `Reference` inside a variant becomes an `_id` field on the variant object, as `condition_id` above.

A `Reference` also carries `type`, `identifier` and `display`, which exist so a FHIR server can resolve a pointer to a resource held elsewhere. That problem does not arise in a warehouse where every target is a table, so they are globally excluded (Part B §1).

## 8. The Fact code and custom is_source field

Where a column is the code that is the Fact (e.g. `Condition.code`, `Observation.code`), it is represented as a variant holding one object per coding: the source coding plus any mapped standard-vocabulary codings.

Each object is `system`, `code`, `display`, `is_source`. The source coding has `is_source` true; a mapping step adds standard vocabulary codes with `is_source` false. Only ever one true per array. **note: `is_source` is a custom field and not native to FHIR**. Paths to Fact codes are declared in the config (see Part B §1).

## 9. Enumerations

A value set of 100 concepts or fewer becomes an enum with a seed. Larger sets stay `string` with the binding recorded. 

100 is an arbitrary cut-off that separates well defined UK codesets, and exceptionally large `is_a` SNOMED codelists.

Every bound column records its `value_set`, and the enum name where the set is enumerable. What that yields depends on the column:

    scalar code (§6)   -> range is the enum, and an accepted_values test
    object column (§6) -> enum name in meta, no test

An object never equals a bare code, so `accepted_values` cannot be applied directly to one. Recording the enum name keeps the test derivable once an object-aware macro lands downstream.

## 10. Repeating Element Variants

A repeating element is held on the parent as a `variant` column: an array of flat objects, one layer of hierarchy only. Ordering fields (e.g. `diagnosis.rank`) stay inside the object so the array can be re-sequenced.

## 11. Repeats inside repeats

In these special cases (multiple layers of nesting) we keep the outer repeat as the variant, flatten a repeating primitive inside it to its first entry.

    Patient.name -> one object per name
      { use: "official", family: "Smith", given: "Jane", prefix: "Dr" }
      { use: "maiden",   family: "Jones", given: "Jane", prefix: null }

The outer repeat carries a full set of information.

All useful cases resolve this way with no additional config. The inner repeat is complex only in cases with little analytical utility, so for the FlatFHIR CDM, these are excluded explicitly in config (Part B §2).

The script will hard-fail on any repeat inside a repeat that is not resolved by the primitive rule or by config. This also results in the flat-variant rule in §10 being enforced.

## 12. Source provenance

As a result of flattening rules, every table carries `meta_source`, `meta_tag_code` and `meta_last_updated` from `Resource.meta`.

---

# Part B - global and per resource configuration

One global configuration defines exclusions that apply to all resources

A configuration file exists per resource at `scripts/resources/<Resource>.yaml`. Each file declares **only deviations**.

Part A is otherwise fixed in the script.

## 1. Global configuration: Exclusions

Some fields are subject to **global exclusion**.

These include backbone `.id`, `text`, `contained`, `implicitRules`, `language`, `note`, `photo`, `attachment` (note `Resource.id` is the PK and is never dropped).

Anonymous `extension` and `modifierExtension` elements are also dropped. These are placeholders with no name, type or binding, so no column can be derived from them. Named extension slices are not excluded by default. The distinction is taken from the element `id`, not the path: a last segment of `extension` is anonymous, `extension:ethnicCategory` is a named slice:

    Patient.extension                  -> dropped
    Patient.name.extension             -> dropped
    Patient.extension:ethnicCategory   -> kept

`Reference` children are dropped where present - only `reference` survives, as the `_id` FK (Part A §7):

    Encounter.subject.reference   -> subject_id (the only time a field name is changed)
    Encounter.subject.type        -> dropped
    Encounter.subject.identifier  -> dropped
    Encounter.subject.display     -> dropped

Everything else is included by default, and only excluded per a resource config.

## 2. Resource configuration: Fact codes

In each config, `fact_code` lists paths where the code is the Fact (see Part A §8). Default empty.

## 3. Resource configuration: Exclusions

`exclude` drops additional paths and everything beneath them. A reason is required for each.

## 4. Resource configuration: Additional Variants

A repeating element becomes a variant when the repetition carries information (e.g. several diagnoses on a spell, several identifiers for a patient).

These are declared with a reason, and list of fields forming the `key` (see §5 below). Declaring a path a variant makes its children the variant's fields automatically.

## 5. Resource configuration: Variant object identity

`key` names the fields that, together with the parent PK, uniquely identify one object in the array.

Any combination of the object's own fields is available. The objective is a primary key for the repeating object that has uniqueness. Where an object holds its own content, that content is usually the natural key. Where it holds a pointer to another resource, the `_id` field (Part A §7) or an ordering field such as `rank` can be candidates instead.

LinkML records `key_fields`. Whether the consumer also materialises a single hashed column over them is a join-ergonomics decision downstream, not part of this spec.

## 6. Foreign key targets

A profile may allow several targets for a reference, so `fk` declares which one the CDM points at (see Part A §7) for referential integrity tests. Default empty.

    fk:
      Encounter.subject:            Patient     # profile allows [Patient, Group]
      Encounter.partOf:             Encounter
      Encounter.diagnosis.condition: Condition  # inside a variant - meta only

Only declare a target the CDM actually models, and only where rows genuinely point at it.
