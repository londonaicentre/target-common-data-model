# Flattened-FHIR (FlatFHIR) CDM Conventions

FlatFHIR is used as an intermediate layer in a medallion data pipeline, predominantly built from transactional source data. Each FHIR resource is modelled as a table in a fact/dimensional schema, flattened where possible, but using variants to capture usefully repeating items.

This document describes conventions that are followed when generating machine readable descriptions of schema from FHIR definitions.

FHIR version target: **R4 (4.0.1)**, using NHS England / UK Core bindings where available and appropriate. Schemas under `cdm/` are auto-generated from the UK Core StructureDefinition.

---

# Core modelling principle

We flatten FHIR resources as fact or dimension tables, in the spirit of OMOP's `_occurrence`/`measurement`/`observation` tables and the SQL-on-FHIR ViewDefinition standard. Each table corresponds to a FHIR resource. Default grain is one row per resource.

---

# How a resource becomes a schema

Generation runs in two stages.

**Stage 1** is pure FHIR with no opinions in it. A UK FHIR profile snapshot must be expanded using core definitions. Stage 1 walks through datatypes, extensions, and choice types until every leaf the CDM could name is present. No exclusion, naming or flattening rule is applied here. This is executed through `scripts/expand_fhir.py`.

**Stage 2** applies reproducible configurations that 'flatten' deeply nested elements, and drop fields with limited analytical utility, according to global and resource specific rules. This is executed through `scripts/generate_linkml.py`, with schemas saved to `cdm/`.

    UK Core StructureDefinition
              |
              |  stage 1   scripts/expand_fhir.py
              v
    build/expanded/<Resource>.yaml
              |
              |  stage 2   scripts/generate_linkml.py
              v
    cdm/<Resource>.yaml   (LinkML)

A schema is generated from a resource only where `scripts/config/resources/<Resource>.yaml` exists. The config file is the opt-in.

---

# Stage 1 - expansion

Stage 1 resolves four things from a FHIR snapshot.

**Datatypes.** An element of complex type is expanded from its definition in the core package. `Encounter.period` yields `period.start` and `period.end`.

**Choice types.** A `foo[x]` element becomes one concrete element per permitted type, named as FHIR names it. E.g. `Condition.onset[x]` yields `onsetDateTime`, `onsetAge`, `onsetPeriod`, `onsetRange`, `onsetString`.

**Extensions.** Extension definitions are loaded to find their value. A simple extension (i.e. one defining a single `value[x]`) collapses to a single element taking the extension's own path, with the type and binding of its `value[x]`:

    Patient.extension:ethnicCategory   -> one element, CodeableConcept,
                                          bound to UKCore-EthnicCategory

A complex extension that defines sub-extensions instead of a value, yields one element per sub-extension value. It does not collapse:

    Patient.extension:deathNotificationStatus
      .extension:deathNotificationStatus  -> CodeableConcept
      .extension:systemEffectiveDate      -> dateTime

**Termination.** FHIR datatypes are mutually recursive - a `Reference` holds an `Identifier`, which holds a `Reference`. Any `Reference` children are not expanded.

FHIRPath system types (`http://hl7.org/fhirpath/System.*`) are dropped.

---

# Stage 2 - resolution

## Execution order

Each expanded element is tested against the conventions below **in order**; for a given field, first match wins and later conventions are not consulted.

                                                               global  resource
    excluded                        -> dropped                 §1      §9
    coding_as_variant declared      -> array of codings                §10
    repeating_as_variant declared   -> array of flat objects           §11
    type Reference                  -> scalar _id              §3
    type Coding or CodeableConcept  -> flattened codings       §2
    repeating, none of the above    -> first entry taken       §4
    complex type                    -> recurse into children   §5
    otherwise                       -> scalar column           §6

Global conventions depend only on the FHIR element and fire identically for every resource. Resource conventions fire only where a resource config declares the path. A path may be excluded globally (§1) or per resource (§9).

Note that two things in FHIR can be plural: a coded concept can carry several **codings**, and an element can **repeat**.  Objects that are to be preserved as a variant can be declared per-resource. Then falls back on default behaviour which is for the first element to be taken and flattened.

    coding:      how many codings survive?
      coding_as_variant declared    -> keep all, as an array          §10
      otherwise                     -> keep the first, flattened      §2

    repeating:  how many entries survive?
      repeating_as_variant declared -> keep all, as an array          §11
      otherwise                     -> keep the first, flattened      §4

Note that naming (§7) and provenance (§8) conventions are not part of the chain, but apply to whatever is produced. Similarly, foreign key targets (§12) supply the target a reference records, rather than resolving an element themselves.

### One nesting layer

The output of the chain has a hard structural bound: **a column is either a scalar or an array of flat objects**.

    id                                  scalar
    class_code                          scalar
    diagnosis   [ {condition_id, use_code, rank} ]   array of flat objects
    code        [ {system, code, is_source} ]        array of flat objects

This is why §2 flattens coded concepts at *every* level rather than keeping them as objects. An object inside an array would be a second layer, so the two declared exceptions above are the only source of nesting, and neither can contain the other.

---

## Global conventions

Fixed in `scripts/generate_linkml.py` and `scripts/config/global.yaml`. These apply to every resource and are not configurable per resource.

## 1. Exclusions

A globally excluded path or type is dropped along with everything beneath it. Exclusion is tested first so no later convention needs to know about scope.

Global exclusions cover backbone `.id`, `text`, `contained`, `implicitRules`, `language`, `photo` and `attachment`. `Resource.id` is the PK and is never dropped.

Anonymous `extension` and `modifierExtension` elements are dropped. These are placeholders with no name, type or binding, so no column can be derived from them. The distinction is taken from the element `id`, not the path: a last segment of exactly `extension` is anonymous, `extension:ethnicCategory` is a named slice:

    Patient.extension                  -> dropped
    Patient.name.extension             -> dropped
    Patient.extension:ethnicCategory   -> kept

Named extension slices are never excluded globally - whether one is in scope is a per-resource judgment (§9).

`Reference` children are dropped where present; only `reference` survives, as the `_id` FK (§3):

    Encounter.subject.reference   -> subject_id
    Encounter.subject.type        -> dropped
    Encounter.subject.identifier  -> dropped
    Encounter.subject.display     -> dropped

Everything else is included by default, and only excluded per a resource config (§9).

## 2. Coded concepts

For `Coding` and `CodeableConcept`, a concept keeps its **first coding**, flattened to scalar columns named for the element that carried it. These are overridden on declared elements in the resource config.

    Encounter.class                        -> class_system
                                              class_code
                                              class_display
    Encounter.hospitalization.admitSource  -> hospitalization_admitsource_system
                                              hospitalization_admitsource_code
                                              hospitalization_admitsource_display

`CodeableConcept.coding` is itself `0..*`, so "first coding" is `coding[0]`. A `CodeableConcept` carrying two codings loses the second; where that repetition is the information, declare the path `coding_as_variant` (§10).

This applies at every level, including inside a declared variant, so a coded field of a variant object is flat like any other:

    Encounter.diagnosis.use  ->  use_system, use_code, use_display
                                 (fields of the diagnosis object, §11)

A bare `code` primitive (`Encounter.status`) is not a Coding and falls through to scalar (§6).

Value set bindings on either kind are recorded against the `_code` column (§7).

## 3. References (foreign keys)

A FHIR `Reference` is a pointer from one resource to another - the equivalent of a foreign key. It flattens to a scalar field with an `_id` suffix.

    Encounter.subject             -> subject_id     the patient the encounter was with
    Encounter.partOf              -> partof_id      the parent encounter, where one
                                                    encounter sits inside another - a
                                                    consultant episode within a spell
    Encounter.diagnosis.condition -> condition_id   the condition this encounter was about

A Reference is caught here before it can recurse, so its children never become columns even though it is a complex type (§5).

The column holds the target's `id`. So `subject_id` on an encounter row holds a value found in `patient.id`. Where sources already hold real keys (`patient_id`, `encounter_id`), the FK is a straight mapping from source data.

The `_id` name comes from the FHIR element: `Encounter.subject` is `subject_id`, not `patient_id`.

A profile may allow several targets - `subject` may be a Patient *or* a Group. Which resource the column points at is declared per resource (§12).

This applies at any depth, so a `Reference` inside a variant becomes an `_id` field on the variant object, as `condition_id` above.

A `Reference` also carries `type`, `identifier` and `display`, which exist so a FHIR server can resolve a pointer to a resource held elsewhere. That problem does not arise in a warehouse where every target is a table, so they are globally excluded (§1).

## 4. First-entry flattening

A repeating element that is **not** declared `repeating_as_variant` keeps its **first entry only**. Remaining entries are discarded.

This is the only convention that loses whole entries, so a repeating element is worth a deliberate decision on whether to declare it a variant (§11).

    Encounter.type       0..* -> first treatment function only
    Condition.category   0..* -> first category only
    Condition.note       0..* -> first annotation only

Note this is entry-level truncation, distinct from the coding-level truncation in §2. `Encounter.type` is `0..*` CodeableConcept, so both apply: the first type is kept, and that type's first coding is flattened.

## 5. Recursion into complex types

An element of complex type that no earlier convention has claimed will resolve by flattening.

    Encounter.period         Period, nothing earlier matches.
      .start   -> scalar     period_start
      .end     -> scalar     period_end

**There is no explicit rule here for handling deep nesting**. Deeply nested objects are restricted by stage 1 terminating recursion, and by `Coding` and `CodeableConcept` handling at §2. What is left are generally a small, shallow set which should be handled by exclusions (§1, §9) which delete whole branches.

This is the only convention that relies on configuration discipline. However, a variant surviving two layers deep will hard-fail on the post-generation check.

## 6. Scalars

The default. Everything not caught above becomes a scalar column with the §7 name, carrying the type and constraints of its FHIR element.

## 7. Naming, types and bindings

Drop the resource prefix, replace `.` and `:` with `_`, lowercase. No camelCase splitting and no shortening.

    Encounter.period.start                 -> period_start
    Encounter.hospitalization.admitSource  -> hospitalization_admitsource_code
    Patient.identifier:nhsNumber.value     -> identifier_nhsnumber_value
    Condition.onsetDateTime                -> onsetdatetime
    Patient.deceasedBoolean                -> deceasedboolean

Two suffixes are added by convention rather than taken from FHIR: `_id` on a foreign key (§3), and `_system` / `_code` / `_display` on a coded concept (§2). Inside a variant the same rules apply to the object's fields, so `Encounter.diagnosis.use` gives `use_code`, not `diagnosis_use_code` - the parent path is the column, and the object's fields are named relative to it.

Every slot representing a FHIR element carries `fhir_path` and `fhir_type`, and inherits the constraints of that element.

**Bindings.** A value set of 100 concepts or fewer becomes an enum with a seed; larger sets stay `string` with the binding recorded. 100 is an arbitrary cut-off that separates well defined UK codesets from exceptionally large `is_a` SNOMED codelists.

A binding is recorded against the `_code` column, which is a bare code at every level, so `accepted_values` applies directly - including inside a variant, once the array is exploded to a view.

## 8. Primary keys and provenance

PK is `id` from `Resource.id`. Facts carry `subject_id` from `<Resource>.subject`. Patient is keyed by `id` like everything else.

Every table carries `meta_source`, `meta_tag_code` and `meta_last_updated` from `Resource.meta`.

`Meta.tag` is `0..*`, so `meta_tag_code` is subject to §4 and holds the first tag only.

---

## Resource conventions

Declared per resource in `scripts/config/resources/<Resource>.yaml`. Each file declares **only deviations** from the global conventions above; everything else is fixed in the script.

A config declares three things: what to exclude, and the two ways of saying "do not collapse this".

## 9. Exclusions

`exclude` drops additional paths and everything beneath them. A reason is required for each.

This is where named extension slices are judged. They survive global exclusion (§1), so each one kept or dropped is an explicit scope decision for that resource.

## 10. coding_as_variant

`coding_as_variant` lists coded paths where the several codings are the information, so the concept keeps all of them instead of collapsing to the first (§2). Default empty.

The column becomes an array of flat objects, one per coding:

    Condition.code -> [ { system, code, display, is_source } ]

`is_source` is always present on such an array. The coding that came from the source system has `is_source` true; a downstream mapping step adds standard-vocabulary codings with `is_source` false. Exactly one true per array. **`is_source` is a custom field, not native to FHIR.**

The usual case is the code that identifies the row - `Condition.code`, `Observation.code` - where a source SNOMED code and its mapped equivalents all need to survive for cohorting.

## 11. repeating_as_variant

`repeating_as_variant` lists repeating paths where the repetition is the information, so the element keeps all its entries instead of collapsing to the first (§4). Default empty. Declared with a reason and a `key`.

The column becomes an array of flat objects, one per entry. Ordering fields (e.g. `diagnosis.rank`) stay inside the object so the array can be re-sequenced.

Declaring a path a variant makes its children the variant's fields, each resolved by the same chain:

    Encounter.diagnosis    declared a variant. Its children resolve
                           independently:
                             diagnosis.condition -> condition_id  (§3)
                             diagnosis.use       -> use_system,
                                                    use_code,
                                                    use_display   (§2)
                             diagnosis.rank      -> rank          (§6)

Objects in the array are flat by construction: §2 guarantees a coded field arrives as scalars rather than a nested object, and §3 reduces a reference to a scalar `_id`. A variant that would still nest is an error (§13).

`key` names the fields that, together with the parent PK, uniquely identify one object in the array. Any combination of the object's own fields is available. The objective is a primary key for the repeating object that has uniqueness. Where an object holds its own content, that content is usually the natural key. Where it holds a pointer to another resource, the `_id` field (§3) or an ordering field such as `rank` can be candidates instead.

LinkML records `key_fields`. Whether the consumer also materialises a single hashed column over them is a join-ergonomics decision downstream, not part of this spec.

## 12. Foreign key targets

A profile may allow several targets for a reference, so `fk` declares which one the CDM points at (§3) for referential integrity tests. Default empty.

    fk:
      Encounter.subject:             Patient     # profile allows [Patient, Group]
      Encounter.partOf:              Encounter
      Encounter.diagnosis.condition: Condition   # inside a variant - meta only

Only declare a target the CDM actually models, and only where rows genuinely point at it.

---

# Post-generation checks

**The generated LinkML must not contain a variant two layers deep. Where one survives, the generator hard-fails.**

The check is evaluated after the chain has resolved to enforce the one-nesting-layer bound.

Where nesting genuinely survives into a declared variant, the flat-object rule in §11 is broken. The fix is a config decision - exclude the inner repeat, or restructure what is declared a variant.

Note that a repeating primitive inside a variant is resolved by §4, keeping its first entry:

    Patient.name -> one object per name
      { use_code: "official", family: "Smith", given: "Jane", prefix: "Dr" }
      { use_code: "maiden",   family: "Jones", given: "Jane", prefix: null }

The outer repeat carries a full set of information; `given` and `prefix` keep their first entry.
