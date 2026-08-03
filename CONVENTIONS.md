# Flattened-FHIR (FlatFHIR) CDM Conventions

FlatFHIR is an intermediate layer built by SQL pipelines from transactional source data. We do not ingest FHIR messages, so we expect referential integrity already holds at source.

FHIR version target: **R4 (4.0.1)**, using NHS England / UK Core bindings where available and appropriate.

Schemas under `cdm/` are auto-generated from the UK Core StructureDefinition.

**Part A** describes what the script derives from the profile.

**Part B** describes custom configurations that are declared globally and per resource.

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

The only time there is custom field naming is creation of FK equivalent `_id` fields (see §6)

We do not introduce custom field naming at this stage.

## 3. Primary keys

PK is `id` from `Resource.id`. Facts carry `subject_id` from `<Resource>.subject`. Patient is keyed by `id` like everything else.

## 4. Referring back to FHIR

Every slot representing a FHIR element carries `fhir_path` and `fhir_type`, and inherits the constraints of that element.

## 5. Coding

`Coding` and `CodeableConcept` are never flattened - they surface as an object of `system`, `code`, `display`. Keys inside the object are the FHIR element names.

A bare `code` primitive (`Encounter.status`) stays scalar and keeps its `accepted_values` test. Instead of introducing a `_display` value for a scalar code, these resolve by joining to `seed_*.csv` in a later analyst view.

An object column carries its `value_set` binding and, where the set is enumerable (see §8), the enum name in `meta`. No `accepted_values` test is emitted on it as an object never = a bare code. The binding is recorded so the test is derivable downstream.

## 6. References

`Reference` flattens per §9 to a scalar FK. An `_id` suffix is added as the one exception to §2.

    Encounter.subject -> subject_id  VARCHAR
    Encounter.partOf  -> partof_id   VARCHAR

As our sources all hold real keys (`patient_id`, `encounter_id`), the FK is a straight mapping.

The other `Reference` fields (`type`, `identifier`, `display`) describe cross-server pointers in FHIR messages and are globally excluded (Part B §1). Display resolves by joining to the target table.

The config declares a FK target (Part B §6) for relationships testing.

## 7. The Fact code

Where a column is the code that is the Fact (e.g. `Condition.code`, `Observation.code`), it is a variant holding one object per coding: the source coding plus any mapped standard-vocabulary codings.

Each object is `system`, `code`, `display`, `is_source`. The source coding has `is_source` true; a mapping step adds standard vocabulary codes with `is_source` false. Only ever one true per array. **note: `is_source` is not canonical FHIR**. Paths to Fact codes are declared in the config (see Part B §1).

## 8. Enumerations

A value set of 100 concepts or fewer becomes an enum with an `accepted_values` test and a seed. Larger sets stay `string` with the binding recorded.

Counting is offline only, so generation is deterministic. Sets that are SNOMED `is-a` queries with no shipped expansion cannot be counted and stay `string`. If expansion is ever fetched, the NHS England Ontology Server is the source.

The threshold is fixed in the script and is not tunable per resource, so changing it reclassifies columns across every resource at once. The counts behind the choice of 100 need re-deriving before it is relied on.

## 9. Flattening

Non-repeating elements flatten with the §2 name. `Encounter.period` becomes `period_start`, `period_end`.

## 10. Variants

A repeating element is held on the parent as a `variant` column: an array of flat objects, one layer of hierarchy only. Ordering fields (e.g. `diagnosis.rank`) stay inside the object so the array can be re-sequenced.

## 11. Repeats inside repeats

Keep the outer repeat as the variant, flatten a repeating primitive inside it to its first entry.

    Patient.name -> one object per name
      { use: "official", family: "Smith", given: "Jane", prefix: "Dr" }
      { use: "maiden",   family: "Jones", given: "Jane", prefix: null }

The outer repeat carries a full set of information.

All useful cases resolve this way with no additional config. The inner repeat is complex only in cases with little analytical utility, so for the FlatFHIR CDM, these are excluded explicitly in config (Part B §2).

The script will hard-fail on any repeat inside a repeat that is not resolved by the primitive rule or by config. This also results in the flat-variant rule in §10 being enforced.

## 12. Source provenance

Every table carries `meta_source`, `meta_tag_code` and `meta_last_updated` from `Resource.meta`, flattened.

---

# Part B - global and per resource configuration

One global configuration defines exclusions that apply to all resources

One configuration file then exists per resource at `scripts/resources/<Resource>.yaml`. Each file declares **only deviations**.

Part A is otherwise fixed in the script.

## 1. Global exclusions

Some fields are subject to **global exclusion**.

These include backbone `.id`, `text`, `contained`, `implicitRules`, `language`, `note`, `photo`, `attachment`. Note `Resource.id` is the PK and is never dropped.

Anonymous `extension` and `modifierExtension` elements are also dropped. These are placeholders with no name, type or binding, so no column can be derived from them. Named extension slices are not excluded by default. The distinction is taken from the element `id`, not the path: a last segment of `extension` is anonymous, `extension:ethnicCategory` is a named slice:

    Patient.extension                  -> dropped
    Patient.name.extension             -> dropped
    Patient.extension:ethnicCategory   -> kept

`Reference` children are dropped where present - only `reference` survives, as the `_id` FK (Part A §6):

    Encounter.subject.reference   -> subject_id (the only time a field name is changed)
    Encounter.subject.type        -> dropped
    Encounter.subject.identifier  -> dropped
    Encounter.subject.display     -> dropped

Everything else is included by default, and only excluded per a resource config.

## 2. Fact codes

`fact_code` lists the paths where the code is the Fact (see Part A §7). Default empty.

## 3. Exclusions

`exclude` drops paths and **everything beneath them**. A reason is required for each.

## 4. Variants

A repeating element becomes a variant when the repetition carries information (e.g. several diagnoses on a spell, several identifiers for a patient).

Declared with a reason, and list of fields forming the `key` (see §5).

Declaring a path a variant makes its children the variant's fields automatically.

## 5. Surrogate keys for variant objects

Each variant object needs a deterministic PK, unique across all objects in the parent table.

This is a content-based hash of the parent PK and the fields identifying the object in source. E.g. for a CodeableConcept, `hash(parent_id, system, code)`.

## 6. Foreign key targets

`fk` declares which reference paths get a `relationships` test, and against which resource (see Part A §6). Default empty - no test is emitted for an undeclared path.

    fk:
      Encounter.subject: Patient   # profile allows [Patient, Group]
      Encounter.partOf:  Encounter

Only declare a target the CDM actually models, and only where rows genuinely point at it.