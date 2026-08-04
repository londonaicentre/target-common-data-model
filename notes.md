The problem
Schemas under cdm/ are currently hand-written.

Two people writing two resources can produce two different answers same convention:

E.g. What repeating elements to flatten? Observation flattened a repeating element (referenceRange) while Encounter makes similar one a variant.
Naming is inconsistent existing schemas (status vs encounter_status, condition_category vs category).
Results in convention drift across schemas.

Solution?
1. LinkML for a resource should be reproducible
Given a FHIR resource, two people should arrive at the same LinkML. Currently not the case, as schema is transcribed by hand from the UK Core profile.

Currently, UK Core npm package has full snapshots for 31 resources including FHIR paths, types, cardinalities, value set bindings, extension
slice names etc. Source is machine-readable.

2. Generation should be declared, subjectivity = configurable
Some decisions can't be derived from FHIR (mainly 'what do we make a variant').

Fully automatic blanket rules don't work:

One extreme: Everything repeating becomes a variant = 81 variant classes across 7 resources (vs 7 today), most coded columns untestable in dbt, results in 19 repeating elements nested inside other repeating elements (breaks flat variant rule).
Other extreme: Only the fact 'resource.code' is a variant loses data that is more valuable as a variant: e.g. CDS primary/secondary diagnosis grid, blood pressure readings.
Subjective calls come from whether recording as a variant carries analytic meaning which is a judgment call. So aim should be to make judgement explicit + declared once + reviewable, and then resulting in programmatic generation of LinkML schema.

Compromise rule: Defualt = flatten, and a repeating element becomes a variant when repetition contains information (e.g. several diagnoses on a spell, several identifiers for a patient). Anything else is flattened to first entry or dropped.
3. Generator script plus a config file per resource
Two pieces:

script to read the UK Core StructureDefinition and emit LinkML — paths, types, bindings, PK, FKs, choice types, EXCLUDED block.
config file (same idea as enum_manifest.yaml) holding the decisions that profile can't answer. Keyed by FHIR path. lists exceptions to the
default. For each repeating element: variant, flatten, surrogate key fields for variants, and a reason. Also declares fields for exclusion
Output becomes deterministic and makes completeness of schema checkable.

Other things in play...
Drop the custom _display sidecar columns and surface these from analyst view via seed_*.csv files
How do we test variants? Still going for "explode the array into a view" step?
Enum being "enumerable" at what threshold?
Variant contents are documented as tested but nothing tests them.
Activity

drjzhn
assigned 
drjzhn
,
Mcanroe
and
lawrenceadams
2h ago
lawrenceadams commented 1 hour ago
@lawrenceadams
lawrenceadams
1h ago
Member
LinkML for a resource should be reproducible
In fairness, yes

Generation
Compromise rule: Defualt = flatten

I think the best approach; albeit I'd include Coding primitives too

As far as I can tell - we want codes to be objects/semi structured types for ergonomic reasons

As for others:

Drop the custom display sidecar columns and surface these from analyst view via seed*.csv files

Yes, either seed files or some other means - they shouldn't have made it this far anyway

How do we test variants? Still going for "explode the array into a view" step?
Variant contents are documented as tested but nothing tests them.

Adding tests that check that for a given column of type Variant or Object the keys exist ± that for a given key the contents are castable into a given type (Currently, Snowflake doesn’t support explicitly-typed objects)

Enum being "enumerable" at what threshold?

~ 100 I'd support; Most of the NHS types are fine but the "Condition" FHIR type contains 100k+ concepts.

---

# Decisions — generated LinkML from UK Core profiles

Settled 2026-08-03. Governing principle throughout: **minimise customisation, revert to
default FHIR when in doubt**. The generator derives everything it can from the
StructureDefinition snapshot; the per-resource config declares only deviations.

## Naming

Column names are a mechanical function of the FHIR path. Drop the resource prefix,
replace `.` and `:` with `_`, lowercase the whole string. No camelCase splitting, no
word-boundary detection, no shortening.

| FHIR path | Column |
|---|---|
| `Encounter.period.start` | `period_start` |
| `Encounter.hospitalization.admitSource` | `hospitalization_admitsource` |
| `Encounter.hospitalization.dischargeDisposition` | `hospitalization_dischargedisposition` |
| `Patient.identifier:nhsNumber.value` | `identifier_nhsnumber_value` |
| `Encounter.extension:dischargeMethod` | `extension_dischargemethod` |

Any shortening rule ("drop the backbone segment when unambiguous") reintroduces the
subjectivity that produced the current drift, so the long names are accepted as-is.

The same rule covers `[x]` choice types — each surfaced form is one column named for the
form, with no separator inserted before it. This supersedes §6a of CONVENTIONS.md, which
split the form off (`onset_datetime`); splitting would need a maintained list of type-form
suffixes to split on, which is the shortening problem again.

| FHIR path | Column |
|---|---|
| `Condition.onsetDateTime` | `onsetdatetime` |
| `Patient.deceasedBoolean` | `deceasedboolean` |

**No naming exceptions.** `master_person_id` is removed from the model. Patient's PK is
`id`; fact tables carry `subject_id` from `<Resource>.subject`. This supersedes §8 of
CONVENTIONS.md. Entity-resolution semantics are documented in the Patient class
description rather than encoded in a column name.

## Coding

`Coding` and `CodeableConcept` become object columns `{system, code, display}` — never
flattened to scalars. This is the ergonomic choice for code handling and removes the
`_display` sidecars, since `display` travels inside the object.

Keys inside the object are the FHIR element names, not `coding_*`. The `coding_` prefix
existed only because the fields were flattened into sibling columns on the parent, where
a bare `system` would have been ambiguous. The object's column name now supplies that
context. The fact-code variant (§5a) becomes `{system, code, display, is_source}` —
`is_source` keeps a non-FHIR name because it is not a FHIR element.

Bare `code` primitives (`Encounter.status`, `Patient.gender`) stay scalar. They have no
`system` or `display` to hold, so making them objects would invent structure FHIR does
not have. They keep their `accepted_values` tests.

```
Encounter.class    (Coding)   -> class  OBJECT {system, code, display}   no accepted_values
Encounter.status   (code)     -> status VARCHAR                          accepted_values kept
Condition.category (CodeableConcept) -> category OBJECT
```

All `_display` sidecar columns are removed. Display for scalar `code` columns resolves
via `seed_*.csv` at the analyst-view layer. The seed layer is therefore a prerequisite
for this change, not an afterthought.

## Enums

Threshold is 100 concepts. Counted **offline only** — no network access in the
generator, so output stays deterministic and CI needs no credentials.

Of 217 relevant value sets in the installed packages: 148 are countable offline
(56 shipped expansions + 92 fully-enumerated composes). The remaining 69 are SNOMED
`is-a`/filter queries with no shippable expansion; these become plain `string` columns
with the binding recorded. They are overwhelmingly the large clinical sets that would
fail the threshold anyway.

**These figures need re-deriving before the threshold is baked in.** The UK Core package
ships 70 value sets (52 with expansions, 18 filter-based); the 217 evidently also counts
bound sets from `hl7.fhir.r4.core` (1316 available), but which ones were treated as
"relevant" is not recorded, so the 148/69 split cannot be reproduced from the method as
stated. This matters because the threshold is hard-coded and not tunable per resource —
see the config section.

If external expansion is ever added, the NHS England Ontology Server
(`https://ontology.nhs.uk/production1/fhir`) is the designated source — authoritative
for the UK SNOMED edition these bindings reference. Not in scope now.

## Inclusion and exclusion

**Include by default, declare exclusions explicitly.** This is the uniform answer across
extensions, `[x]` forms, `meta` and nested elements.

Dropped globally by the script, before config is consulted:

```
backbone .id  (Resource.id is the PK and is never dropped)
text, contained, implicitRules, language
note, photo, attachment
anonymous extension / modifierExtension
```

Everything else — all *named* extension slices, `meta`, and every `[x]` form the profile
allows — surfaces by default and is pruned per resource in config.

**Anonymous extensions are a global drop, not a config matter.** An unsliced `extension`
element is a placeholder with no name, type or binding, so no column can be derived from
it. It carries no data. There are 254 of them across the 31 profiles against 90 named
slices, and 247 are `0..*` — so leaving them in would mean the same boilerplate exclusion
in every config file, and would trip the nesting hard-fail below on elements holding
nothing. `Quantity.extension` inside `doseAndRate` inside `dosageInstruction` is the worst
case: a three-level repeat carrying no data.

The distinction is mechanical and taken from the element `id`, not the path — a last
segment of `extension` is anonymous, `extension:ethnicCategory` is a named slice. Paths
collapse the two (`Patient.extension` is the path for both), so a path-based rule would
drop the named slices as well.

```
Patient.extension                  -> dropped (anonymous)
Patient.name.extension             -> dropped (anonymous, on a datatype)
Patient.extension:ethnicCategory   -> kept, pruned per config
```

Named slices are where the judgment lives ("is ethnicCategory in scope for v1?"), which is
why they stay in config.

## Nested repeating elements

A variant is a flat, single-level object (§2). Repeating-inside-repeating therefore needs
a rule. Across all 31 profiles there are 26 genuinely ambiguous cases once coding-as-object
and the global exclusions absorb the rest.

**Rule: keep the outer repeat as the variant, flatten a primitive inner repeat to its
first entry.** The outer repetition is nearly always the analytically meaningful one
(all names including maiden; the full CDS diagnosis grid), while the inner is usually
incidental (second forename).

```
Patient.name -> VARIANT, one object per name
  { use: "official", family: "Smith", given: "Jane", prefix: "Dr" }
  { use: "maiden",   family: "Jones", given: "Jane", prefix: null }
                                             ^ "Elizabeth" dropped
```

11 cases resolve automatically this way: `name.given`, `name.prefix`, `name.suffix`
(Patient, Practitioner), `address.line` (Patient, RelatedPerson), `availableTime.daysOfWeek`
(HealthcareService, PractitionerRole), `hoursOfOperation.daysOfWeek` (Location).

**Complex inner repeats are excluded case by case in config, with a stated reason.** They
cannot be flattened to a scalar without also choosing which subfields survive, which is a
judgment the script must not make silently. 15 cases:

```
Composition          event.detail, section.author, section.entry, section.section
Condition            evidence.detail, stage.assessment
Observation          component.referenceRange
Organization         contact.telecom
Patient              contact.telecom
Practitioner         qualification.identifier
Specimen             container.identifier, processing.additive
```

The three `doseAndRate` cases (`MedicationRequest`, `MedicationDispense`,
`MedicationStatement`) are **not** in this list. Once `Range` and `Ratio` dose forms are
excluded in config, `doseAndRate` has only primitive-typed children left and the standard
rule above applies to it unchanged. See the medication section below.

The script must **detect and hard-fail** on any repeat-inside-repeat not resolved by the
primitive rule or by config, so the flat-variant invariant is enforced rather than trusted.

`Composition.section.section` is recursive and unresolvable by any flattening rule — it
needs an explicit exclude or depth cap whenever Composition is modelled.

## Medication dosing — resolved by config exclusion, no special rule

Dosing looked like it needed a carve-out from the nesting rule. It does not. Excluding the
`Range` and `Ratio` dose forms in config removes the only nested structure, after which the
**default rules apply unchanged**.

### Why it looked like a problem

```
MedicationRequest.dosageInstruction    0..*  Dosage             <- outer repeat
  doseAndRate                          0..*  Element            <- inner repeat
    dose[x]                            0..1  Range | Quantity
    rate[x]                            0..1  Ratio | Range | Quantity
```

A repeating element inside a repeating element, whose children include complex types — so
under the Q7 rule `doseAndRate` would be excluded and the prescribed dose lost. That matters
because the asymmetry is sharp: `MedicationAdministration.dosage` is `0..1` and keeps its
dose, so an *administered* dose would survive while a *prescribed* one did not.

### Why the exclusion resolves it

`Range` and `Ratio` are the only nested datatypes here — each wraps `Quantity` objects:

```
Quantity  { value: decimal, comparator: code, unit: string, system: uri, code: code }   all primitive
Range     { low: Quantity, high: Quantity }              <- Quantity nested inside
Ratio     { numerator: Quantity, denominator: Quantity } <- Quantity nested inside
```

**`Quantity` is entirely primitive** — five scalar fields, no nesting (verified against
`StructureDefinition-Quantity.json`). So once `doseRange`, `rateRatio` and `rateRange` are
excluded, `doseAndRate`'s remaining children are:

| Child | Type | Handling |
|---|---|---|
| `type` | CodeableConcept | object, per the standard coding rule |
| `doseQuantity` | Quantity | primitive-only → flattens |
| `rateQuantity` | Quantity | primitive-only → flattens |

Nothing complex remains. `doseAndRate` becomes an ordinary repeating element with
flattenable children, and the standard Q7 rule (keep outer as variant, flatten inner to
first entry) covers it with **no named exception and no special-casing in the script**.

### Resulting fields

Inside each `dosageinstruction` variant object:

```
doseandrate_type                     OBJECT {system, code, display}
doseandrate_dosequantity_value       NUMBER
doseandrate_dosequantity_unit        VARCHAR
doseandrate_dosequantity_system      VARCHAR
doseandrate_dosequantity_code        VARCHAR
doseandrate_dosequantity_comparator  VARCHAR
doseandrate_ratequantity_value       NUMBER
doseandrate_ratequantity_unit        VARCHAR
doseandrate_ratequantity_system      VARCHAR
doseandrate_ratequantity_code        VARCHAR
doseandrate_ratequantity_comparator  VARCHAR
```

11 fields, against 36 if every `[x]` form surfaced.

`Quantity.code` + `.system` are the machine-readable UCUM pair and must be kept — `unit`
alone is free text and not safe to aggregate on. `comparator` carries `<` / `>` semantics;
dropping it would silently turn "less than 5mg" into "5mg".

### Config

```yaml
exclude:
  MedicationRequest.dosageInstruction.doseAndRate.doseRange:  ranged dosing not present in real data
  MedicationRequest.dosageInstruction.doseAndRate.rateRatio:  ratio-expressed rates not present in real data
  MedicationRequest.dosageInstruction.doseAndRate.rateRange:  ranged rates not present in real data
```

Same three exclusions on `MedicationDispense.dosageInstruction` and
`MedicationStatement.dosage`.

### Evidence

Both dosage examples shipped in the UK Core package use `doseQuantity`:

```json
// UKCore-MedicationStatement-Amoxicillin-Example — "2 capsules 4 times a day"
"doseQuantity": { "value": 500, "unit": "milligram",
                  "system": "http://unitsofmeasure.org", "code": "mg" }

// UKCore-MedicationRequest-EyeDrops-Example — "1 drop in left eye, every 12 hours"
"doseQuantity": { "value": 1, "unit": "drop",
                  "system": "http://unitsofmeasure.org", "code": "[drp]" }
```

The package ships no `doseRange` or `rate[x]` example at all.

### Residual exposure

`rateQuantity` covers simple infusion rates ("100 mL/h"). It does **not** cover
ratio-expressed rates ("1000mL over 8 hours"), which is how `rateRatio` is normally used —
so infusion-heavy feeds (ICU, oncology) would keep route, site, timing and the free-text
`text`, but not the structured rate. Worth confirming against a real prescribing extract
before the medication resources are built; if `rateRatio` proves material, the fix is to
surface it as four more fields rather than to change any rule.

## Config

One file per resource at `scripts/resources/<Resource>.yaml`, mirroring the
`enum_manifest.yaml` pattern. Keyed by FHIR path. Declares **only deviations from the
defaults** — there is no `defaults:` block anywhere, because the rules above are hard-coded
in the script and documented in CONVENTIONS.md.

```yaml
resource: Encounter
profile: https://fhir.hl7.org.uk/StructureDefinition/UKCore-Encounter
package: fhir.r4.ukcore.stu2

# CodeableConcepts that ARE the fact (§5a is_source + mappings variant).
# List, default empty. Observation would declare both code and valueCodeableConcept.
fact_code: []

# Repeating elements kept as variants because the repetition carries information.
# `key` is required - the script must not guess the surrogate key.
variants:
  Encounter.diagnosis:
    reason: CDS primary/secondary diagnosis grid; rank is analytically meaningful
    key: [condition, rank]

# Dropped paths, including everything beneath them. Reason is mandatory -
# that is what makes the file reviewable rather than a pile of paths.
exclude:
  Encounter.statusHistory: audit trail, not analytical
  Encounter.classHistory:  audit trail, not analytical
  Encounter.participant:   out of scope for v1
  Encounter.location:      deferred - needs its own ward-stay model
```

Excluding a path excludes everything under it. Declaring a path a variant makes its
children the variant's fields automatically. Both keep the config short.

Consequence of hard-coding the defaults: the enum threshold is **not** tunable per
resource. Changing 100 later reclassifies columns between enum and string across every
resource simultaneously, so the number should be sanity-checked against the actual
concept-count distribution before it is baked in.

## Transition

Patient, Encounter and Condition are regenerated from config. The diff against the
hand-written schemas is the generator's acceptance test: **any difference that does not
trace back to a rule above is a bug in the rules.** The existing schemas become the test
fixture rather than being discarded.

Expect these differences: `id` replacing `master_person_id`, `hospitalization_admitsource`
replacing `admit_source`, object columns replacing scalar+`_display` pairs, and extension
slices surfacing under their full path-derived names.

## Known losses, accepted

- Second and subsequent forenames; address lines beyond the first.
- Second and subsequent `doseAndRate` entries — only the first is retained (see medications).
- Ranged dosing (`doseRange`, `rateRange`) and ratio-expressed rates (`rateRatio`).
  `doseQuantity` and `rateQuantity` are retained. Pending confirmation against a real
  prescribing extract; infusion-heavy feeds are the exposure.
- `Observation.component.referenceRange` — components survive, per-component ranges do not.
  Blood-pressure style multi-component observations are unaffected.
- `accepted_values` coverage on every `Coding`-typed column, until the variant tests land.

## Open — sequencing

**Settled: keep the binding, test later.** Object columns carry `value_set` and, where the
set is enumerable, the enum name in `meta`. No `accepted_values` is emitted on them.

The gap is real and is not only a sequencing question. `accepted_values` compares a column
against a value list, and an object column is not a string — `{"code":"IMP",...}` never
equals `"IMP"`. Stock dbt has no way to point the test at `class:code` instead, so closing
the gap needs a custom test macro in the consumer dbt project (`lgt_phm`), not a change
here. That is the same machinery the variant content tests need, so it lands once.

Until then Encounter loses testable coverage on `class`, `type`, `serviceType`, `priority`,
`hospitalization.admitSource` and `hospitalization.dischargeDisposition`. `status` survives
as a scalar `code`. Recording the enum name in `meta` is what keeps the test derivable
later without revisiting the schemas.

LinkML itself never holds tests. It records `range: <SomeEnum>`; `generate_dbt_yaml.py`
reads the enum and derives `accepted_values` from it. Nothing about that changes — only
the branch that decides `data_type: variant` instead of `varchar`.

---

# Implementation notes

## Datatype expansion

Snapshots stop at the datatype boundary. `Encounter.period` is one row typed `Period` -
`.start` and `.end` are not in the profile. Expansion only appears where a profile
constrains a datatype, so depth varies by resource.

The generator must expand these itself, from `hl7.fhir.r4.core` (already a dependency,
so this stays offline):

    walk the profile snapshot
    where an element is a complex datatype with no children present:
      splice in that datatype's snapshot, rebasing paths onto the parent
      apply profile constraints where they exist
    recurse, with a depth cap

Halt at `Reference` (CONVENTIONS §6) and `Extension` (`value[x]` admits ~50 type forms).

Without this, `period_start` (§9) and the `meta_*` columns (§12) are not derivable.

## contentReference

Some elements carry `contentReference` instead of `type` - `Observation.component.referenceRange`
points at `#Observation.referenceRange`. Resolve the target path before typing the element,
or the script crashes on a missing type.

## Reference targets

Profiles allow multiple target types on most references, and most targets are resources
the CDM does not model. Config declares the FK target per path (Part B §6); the script
never infers one.

## Nested repeats

Three repeating named extension slices sit inside repeating elements and hit the §11
hard-fail on first run. Pre-declare them in config rather than discovering them:

    Patient.address.extension:addressKey
    Patient.communication.extension:proficiency.extension:type
    DiagnosticReport.resultsInterpreter.extension:deviceReference

## Still to specify

- `min`/`max` -> `required` / `multivalued`
- FHIR primitive -> LinkML range map (including where `TimestampNtz` applies)
- Hard-fail on duplicate column names after §2 lowercasing
- Variant `key` fields must survive exclusion - hard-fail if a `key` member is excluded
- Enum threshold of 100 to be sanity-checked against the concept-count distribution

## Known losses

- `Reference.identifier` on unmodelled targets - no ODS/SDS code as fallback until
  Organization and Practitioner are modelled.
