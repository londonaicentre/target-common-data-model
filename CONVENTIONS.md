# Flattened-FHIR (FlatFHIR) CDM Conventions

FlatFHIR is used as an intermediate layer in a medallion data pipeline, predominantly built from transactional source data. Each FHIR resource is modelled as a table in a fact/dimensional schema, with variants to capture usefully repeating items.

This document describes conventions that are followed when generating machine readable descriptions of schema from FHIR definitions.

FHIR version target: **R4 (4.0.1)**, using NHS England / UK Core bindings where available and appropriate. Schemas under `cdm/` are auto-generated from the UK Core StructureDefinition.

## Core modelling principle

A FHIR resource becomes a fact or dimension table, in the spirit of OMOP's `_occurrence`/`measurement`/`observation` tables and the SQL-on-FHIR ViewDefinition standard. Each table corresponds to a FHIR resource. Default grain is one row per resource.

The modelling of resources into schema are defined by config files that name FHIR paths. Each named path becomes one field on the table, wherever it sits in the FHIR tree.

---

# How a resource becomes a schema

Generation runs in two stages.

**Stage 1** is pure FHIR with no opinions in it. A UK FHIR profile snapshot is expanded using core definitions, walking through datatypes, extensions and choice types until every leaf the CDM could name is present. This also expands value-sets and names bindings against elements that would be directly validated against them. This is executed through `scripts/expand_fhir.py`.

**Stage 2** applies global exclusions followed by per resource whitelists. A resource config names the FHIR paths that become fields; everything else is dropped. This is executed through `scripts/generate_linkml.py`, with schemas saved to `cdm/`.

    UK Core StructureDefinition
              |
              |  stage 1   scripts/expand_fhir.py
              v
    build/     expanded/<Resource>.yaml   structure
               fhir_bindings.yaml         which vocabulary applies where
               fhir_enums.yaml            what each vocabulary contains
              |
              |  stage 2   scripts/generate_linkml.py
              v
    cdm/<Resource>.yaml   (LinkML)

A schema is generated from a resource only where `scripts/config/resources/<Resource>.yaml` exists. The config file is therefore the opt-in.

---

# How bindings work

A binding ties a field to a vocabulary, and where the vocabulary is knowable it becomes a LinkML enum with a seed.

**Bindings are declared, never inferred.** A field is bound only where the resource config says so. Nothing is bound implicitly, in the same way that nothing is included implicitly (§7).

**Where a binding lands.** A binding names a vocabulary for a *code*, so it applies to the field holding the bare `code` - not to the wrapper around it. FHIR may declares the binding on the `CodeableConcept`, but the value that must come from the value set sits at `.coding.code` beneath it:

    Condition.clinicalStatus                    FHIR declares the binding here
    Condition.clinicalStatus.coding.code        the field it applies to

Stage 1 resolves this and records the binding against the field it applies to. A config names that same field (i.e. the one that will carry the enum)..

**What a config may declare.** Each entry under `bindings` maps a field path to either of:

    default        the FHIR binding recorded for that field in
                   build/fhir_bindings.yaml
    <EnumName>     an entry in scripts/enum_manifest.yaml, used instead (§12)

Note that `default` only resolves where strength is `required` or `extensible`. These are the strengths under which a source system is obliged to use the value set. A `preferred` or `example` binding is considered a suggestion, and is not reachable through `default`.

The value set must be expandable offline in stage 1, from installed packages alone. This deliberately excludes value-sets created from a SNOMED `is-a` filter or ECL, which exceed a reasonable number of concept counts to express in an Enum.

Failing either throws an error - e.g. a config that writes `default` on a `preferred` field, or on a SNOMED subset.

For users, where the CDM wants to enforce a `preferred` binding (e.g. much of the NHS CDS grid is bound this way) it should be declared as a manual binding instead (§12).

**How a binding reaches the schema.**

    config/resources/*.yaml   DECLARED. Which field is bound, and to what.
              |
        default             <EnumName>
              |                   |
              v                   v
    build/fhir_bindings.yaml   scripts/enum_manifest.yaml
      field path                 HAND-WRITTEN. Manual bindings only -
        -> value set URL         CDM-defined vocabularies, and cases
              |                  where the FHIR default is not what
              v                  the CDM enforces.
    build/fhir_enums.yaml            |
      value set URL                  |
        -> concepts                  |
              |                      |
              +----------+-----------+
                         v
                 cdm/enums.yaml   LinkML enums, imported by each resource
                                  schema and referenced by slot `range`.

Only enums a config names are emitted, so `cdm/enums.yaml` holds exactly what the schemas reference.

---

# Stage 1 - expansion

Stage 1 resolves five things from a FHIR snapshot.

**Datatypes.** An element of complex type is expanded from its definition in the core package. `Encounter.period` yields `period.start` and `period.end`.

**Choice types.** A `foo[x]` element becomes one concrete element per permitted type, named as FHIR names it. E.g. `Condition.onset[x]` yields `onsetDateTime`, `onsetAge`, `onsetPeriod`, `onsetRange`, `onsetString`.

**Extensions.** Extension definitions are loaded to find their value. A simple extension (i.e. one defining a single `value[x]`) collapses to a single element taking the extension's own path, with the type and binding of its `value[x]`:

    Patient.extension:ethnicCategory   -> one element, CodeableConcept,
                                          bound to UKCore-EthnicCategory

A complex extension that defines sub-extensions instead of a value, yields one element per sub-extension value. It does not collapse:

    Patient.extension:deathNotificationStatus
      .extension:deathNotificationStatus  -> CodeableConcept
      .extension:systemEffectiveDate      -> dateTime

**Bindings.** A binding declared on a `CodeableConcept` or `Coding` is pushed down to the field that carries the bare `code`, and recorded there in `build/fhir_bindings.yaml` with its value set and strength. Every binding in the resource is recorded, whether or not any config names the field:

    Condition.clinicalStatus              declared on the CodeableConcept
      -> Condition.clinicalStatus.coding.code    recorded here

    Patient.gender                        already a bare code
      -> Patient.gender                          recorded where it stands

Separately, every value set in the installed packages is expanded to its concepts, or recorded with the reason it cannot be expanded offline, in `build/fhir_enums.yaml`. This is a terminology cache over the packages and is not specific to any resource.

**Termination.** FHIR datatypes are mutually recursive - a `Reference` holds an `Identifier`, which holds a `Reference`. Any `Reference` children are not expanded. A datatype is not expanded inside itself, and a depth ceiling bounds the walk.

FHIRPath system types (`http://hl7.org/fhirpath/System.*`) are dropped.

---

# Stage 2 - resolution

Stage 2 defines which FHIR paths become fields, using a mixture of global exclusions, a resource whitelist of paths, and further exclusions of specific paths. All transformations follow the following conventions.

## How the whitelist works

In each resource config, `include` names the FHIR paths that become fields. A path not named explicitly, is not in the model.

The rule for what a named path gives you is:

**A field holds everything that sits under its path, as it is, unless it is part of an array that exists above the path, in which case it resolves to that array's first entry. Where the path itself is `0..*`, the field is a variant.**

Naming a deep path **promotes** it to the top level of the table, lifting it out of whatever structure held it. This is the only mechanism for reaching into a FHIR tree.

Examples:

    Encounter.status              a primitive
                                  -> status

    Encounter.period              a complex type, 0..1
                                  -> period {start, end}

    Encounter.period.start        a primitive, promoted out of the struct
                                  -> period_start

    Encounter.period.end          a primitive, promoted out of the struct
                                  -> period_end

    Condition.clinicalStatus      0..1, but .coding beneath it is 0..*
                                  -> clinicalstatus {coding[], text} - a
                                     variant inside a non-repeating field

    Patient.address.postalCode    an array (address) sits ABOVE
                                  -> address_postalcode, from address[0].
                                     The other addresses are not reachable
                                     from this path.

    Patient.address               the array itself
                                  -> address[], every address, each carrying
                                     its own postalCode, line, city ...

    Condition.code.coding.code    an array (coding) sits ABOVE
                                  -> code_coding_code, from coding[0]

    Condition.code.coding         the array itself
                                  -> code_coding[], every coding

    Encounter.diagnosis           an array of BackboneElement, holding a
                                  Reference and a CodeableConcept
                                  -> diagnosis[], objects of
                                     {condition_id, use{...}, rank}

    Encounter                     the resource root
                                  -> the entire resource, variants within
                                     variants. See below - do not do this.

An array above the path can also be written explicitly as `[0]`. It selects that array's first entry and nothing else, so `Patient.address.postalCode` and `Patient.address[0].postalCode` are the same field - the second says so out loud rather than leaving it to be inferred from the cardinality.

`[0]` is also the only way to name the first entry of an array *whole*:

    Patient.address[0]            the first address, entire
                                  -> address {use, line, city, postalCode}
    Patient.address               every address
                                  -> address [ {use, line, city, postalCode} ]

Only `[0]` is permitted - any other index would depend on a source ordering that isn't guaranteed in FHIR.

Paths are allowed to overlap. A path and its own child can both be named; they are independent fields on the table and do not interact. This is how a much-used value is promoted out of an array without losing the array:

    Patient.identifier                  -> identifier[], the complete record
    Patient.identifier:nhsNumber.value  -> identifier_nhsnumber_value, the
                                           join key, as a single value

An entry may also carry the following configurables:

    exclude    child paths that should not appear beneath it (§8)
    key        for a variant, the fields that identify one object (§9)
    fk         for a Reference, which resource it points at (§10)
    bindings   which fields beneath it are bound, and to what (§12)

## Good authoring practices

The single rule above will faithfully build whatever the whitelist asks for, including things nobody should want! Naming `Encounter` gives the entire resource. Naming a `0..*` path three levels above the value of interest gives an array of objects holding arrays of objects. The generator will not stop you.

**Choosing paths that make an ergonomic table is the config author's job**

In practice:

- Name the direct path that carries the values required.
- Similarly, prefer a promoted scalar to a structure the consumer must dig through, especially if the structure has no affinity to source data, or carries no extra information.
- Watch what sits beneath the path, not just the path. A `0..1` element can still hold arrays below it. Do not name a path whose shape you have not looked at in `build/expanded/<Resource>.yaml`! Trim what is not wanted with `exclude`.

## Global conventions

### 1. Global exclusions

A globally excluded path, type or element name is dropped along with everything beneath it, before the whitelist is consulted. A whitelist entry cannot resurrect one.

Excluded everywhere:

    contained, implicitRules, language           # resource housekeeping
    Narrative, Attachment                        # rendered and binary payloads
    element `.id` at any depth                   # intra-document referencing only
    anonymous extension / modifierExtension      # no name, type or binding
    Coding.version, Coding.userSelected          # terminology server bookkeeping
    Reference children other than `reference`    # cross-server pointer machinery

`Resource.id` is the PK and is never dropped.

`Resource.text` is the rendered narrative and is covered by the `Narrative` datatype above. The `.text` on a CodeableConcept is a different thing - a plain string carrying the rendered term - and is **not** excluded globally. It is trimmed per entry with `exclude` (§8) wherever the coding already carries the meaning, which is what the resource configs do.

The last two entries are properties of stage 1 rather than configurable exclusions, so no key for them appears in `global.yaml`: element `.id` is typed as a FHIRPath system type and is never emitted, and the expander stops at a `Reference`, so its children are never walked.

Anonymous extensions are identified from the element `id`, not the path: a last segment of exactly `extension` is anonymous, `extension:ethnicCategory` is a named slice.

    Patient.extension                  -> dropped
    Patient.name.extension             -> dropped
    Patient.extension:ethnicCategory   -> available to the whitelist

### 2. References become `_id` (FK)

A FHIR `Reference` is a pointer from one resource to another - the equivalent of a foreign key. It resolves to a single scalar field with an `_id` suffix, holding the target's `id`.

    Encounter.subject             -> subject_id
    Encounter.partOf              -> partof_id
    Encounter.diagnosis.condition -> condition_id   (inside a variant)

This is the one rule a whitelist path cannot express, because it is a rename rather than a selection: the value comes from `subject.reference` but the field is `subject_id`.

The `_id` name comes from the FHIR element, not the target resource: `Encounter.subject` is `subject_id`, not `patient_id`. Which resource it points at is declared per resource (§7).

Applies at any depth, so a `Reference` inside a variant becomes an `_id` field on the variant object.

### 3. Primary key and provenance

Every table carries, without being declared:

    id                  Resource.id           PK
    meta_source         Resource.meta.source
    meta_last_updated   Resource.meta.lastUpdated

The rest of `Resource.meta` describes the FHIR message rather than the data in it, and is globally excluded (§1): `versionId` is the resource version on the server it came from, `profile` asserts conformance, and `security` and `tag` are `0..*` Codings populated ad hoc by whatever wrote the resource.

### 4. Naming

The whitelisted path is re-named by dropping the resource prefix, and replacing `.` and `:` with `_`, lowercase. No camelCase splitting, no shortening, and every level of the hierarchy is retained.

    Encounter.status                       -> status
    Encounter.period                       -> period
    Patient.address.postalCode             -> address_postalcode
    Patient.identifier:nhsNumber.value     -> identifier_nhsnumber_value
    Condition.onsetDateTime                -> onsetdatetime
    Condition.code.coding                  -> code_coding

The name carries the full path even where the field was promoted from deep in the tree, so it always traces back to the FHIR element it came from.

Everything **under** the field keeps its FHIR name, unchanged. The whitelisted path is the field; its contents are that element's own structure.

    Encounter.period            -> period {start, end}
    Condition.code.coding       -> code_coding [ {system, code, display} ]
    Patient.address             -> address [ {use, line, city, postalCode} ]

So naming a deeper path is how a value is lifted out of a structure and given a name of its own:

    Encounter.period            -> period {start, end}
    Encounter.period.start      -> period_start, a value in its own right

Every slot carries `fhir_path` and `fhir_type`, and inherits the constraints of its FHIR element.

### 5. Variants

Where a whitelisted path is `0..*`, the field is a variant - an array of objects carrying whatever FHIR puts in them, which may include a further variant.

    diagnosis  [ {condition_id, use{coding[]}, rank} ]  a variant within
                                                        a variant
    code_coding [ {system, code, display, is_source} ]  objects of values

Nesting is bounded by the path, not by a rule: a field can only be as deep as the tree beneath the path it names. Naming a leaf gives a single value; naming the resource root would give the entire resource. Keeping that depth sensible is the config author's job - see *Good authoring practices*.

### 6. The `is_source` flag

Every `Coding` that is included gets an `is_source` flag: the coding that came from the source system has `is_source` true, and a downstream mapping step adds standard-vocabulary codings with `is_source` false. Exactly one true per array. **`is_source` is a custom field, not native to FHIR.**

    Condition.code.coding      -> code_coding [ {system, code, display, is_source} ]
    Condition.code.coding[0]   -> code_coding {system, code, display, is_source}
    Encounter.diagnosis        -> diagnosis [ {..., use {coding [ {..., is_source} ]}} ]
                                  a coding inside a variant is still a coding

Where a single coding survives, `is_source` is true on it.

---

## Resource conventions

These are declared per resource in `scripts/config/resources/<Resource>.yaml`.

### 7. The whitelist

`include` names the FHIR paths that become fields, as set out in *How the whitelist works* above. **A path not named is not in the model.** There are no defaults and nothing is included implicitly.

Each entry may carry:

    exclude    child paths that should not appear beneath it (§8)
    key        for a variant, the fields that identify one object (§9)
    fk         for a Reference, which resource it points at (§10)
    bindings   which fields beneath it are bound, and to what (§12)

### 8. Per-entry exclusions

For each whitelist entry, `exclude` lists paths that should not appear. A reason is required for each.

This is where the unwanted children of a complex datatype are trimmed. It is verbose and repeated across resources by design.

### 9. Variant keys

`key` names the fields that, together with the parent PK, uniquely identify one object in the array. Required on every variant.

Any combination of the object's own fields is available. Where an object holds its own content, that content is usually the natural key. Where it holds a pointer to another resource, the `_id` field (§2) or an ordering field such as `rank` are candidates instead.

LinkML records `key_fields`. Whether the consumer also materialises a single hashed column over them is a join-ergonomics decision downstream, not part of this spec.

### 10. Foreign key targets

A profile may allow several targets for a reference, so `fk` declares which one the CDM points at (§2) for referential integrity tests.

    Encounter.subject:             Patient     # profile allows [Patient, Group]
    Encounter.partOf:              Encounter
    Encounter.diagnosis.condition: Condition   # inside a variant - meta only

Only declare a target the CDM actually models, and only where rows genuinely point at it.

### 11. Rejected paths

`rejected` is an optional block recording why a notable path is absent. It has no effect on generation.

As the whitelist makes absence silent, `rejected` is where the non-obvious calls are documented. For example, fields that look useful but are not populated.

### 12. Bindings

`bindings` declares which fields beneath an entry are bound, and to what. Nothing is bound that is not named here (*How bindings work*).

Each key is the full path of the field carrying the bare `code` - the field the binding applies to, which is what stage 1 recorded. Each value is either `default` or the name of a manifest entry:

    Condition.clinicalStatus:
      bindings:
        Condition.clinicalStatus.coding.code: default

    Patient.gender:
      bindings:
        Patient.gender: default

    Encounter.hospitalization.admitSource:
      bindings:
        Encounter.hospitalization.admitSource.coding.code: AdmitSourceEnum

Paths are written in full. This is verbose, and deliberately so.

**`default`** takes the FHIR binding recorded for that field. It resolves only where the strength is `required` or `extensible` and the value set expands offline; anything else is an error (*How bindings work*).

**A manifest entry** is used where `default` will not do. Entries can be added here to enforce FHIR `preferred` strength bindings, or custom CDM-defined vocabularies. The manifest holds hand-written bindings and nothing else.

---

# Post-generation checks

**Every whitelisted path must exist in the expanded tree.**

**Every variant must declare a `key`.**

**Every declared binding path must exist**, must sit beneath the entry declaring it, and must be a field the schema emits.

**Every `default` binding must resolve** - the field carries a FHIR binding of strength `required` or `extensible`, and its value set expands offline.

**Every manifest entry named by a config must exist in the manifest.**

Reported, but not errors:

**A whitelisted field carries a FHIR binding no config declared.** Emitted as a plain field (*How bindings work*).

**A manifest entry reproduces the value set `default` would have resolved to.** Derivable, so the hand-written copy is redundant and can drift.

**A manifest entry is named by no config.** Not emitted to `cdm/enums.yaml`.
