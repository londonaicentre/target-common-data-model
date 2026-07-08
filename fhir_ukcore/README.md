# UK Core FHIR profiles + terminology

Source of truth for NHS England / UK Core bindings used by the flattened-FHIR CDM
(see `../CONVENTIONS.md` §Target).

## Provenance

- Package: `fhir.r4.ukcore.stu2`
- Version: **2.0.2** (dist-tag `latest`)
- Registry: <https://packages.simplifier.net/fhir.r4.ukcore.stu2/2.0.2>
- FHIR version: R4 (4.0.1)
- Canonical URL base: `https://fhir.hl7.org.uk/StructureDefinition/UKCore-*`
- Pulled: 2026-07-08

## Layout

- `*.profile.json` — 31 resource-constraint StructureDefinitions across 27
  resource types. All carry full **snapshots** (flattening reads the snapshot).
  Base profile per type is `<type>.profile.json`; variants keep a descriptive
  suffix, e.g. `observation-lab.profile.json`, `servicerequest-lab.profile.json`.
- `terminology/` — all UK Core value sets (`vs-*.json`, 70) and code systems
  (`cs-*.json`, 49), so `value_set.binding` / `accepted_values` in the dbt
  contracts are locally resolvable and testable.

## Gaps / notes

- **DocumentReference** has **no** UK Core profile in STU2 — it stays on base
  R4 (`../fhir_r4/documentreference.profile.json`).
- The older base-R4 StructureDefinitions remain in `../fhir_r4/` for reference
  and for DocumentReference.
